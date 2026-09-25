#!/usr/bin/env python3
"""LLM Mini Toolkit: OCR and a knowledge base in one place, sharing one GPU.

The two jobs need different models and the card holds only one at a time, so
the model is swapped per request. Swapping costs 1-2 seconds.

    python3 app.py --host 0.0.0.0 --port 3333
"""
import os, sys, re, json, time, argparse, glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "lib"))

import models
import ocr_core
import kb_core
import store

STATIC = os.path.join(BASE, "static")
MIME = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
        ".ico": "image/x-icon", ".css": "text/css", ".js": "text/javascript"}
OPT = {"prompt_base": None}


def find_models():
    """Build the profiles from whatever is in models/."""
    def one(*pats, exclude=None):
        for pat in pats:
            for f in sorted(glob.glob(os.path.join(BASE, "models", pat))):
                if exclude and exclude in os.path.basename(f).lower():
                    continue
                return f
        return None

    p = {}
    ocr = one("GLM-OCR*.gguf", exclude="mmproj")
    ocr_mm = one("mmproj*GLM-OCR*.gguf")
    if ocr and ocr_mm:
        # 1272 MiB idle; the figure pass allocates a larger CLIP compute buffer
        # and keeps it, so 1804 is what it actually holds while working.
        p["ocr"] = {"label": "GLM-OCR", "model": ocr, "mmproj": ocr_mm,
                    "ngl": 4, "ctx": 8192, "vram_mib": 1804}
    kb = one("Qwen3-*.gguf", "*Instruct*.gguf", exclude="mmproj")
    if kb and "VL" not in os.path.basename(kb):
        # Fully on the GPU with a quantised KV cache. Measured against the same
        # three questions, this beat a larger quant at partial offload on both
        # counts: 57.9 s vs 67.4 s, and 8/8 correct values vs 5/8. The KV cache
        # is what makes it fit - at f16 the weights alone left no room.
        ctx = int(os.environ.get("WB_KB_CTX", "8192"))
        kvq = ["--cache-type-k", "q4_0", "--cache-type-v", "q4_0"]
        # WB_KB_NGL sets where the chain starts. Leave it unset on a machine with
        # ~2.5 GB free and the first attempt (full GPU) wins; set it low on a
        # busier card to skip an attempt that is going to fail anyway.
        start_ngl = int(os.environ.get("WB_KB_NGL", "99"))
        p["kb"] = {"label": os.path.basename(kb).split(".gguf")[0],
                   "model": kb, "ngl": start_ngl, "ctx": ctx, "vram_mib": 2440,
                   "extra": kvq, "note": "full GPU",
                   # Measured: full GPU answers three test questions in 57.9 s
                   # against 67.4 s partially offloaded, and got 8/8 values right
                   # against 5/8. But it only fits when the desktop is quiet — a
                   # few browser tabs take the card below what the weights need.
                   "fallbacks": [] if start_ngl < 99 else [
                       {"ngl": 99, "ctx": 4096, "extra": kvq,
                        "vram_mib": 2300, "note": "full GPU, short context"},
                       {"ngl": 20, "ctx": ctx, "extra": kvq,
                        "vram_mib": 2000, "note": "mostly on GPU"},
                       {"ngl": 12, "ctx": ctx, "extra": kvq,
                        "vram_mib": 1774, "note": "partly on GPU"},
                       {"ngl": 0, "ctx": ctx, "extra": kvq,
                        "vram_mib": 0, "note": "CPU only"},
                   ]}
        if start_ngl < 99:
            p["kb"]["note"] = f"-ngl {start_ngl}"
            p["kb"]["vram_mib"] = 0 if start_ngl == 0 else 1526
    vl = one("Qwen2.5-VL*.gguf", exclude="mmproj")
    vl_mm = one("mmproj*Qwen2.5-VL*.gguf")
    if vl and vl_mm:
        p["vision"] = {"label": "Qwen2.5-VL (figures)", "model": vl, "mmproj": vl_mm,
                       "device": "none", "ngl": 0, "vram_mib": 0,
                       "extra": ["--no-mmproj-offload"]}
    return p


# A skill carries what it needs to run with no internet: python wheels, a
# vendored C library, prebuilt binaries. That is not a few kilobytes.
SKILL_MAX_FILES = 2000
SKILL_MAX_BYTES = 512 * 1024 * 1024      # per skill
SKILL_MAX_FILE = 128 * 1024 * 1024       # per file
SKILL_JSON_BYTES = 8 * 1024 * 1024       # the base64 path stays small


