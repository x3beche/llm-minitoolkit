#!/usr/bin/env python3
"""GLM-OCR - extract text from page images and PDFs.

Talks to a local llama-server over HTTP. Files are prepared first (PDFs are
expanded into one entry per page), then queued and processed one at a time.

    python3 app.py --host 0.0.0.0 --port 3333
"""
import os, sys, json, uuid, time, base64, threading, queue, argparse, shutil, subprocess, glob, io
import socket as socket_mod
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import models

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
WORK = os.path.join(BASE, "work")
os.makedirs(WORK, exist_ok=True)

sys.path.insert(0, os.path.join(BASE, "lib"))   # bundled offline wheels

# --- measured constants (RTX A400 + i7-14700) -------------------------------
# 1600px produced byte-identical output to the 2480px native scan, while
# 1024px misread fine print. Anything larger only costs memory.
TARGET_PX = 1600
THUMB_PX = 130
MAX_TOKENS = 1024
PROMPT = "OCR this page. Output every line of text including headings."
MAX_PAGES = int(os.environ.get("GLM_OCR_MAX_PAGES", "1000"))

# A page that comes back nearly empty is usually a technical drawing: the model
# treats the whole figure as an image and skips the text inside it. Cropping
# changes that - a tile reads as a page, so the embedded table gets transcribed.
# Measured on an MT6365 package-outline page: 74 chars whole-page (at 1600,
# 2000, 2600 and 3200 px alike, and with three different prompts) vs 838 chars
# from the tile holding the dimensions table.
DETAIL_MIN_CHARS = int(os.environ.get("GLM_OCR_DETAIL_MIN", "200"))
DETAIL_PX = TARGET_PX * 2
DETAIL_OVERLAP = 0.12
DETAIL_INK_GAP = float(os.environ.get("GLM_OCR_DETAIL_INK_GAP", "1.6"))

OPT = {"llama": "http://127.0.0.1:8080", "prompt_base": None, "vlm": None}

# The OCR model transcribes what is printed; it cannot read a chart or explain a
# drawing. When a second, general vision model is reachable we ask it about the
# figure on pages that came back nearly empty - those are the figure pages.
# Structure only. The OCR model already extracted every number on the page
# correctly; when this model was also allowed to restate them it produced a
# second, wrong copy of the same table (0.850 vs 0.8000, 0.170 vs 0.3500) and
# the reader could not tell which to trust.
VLM_PROMPT = (
    "Describe what this figure shows and how its parts relate: the kind of "
    "figure, the views or sections it contains, what the arrows or connections "
    "mean, and the overall trend if it is a chart. "
    "Do NOT transcribe or restate any numbers, measurements or table cells - "
    "those are read separately by another tool and your version would conflict "
    "with it. Describe relationships in words only. Keep it under 120 words.")
MIME = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
        ".ico": "image/x-icon", ".css": "text/css", ".js": "text/javascript"}



# --- counters only: no text, no file names, nothing about what was read ------
STATS_FILE = os.path.join(BASE, "stats.json")   # re-pointed per port in main()
STATS_LOCK = threading.Lock()
STATS = {"pages_ok": 0, "pages_failed": 0, "tokens": 0, "chars": 0,
         "seconds": 0.0, "documents": 0, "pdf_pages": 0, "second_pass": 0,
         "described": 0, "started": time.time(), "since": None}


def stats_load():
    try:
        with open(STATS_FILE) as fh:
            saved = json.load(fh)
        for k in ("pages_ok", "pages_failed", "tokens", "chars", "seconds",
                  "documents", "pdf_pages", "second_pass", "described"):
            if isinstance(saved.get(k), (int, float)):
                STATS[k] = saved[k]
        STATS["since"] = saved.get("since") or time.time()
    except Exception:
        STATS["since"] = time.time()


def stats_save():
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({k: v for k, v in STATS.items() if k != "started"}, fh)
        os.replace(tmp, STATS_FILE)
    except OSError:
        pass


