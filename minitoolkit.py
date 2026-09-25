#!/usr/bin/env python3
"""Read documents and query the team's notes from the command line.

Standard library only - no install, Python 3.7+.

    python3 minitoolkit.py read invoice.jpg
    python3 minitoolkit.py read datasheet.pdf --pages 1,5,12
    python3 minitoolkit.py search "swd debug"
    python3 minitoolkit.py get 12
    python3 minitoolkit.py ask "how do we wire SWD for STM32?"
    python3 minitoolkit.py add notes.txt --title "Board bring-up" --tags "stm32 hw"
    python3 minitoolkit.py status

A bare file argument is treated as `read`:

    python3 minitoolkit.py scan.pdf
"""
import argparse, base64, json, os, re, sys, time, urllib.request, urllib.error, urllib.parse

DEFAULT_SERVER = "http://127.0.0.1:3333"
EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif")


def log(msg, quiet=False):
    if not quiet:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def post(server, path, payload, timeout):
    req = urllib.request.Request(server.rstrip("/") + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(server, path, timeout=10):
    with urllib.request.urlopen(server.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read())


def explain(ex, server):
    if isinstance(ex, urllib.error.HTTPError):
        try:
            return json.loads(ex.read()).get("error", str(ex))
        except Exception:
            return f"HTTP {ex.code}"
    if isinstance(ex, urllib.error.URLError):
        return f"cannot reach {server} ({ex.reason})"
    return str(ex)


def reachable(server):
    """Fail in seconds rather than hanging for the job timeout."""
    try:
        get(server, "/api/status", timeout=6)
        return None
    except Exception as ex:
        return explain(ex, server)


def parse_pages(spec):
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


# --------------------------------------------------------------------- read
def cmd_read(a):
    pages = parse_pages(a.pages)
    chunks, blob, failed = [], [], 0
    for f in a.files:
        if not os.path.isfile(f):
            log(f"!! not a file: {f}", a.quiet)
            failed += 1
            continue
        if not f.lower().endswith(EXTS):
            log(f"!! unsupported type: {f}", a.quiet)
            failed += 1
            continue
        log(f"-> {os.path.basename(f)} ({os.path.getsize(f) / 1e6:.1f} MB), reading...", a.quiet)
        with open(f, "rb") as fh:
            payload = {"file_b64": base64.b64encode(fh.read()).decode(),
                       "name": os.path.basename(f)}
        if pages:
            payload["pages"] = pages
        t0 = time.time()
        try:
            r = post(a.server, "/api/v1/ocr", payload, a.timeout)
            if r.get("error"):
                raise RuntimeError(r["error"])
        except Exception as ex:
            log(f"!! {os.path.basename(f)}: {explain(ex, a.server)}", a.quiet)
            failed += 1
            continue
        ok = sum(1 for p in r["pages"] if p["ok"])
        log(f"   {ok}/{len(r['pages'])} page(s) in {time.time() - t0:.0f}s", a.quiet)
        blob.append({"file": f, **r})
        for p in r["pages"]:
            if not p["ok"]:
                failed += 1
                log(f"!! page {p['page']}: {p.get('error', 'failed')}", a.quiet)
                continue
            head = os.path.basename(f)
            if r["page_count"] > 1:
                head += f" · page {p['page']}"
            chunks.append((head, p["text"]))

    if a.split:
        os.makedirs(a.split, exist_ok=True)
        for head, text in chunks:
            name = head.replace(" · ", "_").replace(" ", "_") + ".txt"
            with open(os.path.join(a.split, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        log(f"wrote {len(chunks)} file(s) to {a.split}", a.quiet)

    if a.json:
        out = json.dumps(blob, ensure_ascii=False, indent=2)
    elif len(chunks) == 1:
        out = chunks[0][1]
    else:
        out = "\n\n".join(f"===== {h} =====\n{t}" for h, t in chunks)
    emit(out, a)
    return 1 if failed and not chunks else 0


# ---------------------------------------------------------------------- ask
def cmd_ask(a):
    q = " ".join(a.question).strip()
    if not q:
        log("!! nothing to ask", a.quiet)
        return 2
    mode = "direct" if getattr(a, "direct", False) else "summary"
    log(f"-> {'fetching' if mode == 'direct' else 'asking'} the notes: {q}", a.quiet)
    t0 = time.time()
    try:
        payload = {"question": q, "mode": mode}
        if mode == "direct":
            payload["limit"] = a.limit
            payload["chars"] = a.chars
            if getattr(a, "no_pick", False):
                payload["pick"] = False
        r = post(a.server, "/api/v1/ask", payload, a.timeout)
        if r.get("error"):
            raise RuntimeError(r["error"])
    except Exception as ex:
        log(f"!! {explain(ex, a.server)}", a.quiet)
        return 1
    m = r.get("metrics") or {}
    if mode == "direct":
        log(f"   {r.get('count', 0)} of {r.get('candidates', '?')} article(s) in "
            f"{time.time() - t0:.1f}s, picked by {r.get('picked_by', '?')}", a.quiet)
        if m.get("returned_tokens"):
            # so you can budget your own context before pasting this anywhere
            log(f"   {m['returned_tokens']} tokens, {m['returned_chars']} chars"
                + (f", {m['truncated']} truncated" if m.get("truncated") else ""), a.quiet)
        if a.json:
            emit(json.dumps(r, ensure_ascii=False, indent=2), a)
        else:
            emit(r.get("text", ""), a)
        return 0
    log(f"   {r.get('candidates', 0)} candidate note(s), answered in {time.time() - t0:.0f}s", a.quiet)
    ans = m.get("answer") or {}
    if ans.get("output_tokens"):
        log(f"   {ans['output_tokens']} tokens written at {ans.get('output_per_second', '?')}/s, "
            f"prompt {ans.get('prompt_tokens', '?')}"
            + (f" of {m['ctx_limit']}" if m.get("ctx_limit") else ""), a.quiet)
    if a.json:
        emit(json.dumps(r, ensure_ascii=False, indent=2), a)
        return 0
    out = r.get("answer", "")
    src = r.get("sources") or []
    if src and not a.bare:
        out += "\n\nSources:\n" + "\n".join(f"  [#{s['id']}] {s['title']}" for s in src)
    emit(out, a)
    return 0


# ------------------------------------------------------------------- search
def cmd_search(a):
    from urllib.parse import quote
    q = " ".join(a.query).strip()
    if not q:
        return 2
    try:
        r = get(a.server, f"/api/v1/search?q={quote(q)}&limit={a.limit}", timeout=30)
    except Exception as ex:
        log(f"!! {explain(ex, a.server)}", a.quiet)
        return 1
    if a.json:
        emit(json.dumps(r, ensure_ascii=False, indent=2), a)
        return 0
    hits = r.get("results", [])
    if not hits:
        log("no match", a.quiet)
        return 0
    lines = []
    for h in hits:
        lines.append(f"[#{h['id']}] {h['title']}   ({h['score']})")
        if not a.bare:
            if h.get("tags"):
                lines.append(f"        tags: {h['tags']}")
            lines.append("        " + h["preview"].replace("\n", " ")[:160])
    emit("\n".join(lines), a)
    return 0


def cmd_get(a):
    try:
        n = get(a.server, f"/api/v1/note/{a.id}", timeout=30)
    except Exception as ex:
        log(f"!! {explain(ex, a.server)}", a.quiet)
        return 1
    if a.json:
        emit(json.dumps(n, ensure_ascii=False, indent=2), a)
    else:
        head = f"[#{n['id']}] {n['title']}"
        if n.get("tags"):
            head += f"\ntags: {n['tags']}"
        emit(head + "\n\n" + n.get("body", ""), a)
    return 0


def cmd_add(a):
    body = ""
    if a.file:
        if not os.path.isfile(a.file):
            log(f"!! not a file: {a.file}", a.quiet)
            return 1
        body = open(a.file, encoding="utf-8", errors="replace").read()
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    if not body.strip():
        log("!! nothing to add - give a file or pipe text in", a.quiet)
        return 2
    title = a.title or (os.path.basename(a.file) if a.file else body.strip().splitlines()[0][:80])
    try:
        r = post(a.server, "/api/v1/note",
                 {"title": title, "body": body, "tags": a.tags or "",
                  "author": a.author or "cli"}, 60)
        if r.get("error"):
            raise RuntimeError(r["error"])
    except Exception as ex:
        log(f"!! {explain(ex, a.server)}", a.quiet)
        return 1
    log(f"added [#{r['id']}] {title}", a.quiet)
    if not a.quiet:
        print(r["id"])
    return 0


def emit(text, a):
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        log(f"wrote {a.out}", a.quiet)
    elif text and not (getattr(a, "split", None) and not a.json):
        print(text)


# -------------------------------------------------------------------- status
def cmd_status(a):
    try:
        s = get(a.server, "/api/status")
    except Exception as ex:
        print(f"offline: {explain(ex, a.server)}")
        return 1
    m = s.get("models", {})
    print(f"server : {a.server}")
    print(f"model  : {m.get('label') or 'none loaded'}"
          f"   (swaps: {m.get('switches', 0)}, {m.get('switch_seconds', 0)}s total)")
    print("  " + ", ".join(f"{x['name']} {x['vram_mib']} MiB" for x in m.get("available", []))
          or "  no models")
    print(f"ocr    : queue {s.get('ocr', {}).get('queue', 0)}, "
          f"pdf {'yes' if s.get('ocr', {}).get('pdf') else 'no'}")
    print(f"notes  : {s.get('kb', {}).get('notes', 0)}")
    return 0


# ------------------------------------------------------------------- skills
SKILL_HOME = os.path.expanduser("~/.claude/skills")


def opencode_agents():
    """Where opencode keeps its global instructions.

    ~/.config/opencode is what it uses on linux and mac and what expanduser
    gives on Windows too; the %APPDATA% location wins only if it already
    exists, so an existing install is never bypassed for a fresh empty path.
    """
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "opencode", "AGENTS.md"))
    candidates.append(os.path.expanduser(os.path.join("~", ".config", "opencode",
                                                      "AGENTS.md")))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return candidates[-1]


def local_skills(root=None):
    """What is installed here, enabled or not."""
    root = os.path.expanduser(root) if root else SKILL_HOME
    out = []
    for state, base in (("enabled", root), ("disabled", os.path.join(root, ".disabled"))):
        if not os.path.isdir(base):
            continue
        for n in sorted(os.listdir(base)):
            d = os.path.join(base, n)
            if n.startswith(".") or not os.path.isfile(os.path.join(d, "SKILL.md")):
                continue
            out.append({"name": n, "state": state, "path": d,
                        "description": local_description(d)})
    return out


def local_description(folder):
    try:
        with open(os.path.join(folder, "SKILL.md"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    body, key, out = m.group(1), None, {}
    for line in body.split("\n"):
        kv = re.match(r"^(\w[\w-]*)\s*:\s*(.*)$", line)
        if kv:
            key = kv.group(1).lower()
            v = kv.group(2).strip()
            out[key] = "" if v in (">", ">-", "|", "|-") else v.strip("\"'")
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out.get("description", "")


def agents_block(name, description, path):
    return ("<!-- %s:start -->\n## %s\n%s\n\nRead `%s/SKILL.md` before following it; "
            "everything the skill needs is in that folder.\n<!-- %s:end -->"
            % (name, name, (description or "").strip(), path, name))


def agents_edit(name, block=None, quiet=False):
    """Put the block in, replace it, or take it out. Never duplicates it."""
    path = opencode_agents()
    start, end = "<!-- %s:start -->" % name, "<!-- %s:end -->" % name
    try:
        cur = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                cur = fh.read()
        have = start in cur and end in cur
        if block is None:
            if not have:
                return "not there"
            i, j = cur.index(start), cur.index(end) + len(end)
            cur = (cur[:i].rstrip("\n") + "\n" + cur[j:].lstrip("\n")).rstrip("\n") + "\n"
            verb = "removed"
        else:
            if have:
                i, j = cur.index(start), cur.index(end) + len(end)
                cur = cur[:i] + block + cur[j:]
                verb = "updated"
            else:
                cur = cur.rstrip("\n") + "\n\n" + block + "\n"
                verb = "written"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cur)
        return "%s in %s" % (verb, path)
    except Exception as ex:
        return "could not be edited: %s" % ex


def cmd_skill(a):
    if a.action == "installed":
        rows = local_skills(a.to)
        if a.json:
            emit(json.dumps({"skills": rows}, ensure_ascii=False, indent=2), a)
            return 0
        if not rows:
            emit("nothing installed in " + (os.path.expanduser(a.to) if a.to else SKILL_HOME), a)
            return 0
        emit("\n".join("%-22s %-9s %s" % (r["name"], r["state"], r["description"][:80])
                        for r in rows), a)
        return 0

    if a.action in ("disable", "enable", "uninstall"):
        if not a.name:
            log("!! which skill? try: minitoolkit.py skill installed", a.quiet)
            return 2
        root = os.path.expanduser(a.to) if a.to else SKILL_HOME
        live = os.path.join(root, a.name)
        parked = os.path.join(root, ".disabled", a.name)
        tool = "claude" if a.no_opencode else (a.tool or "both")

        if a.action == "uninstall":
            import shutil
            gone = []
            for d in (live, parked):
                if os.path.isdir(d) and tool in ("both", "claude", "none"):
                    shutil.rmtree(d)
                    gone.append(d)
            if tool in ("both", "opencode"):
                log("   opencode: %s" % agents_edit(a.name), a.quiet)
            if not gone and tool in ("both", "claude"):
                log("!! %s is not installed in %s" % (a.name, root), a.quiet)
                return 1
            for d in gone:
                log("   removed %s" % d, a.quiet)
            log("   the skill is still published; install it again whenever.", a.quiet)
            return 0

        if a.action == "disable":
            if os.path.isdir(parked):
                log("   %s is already disabled" % a.name, a.quiet)
            elif os.path.isdir(live):
                os.makedirs(os.path.dirname(parked), exist_ok=True)
                os.replace(live, parked)
                log("   moved to %s — the files are kept" % parked, a.quiet)
            else:
                log("!! %s is not installed in %s" % (a.name, root), a.quiet)
                return 1
            if tool in ("both", "opencode"):
                log("   opencode: %s" % agents_edit(a.name), a.quiet)
            emit(parked, a)
            return 0

        # enable
        if os.path.isdir(live):
            log("   %s is already enabled" % a.name, a.quiet)
        elif os.path.isdir(parked):
            os.replace(parked, live)
            log("   moved back to %s" % live, a.quiet)
        else:
            log("!! %s is not installed in %s" % (a.name, root), a.quiet)
            return 1
        if tool in ("both", "opencode"):
            log("   opencode: %s"
                % agents_edit(a.name, agents_block(a.name, local_description(live), live)),
                a.quiet)
        emit(live, a)
        return 0

    if a.action == "list":
        d = get(a.server, "/api/skills", a.timeout)
        rows = d.get("skills") or []
        if a.json:
            emit(json.dumps(d, ensure_ascii=False, indent=2), a)
            return 0
        if not rows:
            emit("no skills published", a)
            return 0
        out = []
        for s in rows:
            out.append("%-22s %2d file(s)  %s" % (s["name"], s["n_files"],
                                                  (s.get("description") or "").split("\n")[0][:96]))
        emit("\n".join(out), a)
        return 0

    if a.action == "publish":
        return skill_publish(a)

    if not a.name:
        log("!! which skill? try: minitoolkit.py skill list", a.quiet)
        return 2
    d = get(a.server, "/api/skill/" + urllib.parse.quote(a.name), a.timeout)
    if d.get("error"):
        log("!! " + d["error"], a.quiet)
        return 1

    if a.action == "show":
        if a.json:
            emit(json.dumps(d, ensure_ascii=False, indent=2), a)
        else:
            files = "\n".join("  %-40s %6d B" % (f["path"], f["bytes"]) for f in d["files"])
            emit("%s\n\nfiles:\n%s\n\n%s" % (d["name"], files, d.get("body", "")), a)
        return 0

    # install: write the files out, run nothing
    tool = "claude" if a.no_opencode else (a.tool or "both")
    dest = os.path.join(os.path.expanduser(a.to) if a.to else SKILL_HOME, d["name"])
    if os.path.isdir(dest) and not a.force:
        have = sorted(os.listdir(dest))
        log("!! %s already exists (%d entries) - pass --force to replace it"
            % (dest, len(have)), a.quiet)
        return 1
    log("-> %s: %d file(s) into %s" % (d["name"], len(d["files"]), dest), a.quiet)
    for f in d["files"]:
        log("     %s" % f["path"], a.quiet)
    os.makedirs(dest, exist_ok=True)
    for f in d["files"]:
        url = "%s/api/skill/%s/files/%s" % (a.server.rstrip("/"),
                                            urllib.parse.quote(d["name"]),
                                            urllib.parse.quote(f["path"]))
        with urllib.request.urlopen(url, timeout=a.timeout) as r:
            blob = r.read()
        target = os.path.join(dest, *f["path"].split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)
    log("   installed. Nothing was run - read SKILL.md before using it.", a.quiet)

    wheels = os.path.join(dest, "wheels")
    if os.path.isdir(wheels):
        whl = sorted(f for f in os.listdir(wheels) if f.endswith((".whl", ".tar.gz")))
        req = os.path.join(dest, "requirements.txt")
        cmd = [sys.executable, "-m", "pip", "install", "--no-index",
               "--find-links", wheels]
        cmd += ["-r", req] if os.path.isfile(req) else [os.path.join(wheels, f) for f in whl]
        if a.with_wheels:
            log("   installing %d wheel(s) with --no-index (nothing is fetched)" % len(whl),
                a.quiet)
            import subprocess
            p = subprocess.run(cmd, capture_output=True, text=True)
            out = (p.stdout or "") + (p.stderr or "")
            if p.returncode == 0:
                log("   installed", a.quiet)
            elif "externally-managed-environment" in out or "PEP 668" in out:
                # Debian and friends refuse to let pip touch the system python.
                # A well-built skill imports its own wheels anyway, so this is
                # a note, not a failure.
                log("   pip refused: this python is externally managed (PEP 668).", a.quiet)
                log("   Nothing is broken - a skill that ships wheels should import", a.quiet)
                log("   them from its own folder. To install them for other programs:", a.quiet)
                log("     python3 -m venv %s/.venv && %s/.venv/bin/python -m pip \\"
                    % (dest, dest), a.quiet)
                log("       install --no-index --find-links %s ." % wheels, a.quiet)
            else:
                log("   pip exited %d: %s" % (p.returncode, out.strip().splitlines()[-1][:160]
                                              if out.strip() else "no output"), a.quiet)
        else:
            log("   %d wheel(s) shipped with it; the skill should import them from"
                % len(whl), a.quiet)
            log("   its own folder without installing anything. --with-wheels installs", a.quiet)
            log("   them for other programs too.", a.quiet)

    # Both tools, by default: a skill installed into one and missing from the
    # other is the thing people forget, and then wonder why opencode ignores it.
    if tool in ("both", "opencode"):
        agents_path = opencode_agents()
        block = agents_block(d["name"], d.get("description"), dest)
        try:
            os.makedirs(os.path.dirname(agents_path), exist_ok=True)
            cur = ""
            if os.path.isfile(agents_path):
                with open(agents_path, encoding="utf-8") as fh:
                    cur = fh.read()
            start, end = "<!-- %s:start -->" % d["name"], "<!-- %s:end -->" % d["name"]
            if start in cur and end in cur:      # replace, never duplicate
                cur = cur[:cur.index(start)] + block + cur[cur.index(end) + len(end):]
            else:
                cur = cur.rstrip("\n") + "\n\n" + block + "\n"
            with open(agents_path, "w", encoding="utf-8") as fh:
                fh.write(cur)
            log("   opencode: block written into %s" % agents_path, a.quiet)
        except Exception as ex:
            log("!! opencode block not written: %s "
                "(the files are installed; pass --no-opencode to stop trying)"
                % ex, a.quiet)
    emit(dest, a)
    return 0


SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".prev"}


def skill_publish(a):
    """Send a folder up file by file, then commit. Raw bodies, so a 40 MB wheel
    costs 40 MB on the wire rather than 53 MB of base64."""
    src = os.path.abspath(os.path.expanduser(a.name or "."))
    if not os.path.isdir(src):
        log("!! not a folder: %s" % src, a.quiet)
        return 2
    md = os.path.join(src, "SKILL.md")
    if not os.path.isfile(md):
        log("!! no SKILL.md in %s" % src, a.quiet)
        return 2
    name = ""
    with open(md, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^name\s*:\s*(\S+)", line)
            if m:
                name = m.group(1).strip().strip("\"'")
                break
    name = name or os.path.basename(src).lower()

    files = []
    for base, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(base, n)
            if os.path.isfile(full):
                files.append((os.path.relpath(full, src).replace(os.sep, "/"), full))
    files.sort(key=lambda x: (x[0].lower() != "skill.md", x[0]))
    total = sum(os.path.getsize(f) for _, f in files)
    log("-> %s: %d file(s), %.1f MB" % (name, len(files), total / 1048576.0), a.quiet)

    sent = 0
    for rel, full in files:
        with open(full, "rb") as fh:
            blob = fh.read()
        url = "%s/api/skill-stage/%s?path=%s" % (a.server.rstrip("/"),
                                                 urllib.parse.quote(name),
                                                 urllib.parse.quote(rel))
        req = urllib.request.Request(url, data=blob,
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            urllib.request.urlopen(req, timeout=a.timeout).read()
        except urllib.error.HTTPError as ex:
            log("!! %s: %s" % (rel, ex.read().decode("utf-8", "replace")[:160]), a.quiet)
            post(a.server, "/api/skill-cancel/" + urllib.parse.quote(name), {}, a.timeout)
            return 1
        sent += len(blob)
        log("     %-52s %7.1f KB" % (rel[:52], len(blob) / 1024.0), a.quiet)
    r = post(a.server, "/api/skill-publish/" + urllib.parse.quote(name),
             {"author": a.author or ""}, a.timeout)
    if r.get("error"):
        log("!! " + r["error"], a.quiet)
        return 1
    log("   published %s: %d file(s), %.1f MB" % (r["name"], r["n_files"],
                                                  r["bytes"] / 1048576.0), a.quiet)
    emit(r["name"], a)
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="minitoolkit.py",
        description="Read documents and query the team's notes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="A bare file argument means `read`:  minitoolkit.py scan.pdf")
    # Shared flags live on a parent parser so they work on either side of the
    # subcommand: `search x --json` reads as naturally as `--json search x`.
    # The same action objects are shared between the top parser and every
    # subparser so a flag works on either side. That means they cannot carry a
    # default - the subparser would apply it a second time and overwrite what
    # was given before the subcommand. So: SUPPRESS here, real defaults after.
    COMMON_DEFAULTS = {
        "server": os.environ.get("MINITOOLKIT_URL", DEFAULT_SERVER),
        "timeout": 3600, "out": None, "json": False, "quiet": False,
    }
    common = argparse.ArgumentParser(add_help=False,
                                     argument_default=argparse.SUPPRESS)
    common.add_argument("--server", help=f"default: {DEFAULT_SERVER}")
    common.add_argument("--timeout", type=int, help="seconds, default 3600")
    common.add_argument("--out", help="write to this file instead of stdout")
    common.add_argument("--json", action="store_true", help="print the raw reply")
    common.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")
    for x in common._actions:
        if x.dest != "help":
            ap._add_action(x)
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("read", help="extract text from images and PDFs", parents=[common])
    r.add_argument("files", nargs="+")
    r.add_argument("--pages", help="PDF pages, e.g. 1,5,12 or 2-6")
    r.add_argument("--split", metavar="DIR", help="one .txt per page into DIR")

    k = sub.add_parser("ask", help="ask the knowledge base", parents=[common])
    k.add_argument("question", nargs="+")
    k.add_argument("--bare", action="store_true", help="answer only, no source list")
    k.add_argument("--direct", action="store_true",
                   help="return the articles themselves instead of a summary; the "
                        "model still picks which ones, it just does not paraphrase")
    k.add_argument("--no-pick", action="store_true",
                   help="with --direct, skip the model and trust the keyword rank "
                        "- sub-second, no GPU")
    k.add_argument("--limit", type=int, default=4, help="articles in direct mode")
    k.add_argument("--chars", type=int, default=12000, help="per article in direct mode")

    se = sub.add_parser("search", help="find notes by keyword - instant, no model", parents=[common])
    se.add_argument("query", nargs="+")
    se.add_argument("--limit", type=int, default=12)
    se.add_argument("--bare", action="store_true", help="titles only")

    g = sub.add_parser("get", help="print one note in full", parents=[common])
    g.add_argument("id", type=int)

    ad = sub.add_parser("add", help="add a note from a file or stdin", parents=[common])
    ad.add_argument("file", nargs="?")
    ad.add_argument("--title")
    ad.add_argument("--tags")
    ad.add_argument("--author")

    sub.add_parser("status", help="what the server is doing", parents=[common])

    sk = sub.add_parser("skill", help="list, read and install shared skills", parents=[common])
    sk.add_argument("action", nargs="?", default="list",
                    choices=["list", "show", "install", "publish",
                             "installed", "disable", "enable", "uninstall"])
    sk.add_argument("name", nargs="?", help="skill name, or a folder for publish")
    sk.add_argument("--with-wheels", action="store_true",
                    help="after installing, pip install the skill's own wheels/ "
                         "with --no-index, so nothing is fetched from the network")
    sk.add_argument("--to", help="where to install (default ~/.claude/skills)")
    sk.add_argument("--tool", choices=["both", "claude", "opencode", "none"],
                    default="both",
                    help="which tool to register the skill with (default: both). "
                         "claude = ~/.claude/skills only, opencode = the AGENTS.md "
                         "block only, none = just unpack the files")
    sk.add_argument("--no-opencode", action="store_true",
                    help=argparse.SUPPRESS)          # kept working, same as --tool claude
    sk.add_argument("--force", action="store_true", help="overwrite an existing folder")

    # allow `minitoolkit.py file.pdf` with no subcommand
    argv = sys.argv[1:]
    known = {"read", "ask", "search", "get", "add", "status", "skill", "-h", "--help"}
    if argv and not argv[0].startswith("-") and argv[0] not in known:
        argv = ["read"] + argv
    a = ap.parse_args(argv)
    for k, v in COMMON_DEFAULTS.items():
        if getattr(a, k, None) is None and not hasattr(a, k):
            setattr(a, k, v)
        elif getattr(a, k, None) is None:
            setattr(a, k, v)
    if not a.cmd:
        ap.print_help()
        return 2

    for name in ("pages", "split", "bare", "limit", "chars", "direct", "no_pick",
                 "file", "title", "tags", "author", "id",
                 "action", "name", "to", "no_opencode", "tool", "force", "with_wheels"):
        if not hasattr(a, name):
            setattr(a, name, None)

    # these only touch the local filesystem; the server does not have to be up
    LOCAL = {"installed", "disable", "enable", "uninstall"}
    if a.cmd not in ("status",) and not (a.cmd == "skill" and a.action in LOCAL):
        why = reachable(a.server)
        if why:
            log(f"!! toolkit unavailable: {why}", False)
            log("   pass --server http://HOST:PORT if the address is wrong", False)
            return 1

    return {"read": cmd_read, "ask": cmd_ask, "search": cmd_search,
            "get": cmd_get, "add": cmd_add, "status": cmd_status,
            "skill": cmd_skill}[a.cmd](a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
