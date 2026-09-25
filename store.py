#!/usr/bin/env python3
"""Notes store and search. sqlite3 + BM25, both from the standard library.

Search runs in two stages, which is what keeps a small model useful over an
archive far larger than its context window:

  1. BM25 over titles and tags picks candidates - no model, no VRAM.
  2. The model sees only those titles and decides which to open.
  3. Full text of the chosen few goes back to the model to answer from.
"""
import os, re, json, math, shutil, sqlite3, time, threading

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "notes.db")
os.makedirs(os.path.dirname(DB), exist_ok=True)

_local = threading.local()
WORD = re.compile(r"[a-z0-9_]+")
# short words that carry no signal in a technical archive
# A thin stop list let "do" and "over" score, and because BM25 rewards short
# documents a two-line note about coffee outranked the firmware note for
# "how do I flash firmware over serial". Question words and filler must go.
STOP = set("""a an and are as at be been being by for from has have had how i if in into is it
its of on or over that the their then there these this those to was were what when where which
while who whom why with you your yours can cannot could do does did doing done should would will
shall may might must am but not no nor so than too very just also about after before between
during under above again further once here he she they we us our my me him her them get got make
made use used using need needs want like want any all each few more most other some such only own
same both own via per within without across around""".split())


def conn():
    if not hasattr(_local, "c"):
        _local.c = sqlite3.connect(DB, check_same_thread=False)
        _local.c.row_factory = sqlite3.Row
    return _local.c


def init():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      tags TEXT DEFAULT '',
      author TEXT DEFAULT '',
      created REAL, updated REAL
    );
    CREATE INDEX IF NOT EXISTS idx_updated ON notes(updated DESC);
    """)
    # imported guides carry where they came from, so the library can be browsed
    # deck by deck the way the site presents it
    have = {r[1] for r in c.execute("PRAGMA table_info(notes)")}
    for col in ("deck", "slug", "url", "category", "topic"):
        if col not in have:
            c.execute("ALTER TABLE notes ADD COLUMN %s TEXT DEFAULT ''" % col)
    if "ord" not in have:
        c.execute("ALTER TABLE notes ADD COLUMN ord INTEGER DEFAULT 0")
    c.execute("CREATE INDEX IF NOT EXISTS idx_deck ON notes(deck, ord)")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS queries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts REAL, mode TEXT, question TEXT,
      candidates INTEGER DEFAULT 0, opened INTEGER DEFAULT 0,
      note_ids TEXT DEFAULT '', top_score REAL,
      seconds REAL, tokens_in INTEGER, tokens_out INTEGER,
      answered INTEGER DEFAULT 1        -- 0 when the notes did not cover it
    );
    CREATE INDEX IF NOT EXISTS idx_q_ts ON queries(ts DESC);
    CREATE TABLE IF NOT EXISTS skills (
      name TEXT PRIMARY KEY, title TEXT DEFAULT '', description TEXT DEFAULT '',
      body TEXT DEFAULT '', author TEXT DEFAULT '', tags TEXT DEFAULT '',
      n_files INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
      created REAL, updated REAL
    );
    CREATE TABLE IF NOT EXISTS decks (
      name TEXT PRIMARY KEY, title TEXT DEFAULT '', summary TEXT DEFAULT '',
      url TEXT DEFAULT ''
    );""")
    c.commit()


def stem(w):
    """Crudest possible stemmer, and it earns its place: without it a search for
    "stm32 debugging" ranked the bootloader note above the one titled "SWD debug"
    purely because the query said debugging and the note said debug."""
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
        if len(w) > 3 and w[-1] == w[-2]:      # debugg -> debug
            w = w[:-1]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    return w


def tokens(text):
    return [stem(w) for w in WORD.findall((text or "").lower())
            if w not in STOP and len(w) > 1]


SRC = ("deck", "slug", "url", "category", "topic", "ord")


def add(title, body, tags="", author="", **src):
    t = time.time()
    c = conn()
    cols = [k for k in SRC if k in src]
    cur = c.execute(
        "INSERT INTO notes(title,body,tags,author,created,updated%s) VALUES(?,?,?,?,?,?%s)"
        % ("".join("," + k for k in cols), ",?" * len(cols)),
        (title.strip(), body.strip(), tags.strip(), author.strip(), t, t)
        + tuple(src[k] for k in cols))
    c.commit()
    return cur.lastrowid


def update(nid, title, body, tags="", author="", **src):
    c = conn()
    cols = [k for k in SRC if k in src]
    c.execute("UPDATE notes SET title=?,body=?,tags=?,author=?,updated=?%s WHERE id=?"
              % "".join(",%s=?" % k for k in cols),
              (title.strip(), body.strip(), tags.strip(), author.strip(), time.time())
              + tuple(src[k] for k in cols) + (nid,))
    c.commit()


OWN = "own"          # notes written here rather than imported from a deck