def stats_add(**kw):
    with STATS_LOCK:
        for k, v in kw.items():
            STATS[k] = round(STATS.get(k, 0) + v, 2) if isinstance(v, float) else STATS.get(k, 0) + v
        stats_save()


def stats_view():
    with STATS_LOCK:
        d = dict(STATS)
    done = d["pages_ok"] + d["pages_failed"]
    d["pages_total"] = done
    d["avg_seconds"] = round(d["seconds"] / d["pages_ok"], 1) if d["pages_ok"] else 0
    d["avg_tokens"] = round(d["tokens"] / d["pages_ok"]) if d["pages_ok"] else 0
    d["tokens_per_sec"] = round(d["tokens"] / d["seconds"], 1) if d["seconds"] else 0
    d["uptime"] = int(time.time() - d.pop("started"))
    d["seconds"] = round(d["seconds"])
    return d


# ------------------------------------------------------------- llama-server
def llama_up(timeout=2):
    """The OCR model is loaded on demand, so 'up' means 'can be loaded'."""
    return "ocr" in models.PROFILES


def vlm_describe(png_path, timeout=900):
    """Ask the second model about a figure. Returns '' if it is not configured
    or not answering - the OCR result must never depend on it."""
    if not OPT.get("vlm"):
        return ""
    try:
        with open(png_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        body = json.dumps({
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                {"type": "text", "text": VLM_PROMPT}]}],
            "temperature": 0, "max_tokens": 512, "stream": False}).encode()
        with models.use("vision") as base:
            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
        return (d["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""


def vlm_up(timeout=2):
    return bool(OPT.get("vlm")) and "vision" in models.PROFILES


def llama_stream(img_b64, on_text):
    body = json.dumps({
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": img_b64}},
            {"type": "text", "text": PROMPT}]}],
        "temperature": 0, "max_tokens": MAX_TOKENS, "stream": True,
    }).encode()
    out = []
    with models.use("ocr") as base:
      req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                   headers={"Content-Type": "application/json"})
      with urllib.request.urlopen(req, timeout=1800) as r:
          for raw in r:
              line = raw.decode("utf-8", "replace").strip()
              if not line.startswith("data: "):
                  continue
              payload = line[6:]
              if payload == "[DONE]":
                  break
              try:
                  d = json.loads(payload)
              except Exception:
                  continue
              piece = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content")
              if piece:
                  out.append(piece)
                  on_text(piece)
    stats_add(tokens=len(out))
    return "".join(out)


# ------------------------------------------------------------------ prepare
def has_pdf():
    try:
        import pypdfium2  # noqa: F401
        return True
    except ImportError:
        return False


def _save_variants(pil_img, out_png, out_thumb):
    from PIL import Image
    w, h = pil_img.size
    if w != TARGET_PX:
        pil_img = pil_img.convert("RGB").resize(
            (TARGET_PX, round(h * TARGET_PX / w)), Image.LANCZOS)
    pil_img.convert("RGB").save(out_png)
    tw, th = pil_img.size
    pil_img.convert("RGB").resize((THUMB_PX, round(th * THUMB_PX / tw)),
                                  Image.LANCZOS).save(out_thumb, quality=78)


