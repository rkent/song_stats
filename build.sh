#!/usr/bin/env bash
# Build the song-stats website into docs/ (PDF reports also go to output/).
#
# Planning Center credentials (CLIENT_ID, CLIENT_SECRET) are read from the
# environment or from .env.
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=venv

# Create the virtual environment on first use.
if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --quiet -r requirements.txt

# Generates every report, the index page, and the latest song-candidate page.
"$VENV_DIR/bin/python" song_stats.py
