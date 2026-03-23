#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  if command -v python3 >/dev/null 2>&1; then
    BASE_PYTHON="python3"
  elif command -v python >/dev/null 2>&1; then
    BASE_PYTHON="python"
  else
    echo "[ERROR] Python wurde nicht gefunden. Bitte Python 3.10+ installieren."
    exit 1
  fi

  echo "[INFO] Erstelle lokale virtuelle Umgebung in .venv ..."
  "$BASE_PYTHON" -m venv .venv
  PYTHON=".venv/bin/python"
fi

echo "[INFO] Aktualisiere pip ..."
"$PYTHON" -m pip install --upgrade pip

echo "[INFO] Installiere/aktualisiere Abhaengigkeiten in .venv ..."
"$PYTHON" -m pip install -r requirements.txt

PORT=${PORT:-5000}
HOST=${HOST:-0.0.0.0}
URL="http://127.0.0.1:${PORT}"

echo "[INFO] Starte YouTube Downloader auf ${URL}"

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 &
fi

HOST="$HOST" PORT="$PORT" "$PYTHON" app.py
