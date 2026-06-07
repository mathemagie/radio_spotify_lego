# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LEGO Radio — a single-file Python curses TUI (`lego_radio.py`) that plays the user's Spotify Liked Songs by remote-controlling Spotify Connect devices (it does not decode audio itself). Spotify Premium is required for playback control.

## Commands

```bash
.venv/bin/pip install -r requirements.txt   # only dependency: spotipy
.venv/bin/python src/lego_radio.py          # run the app
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v   # run the test suite (tests/test_lego_radio.py)
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lego_radio.TestSearch -v   # one test class
.venv/bin/python -m py_compile src/lego_radio.py  # syntax check
```

Tests are stdlib `unittest` only (no pytest — keep it dependency-free). They never initialize curses: `App(None, player)` is fine as long as draw paths aren't touched. The Spotify client is replaced by `FakeSP` (records calls, raises on demand, can gate `devices()` on an Event); command threads are real, so tests synchronize on observable effects with `wait_until()`. Curses draw paths are covered separately by a pty smoke test: instantiate `Player` with a fake `sp`, populate `player.tracks` / `player.playback`, then drive `App.draw()` / `App.handle(ch)` under `curses.initscr()`. `curses.wrapper` cleanup fails without a real tty — catch `curses.error` around `endwin()` in sandboxed runs.

## Architecture

Everything lives in `src/lego_radio.py`, in four layers (top to bottom of file):

1. **Config + first-run wizard** — plain-ANSI (not curses) prompts before the TUI starts. Saves `{client_id}` to `~/.config/lego_radio/config.json`; token cache lives beside it. Auth is spotipy `SpotifyPKCE` (Client ID only, no secret). The redirect URI `http://127.0.0.1:8888/callback` is hardcoded in `REDIRECT_URI` and must match the user's Spotify dashboard app exactly.
2. **`Player`** — all Spotify state + API calls, shared across threads behind `self.lock`. Three thread types: a likes loader (`load_likes`, paginates 50 at a time, updates `tracks` incrementally so the UI fills in live), a 2.5s playback poll loop, and fire-and-forget command threads via `_cmd()` which translate API errors (`NO_ACTIVE_DEVICE`, `PREMIUM_REQUIRED`) into user-friendly `error` strings shown for 6s. UI code must never call the Spotify API synchronously.
3. **UI helpers** — `Pal` registers numbered color pairs (256-color with an 8-color fallback); `put()` is the only safe way to write to the screen (clips at edges, swallows `curses.error`). Always use `put()`, never raw `addstr`.
4. **`App`** — the curses loop. `scr.timeout(250)` getch drives both input and redraws (progress bar interpolates from `poll_ts` between polls). Input is modal: `handle()` dispatches to `handle_search()` or `handle_devices()` when those modes are active. Track filtering (`visible_tracks`) is accent/case-insensitive via `fold()`. "Play" sends a `uris` chunk of up to 100 tracks starting at the selection (the API can't target the Liked Songs collection as a context).

## Debugging / logs

`setup_logging()` (called first thing in `main()`) routes **all** logging — the
app's own `log = logging.getLogger("lego_radio")`, spotipy/urllib3, and captured
`warnings` — to a rotating file at `~/.config/lego_radio/lego_radio.log` (and
**nothing** to stderr, so stray output can't corrupt the curses screen). Uncaught
exceptions on the main thread and in worker threads are logged via
`sys.excepthook` / `threading.excepthook`; a curses-loop crash is logged then
re-raised. **When the app misbehaves or crashes, read that log file first** — it
records startup, auth, likes loading, every user-facing error (`flag_error` logs
centrally), command failures, and full tracebacks.

## Conventions

- Keep it a single file; no new dependencies beyond spotipy unless necessary.
- Aesthetic is intentional ("LEGO brick"): yellow stud header, Spotify-green accents, `▰▱` progress bricks, braille spinner. Match it in new UI work.
- Wizard/startup output uses the module-level ANSI constants (`G`, `Y`, `R`, `D`, `B`, `X`); curses code uses `Pal` pairs.