def parse_skill_md(text):
    """name / description out of the YAML front matter a SKILL.md carries."""
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if m:
        body = text[m.end():]
        key = None
        for line in m.group(1).split("\n"):
            kv = re.match(r"^(\w[\w-]*)\s*:\s*(.*)$", line)
            if kv:
                key = kv.group(1).lower()
                v = kv.group(2).strip()
                # a folded block opens with >, >-, | or |- and continues indented
                meta[key] = "" if v in (">", ">-", "|", "|-") else v.strip('"\'')
            elif key and line.strip():
                meta[key] = (meta[key] + " " + line.strip()).strip()
    title = meta.get("title") or ""
    if not title:
        h = re.search(r"^#\s+(.+)$", body, re.M)
        title = h.group(1).strip() if h else ""
    return {"name": (meta.get("name") or "").strip().lower(),
            "title": title,
            "description": meta.get("description", "").strip(),
            "tags": meta.get("tags", "").replace(",", " ").strip().lower(),
            "body": text}


def install_script(name, base, tool="both", wheels=True, powershell=False):
    """A one-liner installer, the shape people already expect from an IDE.

    It fetches the client and hands over, so there is one implementation of the
    install rather than two. Nothing from the skill itself is executed.

    The body lives in install-template.sh / .ps1 rather than in a python string:
    quoting a shell script through two layers of escaping produced a broken one,
    and a template file is what a shell script should look like anyway.
    """
    tool = tool if tool in ("both", "claude", "opencode", "none") else "both"
    flags = ("" if tool == "both" else " --tool " + tool) + (" --with-wheels" if wheels else "")
    where = {"both": "~/.claude/skills and the opencode AGENTS.md",
             "claude": "~/.claude/skills",
             "opencode": "the opencode AGENTS.md block only",
             "none": "a folder, registered with nothing"}[tool]
    sk = store.skills(name) or {}
    summary = re.sub(r"\s+", " ", (sk.get("description") or "")).strip()
    if len(summary) > 150:
        summary = summary[:147].rsplit(" ", 1)[0] + "..."
    f = os.path.join(BASE, "install-template.ps1" if powershell else "install-template.sh")
    with open(f, encoding="utf-8") as fh:
        body = fh.read()
    for k, v in (("{{NAME}}", name), ("{{BASE}}", base), ("{{FLAGS}}", flags),
                 ("{{WHERE}}", where), ("{{SUMMARY}}", summary)):
        body = body.replace(k, v)
    return body


def _safe_name(t, fallback):
    t = re.sub(r"[^\w.\- ]+", "", (t or "").strip(), flags=re.U).strip() or fallback
    return re.sub(r"\s+", "-", t)[:120]


def build_export(zip_path):
    """One deck per folder, one markdown file per note, plus library.json so a
    restore can put the deck, slug and url back where they were."""
    import zipfile
    rows = store.export_rows()
    stamp = time.strftime("%Y-%m-%d")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        seen = set()
        for n in rows:
            deck = n["deck"] or "own-notes"
            base = _safe_name(n["slug"] or n["title"], "note-%d" % n["id"])
            name = "%s/%s.md" % (deck, base)
            if name in seen:                      # two notes can share a title
                name = "%s/%s-%d.md" % (deck, base, n["id"])
            seen.add(name)
            head = "# %s\n\ntags: %s\n\n" % (n["title"], n["tags"] or "")
            z.writestr(name, head + (n["body"] or ""))
        z.writestr("library.json", json.dumps(
            {"exported": stamp, "count": len(rows), "notes": rows},
            ensure_ascii=False, indent=1))
        z.writestr("README.txt",
                   "LLM Mini Toolkit library export, %s, %d notes.\n\n"
                   "Each .md is one note: '# Title', a 'tags:' line, then the body -\n"
                   "the same shape the add tab accepts, so any of these can be dropped\n"
                   "straight back in.\n\n"
                   "library.json holds every field, including deck, slug and source url,\n"
                   "which the .md files do not carry. Restore from that for an exact copy;\n"
                   "restore from the .md files for the text alone.\n\n"
                   "For a byte-exact backup take /api/backup.db instead: it is the sqlite\n"
                   "database, and putting it back is a file copy into data/notes.db with\n"
                   "the server stopped.\n" % (stamp, len(rows)))
    return zip_path


