# LEGO Radio × Raspberry Pi — Embedding Plan

**Date:** 2026-06-07
**Goal:** Embed a Raspberry Pi Zero 2 W inside the LEGO Retro Radio (set 10334) so it becomes a real, working Spotify radio: headless, plays audio through its own speaker, controlled by the LEGO knobs.

## Decisions (validated)

| Topic | Decision |
|---|---|
| Audio | Pi plays the audio itself (librespot/raspotify + I2S amp + speaker) |
| Display | Headless — no screen |
| Controls | Physical knobs/buttons via GPIO (rotary encoders behind LEGO knobs) |
| Pi model | Raspberry Pi Zero 2 W |
| Power | USB cable out the back (wall adapter, always-on) |

## Architecture

```
                        ┌────────────────────────────────────┐
                        │        LEGO Retro Radio 10334      │
                        │                                    │
 Spotify Web API ◄──────┼── lego_radio_gpio.py (daemon)      │
 (play/pause/next/vol)  │        ▲ gpiozero                  │
                        │        │                           │
                        │   2× KY-040 rotary encoders        │
                        │   (behind LEGO knobs)              │
                        │                                    │
 Spotify Connect ───────┼─► raspotify (librespot)            │
 (audio stream)         │        │ I2S                       │
                        │   MAX98357A amp ──► 3W speaker     │
                        │                    (behind grille) │
                        │   Pi Zero 2 W ◄── 5V USB (rear)    │
                        └────────────────────────────────────┘
```

Two independent processes:

1. **raspotify** (packaged librespot): makes the Pi a Spotify Connect device named **"LEGO Radio"**. Handles the audio stream entirely; survives reboots; reconnects on Wi-Fi drops.
2. **lego_radio_gpio.py** (new daemon): reads the encoders via `gpiozero`, calls the Spotify Web API (spotipy, PKCE — same auth as the existing TUI) targeting the "LEGO Radio" device.

The existing `lego_radio.py` TUI is kept for desktop use. Its Spotify logic (PKCE auth, Liked Songs paging, playback control) is extracted into a shared module `spotify_core.py` used by both the TUI and the GPIO daemon.

## Hardware (BOM, ~45–55 €)

| Part | Role | ~Price |
|---|---|---|
| Raspberry Pi Zero 2 W + soldered header | brain | 22 € |
| Adafruit MAX98357A I2S 3W amp breakout | audio out (Pi Zero has none) | 6 € |
| Speaker 4Ω / 3W, ~40–50 mm | behind the LEGO grille | 5 € |
| 2× KY-040 rotary encoders (with push switch) | volume + tuning knobs | 5 € |
| MicroSD 16 GB+ (A1) | OS | 8 € |
| 5 V / 2.5 A USB supply + cable | power "cord" out the back | 8 € |
| Dupont wires, small perfboard, heat-shrink | wiring | — |

## Wiring

**MAX98357A (I2S):**

| Amp pin | Pi pin |
|---|---|
| VIN | 5V (pin 2) |
| GND | GND (pin 6) |
| BCLK | GPIO 18 (pin 12) |
| LRC | GPIO 19 (pin 35) |
| DIN | GPIO 21 (pin 40) |

Speaker on the amp's screw terminals. Leave GAIN unconnected (9 dB default).

**Encoder A — Volume (left knob):** CLK→GPIO 5, DT→GPIO 6, SW→GPIO 13, +→3V3, GND→GND
**Encoder B — Tuning (right knob):** CLK→GPIO 16, DT→GPIO 20, SW→GPIO 26, +→3V3, GND→GND

## Controls mapping

| Gesture | Action |
|---|---|
| Volume knob rotate | volume ± (Spotify API `volume` on device) |
| Volume knob press | play / pause |
| Tuning knob rotate | next / previous Liked Song |
| Tuning knob press | re-shuffle the Liked Songs queue |
| Tuning knob long-press (3 s) | safe shutdown (`systemctl poweroff`) |

## Software plan

### Phase 0 — Bench setup (no LEGO yet)
Everything assembled and tested on the desk first. Only brick it up once software is solid.

