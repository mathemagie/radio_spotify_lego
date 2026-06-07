# 🧱 LEGO Radio

A brick-built **ncurses** terminal player for your **Spotify Liked Songs**.

It remote-controls any of your Spotify Connect devices (desktop app, phone,
speakers) — pick a liked track, hit ⏎, and it plays. Spotify **Premium** is
required for playback control (a Spotify API rule).

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/lego_radio.py
```

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
Credentials are stored in `~/.config/lego_radio/` (mode 600).

A browser window opens once so you can approve access; after that the token
refreshes automatically.

## Keys

| Key       | Action                          |
|-----------|---------------------------------|
| `↑↓` `jk` | move                            |
| `⏎`       | play from selected track        |
| `space`   | pause / resume                  |
| `n` / `p` | next / previous                 |
| `s`       | toggle shuffle                  |
| `+` / `-` | volume ±10%                     |
| `/`       | search (Esc clears)             |
| `d`       | Spotify Connect device picker   |
| `g` / `G` | jump to top / bottom            |
| `r`       | re-sync liked songs             |
| `q`       | quit                            |

## Troubleshooting

- **“no active device”** — open Spotify on any device (or press `d` to pick
  one), then play again.
- **“Premium required”** — Spotify only allows remote playback control on
  Premium accounts.
- **Auth fails** — double-check the Redirect URI in your dashboard app is
  exactly `http://127.0.0.1:8888/callback`.
