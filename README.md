# 🧱 LEGO Radio

A brick-built **ncurses** terminal player for your **Spotify Liked Songs**.

It remote-controls any of your Spotify Connect devices (desktop app, phone,
speakers) — pick a liked track, hit ⏎, and it plays. Spotify **Premium** is
required for playback control (a Spotify API rule).

## Features

- **Liked Songs**, loaded incrementally and cached to disk so the list shows
  instantly on the next launch (refreshed in the background).
- **Search** your likes (`/`) or the **whole Spotify catalog** (`f`).
- **Like / unlike** any track with `a` — even from catalog search results,
  which show a ♥ for songs you already have.
- **Resumes where you left off**: the cursor position is saved continuously
  and restored on the next run.
- **Open in browser** (`o`) — jumps to the track on the Spotify web player,
  preferring Google Chrome.
- Snappy, non-blocking UI with optimistic updates; `Ctrl-C` quits cleanly.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/lego_radio.py
```

Or with the included Makefile: `make install && make run`.

## First-run setup (≈2 minutes, free)

Spotify requires the API app to be registered under **your** account —
nobody can create the key for you. The first run walks you through it:

1. Go to <https://developer.spotify.com/dashboard> and log in
2. Click **Create app** and fill in:
   - **App name**: `LEGO Radio`
   - **App description**: `terminal player`
   - **Redirect URI**: `http://127.0.0.1:8888/callback` ← must match exactly
   - **API used**: Web API
3. Save → open the app → copy the **Client ID**
4. Paste it into the wizard when prompted

No Client Secret is needed — the app uses the secure OAuth **PKCE** flow.
Credentials and caches are stored in `~/.config/lego_radio/` (mode 600).

A browser window opens once so you can approve access; after that the token
refreshes automatically. (Liking tracks needs the `user-library-modify`
permission, so you may be asked to re-approve once after an update.)

## Keys

| Key       | Action                                |
|-----------|---------------------------------------|
| `↑↓` `jk` | move                                  |
| `g` / `G` | jump to top / bottom                  |
| `⏎`       | play from selected track              |
| `space`   | pause / resume                        |
| `n` / `p` | next / previous                       |
| `s`       | toggle shuffle                        |
| `+` / `-` | volume ±10%                           |
| `/`       | filter liked songs (Esc clears)       |
| `f`       | search the whole Spotify catalog      |
| `a`       | like / unlike the selected track      |
| `o`       | open the selected track in the browser|
| `d`       | Spotify Connect device picker         |
| `r`       | re-sync liked songs                   |
| `?`       | show more / fewer key hints           |
| `q` / `Ctrl-C` | quit                             |

## Troubleshooting

- **“no active device”** — open Spotify on any device (or press `d` to pick
  one), then play again.
- **“Premium required”** — Spotify only allows remote playback control on
  Premium accounts.
- **Auth fails** — double-check the Redirect URI in your dashboard app is
  exactly `http://127.0.0.1:8888/callback`.
- **Liking does nothing / asks to log in again** — `a` needs the
  `user-library-modify` permission; approve it once in the browser when
  prompted after updating.

## Development

```bash
make install-dev    # runtime + dev deps (Ruff, Black)
make install-hooks  # install the pre-commit hook
make test           # run the unittest suite
make lint           # Ruff + Black checks
make format         # auto-format
```

Everything lives in a single file, `src/lego_radio.py`; tests use the stdlib
`unittest` (no extra test deps). See `CLAUDE.md` for the architecture notes.