### Phase 1 — OS & network
- Raspberry Pi OS **Lite 64-bit** flashed with Raspberry Pi Imager (preconfigure: hostname `legoradio`, SSH on, Wi-Fi credentials, user).
- Boot, `ssh legoradio.local`, `apt update && apt full-upgrade`.

### Phase 2 — Audio: raspotify
- `/boot/firmware/config.txt`: `dtparam=audio=off` and `dtoverlay=hifiberry-dac` (drives the MAX98357A).
- Install raspotify: `curl -sL https://dtcooper.github.io/raspotify/install.sh | sh`.
- `/etc/raspotify/conf`: `LIBRESPOT_NAME="LEGO Radio"`, `LIBRESPOT_BITRATE="320"`, `LIBRESPOT_INITIAL_VOLUME="40"`.
- Test: speaker-test, then pick "LEGO Radio" from the phone's Spotify app and play.

### Phase 3 — Code refactor (on the Mac)
- Extract from `lego_radio.py` into `spotify_core.py`: config load/save, PKCE auth + token cache, Liked Songs fetching/paging, playback control (play/pause/next/prev/volume/device targeting).
- `lego_radio.py` keeps curses UI, imports the core.
- New `lego_radio_gpio.py`: gpiozero `RotaryEncoder`/`Button` handlers → core playback calls. Debounced, with a small worker queue so API calls never block GPIO callbacks. Auto-targets the device named "LEGO Radio"; if playback is idle, starts a shuffled Liked Songs queue on it.
- TDD where it pays: core extraction is covered by tests with a mocked spotipy client.

### Phase 4 — Auth on the Pi (headless PKCE)
The OAuth redirect goes to `127.0.0.1:8888` — on a headless Pi there's no browser. Two options:
- **Chosen:** authorize once on the Mac (existing flow), then copy `~/.config/lego_radio/` (config + token cache) to the Pi. spotipy refreshes tokens automatically thereafter.
- Fallback: `ssh -L 8888:127.0.0.1:8888 legoradio.local` and complete the flow from the Mac's browser.

### Phase 5 — Services
- `lego-radio-gpio.service` (systemd): runs the daemon as the regular user, `Restart=always`, `After=network-online.target`.
- Journald logging; `journalctl -u lego-radio-gpio -f` for debugging.
- Read-only-ish hygiene: token cache is the only thing written; optionally enable overlayfs later for SD-card longevity.

### Phase 6 — LEGO integration
- Mount the speaker behind the front grille (the set's speaker panel is mostly open studs — sound passes well).
- Couple each KY-040 shaft to the set's rotating knobs: a Technic axle + axle-connector glued/press-fit to the encoder's D-shaft works; the set's knobs already spin freely.
- Pi + perfboard on the floor of the radio body, velcro or brick cage; route the USB power lead out the rear, like a mains cord.
- Leave a few studs open near the Pi for airflow (Zero 2 W barely warms at this load).

### Phase 7 — Testing & polish
- Cold-boot test: power on → "LEGO Radio" appears on Connect ≤ 30 s, knobs work with no interaction needed.
- Wi-Fi drop test: kill AP, restore — raspotify reconnects, daemon retries with backoff.
- API failure handling: 429/5xx → exponential backoff, never crash.
- Long-press shutdown verified before final brick-up (avoids SD corruption).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Spotify Premium required for Connect control | Already required by the existing TUI; no change |
| Headless OAuth | Token cache copied from Mac; spotipy auto-refresh |
| Encoder bounce / missed steps | gpiozero RotaryEncoder (steps mode) + API call queue |
| SD card corruption on power pulls | Long-press shutdown; optional overlayfs read-only root |
| librespot disappears from device list | raspotify systemd auto-restart; daemon re-resolves device id by name |
| API rate limits from fast knob spinning | Coalesce events: only the latest volume/seek state is sent, ≥250 ms apart |

## Out of scope (YAGNI)

- Battery power, display, web UI, multi-room audio, physical "stations" presets dial (could come later — the tuning dial mechanism would suit presets→playlists nicely).