# A question nobody could answer is the most useful thing this thing records:
# it is the list of what still has to be written down.
# --------------------------------------------------------------------- skills
SKILL_DIR = os.path.join(os.path.dirname(DB), "skills")
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def skill_path(name, *parts):
    """Inside the skill's own folder and nowhere else: an uploaded path like
    ../../etc/passwd must not be able to climb out."""
    if not SKILL_NAME.match(name or ""):
        raise ValueError("bad skill name")
    root = os.path.realpath(os.path.join(SKILL_DIR, name))
    p = os.path.realpath(os.path.join(root, *parts)) if parts else root
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("path escapes the skill folder")
    return p


def skills(name=None):
    if name:
        r = conn().execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None
    return [dict(r) for r in conn().execute(
        "SELECT name,title,description,author,tags,n_files,bytes,updated "
        "FROM skills ORDER BY name")]


def skill_files(name):
    root = skill_path(name)
    out = []
    for base, dirs, names in os.walk(root):
        # .prev holds the copy kept from the last upload; it is not part of the
        # skill and must never be listed, zipped or installed
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(names):
            if f.startswith("."):
                continue
            full = os.path.join(base, f)
            out.append({"path": os.path.relpath(full, root).replace(os.sep, "/"),
                        "bytes": os.path.getsize(full)})

    return sorted(out, key=lambda x: (x["path"] != "SKILL.md", x["path"]))


def stage_path(name, *parts):
    """Where an upload accumulates before it is known to be complete."""
    if not SKILL_NAME.match(name or ""):
        raise ValueError("bad skill name")
    root = os.path.realpath(os.path.join(SKILL_DIR, ".staging", name))
    p = os.path.realpath(os.path.join(root, *parts)) if parts else root
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("path escapes the staging folder")
    return p