def detail_tiles(page):
    """Re-render the page large, then cut it into overlapping quarters."""
    from PIL import Image
    src, pidx = page.get("src"), page.get("pdf_index")
    if pidx is not None:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(src)
        try:
            pg = doc[pidx]
            w, _ = pg.get_size()
            img = pg.render(scale=DETAIL_PX / w).to_pil()
        finally:
            doc.close()
    else:
        img = Image.open(src).convert("RGB")
        if img.width < DETAIL_PX:
            img = img.resize((DETAIL_PX, round(img.height * DETAIL_PX / img.width)),
                             Image.LANCZOS)
    W, H = img.size
    ox, oy = int(W * DETAIL_OVERLAP), int(H * DETAIL_OVERLAP)
    boxes = [(0, 0, W // 2 + ox, H // 2 + oy), (W // 2 - ox, 0, W, H // 2 + oy),
             (0, H // 2 - oy, W // 2 + ox, H), (W // 2 - ox, H // 2 - oy, W, H)]
    return [img.crop(b) for b in boxes]


def expand_pdf(path, sdir, base_name, start_idx):
    """One entry per PDF page, rendered straight to TARGET_PX."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    pages = []
    try:
        n = len(doc)
        if n == 0:
            raise RuntimeError("PDF has no pages")
        # check before rendering: 300 pages cost ~53 s and ~330 MB of disk,
        # too much to spend on a document we are about to reject
        if start_idx + n > MAX_PAGES:
            raise RuntimeError(f"{n} pages, limit is {MAX_PAGES} "
                               f"(raise GLM_OCR_MAX_PAGES)")
        for i in range(n):
            page = doc[i]
            w, _ = page.get_size()
            img = page.render(scale=TARGET_PX / w).to_pil()
            idx = start_idx + i
            png = os.path.join(sdir, f"{idx}.png")
            thumb = os.path.join(sdir, f"{idx}.jpg")
            _save_variants(img, png, thumb)
            pages.append({"name": f"{base_name} · p{i + 1}", "path": png,
                          "thumb": thumb, "src": path, "pdf_index": i})
    finally:
        doc.close()
    return pages


SET_TTL = 6 * 3600          # prepared uploads older than this are dropped


def sweep_sets():
    """Delete stale prepared uploads so work/ does not grow without bound."""
    now = time.time()
    for sid, s in list(SETS.items()):
        if now - s["ts"] > SET_TTL:
            shutil.rmtree(s["dir"], ignore_errors=True)
            SETS.pop(sid, None)
    # directories left behind by an earlier run of the app
    for d in glob.glob(os.path.join(WORK, "*")):
        if os.path.isdir(d) and os.path.basename(d) not in SETS:
            try:
                if now - os.path.getmtime(d) > SET_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def prepare_files(files):
    """files: [{name, b64}] -> set dir with one rendered page per entry."""
    sweep_sets()
    sid = uuid.uuid4().hex[:12]
    sdir = os.path.join(WORK, sid)
    os.makedirs(sdir, exist_ok=True)
    try:
        pages = _render_all(files, sdir)
    except Exception:
        shutil.rmtree(sdir, ignore_errors=True)   # never leave a half set behind
        raise
    SETS[sid] = {"dir": sdir, "pages": pages, "ts": time.time()}
    stats_add(documents=len(files),
              pdf_pages=sum(1 for pg in pages if pg.get("pdf_index") is not None))
    return sid, pages


def _render_all(files, sdir):
    pages = []
    for f in files:
        name = os.path.basename(f.get("name") or "page")
        try:
            raw = base64.b64decode((f.get("b64") or "").split(",")[-1])
        except Exception:
            raw = b""
        if not raw:
            raise RuntimeError(f"unreadable: {name}")
        src = os.path.join(sdir, "src_" + name)
        with open(src, "wb") as fh:
            fh.write(raw)

        if name.lower().endswith(".pdf") or raw[:5] == b"%PDF-":
            if not has_pdf():
                raise RuntimeError("PDF support unavailable (pypdfium2 missing)")
            pages.extend(expand_pdf(src, sdir, name, len(pages)))
        else:
            from PIL import Image
            idx = len(pages)
            png = os.path.join(sdir, f"{idx}.png")
            thumb = os.path.join(sdir, f"{idx}.jpg")
            _save_variants(Image.open(src), png, thumb)
            pages.append({"name": name, "path": png, "thumb": thumb,
                          "src": src, "pdf_index": None})

        if len(pages) > MAX_PAGES:
            raise RuntimeError(f"too many pages (limit {MAX_PAGES})")
    return pages


def prepare_path(tmp_path, name):
    """Upload already streamed to disk. Nothing is base64 encoded and the file
    is never held in memory - a 70 MB PDF costs ~0 extra RAM here."""
    sweep_sets()
    sid = uuid.uuid4().hex[:12]
    sdir = os.path.join(WORK, sid)
    os.makedirs(sdir, exist_ok=True)
    try:
        src = os.path.join(sdir, "src_" + os.path.basename(name))
        shutil.move(tmp_path, src)
        with open(src, "rb") as fh:
            head = fh.read(5)
        pages = []
        if name.lower().endswith(".pdf") or head == b"%PDF-":
            if not has_pdf():
                raise RuntimeError("PDF support unavailable (pypdfium2 missing)")
            pages = expand_pdf(src, sdir, os.path.basename(name), 0)
        else:
            from PIL import Image
            png = os.path.join(sdir, "0.png")
            thumb = os.path.join(sdir, "0.jpg")
            _save_variants(Image.open(src), png, thumb)
            pages = [{"name": os.path.basename(name), "path": png,
                      "thumb": thumb, "src": src, "pdf_index": None}]
    except Exception:
        shutil.rmtree(sdir, ignore_errors=True)
        raise
    SETS[sid] = {"dir": sdir, "pages": pages, "ts": time.time()}
    stats_add(documents=1,
              pdf_pages=sum(1 for p in pages if p.get("pdf_index") is not None))
    return sid, pages


SETS = {}


# -------------------------------------------------------------------- queue
class Batch:
    def __init__(self, bid, items):
        self.id, self.items = bid, items      # items: [{name, path}]
        self.events, self.done = [], False
        self.cv = threading.Condition()

    def emit(self, kind, **d):
        with self.cv:
            self.events.append({"kind": kind, **d})
            self.cv.notify_all()

    def finish(self):
        with self.cv:
            self.done = True
            self.cv.notify_all()


BATCHES = {}
TASKS = queue.Queue()
PENDING = []
PLOCK = threading.Lock()


def broadcast_positions():
    with PLOCK:
        snap = list(PENDING)
    for pos, (bid, idx) in enumerate(snap):
        b = BATCHES.get(bid)
        if b:
            b.emit("position", index=idx, position=pos)


def ink_ratio(tile):
    """Fraction of non-white pixels. Cheap way to skip a blank quarter."""
    try:
        from PIL import Image
        g = tile.convert("L").resize((160, 160), Image.BILINEAR)
        dark = sum(1 for px in g.getdata() if px < 200)
        return dark / (160 * 160)
    except Exception:
        return 1.0        # cannot tell -> read it


VLM_PX = int(os.environ.get("GLM_OCR_VLM_PX", "900"))


def shrink_for_vlm(path, tag):
    """The figure model reads a downscaled page faster and just as well:
    measured 65 s at 900 px vs 81 s for a crop and 173 s at full size."""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        if im.width > VLM_PX:
            im = im.resize((VLM_PX, round(im.height * VLM_PX / im.width)), Image.LANCZOS)
        out = os.path.join(WORK, f"{tag}_fig.png")
        im.save(out)
        return out
    except Exception:
        return path


def detail_pass(b, idx, page):
    """Second look at a sparse page. Only quarters that actually carry ink are
    sent: on the page this was built for, three of four were near-blank and
    each wasted a full inference."""
    import io as _io
    try:
        tiles = detail_tiles(page)
    except Exception:
        return ""
    # Relative, not a fixed threshold: on the page this was built for the tile
    # holding the table had 3.3% ink while the three carrying only the header
    # or footer had 1.2%. If no tile stands out that clearly, read them all
    # rather than risk dropping content.
    ink = [ink_ratio(t) for t in tiles]
    lo, hi = min(ink), max(ink)
    if hi >= lo * DETAIL_INK_GAP:
        live = [(n, t) for n, t in enumerate(tiles) if ink[n] >= lo * DETAIL_INK_GAP]
    else:
        live = list(enumerate(tiles))
    if not live:
        live = [(int(ink.index(hi)), tiles[ink.index(hi)])]
    b.emit("detail", index=idx, tiles=len(live), skipped=len(tiles) - len(live))
    found = []
    for pos, (n, tile) in enumerate(live):
        buf = _io.BytesIO()
        tile.save(buf, format="PNG")
        data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        try:
            t = llama_stream(data, lambda x: None).strip()
        except Exception:
            continue
        # a tile holding only a heading just repeats what the full page gave
        if len(t) >= DETAIL_MIN_CHARS:
            found.append(t)
        b.emit("detail_tile", index=idx, tile=pos + 1, total=len(live), chars=len(t))
    return "\n\n".join(found)


def worker():
    """Single worker: one page at a time. Two concurrent pages OOM the GPU."""
    while True:
        bid, idx = TASKS.get()
        b = BATCHES.get(bid)
        if not b:
            TASKS.task_done()
            continue
        with PLOCK:
            if (bid, idx) in PENDING:
                PENDING.remove((bid, idx))
        broadcast_positions()

        b.emit("start", index=idx)
        t0 = time.time()
        try:
            with open(b.items[idx]["path"], "rb") as fh:
                img = "data:image/png;base64," + base64.b64encode(fh.read()).decode()
            text = llama_stream(img, lambda t: b.emit("out", index=idx, text=t)).strip()
            detail = tried_detail = False
            if len(text) < DETAIL_MIN_CHARS:
                tried_detail = True          # counted even when it finds nothing:
                extra = detail_pass(b, idx, b.items[idx])   # the time was spent
                if extra:
                    text = (text + "\n\n" + extra).strip()
                    detail = True
            described = False
            if tried_detail and OPT.get("vlm"):
                # a sparse page is a figure page: ask the vision model what it
                # is looking at, on the densest tile rather than the whole page
                b.emit("describing", index=idx)
                shot = shrink_for_vlm(b.items[idx]["path"], f"{b.id}_{idx}")
                try:
                    note = vlm_describe(shot)
                finally:
                    if shot and os.path.exists(shot):
                        os.unlink(shot)
                if note:
                    text = (text + "\n\n--- figure ---\n" + note).strip()
                    described = True

            if not text:
                raise RuntimeError("empty result")
            wall = round(time.time() - t0, 1)
            stats_add(pages_ok=1, chars=len(text), seconds=float(wall),
                      **({"second_pass": 1} if tried_detail else {}),
                      **({"described": 1} if described else {}))
            b.emit("page_done", index=idx, ok=True, wall=wall,
                   text=text, detail=detail, described=described)
        except urllib.error.URLError:
            stats_add(pages_failed=1)
            b.emit("page_done", index=idx, ok=False, wall=round(time.time() - t0, 1),
                   text="", error="llama-server unreachable")
        except Exception as ex:
            stats_add(pages_failed=1)
            b.emit("page_done", index=idx, ok=False, wall=round(time.time() - t0, 1),
                   text="", error=str(ex)[:200])

        if all(any(e["kind"] == "page_done" and e["index"] == i for e in b.events)
               for i in range(len(b.items))):
            b.emit("batch_done")
            b.finish()
        TASKS.task_done()


threading.Thread(target=worker, daemon=True).start()



def run_sync(files, indices=None, timeout=3600):
    """Blocking OCR used by the agent/MCP endpoint: submit, wait, collect."""
    sid, pages = prepare_files(files)
    if indices is None:
        idxs = list(range(len(pages)))
    else:
        idxs = [i for i in indices if 0 <= i < len(pages)]
    if not idxs:
        raise RuntimeError("no pages selected")

    items = [pages[i] for i in idxs]
    bid = uuid.uuid4().hex[:12]
    b = Batch(bid, items)
    BATCHES[bid] = b
    with PLOCK:
        PENDING.extend((bid, i) for i in range(len(items)))
    for i in range(len(items)):
        TASKS.put((bid, i))
    broadcast_positions()

    deadline = time.time() + timeout
    with b.cv:
        while not b.done and time.time() < deadline:
            b.cv.wait(timeout=1.0)
    if not b.done:
        raise RuntimeError("timed out")

    out = {}
    for e in b.events:
        if e["kind"] == "page_done":
            out[e["index"]] = e
    result = []
    for n, i in enumerate(sorted(out)):
        e = out[i]
        result.append({"page": idxs[i] + 1, "name": items[i]["name"],
                       "ok": e["ok"], "text": e.get("text", ""),
                       "seconds": e.get("wall"), "second_pass": bool(e.get("detail")),
                       "error": e.get("error", "")})
    BATCHES.pop(bid, None)
    return sid, len(pages), result


# --------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _base(self):
        """Address to hand out in the prompt and in the downloaded client.

        --prompt-ip wins. Otherwise the Host header is right for anyone who
        opened the page over the network — but when the page was opened on the
        machine itself the header says localhost, which is useless to every
        other caller, so fall back to this host's LAN address."""
        if OPT.get("prompt_base"):
            return OPT["prompt_base"]
        host = self.headers.get("Host") or ""
        name = host.split(":")[0]
        if name in ("localhost", "127.0.0.1", "::1", "") and OPT.get("lan_base"):
            return OPT["lan_base"]
        return "http://" + (host or "127.0.0.1:3333")

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        buf = bytearray()
        while len(buf) < n:                       # large PDFs arrive in chunks
            chunk = self.rfile.read(min(1 << 20, n - len(buf)))
            if not chunk:
                break
            buf.extend(chunk)
        return json.loads(bytes(buf) or b"{}")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            full = os.path.join(STATIC, os.path.basename(p))
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    return self._send(200, fh.read(),
                                      MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
            return self._send(404, {"error": "not found"})
        if p.startswith("/api/sets/") and "/page/" in p:
            parts = p.split("/")
            s = SETS.get(parts[3])
            try:
                idx = int(parts[5])
            except (IndexError, ValueError):
                return self._send(404, {"error": "bad index"})
            if not s or idx >= len(s["pages"]):
                return self._send(404, {"error": "not found"})
            with open(s["pages"][idx]["path"], "rb") as fh:
                return self._send(200, fh.read(), "image/png")
        if p.startswith("/api/sets/") and "/thumb/" in p:
            parts = p.split("/")
            s = SETS.get(parts[3])
            try:
                idx = int(parts[5])
            except (IndexError, ValueError):
                return self._send(404, {"error": "bad index"})
            if not s or idx >= len(s["pages"]):
                return self._send(404, {"error": "not found"})
            with open(s["pages"][idx]["thumb"], "rb") as fh:
                return self._send(200, fh.read(), "image/jpeg")
        if p in ("/minitoolkit.py", "/api/minitoolkit.py",
                 "/workbench.py", "/api/workbench.py"):   # the old name, still served
            f = os.path.join(BASE, "minitoolkit.py")
            if not os.path.isfile(f):
                return self._send(404, {"error": "client missing"})
            with open(f, encoding="utf-8") as fh:
                src = fh.read()
            # bake in the address it was fetched from, so the downloaded copy
            # needs no --server flag
            src = src.replace('DEFAULT_SERVER = "http://127.0.0.1:3333"',
                              'DEFAULT_SERVER = "%s"' % self._base())
            return self._send(200, src, "text/x-python; charset=utf-8")
        if p == "/api/prompt":
            f = os.path.join(BASE, "AGENT-PROMPT.md")
            if not os.path.isfile(f):
                return self._send(404, {"error": "prompt file missing"})
            with open(f, encoding="utf-8") as fh:
                txt = fh.read().replace("{BASE_URL}", self._base())
            return self._send(200, txt, "text/plain; charset=utf-8")
        if p == "/api/stats":
            return self._send(200, stats_view())
        if p == "/api/status":
            with PLOCK:
                q = len(PENDING)
            return self._send(200, {"ready": llama_up(), "queue": q,
                                    "pdf": has_pdf(), "max_pages": MAX_PAGES,
                                    "vlm": vlm_up()})
        if p.startswith("/api/batches/") and p.endswith("/events"):
            return self.stream(p.split("/")[3])
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/cancel":
            """Drop everything still waiting. The page being processed right
            now finishes - llama-server has no mid-request abort."""
            dropped = 0
            with PLOCK:
                del PENDING[:]
            while True:
                try:
                    TASKS.get_nowait()
                    TASKS.task_done()
                    dropped += 1
                except queue.Empty:
                    break
            for b in BATCHES.values():
                if not b.done:
                    b.emit("cancelled")
                    b.finish()
            return self._send(200, {"dropped": dropped})

        if self.path == "/api/v1/ocr":
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "malformed request"})
            if not llama_up():
                return self._send(503, {"error": "llama-server is not running"})

            files = []
            path = body.get("path")
            if path:
                if not os.path.isfile(path):
                    return self._send(400, {"error": f"file not found: {path}"})
                with open(path, "rb") as fh:
                    files = [{"name": os.path.basename(path),
                              "b64": base64.b64encode(fh.read()).decode()}]
            elif body.get("file_b64"):
                files = [{"name": body.get("name") or "upload.png",
                          "b64": body["file_b64"]}]
            else:
                return self._send(400, {"error": "provide 'path' or 'file_b64'"})

            want = body.get("pages")                       # 1-based, optional
            idxs = [p - 1 for p in want] if want else None
            t0 = time.time()
            try:
                _sid, total, res = run_sync(files, idxs)
            except Exception as ex:
                return self._send(400, {"error": str(ex)[:200]})
            return self._send(200, {
                "page_count": total,
                "seconds": round(time.time() - t0, 1),
                "text": "\n\n".join(
                    f"===== {r['name']} =====\n{r['text']}" for r in res if r["ok"]),
                "pages": res,
            })

        if self.path == "/api/prepare-raw":
            import urllib.parse as _up
            name = _up.unquote(self.headers.get("X-Filename") or "upload.pdf")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._send(400, {"error": "empty upload"})
            tmp = os.path.join(WORK, "up_" + uuid.uuid4().hex[:12])
            got = 0
            try:
                with open(tmp, "wb") as fh:
                    while got < n:          # 1 MB at a time, straight to disk
                        chunk = self.rfile.read(min(1 << 20, n - got))
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                sid, pages = prepare_path(tmp, name)
            except Exception as ex:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                return self._send(400, {"error": str(ex)[:200]})
            return self._send(200, {"set_id": sid, "pages": [
                {"name": pg["name"], "thumb": f"/api/sets/{sid}/thumb/{i}",
                 "full": f"/api/sets/{sid}/page/{i}"}
                for i, pg in enumerate(pages)]})

        if self.path == "/api/prepare":
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "malformed request"})
            files = body.get("files") or []
            if not files:
                return self._send(400, {"error": "no files"})
            try:
                sid, pages = prepare_files(files)
            except Exception as ex:
                return self._send(400, {"error": str(ex)[:200]})
            return self._send(200, {"set_id": sid, "pages": [
                {"name": pg["name"], "thumb": f"/api/sets/{sid}/thumb/{i}",
                 "full": f"/api/sets/{sid}/page/{i}"}
                for i, pg in enumerate(pages)]})

        if self.path == "/api/batch":
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "malformed request"})
            if not llama_up():
                return self._send(503, {"error": "llama-server is not running"})
            s = SETS.get(body.get("set_id"))
            if not s:
                return self._send(400, {"error": "unknown or expired upload"})
            idxs = body.get("indices")
            if idxs is None:
                idxs = list(range(len(s["pages"])))
            items = [s["pages"][i] for i in idxs if 0 <= i < len(s["pages"])]
            if not items:
                return self._send(400, {"error": "no pages selected"})

            bid = uuid.uuid4().hex[:12]
            BATCHES[bid] = Batch(bid, items)
            with PLOCK:
                PENDING.extend((bid, i) for i in range(len(items)))
            for i in range(len(items)):
                TASKS.put((bid, i))
            broadcast_positions()
            return self._send(200, {"batch_id": bid, "count": len(items)})

        return self._send(404, {"error": "not found"})

    def stream(self, bid):
        b = BATCHES.get(bid)
        if not b:
            return self._send(404, {"error": "unknown batch"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        i = 0
        try:
            while True:
                with b.cv:
                    while i >= len(b.events) and not b.done:
                        b.cv.wait(timeout=1.0)
                    chunk, i, fin = b.events[i:], len(b.events), b.done
                for ev in chunk:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                if fin and not chunk:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description="GLM-OCR web interface")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--llama", default="http://127.0.0.1:8080")
    ap.add_argument("--vlm", default=None,
                    help="second model that explains figures, e.g. http://127.0.0.1:8096")
    ap.add_argument("--prompt-ip", default=None,
                    help="address the LLM prompt should advertise, e.g. 192.0.2.10 "
                         "(use when serving on 0.0.0.0)")
    a = ap.parse_args()
    OPT["llama"] = a.llama.rstrip("/")
    OPT["vlm"] = a.vlm.rstrip("/") if a.vlm else None

    # Two copies can run from this folder on different ports, one per GPU.
    # Give each its own counters and scratch space so they do not clobber
    # each other.
    global STATS_FILE, WORK
    if a.port != 3333:
        STATS_FILE = os.path.join(BASE, f"stats-{a.port}.json")
        WORK = os.path.join(BASE, f"work-{a.port}")
        os.makedirs(WORK, exist_ok=True)
    stats_load()
    try:
        _s = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        OPT["lan_base"] = f"http://{_s.getsockname()[0]}:{a.port}"
        _s.close()
    except Exception:
        OPT["lan_base"] = None

    if a.prompt_ip:
        ip = a.prompt_ip if "://" in a.prompt_ip else "http://" + a.prompt_ip
        OPT["prompt_base"] = ip if ":" in ip.split("//", 1)[1] else f"{ip}:{a.port}"

    try:
        from PIL import Image  # noqa: F401
        pil = "yes"
    except ImportError:
        pil = "MISSING - run install_deps.py"

    print("=" * 60)
    print("GLM-OCR")
    print("  llama-server :", OPT["llama"], "-", "ready" if llama_up(5) else "OFFLINE")
    print("  Pillow       :", pil)
    print("  PDF          :", "pypdfium2 (all pages)" if has_pdf() else "not available")
    print("  figure model :", (OPT["vlm"] + (" - ready" if vlm_up(5) else " - OFFLINE"))
          if OPT["vlm"] else "not configured (--vlm URL)")
    print("  queue        : one page at a time (max %d pages)" % MAX_PAGES)
    print("  LLM prompt   :", OPT.get("prompt_base") or OPT.get("lan_base") or "-",
          "(advertised)" if OPT.get("prompt_base") else "(auto-detected; set --prompt-ip to override)")
    if a.host == "0.0.0.0":
        ip = (OPT.get("lan_base") or "").replace("http://", "") or "<this-machine>"
        print(f"\n  -> http://{ip}   (reachable from the network)")
    print(f"  -> http://127.0.0.1:{a.port}\n" + "=" * 60)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
