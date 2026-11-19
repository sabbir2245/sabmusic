#!/usr/bin/env bash
# SabMusic launcher for Linux/macOS.
# Starts sabmusic.py with python3 (tkinter required).

set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import tkinter" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "SabMusic: Python 3 with tkinter was not found." >&2
    echo "Install it, e.g.:" >&2
    echo "  Debian/Ubuntu: sudo apt install python3 python3-tk" >&2
    echo "  Fedora:        sudo dnf install python3 python3-tkinter" >&2
    echo "  Arch:          sudo pacman -S python tk" >&2
    exit 1
fi

if ! command -v mpv >/dev/null 2>&1 && ! command -v vlc >/dev/null 2>&1; then
    echo "SabMusic: warning: neither mpv nor vlc was found on PATH." >&2
    echo "Install mpv (recommended) or VLC to play music." >&2
fi

exec "$PY" "$DIR/sabmusic.py" "$@"