def direct_answer(question, limit=4, chars=12000, pick=True):
    """Hand back the matching articles themselves rather than a precis of them.

    BM25 draws up a shortlist, then the model reads the titles and says which
    are worth opening - the same judgement it makes in summary mode, but the
    reply is the source text, not the model's words. An assistant with a large
    context is better served that way, and nothing gets paraphrased on the way.

    pick=False skips the model entirely and trusts the BM25 order.
    """
    import re as _re
    t0 = time.time()
    cands = store.search(question, limit=12)
    search_s = round(time.time() - t0, 3)
    if not cands:
        store.log_query("fetch", question, seconds=search_s)
        return {"mode": "direct", "query": question, "count": 0,
                "articles": [], "text": "", "sources": [], "picked_by": "none",
                "metrics": {"search_seconds": search_s, "candidates": 0}}

    picked_by = "keyword rank"
    chosen = cands[:limit]
    pick_stats = {}
    if pick:
        try:
            chosen = kb_core.pick_notes(question, cands,
                                        on_stats=pick_stats.update)[:limit] or chosen
            picked_by = "model"
        except Exception:
            pass                      # model unavailable: the ranking still stands

    arts = []
    for h in chosen:
        n = store.get(h["id"])
        if not n:
            continue
        body = n["body"]
        m = _re.search(r"^Source:\s*(\S+)\s*$", body, _re.M)
        url = m.group(1) if m else None
        if m:
            body = body[:m.start()].rstrip()
        arts.append({"id": n["id"], "title": n["title"], "tags": n["tags"],
                     "score": h.get("score"), "url": url,
                     "chars": len(body), "truncated": len(body) > chars,
                     "text": body[:chars]})
    joined = "\n\n".join(
        f"===== [#{a['id']}] {a['title']} =====\n" +
        (f"source: {a['url']}\n" if a["url"] else "") + a["text"]
        for a in arts)
    # what this payload will cost whoever reads it next, counted by the model's
    # own tokenizer rather than guessed from the character count
    tok = kb_core.count_tokens(joined) if pick else None
    metrics = {"search_seconds": search_s, "candidates": len(cands),
               "opened": len(arts), "returned_chars": len(joined),
               "returned_tokens": tok,
               "truncated": sum(1 for a in arts if a["truncated"]),
               "pick": pick_stats or None}
    p = pick_stats or {}
    store.log_query("fetch", question, candidates=len(cands), opened=len(arts),
                    note_ids=[a["id"] for a in arts],
                    top_score=cands[0].get("score") if cands else None,
                    seconds=round(time.time() - t0, 2),
                    tokens_in=p.get("prompt_tokens"), tokens_out=tok)
    return {"mode": "direct", "query": question, "count": len(arts),
            "candidates": len(cands), "picked_by": picked_by,
            "metrics": {k: v for k, v in metrics.items() if v is not None},
            "articles": arts, "text": joined,
            "sources": [{"id": a["id"], "title": a["title"]} for a in arts]}



