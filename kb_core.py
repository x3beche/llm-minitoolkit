#!/usr/bin/env python3
"""Knowledge service: people add notes, a small model answers questions from them.

    python3 app.py --host 0.0.0.0 --port 3400 --llm http://127.0.0.1:8100
"""
import os, sys, json, time, argparse, threading, urllib.request, urllib.error
import models
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import store

STATIC = os.path.join(BASE, "static")
OPT = {"llm": "http://127.0.0.1:8100"}

CANDIDATES = 12          # how many notes BM25 hands to the model
MAX_OPEN = 4             # how many it may read in full
BODY_CHARS = 6000        # per note, so one huge note cannot crowd out the rest
ANSWER_TOKENS = 700      # kept free for the reply when the context is packed

MIME = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


# ------------------------------------------------------------------ model
def llm_up(timeout=2):
    """Loaded on demand; 'up' means the profile exists and can be started."""
    return "kb" in models.PROFILES


def count_tokens(text):
    """Exact token count from the model's own tokenizer. None if it is not up."""
    if not text:
        return 0
    try:
        with models.use("kb") as base:
            req = urllib.request.Request(base + "/tokenize",
                                         data=json.dumps({"content": text}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return len(json.loads(r.read()).get("tokens", []))
    except Exception:
        return None


def chat(messages, max_tokens=700, temperature=0.2, on_text=None, timeout=900,
         on_stats=None):
    """on_stats gets what llama-server actually reported - token counts and
    speeds - rather than anything guessed from character counts."""
    payload = {"messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "stream": bool(on_text)}
    if on_text:
        payload["stream_options"] = {"include_usage": True}
    body = json.dumps(payload).encode()
    # use() keeps the model loaded for the whole call: an OCR job started while
    # this reply was streaming used to kill the server and return an empty body
    with models.use("kb") as base:
        req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        t_start = time.time()
        if not on_text:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if on_stats:
                on_stats(_stats(d, t_start, None))
            return (d["choices"][0]["message"]["content"] or "").strip()
        out = []
        first = None
        last = {}
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                p = line[6:]
                if p == "[DONE]":
                    break
                try:
                    d = json.loads(p)
                except Exception:
                    continue
                if d.get("timings") or d.get("usage"):
                    last = d
                piece = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if piece:
                    if first is None:
                        first = time.time()
                    out.append(piece)
                    on_text(piece)
        if on_stats:
            on_stats(_stats(last, t_start, first))
        return "".join(out).strip()


def _stats(d, t_start, t_first):
    """llama-server's own numbers, flattened. Everything here was measured."""
    u = (d or {}).get("usage") or {}
    t = (d or {}).get("timings") or {}
    s = {
        "prompt_tokens": u.get("prompt_tokens", t.get("prompt_n")),
        "output_tokens": u.get("completion_tokens", t.get("predicted_n")),
        "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens",
                                                                    t.get("cache_n")),
        "prompt_per_second": t.get("prompt_per_second"),
        "output_per_second": t.get("predicted_per_second"),
        "wall_seconds": round(time.time() - t_start, 2),
    }
    if t_first:
        s["first_token_seconds"] = round(t_first - t_start, 2)
    return {k: (round(v, 1) if isinstance(v, float) else v)
            for k, v in s.items() if v is not None}


# --------------------------------------------------------------- two stages
PICK_SYS = ("You choose which notes to open. You are given a question and a numbered "
            "list of note titles with a short preview. Reply with nothing but the "
            "numbers of the notes worth reading in full, most useful first, comma "
            "separated, at most %d of them. If none fit, reply NONE." % MAX_OPEN)

ANSWER_SYS = ("Answer the question using only the notes given to you. Quote concrete "
              "details - pin names, values, commands - exactly as written. Cite each "
              "fact as [#id] using the note ids shown. If the notes do not cover the "
              "question, say so plainly instead of guessing. Be brief.")


def pick_notes(question, cands, on_stats=None):
    if not cands:
        return []
    listing = "\n".join(
        f"{i+1}. [#{c['id']}] {c['title']}"
        + (f"  (tags: {c['tags']})" if c["tags"] else "")
        + f"\n   {c['preview'][:200]}"
        for i, c in enumerate(cands))
    try:
        reply = chat([{"role": "system", "content": PICK_SYS},
                      {"role": "user", "content": f"Question: {question}\n\nNotes:\n{listing}"}],
                     max_tokens=60, temperature=0, on_stats=on_stats)
    except Exception:
        reply = ""
    nums = []
    for tok in reply.replace("#", " ").replace(".", " ").split(","):
        tok = tok.strip().split()[0] if tok.strip().split() else ""
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= len(cands) and n not in nums:
                nums.append(n)
    if not nums:                      # model unhelpful -> trust BM25 order
        nums = list(range(1, min(MAX_OPEN, len(cands)) + 1))
    return [cands[n - 1] for n in nums[:MAX_OPEN]]


def build_context(picked, reserve=0):
    """Fill the window and stop.

    BODY_CHARS alone was enough while notes were short. Once they were rebuilt
    from the published pages they run past 20k characters each, four of them
    overflowed the 8k window, and llama-server answered 400 to every question.
    So the budget is counted in tokens, by the model's own tokenizer, with room
    left for the answer.
    """
    ctx = models.PROFILES.get("kb", {}).get("ctx") or 8192
    budget = ctx - reserve - 400          # 400: system prompt, question, template
    parts, used = [], 0
    for c in picked:
        n = store.get(c["id"])
        if not n:
            continue
        head = "[#%s] %s\n" % (n["id"], n["title"])
        body = n["body"][:BODY_CHARS]
        piece = head + body
        cost = count_tokens(piece)
        if cost is None:                  # tokenizer unavailable: fall back
            cost = len(piece) // 3        # deliberately pessimistic
        if used + cost > budget:
            room = budget - used
            if room < 200:                # not worth a scrap of a note
                break
            keep = max(0, int(len(piece) * room / cost) - 200)
            piece = head + body[:keep] + "\n[… truncated to fit the context]"
            parts.append(piece)
            break
        parts.append(piece)
        used += cost
    return "\n\n---\n\n".join(parts)


def answer(question, on_text=None, on_stage=None):
    t0 = time.time()
    cands = store.search(question, limit=CANDIDATES)
    search_s = round(time.time() - t0, 3)
    if on_stage:
        on_stage("search", found=len(cands))
    if not cands:
        store.log_query("ask", question, seconds=search_s, answer_text="nothing in the")
        return {"answer": "Nothing in the knowledge base matches that question.",
                "sources": [], "candidates": 0,
                "metrics": {"search_seconds": search_s, "candidates": 0}}
    pick_stats = {}
    picked = pick_notes(question, cands, on_stats=pick_stats.update)
    if on_stage:
        on_stage("picked", notes=[{"id": p["id"], "title": p["title"]} for p in picked])
    # the interface shows these as cards, the same way it shows search hits
    srcs = [{"id": p["id"], "title": p["title"], "tags": p.get("tags", ""),
             "score": p.get("score"), "preview": (p.get("preview") or "")[:320]}
            for p in picked]
    ctx = build_context(picked, reserve=ANSWER_TOKENS)
    ans_stats = {}
    text = chat([{"role": "system", "content": ANSWER_SYS},
                 {"role": "user", "content": f"Notes:\n\n{ctx}\n\nQuestion: {question}"}],
                max_tokens=ANSWER_TOKENS, on_text=on_text, on_stats=ans_stats.update)
    m = {
        "search_seconds": search_s,
        "candidates": len(cands),
        "opened": len(picked),
        "context_chars": len(ctx),
        "ctx_limit": models.PROFILES.get("kb", {}).get("ctx"),
        "pick": pick_stats or None,
        "answer": ans_stats or None,
    }
    store.log_query("ask", question, candidates=len(cands), opened=len(picked),
                    note_ids=[p["id"] for p in picked],
                    top_score=cands[0].get("score") if cands else None,
                    seconds=round(time.time() - t0, 2),
                    tokens_in=ans_stats.get("prompt_tokens"),
                    tokens_out=ans_stats.get("output_tokens"),
                    answer_text=text)
    return {"answer": text,
            "sources": srcs,
            "candidates": len(cands),
            "metrics": {k: v for k, v in m.items() if v is not None}}


# --------------------------------------------------------------------- http
JOBS = {}


class Job:
    def __init__(self):
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


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        buf = bytearray()
        while len(buf) < n:
            c = self.rfile.read(min(1 << 20, n - len(buf)))
            if not c:
                break
            buf.extend(c)
        return json.loads(bytes(buf) or b"{}")

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
        if p == "/api/status":
            c = store.count()
            return self._send(200, {"ready": llm_up(), "notes": c["n"], "chars": c["b"]})
        if p == "/api/notes":
            return self._send(200, {"notes": store.all_notes()})
        if p.startswith("/api/notes/"):
            n = store.get(int(p.rsplit("/", 1)[1]))
            return self._send(200, n) if n else self._send(404, {"error": "not found"})
        if p.startswith("/api/ask/") and p.endswith("/events"):
            return self.stream(p.split("/")[3])
        if p == "/api/prompt":
            f = os.path.join(BASE, "AGENT-PROMPT.md")
            if not os.path.isfile(f):
                return self._send(404, {"error": "missing"})
            base = OPT.get("prompt_base") or "http://" + (self.headers.get("Host") or "")
            with open(f, encoding="utf-8") as fh:
                return self._send(200, fh.read().replace("{BASE_URL}", base),
                                  "text/plain; charset=utf-8")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            b = self._json()
        except Exception:
            return self._send(400, {"error": "bad json"})

        if p == "/api/notes":
            if not (b.get("title") or "").strip() or not (b.get("body") or "").strip():
                return self._send(400, {"error": "title and body are required"})
            nid = store.add(b["title"], b["body"], b.get("tags", ""), b.get("author", ""))
            return self._send(200, {"id": nid})

        if p.startswith("/api/notes/"):
            nid = int(p.rsplit("/", 1)[1])
            if b.get("_delete"):
                store.delete(nid)
                return self._send(200, {"deleted": nid})
            store.update(nid, b.get("title", ""), b.get("body", ""),
                         b.get("tags", ""), b.get("author", ""))
            return self._send(200, {"id": nid})

        if p == "/api/v1/ask":                       # for other models / agents
            q = (b.get("question") or "").strip()
            if not q:
                return self._send(400, {"error": "question is required"})
            if not llm_up():
                return self._send(503, {"error": "the answering model is not running"})
            t0 = time.time()
            try:
                r = answer(q)
            except Exception as ex:
                return self._send(500, {"error": str(ex)[:200]})
            r["seconds"] = round(time.time() - t0, 1)
            return self._send(200, r)

        if p == "/api/ask":                          # for the web UI, streamed
            q = (b.get("question") or "").strip()
            if not q:
                return self._send(400, {"error": "question is required"})
            if not llm_up():
                return self._send(503, {"error": "the answering model is not running"})
            jid = os.urandom(6).hex()
            job = Job()
            JOBS[jid] = job

            def run():
                t0 = time.time()
                try:
                    r = answer(q,
                               on_text=lambda t: job.emit("out", text=t),
                               on_stage=lambda k, **kw: job.emit(k, **kw))
                    job.emit("done", seconds=round(time.time() - t0, 1),
                             sources=r["sources"], candidates=r["candidates"],
                             metrics=r.get("metrics") or {})
                except Exception as ex:
                    job.emit("done", error=str(ex)[:200], sources=[], candidates=0)
                finally:
                    job.finish()
            threading.Thread(target=run, daemon=True).start()
            return self._send(200, {"job": jid})

        return self._send(404, {"error": "not found"})

    def stream(self, jid):
        job = JOBS.get(jid)
        if not job:
            return self._send(404, {"error": "unknown"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        i = 0
        try:
            while True:
                with job.cv:
                    while i >= len(job.events) and not job.done:
                        job.cv.wait(timeout=1.0)
                    chunk, i, fin = job.events[i:], len(job.events), job.done
                for ev in chunk:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                if fin and not chunk:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description="Knowledge base with a small local model")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3400)
    ap.add_argument("--llm", default="http://127.0.0.1:8100")
    ap.add_argument("--prompt-ip", default=None)
    a = ap.parse_args()
    OPT["llm"] = a.llm.rstrip("/")
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
    print("=" * 60)
    print("Knowledge base")
    print("  model :", OPT["llm"], "-", "ready" if llm_up(5) else "OFFLINE")
    print(f"  notes : {c['n']} ({c['b']} characters)")
    print(f"  search: BM25 over titles and tags, then the model opens up to {MAX_OPEN}")
    print(f"\n  -> {OPT.get('prompt_base') or f'http://127.0.0.1:{a.port}'}\n" + "=" * 60)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
