#!/usr/bin/env python3
"""One GPU, several models: load the one the current job needs and stop the rest.

GLM-OCR takes 1272 MiB and Qwen3-4B takes 1774 MiB; together they exceed the
card, so they take turns. Switching costs 1-2 seconds because the weights are
already in the page cache and llama.cpp maps them - far less than the 25 s a
page of OCR takes or the minute a question takes, so it is not worth trying to
keep both resident.
"""
import os, time, json, shutil, signal, subprocess, threading, urllib.request
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("WB_LLAMA_PORT", "8111"))
URL = f"http://127.0.0.1:{PORT}"

_lock = threading.RLock()
_idle = threading.Condition(_lock)
_inflight = 0                  # requests currently talking to the loaded model
_holders = {}                  # thread id -> how many use() blocks it has open
SWITCH_WAIT = float(os.environ.get("WB_SWITCH_WAIT", "300"))
_state = {"name": None, "proc": None, "since": 0.0, "switches": 0, "seconds": 0.0}


@contextmanager
def use(name):
    """Load `name` if needed and hold it for the duration of the call.

    Without this a request that wants the other model kills the server out from
    under a reply that is still streaming: an OCR page started while a question
    was being answered took the question down with it and it returned nothing.
    """
    global _inflight
    tid = threading.get_ident()
    url = ensure(name)
    with _lock:
        _inflight += 1
        _holders[tid] = _holders.get(tid, 0) + 1
    try:
        yield url
    finally:
        with _lock:
            _inflight -= 1
            if _holders.get(tid, 0) <= 1:
                _holders.pop(tid, None)
            else:
                _holders[tid] -= 1
            _idle.notify_all()

PROFILES = {}          # filled by configure()


def configure(profiles, server=None, lib=None):
    PROFILES.clear()
    PROFILES.update(profiles)
    _state["server"] = server or find_server()
    _state["lib"] = lib or (os.path.dirname(_state["server"]) if _state["server"] else "")


def find_server():
    if os.environ.get("LLAMA_SERVER") and os.access(os.environ["LLAMA_SERVER"], os.X_OK):
        return os.environ["LLAMA_SERVER"]
    w = shutil.which("llama-server")
    if w:
        return w
    import glob
    for pat in ("~/llama.cpp*/llama-server", "~/llama.cpp*/build/bin/llama-server",
                "/opt/llama.cpp*/llama-server", "/usr/local/bin/llama-server"):
        for c in glob.glob(os.path.expanduser(pat)):
            if os.access(c, os.X_OK):
                return c
    return None


def healthy(timeout=1.5):
    try:
        with urllib.request.urlopen(URL + "/health", timeout=timeout) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def current():
    with _lock:
        return _state["name"] if _state["proc"] and _state["proc"].poll() is None else None


def status():
    with _lock:
        p = PROFILES.get(_state["name"], {})
        return {"loaded": current(), "label": p.get("label"),
                "variant": _state.get("variant"),
                "vram_mib": p.get("vram_mib"),
                "loaded_for": round(time.time() - _state["since"], 1) if current() else 0,
                "switches": _state["switches"], "inflight": _inflight,
                "switch_seconds": round(_state["seconds"], 1),
                "available": [{"name": k, "label": v.get("label"), "vram_mib": v.get("vram_mib")}
                              for k, v in PROFILES.items()]}


def stop():
    with _lock:
        p = _state["proc"]
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                p.terminate()
            try:
                p.wait(timeout=20)
            except Exception:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    p.kill()
        _state["proc"], _state["name"] = None, None


def ensure(name, on_switch=None):
    """Make `name` the running model. Blocks; serialised so two requests that
    want different models cannot fight over the card."""
    if name not in PROFILES:
        raise KeyError(f"unknown model profile: {name}")
    with _lock:
        if current() == name and healthy():
            return URL
        # someone is mid-reply on the model that is loaded: wait it out rather
        # than pulling the process from under them. A thread that is itself
        # holding one must not wait for itself - it would stall until the
        # timeout and then switch anyway.
        mine = _holders.get(threading.get_ident(), 0)
        deadline = time.time() + SWITCH_WAIT
        while _inflight - mine > 0 and time.time() < deadline:
            _idle.wait(timeout=1.0)
            if current() == name and healthy():
                return URL
        t0 = time.time()
        if on_switch:
            on_switch(_state["name"], name)
        stop()

        p = PROFILES[name]
        # A profile may list fallbacks. Full GPU offload is worth trying but it
        # is not dependable on a small card: the desktop grows a few hundred MiB
        # when browser tabs open and the same command that worked a minute ago
        # runs out of memory. Rather than fail the request, step down.
        attempts = [p] + [dict(p, **f) for f in p.get("fallbacks", [])]
        last = None
        for attempt in attempts:
            try:
                _start(name, attempt)
                _state["switches"] += 1
                _state["seconds"] += time.time() - t0
                _state["variant"] = attempt.get("note", "primary")
                return URL
            except RuntimeError as ex:
                last = ex
                stop()
        raise last or RuntimeError(f"{name} would not start")


def _start(name, p):
    with _lock:
        cmd = [_state["server"], "-m", p["model"]]
        if p.get("mmproj"):
            cmd += ["--mmproj", p["mmproj"]]
        cmd += ["--device", p.get("device", "CUDA0"), "-ngl", str(p.get("ngl", 4)),
                "-c", str(p.get("ctx", 8192)), "-t", str(p.get("threads", 8)),
                "--host", "127.0.0.1", "--port", str(PORT)]
        cmd += p.get("extra", [])

        env = dict(os.environ)
        if _state.get("lib"):
            env["LD_LIBRARY_PATH"] = _state["lib"] + ":" + env.get("LD_LIBRARY_PATH", "")
        log = open(os.path.join(BASE, "logs", f"llama-{name}.log"), "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        _state["proc"], _state["name"], _state["since"] = proc, name, time.time()

        for _ in range(480):                     # up to 4 minutes for a cold file
            if healthy():
                return
            if proc.poll() is not None:
                tail = ""
                try:
                    with open(os.path.join(BASE, "logs", f"llama-{name}.log")) as fh:
                        tail = "".join(fh.readlines()[-12:])
                except Exception:
                    pass
                _state["proc"], _state["name"] = None, None
                raise RuntimeError(f"{name} failed to start:\n{tail}")
            time.sleep(0.5)
        stop()
        raise RuntimeError(f"{name} did not become ready")
