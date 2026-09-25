#!/usr/bin/env python3
"""Install the bundled wheels into ./lib - works fully offline.

Tries pip with --no-index first; if pip is unavailable, falls back to
unzipping the wheel by hand (a wheel is just a zip archive).
"""
import os, sys, glob, zipfile, subprocess, importlib

BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "vendor")
LIB = os.path.join(BASE, "lib")
TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"


def have(mod):
    # lib/ may have been created after an earlier failed probe, so drop the
    # negative entry the import system cached for it.
    importlib.invalidate_caches()
    sys.path.insert(0, LIB)
    try:
        __import__(mod)
        return True
    except ImportError:
        return False
    finally:
        sys.path.pop(0)


def pick(pattern_words):
    """Newest wheel matching this interpreter (abi3/py3 wheels match anything)."""
    best = []
    for w in sorted(glob.glob(os.path.join(VENDOR, "*.whl"))):
        name = os.path.basename(w).lower()
        if not all(word in name for word in pattern_words):
            continue
        if TAG in name or "-abi3-" in name or "-py3-none-" in name or "-none-any" in name:
            best.append(w)
    return best[-1] if best else None


def install(wheel):
    os.makedirs(LIB, exist_ok=True)
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index",
                        "--no-deps", "--target", LIB, wheel],
                       check=True, capture_output=True, timeout=300)
        return "pip"
    except Exception:
        with zipfile.ZipFile(wheel) as z:          # wheel == zip
            z.extractall(LIB)
        return "unzip"


def main():
    rc = 0
    for mod, words, needed in (("PIL", ["pillow"], True),
                               ("pypdfium2", ["pypdfium2"], False)):
        if have(mod):
            print(f"  {mod:<10} already available")
            continue
        wheel = pick(words)
        if not wheel:
            msg = "REQUIRED - resizing disabled" if needed else "optional - PDF support disabled"
            print(f"  {mod:<10} no wheel for {TAG} in vendor/  ({msg})")
            if needed:
                rc = 1
            continue
        how = install(wheel)
        ok = have(mod)
        print(f"  {mod:<10} installed via {how}: {os.path.basename(wheel)}"
              + ("" if ok else "  -> STILL NOT IMPORTABLE"))
        if needed and not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    print(f"offline dependencies (python {sys.version_info.major}.{sys.version_info.minor}, tag {TAG})")
    sys.exit(main())
