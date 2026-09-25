#!/usr/bin/env bash
# LLM Mini Toolkit: OCR + knowledge base, one GPU, models swapped per request.
set -e
cd "$(dirname "$0")"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --host|--port|--prompt-ip|--llama|--preload) ARGS+=("$1" "$2"); shift 2;;
    -h|--help) python3 app.py --help; exit 0;;
    *) ARGS+=("$1"); shift;;
  esac
done
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
[ -f install_deps.py ] && python3 install_deps.py 2>/dev/null || true
export PYTHONPATH="$PWD/lib:${PYTHONPATH:-}"
mkdir -p logs work data models
exec python3 -u app.py "${ARGS[@]}"
