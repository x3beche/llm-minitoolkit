#!/usr/bin/env bash
cd "$(dirname "$0")"
for p in $(ss -lptn "sport = :${PORT:-3333}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u); do
  kill "$p" 2>/dev/null && echo "llm-minitoolkit stopped"
done
sleep 1
for p in $(pgrep -x llama-server 2>/dev/null); do kill "$p" 2>/dev/null && echo "model stopped"; done
