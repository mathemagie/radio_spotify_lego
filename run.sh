#!/usr/bin/env bash
# Launch LEGO Radio on the Pi. ncurses needs a real terminal type.
cd "$(dirname "$0")"

# ncurses needs a TERM whose terminfo entry supports cursor movement.
# Fall back to xterm-256color if TERM is empty, "dumb", or has no terminfo
# entry on this machine (otherwise curses dies with
# "setupterm: could not find terminal").
if [ -z "${TERM:-}" ] || [ "$TERM" = "dumb" ] || ! infocmp "$TERM" >/dev/null 2>&1; then
  export TERM=xterm-256color
fi

exec env PYTHONPATH=src .venv/bin/python src/lego_radio.py