def stage_write(name, rel, data):
    if not rel or ".." in rel.replace("\\", "/").split("/"):
        raise ValueError("bad path: %s" % rel)
    dest = stage_path(name, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    return os.path.getsize(dest)


def stage_list(name):
    root = stage_path(name)
    out = []
    for base, _, names in os.walk(root):
        for f in names:
            full = os.path.join(base, f)
            out.append({"path": os.path.relpath(full, root).replace(os.sep, "/"),
                        "bytes": os.path.getsize(full)})
    return out


def stage_drop(name):
    shutil.rmtree(stage_path(name), ignore_errors=True)


def stage_commit(name, title, description, body, author="", tags=""):
    """Swap what was staged into place. Nothing is deleted until the new copy
    is complete, so an upload that dies halfway leaves the old skill standing."""
    staged = stage_path(name)
    files = stage_list(name)
    if not files:
        raise ValueError("nothing staged")
    root = skill_path(name)
    if os.path.isdir(root):
        prev = root + ".prev"
        shutil.rmtree(prev, ignore_errors=True)
        os.replace(root, prev)
        shutil.move(prev, os.path.join(staged, ".prev"))
    os.replace(staged, root)
    total = sum(f["bytes"] for f in files)
    t = time.time()
    c = conn()
    c.execute("INSERT INTO skills(name,title,description,body,author,tags,n_files,bytes,"
              "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(name) DO UPDATE SET title=excluded.title,"
              "description=excluded.description,body=excluded.body,author=excluded.author,"
              "tags=excluded.tags,n_files=excluded.n_files,bytes=excluded.bytes,"
              "updated=excluded.updated",
              (name, title, description, body, author, tags, len(files), total, t, t))
    c.commit()
    return {"name": name, "n_files": len(files), "bytes": total}


def skill_save(name, title, description, body, files, author="", tags=""):
    """files: [{path, data(bytes)}]. Replaces the folder, keeping one copy of
    what was there so a bad upload is recoverable."""
    root = skill_path(name)
    if os.path.isdir(root):
        prev = root + "/.prev"
        shutil.rmtree(prev, ignore_errors=True)
        keep = [d for d in os.listdir(root) if d != ".prev"]
        os.makedirs(prev, exist_ok=True)
        for d in keep:
            shutil.move(os.path.join(root, d), os.path.join(prev, d))
    os.makedirs(root, exist_ok=True)
    total = 0
    for f in files:
        dest = skill_path(name, f["path"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(f["data"])
        total += len(f["data"])
    t = time.time()
    c = conn()
    c.execute("INSERT INTO skills(name,title,description,body,author,tags,n_files,bytes,"
              "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(name) DO UPDATE SET title=excluded.title,"
              "description=excluded.description,body=excluded.body,author=excluded.author,"
              "tags=excluded.tags,n_files=excluded.n_files,bytes=excluded.bytes,"
              "updated=excluded.updated",
              (name, title, description, body, author, tags, len(files), total, t, t))
    c.commit()
    return {"name": name, "n_files": len(files), "bytes": total}


def skill_delete(name):
    shutil.rmtree(skill_path(name), ignore_errors=True)
    c = conn()
    c.execute("DELETE FROM skills WHERE name=?", (name,))
    c.commit()


NOT_COVERED = ("do not cover", "does not cover", "not covered", "nothing in the",
               "kapsam", "bulunmuyor", "yer almıyor", "bilgi yok")
WEAK_SCORE = 5.0


def log_query(mode, question, candidates=0, opened=0, note_ids=(), top_score=None,
              seconds=None, tokens_in=None, tokens_out=None, answer_text=None):
    low = (answer_text or "").lower()
    answered = 1
    if opened == 0 or candidates == 0:
        answered = 0
    elif answer_text is not None and any(k in low for k in NOT_COVERED):
        answered = 0
    elif top_score is not None and top_score < WEAK_SCORE:
        answered = 0
    try:
        c = conn()
        c.execute("INSERT INTO queries(ts,mode,question,candidates,opened,note_ids,"
                  "top_score,seconds,tokens_in,tokens_out,answered) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), mode, (question or "")[:500], candidates, opened,
                   ",".join(str(i) for i in note_ids), top_score, seconds,
                   tokens_in, tokens_out, answered))
        c.commit()
    except Exception:
        pass            # a question must never fail because logging did


def queries(limit=200, only_gaps=False):
    where = "WHERE answered=0 " if only_gaps else ""
    rows = conn().execute(
        "SELECT * FROM queries " + where + "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def snapshot(path):
    """A consistent copy of the database, taken through sqlite's own backup so
    a write in flight cannot tear it - copying the file would."""
    dst = sqlite3.connect(path)
    try:
        conn().backup(dst)
    finally:
        dst.close()
    return path


def export_rows():
    rows = conn().execute(
        "SELECT id,title,body,tags,author,created,updated,deck,slug,url,category,topic,ord "
        "FROM notes ORDER BY deck, ord, id").fetchall()
    return [dict(r) for r in rows]


def set_deck(name, title="", summary="", url=""):
    c = conn()
    c.execute("INSERT INTO decks(name,title,summary,url) VALUES(?,?,?,?) "
              "ON CONFLICT(name) DO UPDATE SET title=excluded.title, "
              "summary=excluded.summary, url=excluded.url",
              (name, title or "", summary or "", url or ""))
    c.commit()


def decks():
    """Every deck with its size, for the catalog. Notes written here are
    gathered under one pseudo-deck so nothing in the library is unreachable."""
    rows = conn().execute(
        "SELECT CASE WHEN deck='' THEN ? ELSE deck END AS deck, COUNT(*) n, "
        "SUM(length(body)) chars, MIN(category) category "
        "FROM notes GROUP BY 1 ORDER BY (deck=?), deck", (OWN, OWN)).fetchall()
    meta = {r["name"]: dict(r) for r in conn().execute("SELECT * FROM decks")}
    out = []
    for r in rows:
        d = dict(r)
        m = meta.get(d["deck"], {})
        d["title"] = m.get("title") or d["deck"]
        d["summary"] = m.get("summary", "")
        d["url"] = m.get("url", "")
        out.append(d)
    return out


def deck_notes(deck):
    where = "deck=''" if deck == OWN else "deck=?"
    args = () if deck == OWN else (deck,)
    rows = conn().execute(
        "SELECT id,title,tags,slug,url,category,topic,ord,length(body) AS size, "
        "substr(body,1,320) AS preview FROM notes WHERE " + where + " ORDER BY ord, id",
        args).fetchall()
    return [dict(r) for r in rows]


def delete(nid):
    c = conn()
    c.execute("DELETE FROM notes WHERE id=?", (nid,))
    c.commit()


def get(nid):
    r = conn().execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    return dict(r) if r else None


def all_notes(limit=500, offset=0):
    rows = conn().execute(
        "SELECT id,title,tags,author,created,updated,deck,slug,url,category,topic, "
        "length(body) AS size "
        "FROM notes ORDER BY updated DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count():
    return conn().execute("SELECT COUNT(*) AS n, COALESCE(SUM(length(body)),0) AS b "
                          "FROM notes").fetchone()


def search(query, limit=12, title_weight=3.0):
    """BM25 with the title and tags counted several times over, because a note
    about STM32 debugging usually says so in its title."""
    q = tokens(query)
    if not q:
        return []
    rows = conn().execute("SELECT id,title,body,tags FROM notes").fetchall()
    if not rows:
        return []

    docs, lens = [], []
    for r in rows:
        head = tokens(r["title"]) + tokens(r["tags"])
        toks = head * int(title_weight) + tokens(r["body"])
        docs.append((r, toks))
        lens.append(len(toks))
    avg = sum(lens) / len(lens) or 1
    N = len(docs)

    df = {}
    for _, toks in docs:
        for w in set(toks):
            df[w] = df.get(w, 0) + 1

    # b below the usual 0.75: short notes were winning on length alone
    k1, b = 1.5, 0.45
    scored = []
    for (r, toks), L in zip(docs, lens):
        tf = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        s, matched = 0.0, 0
        for w in set(q):
            if w not in tf:
                continue
            matched += 1
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * L / avg))
        if s > 0:
            # a note that hits three of the query's words is worth far more than
            # one that hits a single common word many times
            s *= (matched / len(set(q))) ** 1.5
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [{"id": r["id"], "title": r["title"], "tags": r["tags"],
             "score": round(s, 2),
             "preview": (r["body"][:260] + ("…" if len(r["body"]) > 260 else ""))}
            for s, r in scored[:limit]]


init()