class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # both cores expect these helpers on the handler
    _send = ocr_core.Handler._send
    _read_json = ocr_core.Handler._read_json
    _json = kb_core.H._json

    def _base(self):
        return OPT.get("prompt_base") or \
            "http://" + (self.headers.get("Host") or "127.0.0.1:3333")

    def _skill_zip(self, name):
        """The whole skill in one file - what the client installs from."""
        import io, zipfile
        if not store.skills(name):
            return self._send(404, {"error": "no such skill"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in store.skill_files(name):
                z.write(store.skill_path(name, f["path"]), f["path"])
        blob = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="%s.zip"' % name)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _backup(self, as_zip):
        """The whole library, either as the database itself or as readable
        markdown. Built into a temp file so a half-written body is never sent."""
        import tempfile
        stamp = time.strftime("%Y%m%d-%H%M")
        fd, tmp = tempfile.mkstemp(suffix=".zip" if as_zip else ".db")
        os.close(fd)
        try:
            build_export(tmp) if as_zip else store.snapshot(tmp)
            with open(tmp, "rb") as fh:
                blob = fh.read()
        except Exception as ex:
            return self._send(500, {"error": "backup failed: %s" % ex})
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        name = "minitoolkit-%s.%s" % (stamp, "zip" if as_zip else "db")
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/zip" if as_zip else "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            f = os.path.join(STATIC, os.path.basename(p))
            if os.path.isfile(f):
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(),
                                      MIME.get(os.path.splitext(f)[1], "application/octet-stream"))
            return self._send(404, {"error": "not found"})
        if p in ("/minitoolkit.py", "/api/minitoolkit.py",
                 "/workbench.py", "/api/workbench.py"):   # the old name, still served
            f = os.path.join(BASE, "minitoolkit.py")
            if not os.path.isfile(f):
                return self._send(404, {"error": "client missing"})
            with open(f, encoding="utf-8") as fh:
                src = fh.read()
            src = src.replace('DEFAULT_SERVER = "http://127.0.0.1:3333"',
                              'DEFAULT_SERVER = "%s"' % self._base())
            return self._send(200, src, "text/x-python; charset=utf-8")
        if p == "/api/prompt":
            f = os.path.join(BASE, "AGENT-PROMPT.md")
            if not os.path.isfile(f):
                return self._send(404, {"error": "missing"})
            with open(f, encoding="utf-8") as fh:
                return self._send(200, fh.read().replace("{BASE_URL}", self._base()),
                                  "text/plain; charset=utf-8")
        if p in ("/api/backup.db", "/api/export.zip"):
            return self._backup(p.endswith(".zip"))
        if p in ("/api/write-prompt", "/api/skill-prompt"):
            f = os.path.join(BASE, "WRITE-PROMPT.md" if p.endswith("write-prompt")
                             else "SKILL-PROMPT.md")
            if not os.path.isfile(f):
                return self._send(404, {"error": "missing"})
            with open(f, encoding="utf-8") as fh:
                return self._send(200, fh.read(), "text/plain; charset=utf-8")
        if p.startswith("/api/v1/search"):
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            limit = int((qs.get("limit") or ["12"])[0])
            if not q.strip():
                return self._send(400, {"error": "q is required"})
            # BM25 only: no model is loaded, so this answers in milliseconds
            t0 = time.time()
            hits = store.search(unquote(q), limit=max(1, min(limit, 50)))
            store.log_query("search", q, candidates=len(hits), opened=len(hits),
                            note_ids=[h["id"] for h in hits[:4]],
                            top_score=hits[0]["score"] if hits else None,
                            seconds=round(time.time() - t0, 3))
            return self._send(200, {"query": q, "count": len(hits), "results": hits})
        if p.startswith("/api/v1/note/"):
            try:
                n = store.get(int(p.rsplit("/", 1)[1]))
            except ValueError:
                return self._send(400, {"error": "bad id"})
            return self._send(200, n) if n else self._send(404, {"error": "not found"})
        if p == "/api/skills":
            return self._send(200, {"skills": store.skills()})
        if p.startswith("/api/skill/"):
            rest = p[len("/api/skill/"):]
            if rest.endswith(".zip"):
                return self._skill_zip(rest[:-4])
            if rest.endswith("/install.sh") or rest.endswith("/install.ps1"):
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                name = rest.rsplit("/", 1)[0]
                if not store.skills(name):
                    return self._send(404, {"error": "no such skill"}, "text/plain")
                return self._send(200, install_script(
                    name, self._base(),
                    (qs.get("tool") or ["both"])[0],
                    (qs.get("wheels") or ["1"])[0] not in ("0", "no", "false"),
                    rest.endswith(".ps1")), "text/plain; charset=utf-8")
            name, _, sub = rest.partition("/")
            sk = store.skills(name)
            if not sk:
                return self._send(404, {"error": "no such skill"})
            if sub.startswith("files/"):
                try:
                    f = store.skill_path(name, sub[len("files/"):])
                except ValueError:
                    return self._send(400, {"error": "bad path"})
                if not os.path.isfile(f):
                    return self._send(404, {"error": "no such file"})
                with open(f, "rb") as fh:
                    blob = fh.read()
                ctype = "text/plain; charset=utf-8"
                try:
                    blob.decode("utf-8")
                except UnicodeDecodeError:
                    ctype = "application/octet-stream"
                return self._send(200, blob, ctype)
            sk["files"] = store.skill_files(name)
            return self._send(200, sk)
        if p.startswith("/api/queries"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            n = int((qs.get("limit") or ["200"])[0])
            gaps = (qs.get("gaps") or ["0"])[0] not in ("0", "", "false")
            return self._send(200, {"queries": store.queries(max(1, min(n, 2000)), gaps)})
        if p == "/api/decks":
            return self._send(200, {"decks": store.decks()})
        if p.startswith("/api/deck/"):
            from urllib.parse import unquote
            name = unquote(p.split("/api/deck/", 1)[1]).strip("/")
            notes = store.deck_notes(name)
            if not notes:
                return self._send(404, {"error": "no such deck"})
            return self._send(200, {"deck": name, "count": len(notes), "notes": notes})
        if p == "/api/models":
            return self._send(200, models.status())
        if p == "/api/status":
            c = store.count()
            with ocr_core.PLOCK:
                q = len(ocr_core.PENDING)
            return self._send(200, {
                "models": models.status(),
                "ocr": {"queue": q, "pdf": ocr_core.has_pdf(),
                        "max_pages": ocr_core.MAX_PAGES,
                        "figures": "vision" in models.PROFILES},
                "kb": {"notes": c["n"], "chars": c["b"]}})
        # The two cores each define a stream() for their own SSE jobs; the names
        # collide, so route to the right one explicitly rather than inheriting.
        if p.startswith("/api/batches/") and p.endswith("/events"):
            return ocr_core.Handler.stream(self, p.split("/")[3])
        if p.startswith("/api/ask/") and p.endswith("/events"):
            return kb_core.H.stream(self, p.split("/")[3])
        if p.startswith(("/api/sets/", "/api/stats")):
            return ocr_core.Handler.do_GET(self)
        if p.startswith("/api/notes"):
            return kb_core.H.do_GET(self)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith(("/api/prepare", "/api/batch", "/api/cancel", "/api/v1/ocr")):
            return ocr_core.Handler.do_POST(self)
        if p.startswith("/api/skill-stage/"):
            from urllib.parse import urlparse, parse_qs, unquote
            name = p[len("/api/skill-stage/"):].strip("/")
            rel = unquote((parse_qs(urlparse(self.path).query).get("path") or [""])[0])
            n = int(self.headers.get("Content-Length") or 0)
            if n > SKILL_MAX_FILE:
                return self._send(400, {"error": "file over %d MB"
                                        % (SKILL_MAX_FILE // 1048576)})
            data = self.rfile.read(n) if n else b""
            try:
                have = store.stage_list(name)
                if len(have) >= SKILL_MAX_FILES:
                    return self._send(400, {"error": "at most %d files" % SKILL_MAX_FILES})
                if sum(f["bytes"] for f in have) + len(data) > SKILL_MAX_BYTES:
                    return self._send(400, {"error": "over %d MB in total"
                                            % (SKILL_MAX_BYTES // 1048576)})
                size = store.stage_write(name, rel, data)
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            return self._send(200, {"path": rel, "bytes": size})

        if p.startswith("/api/skill-cancel/"):
            store.stage_drop(p[len("/api/skill-cancel/"):].strip("/"))
            return self._send(200, {"ok": True})

        if p.startswith("/api/skill-publish/"):
            name = p[len("/api/skill-publish/"):].strip("/")
            try:
                b = self._json()
            except Exception:
                b = {}
            try:
                staged = store.stage_list(name)
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            md = next((f for f in staged if f["path"].lower() == "skill.md"), None)
            if not md:
                store.stage_drop(name)
                return self._send(400, {"error": "a skill needs a SKILL.md at its root"})
            with open(store.stage_path(name, md["path"]), "rb") as fh:
                meta = parse_skill_md(fh.read().decode("utf-8", "replace"))
            if meta["name"] and meta["name"] != name:
                store.stage_drop(name)
                return self._send(400, {"error": "SKILL.md says name: %s, not %s"
                                        % (meta["name"], name)})
            if not meta["description"]:
                store.stage_drop(name)
                return self._send(400, {"error": "SKILL.md needs a description: field"})
            try:
                r = store.stage_commit(name, meta["title"], meta["description"],
                                       meta["body"], b.get("author", ""), meta["tags"])
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            return self._send(200, r)

        if p == "/api/skills" or p.startswith("/api/skill/"):
            import base64
            try:
                b = self._json()
            except Exception:
                return self._send(400, {"error": "bad json"})
            if p.startswith("/api/skill/") and b.get("_delete"):
                nm = p[len("/api/skill/"):].strip("/")
                if not store.skills(nm):
                    return self._send(404, {"error": "no such skill"})
                store.skill_delete(nm)
                return self._send(200, {"deleted": nm})
            files = b.get("files") or []
            if not files:
                return self._send(400, {"error": "no files"})
            if len(files) > SKILL_MAX_FILES:
                return self._send(400, {"error": "at most %d files" % SKILL_MAX_FILES})
            out, total = [], 0
            for f in files:
                path = (f.get("path") or "").lstrip("/")
                if not path or ".." in path.split("/"):
                    return self._send(400, {"error": "bad path: %s" % path})
                data = (base64.b64decode(f["b64"]) if f.get("b64") is not None
                        else (f.get("text") or "").encode())
                total += len(data)
                if total > SKILL_JSON_BYTES:
                    return self._send(400, {
                        "error": "over %d MB - upload file by file instead "
                                 "(POST /api/skill-stage/<name>?path=...)"
                                 % (SKILL_JSON_BYTES // 1048576)})
                out.append({"path": path, "data": data})
            md = next((f for f in out if f["path"].lower() == "skill.md"), None)
            if not md:
                return self._send(400, {"error": "a skill needs a SKILL.md at its root"})
            meta = parse_skill_md(md["data"].decode("utf-8", "replace"))
            name = (b.get("name") or meta["name"] or "").strip().lower()
            if not store.SKILL_NAME.match(name):
                return self._send(400, {"error": "SKILL.md needs a name: field "
                                                 "(lowercase, 2-64 chars)"})
            if not meta["description"]:
                return self._send(400, {"error": "SKILL.md needs a description: field"})
            try:
                r = store.skill_save(name, meta["title"], meta["description"],
                                     meta["body"], out,
                                     b.get("author", ""), meta["tags"])
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            return self._send(200, r)
        if p == "/api/v1/note":
            try:
                b = self._json()
            except Exception:
                return self._send(400, {"error": "bad json"})
            t, body = (b.get("title") or "").strip(), (b.get("body") or "").strip()
            if not t or not body:
                return self._send(400, {"error": "title and body are required"})
            nid = store.add(t, body, b.get("tags", ""), b.get("author", "cli"))
            return self._send(200, {"id": nid})
        if p == "/api/v1/ask":
            try:
                b = self._json()
            except Exception:
                return self._send(400, {"error": "bad json"})
            q = (b.get("question") or "").strip()
            if not q:
                return self._send(400, {"error": "question is required"})
            if (b.get("mode") or "summary").lower().startswith("direct"):
                t0 = time.time()
                r = direct_answer(q, int(b.get("limit") or 4),
                                  int(b.get("chars") or 12000),
                                  pick=b.get("pick", True) is not False)
                r["seconds"] = round(time.time() - t0, 2)
                return self._send(200, r)
            # fall through to the summarising path
        if p.startswith(("/api/notes", "/api/ask", "/api/v1/ask")):
            return kb_core.H.do_POST(self)
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="OCR + knowledge base on one GPU")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--prompt-ip", default=None)
    ap.add_argument("--llama", default=None, help="path to llama-server")
    ap.add_argument("--preload", default=None, choices=["ocr", "kb", "vision"],
                    help="load one model at startup instead of on first use")
    a = ap.parse_args()

    profiles = find_models()
    models.configure(profiles, server=a.llama)
    ocr_core.OPT["vlm"] = "managed" if "vision" in profiles else None

    if a.prompt_ip:
        ip = a.prompt_ip if "://" in a.prompt_ip else "http://" + a.prompt_ip
        OPT["prompt_base"] = ip if ":" in ip.split("//", 1)[1] else f"{ip}:{a.port}"
    else:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            OPT["prompt_base"] = f"http://{s.getsockname()[0]}:{a.port}"
            s.close()
        except Exception:
            OPT["prompt_base"] = None

    c = store.count()
    print("=" * 62)
    print("LLM Mini Toolkit  —  OCR + knowledge base, one GPU")
    if not models._state.get("server"):
        print("  ERROR: llama-server not found (--llama /path)")
    for k, v in profiles.items():
        extra = f", falls back {len(v['fallbacks'])} times if it will not fit" if v.get("fallbacks") else ""
        print(f"  {k:<7}: {v['label']}  ({v['vram_mib']} MiB when loaded{extra})")
    if not profiles:
        print("  no models found in models/")
    print(f"  notes  : {c['n']} ({c['b']} characters)")
    print("  models are swapped per request, about 1-2 s each way")
    if a.preload:
        print(f"  preloading {a.preload} ...")
        try:
            models.ensure(a.preload)
            print("  loaded.")
        except Exception as ex:
            print("  preload failed:", ex)
    print(f"\n  -> {OPT.get('prompt_base') or f'http://127.0.0.1:{a.port}'}\n" + "=" * 62)
    try:
        ThreadingHTTPServer((a.host, a.port), H).serve_forever()
    finally:
        models.stop()


if __name__ == "__main__":
    main()
