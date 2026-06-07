#!/usr/bin/env python3
"""
██╗     ███████╗ ██████╗  ██████╗     ██████╗  █████╗ ██████╗ ██╗ ██████╗
██║     ██╔════╝██╔════╝ ██╔═══██╗    ██╔══██╗██╔══██╗██╔══██╗██║██╔═══██╗
██║     █████╗  ██║  ███╗██║   ██║    ██████╔╝███████║██║  ██║██║██║   ██║
██║     ██╔══╝  ██║   ██║██║   ██║    ██╔══██╗██╔══██║██║  ██║██║██║   ██║
███████╗███████╗╚██████╔╝╚██████╔╝    ██║  ██║██║  ██║██████╔╝██║╚██████╔╝
╚══════╝╚══════╝ ╚═════╝  ╚═════╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝ ╚═════╝

LEGO RADIO — a brick-built ncurses player for your Spotify Liked Songs.

Plays through Spotify Connect: it remote-controls any of your devices
(the Spotify desktop app, your phone, a speaker...). Premium required
for playback control — that's a Spotify API rule, not ours.

First run walks you through creating a (free) Spotify Client ID.
"""

import concurrent.futures
import curses
import functools
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser

CONFIG_DIR = os.path.expanduser("~/.config/lego_radio")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_FILE = os.path.join(CONFIG_DIR, "token_cache.json")
LIKES_CACHE_FILE = os.path.join(CONFIG_DIR, "likes_cache.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
LOG_FILE = os.path.join(CONFIG_DIR, "lego_radio.log")

log = logging.getLogger("lego_radio")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-library-read user-library-modify user-read-playback-state "
    "user-modify-playback-state user-read-currently-playing"
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BRICK_FULL, BRICK_EMPTY = "▰", "▱"

# ───────────────────────── config & first-run wizard ─────────────────────────

G = "\x1b[38;5;41m"  # spotify green
Y = "\x1b[38;5;220m"  # lego yellow
R = "\x1b[38;5;196m"  # lego red
D = "\x1b[38;5;244m"  # dim
B = "\x1b[1m"
X = "\x1b[0m"


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass  # best-effort; losing the cursor position is not worth crashing


def first_run_wizard():
    print(f"""
{Y}{B}  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  █  ▘▘ ▘▘ ▘▘ ▘▘ ▘▘ ▘▘  L E G O   R A D I O  ▘▘ ▘▘ ▘▘ ▘▘  █
  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀{X}

  {B}One-time setup{X} — Spotify requires the app to be registered under
  {B}your{X} account (≈2 minutes, free, no credit card):

  {G}1.{X} Open {B}https://developer.spotify.com/dashboard{X}  (opening it for you…)
  {G}2.{X} Log in with your Spotify account → click {B}“Create app”{X}
  {G}3.{X} Fill in:
       App name        : {Y}LEGO Radio{X}
       App description : {Y}terminal player{X}
       Redirect URI    : {Y}{REDIRECT_URI}{X}   {R}← must match exactly{X}
       API used        : {Y}Web API{X}
  {G}4.{X} Save → open the app → copy the {B}Client ID{X}
       (no Client Secret needed — we use the secure PKCE flow)
""")
    try:
        webbrowser.open("https://developer.spotify.com/dashboard")
    except Exception:
        pass
    while True:
        client_id = input(f"  {B}Paste your Client ID:{X} ").strip()
        if len(client_id) == 32 and all(c in "0123456789abcdef" for c in client_id):
            break
        print(f"  {R}That doesn't look like a Client ID (32 hex chars). Try again.{X}")
    cfg = {"client_id": client_id}
    save_config(cfg)
    print(f"\n  {G}✓ Saved to {CONFIG_FILE}{X}")
    return cfg


def get_spotify(cfg):
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE

    auth = SpotifyPKCE(
        client_id=cfg["client_id"],
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_path=CACHE_FILE,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=10, retries=2)


# ───────────────────────────── data layer ─────────────────────────────


class Player:
    """Background-synced state for liked tracks + playback."""

    def __init__(self, sp):
        self.sp = sp
        self.lock = threading.Lock()
        self.tracks = []  # [{uri, name, artist, album, ms}]
        self.loading = True
        self.load_count = 0
        self.playback = None  # raw playback dict
        self.poll_ts = 0.0
        self.error = ""
        self.error_ts = 0.0
        self.notice = ""  # transient status (e.g. browser opening)
        self.notice_ts = 0.0
        self.search_results = None  # None = inactive; list = global results
        self.search_loading = False
        self.search_gen = 0  # invalidates stale/clobbered search threads

    # -- liked songs --
    @staticmethod
    def _parse_page(page):
        items = []
        for it in page["items"]:
            t = it.get("track")
            if not t or not t.get("uri"):
                continue
            artist = ", ".join(a["name"] for a in t["artists"])
            album = (t.get("album") or {}).get("name", "")
            items.append(
                {
                    "uri": t["uri"],
                    "name": t["name"],
                    "artist": artist,
                    "album": album,
                    "ms": t.get("duration_ms", 0),
                    # precomputed search key — folding 400+ strings per
                    # frame while typing is what made search feel slow
                    "key": fold(t["name"] + " " + artist + " " + album),
                }
            )
        return items

    @staticmethod
    def _read_likes_cache():
        try:
            with open(LIKES_CACHE_FILE) as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return None

    @staticmethod
    def _write_likes_cache(items):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(LIKES_CACHE_FILE, "w") as f:
                json.dump(items, f)
            os.chmod(LIKES_CACHE_FILE, 0o600)
        except OSError:
            pass  # best-effort; a write failure just means a cold sync next launch

    def load_likes(self):
        # last session's library shows instantly from disk; the network
        # refresh swaps in atomically below, so a slow or offline sync
        # never blanks the list the user is already scrolling
        cached = self._read_likes_cache()
        if cached:
            with self.lock:
                self.tracks = cached
                self.load_count = len(cached)
        try:
            first = self.sp.current_user_saved_tracks(limit=50, offset=0)
            items = self._parse_page(first)
            if not cached:  # nothing on screen yet → fill in live, page by page
                with self.lock:
                    self.tracks = list(items)
                    self.load_count = len(items)
            # the first page tells us the library size, so the remaining
            # pages can be fetched concurrently (4 workers stays well under
            # Spotify's rate limits) instead of one round-trip at a time.
            # executor.map yields results in offset order, so the list
            # still fills in top-to-bottom as pages land.
            offsets = range(50, first.get("total") or 0, 50)
            if offsets:
                fetch = lambda o: self.sp.current_user_saved_tracks(limit=50, offset=o)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                    for page in ex.map(fetch, offsets):
                        items = items + self._parse_page(page)
                        if not cached:  # cold: grow live; warm: one swap at the end
                            with self.lock:
                                self.tracks = items
                                self.load_count = len(items)
            with self.lock:  # atomic swap — warm start updates here in one step
                self.tracks = items
                self.load_count = len(items)
            self._write_likes_cache(items)
            log.info("loaded %d liked songs", len(items))
        except Exception as e:
            log.exception("load_likes failed")
            self.flag_error(f"likes: {e}")  # keep cached tracks on screen
        finally:
            with self.lock:
                self.loading = False

    # -- global catalog search --
    def search(self, query):
        # bump the generation so any slower in-flight search (or a typed-over
        # query) can't land its results after this newer one — latest wins
        with self.lock:
            self.search_loading = True
            self.search_gen += 1
            gen = self.search_gen

        def run():
            try:
                # dev-mode apps 400 on limit > 10 (docs still say 50), so
                # fetch 5 pages of 10 concurrently — same trick as load_likes
                def fetch(off):
                    res = self.sp.search(q=query, type="track", limit=10, offset=off)
                    return (res.get("tracks") or {}).get("items") or []

                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                    pages = list(ex.map(fetch, range(0, 50, 10)))
                # search repeats tracks across adjacent pages — keep first
                items, seen = [], set()
                for t in (t for page in pages for t in page):
                    uri = t.get("uri")
                    if uri and uri not in seen:
                        seen.add(uri)
                        items.append(t)
                # search returns bare track dicts; _parse_page expects the
                # saved-tracks {"track": t} wrapper, so adapt and reuse it
                parsed = self._parse_page({"items": [{"track": t} for t in items]})
                with self.lock:
                    if gen == self.search_gen:  # still the newest request?
                        self.search_results = parsed
            except Exception as e:
                self.flag_error(f"search: {e}")
            finally:
                with self.lock:
                    if gen == self.search_gen:
                        self.search_loading = False

        threading.Thread(target=run, daemon=True).start()

    def clear_search(self):
        # bump the generation too, so an in-flight search started before Esc
        # can't repopulate the list after the user cleared it
        with self.lock:
            self.search_results = None
            self.search_gen += 1
            self.search_loading = False

    # -- playback polling --
    def poll(self):
        try:
            pb = self.sp.current_playback()
            with self.lock:
                self.playback = pb
                self.poll_ts = time.monotonic()
        except Exception as e:
            self.flag_error(f"poll: {e}")

    def poll_loop(self, stop):
        while not stop.is_set():
            self.poll()
            stop.wait(2.5)

    # -- commands (each in a fire-and-forget thread to keep UI snappy) --
    def _cmd(self, fn, label):
        def run():
            try:
                fn()
                # The optimistic UI update already happened; polling too
                # early would read pre-command state and revert it. Wait
                # for Spotify to apply the command, then reconcile once.
                time.sleep(0.25)
                self.poll()
            except Exception as e:
                log.warning("command %s failed: %s", label, e)
                msg = str(e)
                if "NO_ACTIVE_DEVICE" in msg or "Device not found" in msg:
                    # [o] opens the web player, which registers as a
                    # Connect device — but launching a browser is the
                    # user's call, never the app's (trust beats magic)
                    msg = "no active device — [o] web player or [d] devices"
                elif "PREMIUM_REQUIRED" in msg:
                    msg = "Spotify Premium is required for remote playback control"
                self.flag_error(f"{label}: {msg}")
                # the optimistic update is now wrong — reconcile immediately
                # instead of leaving stale state until the next 2.5s poll
                try:
                    self.poll()
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def play_from(self, idx, track_list):
        chunk = [t["uri"] for t in track_list[idx : idx + 100]]
        if not chunk:
            return
        t = track_list[idx]
        with self.lock:
            shuffled = bool(self.playback and self.playback.get("shuffle_state"))
            # optimistic: show the chosen track as playing now — the API
            # round-trip + reconcile poll would otherwise leave the bar
            # stale for up to a second after ⏎
            self.playback = {
                "is_playing": True,
                "shuffle_state": False,
                "progress_ms": 0,
                "item": {
                    "uri": t["uri"],
                    "name": t["name"],
                    "duration_ms": t["ms"],
                    "artists": [{"name": t["artist"]}],
                    "album": {"name": t["album"]},
                },
                "device": (self.playback or {}).get("device"),
            }
            self.poll_ts = time.monotonic()

        def start():
            # _device_id() may hit the network — must run here in the
            # worker thread, never on the UI thread.
            dev = self._device_id()
            # An explicit ⏎ means "play THIS track" — shuffle would make
            # Spotify pick a random one from the chunk, so disable it first.
            if shuffled:
                try:
                    self.sp.shuffle(False, device_id=dev)
                except Exception:
                    pass
            self.sp.start_playback(device_id=dev, uris=chunk)

        self._cmd(start, "play")

    def toggle_pause(self):
        with self.lock:
            playing = bool(self.playback and self.playback.get("is_playing"))
            if self.playback:  # optimistic flip so the UI reacts instantly
                self.playback["is_playing"] = not playing
                self.poll_ts = time.monotonic()
        if playing:
            self._cmd(lambda: self.sp.pause_playback(), "pause")
        else:
            self._cmd(lambda: self.sp.start_playback(), "resume")

    def next(self):
        with self.lock:  # optimistic: reset the bar so the skip is visible now
            if self.playback:
                self.playback["progress_ms"] = 0
                self.poll_ts = time.monotonic()
        self._cmd(lambda: self.sp.next_track(), "next")

    def prev(self):
        with self.lock:
            if self.playback:
                self.playback["progress_ms"] = 0
                self.poll_ts = time.monotonic()
        self._cmd(lambda: self.sp.previous_track(), "prev")

    def volume(self, delta):
        with self.lock:
            pb = self.playback
            if not pb or not pb.get("device"):
                return
            vol = max(0, min(100, (pb["device"].get("volume_percent") or 50) + delta))
            pb["device"]["volume_percent"] = vol  # optimistic: show new % now
        self._cmd(lambda: self.sp.volume(vol), "volume")

    def shuffle_toggle(self):
        with self.lock:
            cur = bool(self.playback and self.playback.get("shuffle_state"))
            if self.playback:  # optimistic: flip the ⤨ icon now
                self.playback["shuffle_state"] = not cur
        self._cmd(lambda: self.sp.shuffle(not cur), "shuffle")

    def is_liked(self, uri):
        with self.lock:
            return any(t["uri"] == uri for t in self.tracks)

    def toggle_like(self, track):
        """Add/remove a track from Liked Songs, optimistically.

        The in-memory likes list is the source of truth — a track is liked
        iff its uri is in self.tracks — so no extra API round-trip is needed
        to know which way to toggle. The list updates immediately (newly
        liked songs land on top, as Spotify orders them) and reverts if the
        API call fails.
        """
        uri = track["uri"]
        tid = uri.rsplit(":", 1)[-1]
        with self.lock:
            idx = next((i for i, t in enumerate(self.tracks) if t["uri"] == uri), None)
            liked = idx is not None
            if liked:
                removed = self.tracks.pop(idx)
            else:
                entry = {
                    k: track.get(k)
                    for k in ("uri", "name", "artist", "album", "ms", "key")
                }
                self.tracks.insert(0, entry)
        self.flag_notice(
            ("♡ removed " if liked else "♥ added ") + clip(track["name"], 40)
        )

        def run():
            try:
                if liked:
                    self.sp.current_user_saved_tracks_delete(tracks=[tid])
                else:
                    self.sp.current_user_saved_tracks_add(tracks=[tid])
            except Exception as e:
                with self.lock:  # API rejected it — undo the optimistic change
                    if liked:
                        self.tracks.insert(min(idx, len(self.tracks)), removed)
                    else:
                        self.tracks[:] = [t for t in self.tracks if t["uri"] != uri]
                self.flag_error(f"like: {e}")

        threading.Thread(target=run, daemon=True).start()

    def devices(self):
        try:
            return self.sp.devices().get("devices", [])
        except Exception as e:
            self.flag_error(f"devices: {e}")
            return []

    def transfer(self, device):
        with self.lock:  # optimistic: show the new device in the bar now
            if self.playback and self.playback.get("device"):
                self.playback["device"] = dict(
                    self.playback["device"], id=device["id"], name=device["name"]
                )
        self._cmd(
            lambda: self.sp.transfer_playback(device["id"], force_play=True), "transfer"
        )

    def _device_id(self):
        with self.lock:
            pb = self.playback
        if pb and pb.get("device"):
            return pb["device"]["id"]
        devs = self.devices()
        return devs[0]["id"] if devs else None

    def flag_error(self, msg):
        log.error(msg)
        with self.lock:
            self.error = msg[:200]
            self.error_ts = time.monotonic()

    def flag_notice(self, msg):
        with self.lock:
            self.notice = msg[:200]
            self.notice_ts = time.monotonic()


def open_url(url):
    """Open url in Chrome when available, else the default browser.

    Chrome is detected through the webbrowser API: get() raises
    webbrowser.Error when the named browser is missing, and open()
    returns False when the launch fails. macOS Pythons don't register
    Chrome under a usable name, so there we ask Launch Services
    (`open -Ra`) whether Chrome exists before targeting it.
    """
    if sys.platform == "darwin":
        has_chrome = (
            subprocess.run(
                ["open", "-Ra", "Google Chrome"], capture_output=True
            ).returncode
            == 0
        )
        if (
            has_chrome
            and subprocess.run(
                ["open", "-a", "Google Chrome", url], capture_output=True
            ).returncode
            == 0
        ):
            return
    else:
        for name in ("chrome", "google-chrome", "chromium"):
            try:
                if webbrowser.get(name).open(url):
                    return
            except webbrowser.Error:
                pass
    webbrowser.open(url)


# ───────────────────────────── ui helpers ─────────────────────────────


@functools.lru_cache(maxsize=4096)
def fold(s):
    return "".join(
        c
        for c in unicodedata.normalize("NFD", s.lower())
        if unicodedata.category(c) != "Mn"
    )


def fmt_ms(ms):
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def clip(s, w):
    if w <= 0:
        return ""
    return s[: w - 1] + "…" if len(s) > w else s


class Pal:
    """Color pair registry."""

    GREEN, DIM, BRIGHT, YELLOW, RED, SEL, HEAD, BAR = range(1, 9)

    @staticmethod
    def init():
        curses.start_color()
        curses.use_default_colors()
        if curses.COLORS >= 256:
            green, dim, bright, yellow, red, black = 41, 244, 231, 220, 196, 233
        else:
            green, dim, bright, yellow, red, black = (
                curses.COLOR_GREEN,
                curses.COLOR_WHITE,
                curses.COLOR_WHITE,
                curses.COLOR_YELLOW,
                curses.COLOR_RED,
                curses.COLOR_BLACK,
            )
        curses.init_pair(Pal.GREEN, green, -1)
        curses.init_pair(Pal.DIM, dim, -1)
        curses.init_pair(Pal.BRIGHT, bright, -1)
        curses.init_pair(Pal.YELLOW, yellow, -1)
        curses.init_pair(Pal.RED, red, -1)
        curses.init_pair(Pal.SEL, black, green)
        curses.init_pair(Pal.HEAD, green, black if curses.COLORS >= 256 else -1)
        curses.init_pair(Pal.BAR, green, -1)

    @staticmethod
    def c(pair, bold=False):
        a = curses.color_pair(pair)
        return a | curses.A_BOLD if bold else a


def put(win, y, x, text, attr=0):
    """addstr that never crashes on edges."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        win.addnstr(y, x, text, max(0, w - x - 1), attr)
    except curses.error:
        pass


# ───────────────────────────── main app ─────────────────────────────


class App:
    def __init__(self, stdscr, player, saved_uri=None):
        self.scr = stdscr
        self.p = player
        self.sel = 0
        self.top = 0
        # restore cursor across restarts (see _restore_cursor): land back on
        # last session's track once the list loads. Position is saved
        # continuously as the cursor moves (see _persist_position), so a
        # crash keeps it too. Restore is one-shot and yields once the user
        # navigates.
        self._saved_uri = saved_uri
        self._restored_saved = False
        self._pending_uri = None  # [r] reload re-anchor target
        self._last_saved_uri = saved_uri  # avoids rewriting the same value
        self.query = ""
        self.searching = False
        self.global_query = ""
        self.global_searching = False
        self.spin = 0
        self.device_mode = False
        self.device_list = []
        self.device_sel = 0
        self.device_loading = False
        self.all_keys = False  # footer: core keys by default, ? shows all
        self._vis_cache = None  # (tracks identity, query, folded query, results)

    # -- derived --
    def visible_tracks(self):
        with self.p.lock:
            # global results replace likes while active; / filters either
            tracks = (
                self.p.search_results
                if self.p.search_results is not None
                else self.p.tracks
            )
        if not self.query:
            return tracks
        # memoized: draw() calls this several times per frame, and the
        # loader replaces p.tracks wholesale, so identity is a valid key
        cached = self._vis_cache
        q = fold(self.query)
        source = tracks
        if cached and cached[0] is tracks:
            if cached[1] == self.query:
                return cached[3]
            # Typing makes the query stricter. Re-filtering the previous
            # result set avoids scanning large libraries on every keystroke.
            if cached[2] and q.startswith(cached[2]):
                source = cached[3]
        vis = [t for t in source if q in t["key"]]
        self._vis_cache = (tracks, self.query, q, vis)
        return vis

    def selected_uri(self):
        tracks = self.visible_tracks()
        if tracks and 0 <= self.sel < len(tracks):
            return tracks[self.sel]["uri"]
        return None

    @staticmethod
    def _index_of(tracks, uri):
        if not uri:
            return None
        return next((i for i, t in enumerate(tracks) if t["uri"] == uri), None)

    def _restore_cursor(self, tracks):
        """Land the cursor back on a remembered track once the list loads.

        [r] reload re-anchors to the same track by uri; launch restores last
        session's cursor. Both are one-shot and stop firing once the user
        navigates.
        """
        if not tracks or self.query:
            return
        if self._pending_uri:  # [r] reload: keep position by uri
            idx = self._index_of(tracks, self._pending_uri)
            if idx is not None:
                self.sel = idx
                self._pending_uri = None
            return
        if not self._restored_saved:
            idx = self._index_of(tracks, self._saved_uri)
            if idx is not None:
                self.sel = idx
            self._restored_saved = True  # one-shot even if the uri is gone

    def _persist_position(self):
        """Save the highlighted track whenever it changes (crash-safe)."""
        uri = self.selected_uri()
        if uri and uri != self._last_saved_uri:
            self._last_saved_uri = uri
            save_state({"sel_uri": uri})

    # -- drawing --
    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.draw_header(w)
        list_top, list_bot = 3, h - 6
        self.draw_list(list_top, list_bot, w)
        self.draw_nowplaying(h - 5, w)
        self.draw_footer(h - 1, w)
        if self.device_mode:
            self.draw_devices(h, w)
        scr.refresh()

    def draw_header(self, w):
        studs = "▘▘ " * (max(0, w - 24) // 6)
        title = "  L E G O   R A D I O  "
        put(self.scr, 0, 0, "▄" * (w - 1), Pal.c(Pal.YELLOW))
        bar = f" {studs}"
        put(self.scr, 1, 0, bar, Pal.c(Pal.YELLOW))
        put(
            self.scr,
            1,
            max(0, (w - len(title)) // 2),
            title,
            Pal.c(Pal.HEAD, bold=True),
        )
        with self.p.lock:
            n, loading, cnt = len(self.p.tracks), self.p.loading, self.p.load_count
            results, searching = self.p.search_results, self.p.search_loading
        if searching:
            spin = SPINNER[self.spin % len(SPINNER)]
            put(self.scr, 1, w - 22, f"{spin} searching…", Pal.c(Pal.GREEN))
        elif results is not None:
            label = f"⌕ {len(results)} results"
            put(self.scr, 1, w - len(label) - 2, label, Pal.c(Pal.GREEN, bold=True))
        elif loading:
            spin = SPINNER[self.spin % len(SPINNER)]
            put(self.scr, 1, w - 22, f"{spin} syncing {cnt}…", Pal.c(Pal.GREEN))
        else:
            label = f"♥ {n} likes"
            put(self.scr, 1, w - len(label) - 2, label, Pal.c(Pal.GREEN, bold=True))
        put(self.scr, 2, 0, "▀" * (w - 1), Pal.c(Pal.YELLOW))

    def draw_list(self, top, bot, w):
        tracks = self.visible_tracks()
        rows = max(1, bot - top)

        with self.p.lock:
            pb = self.p.playback
        cur_uri = ((pb or {}).get("item") or {}).get("uri")

        self._restore_cursor(tracks)
        self.sel = max(0, min(self.sel, len(tracks) - 1)) if tracks else 0
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        self.top = max(0, min(self.top, max(0, len(tracks) - rows)))

        # while browsing search results, mark the ones already in Liked Songs
        with self.p.lock:
            results = self.p.search_results
            liked_uris = (
                {t["uri"] for t in self.p.tracks} if results is not None else None
            )
        if not tracks:
            if self.query:
                msg = "no matches — Esc to clear search"
            elif results is not None:
                msg = "no results — Esc for likes, f to search again"
            else:
                msg = "♥ your liked songs will appear here…"
            put(self.scr, top + 2, 4, msg, Pal.c(Pal.DIM))
        for i in range(rows):
            idx = self.top + i
            if idx >= len(tracks):
                break
            t = tracks[idx]
            y = top + i
            is_sel = idx == self.sel
            is_cur = t["uri"] == cur_uri
            is_liked = liked_uris is not None and t["uri"] in liked_uris
            marker = "▶ " if is_cur else ("♥ " if is_liked else "  ")
            num = f"{idx + 1:>4} "
            dur = f" {fmt_ms(t['ms']):>5} "
            name_w = max(10, int((w - 14) * 0.55))
            art_w = max(8, w - 14 - name_w)
            line = f"{marker}{num}{clip(t['name'], name_w):<{name_w}}{clip(t['artist'], art_w)}"
            if is_sel:
                put(self.scr, y, 0, " " * (w - 1), Pal.c(Pal.SEL))
                put(self.scr, y, 1, line, Pal.c(Pal.SEL, bold=True))
                put(self.scr, y, w - 8, dur, Pal.c(Pal.SEL))
            else:
                attr = Pal.c(Pal.GREEN, bold=True) if is_cur else Pal.c(Pal.BRIGHT)
                put(self.scr, y, 1, marker, Pal.c(Pal.GREEN, bold=True))
                put(self.scr, y, 3, num, Pal.c(Pal.DIM))
                put(self.scr, y, 8, clip(t["name"], name_w), attr)
                put(self.scr, y, 8 + name_w, clip(t["artist"], art_w), Pal.c(Pal.DIM))
                put(self.scr, y, w - 8, dur, Pal.c(Pal.DIM))

    def draw_nowplaying(self, y, w):
        put(self.scr, y, 0, "─" * (w - 1), Pal.c(Pal.DIM))
        with self.p.lock:
            pb, ts = self.p.playback, self.p.poll_ts
            err, err_ts = self.p.error, self.p.error_ts
            note, note_ts = self.p.notice, self.p.notice_ts
        item = (pb or {}).get("item")
        if item:
            playing = pb.get("is_playing")
            icon = "▶" if playing else "⏸"
            shuffle = " ⤨" if pb.get("shuffle_state") else ""
            dev = pb.get("device") or {}
            vol = dev.get("volume_percent")
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            album = (item.get("album") or {}).get("name", "")
            byline = f"— {clip(artists, w // 3)}"
            if album and album != item["name"]:  # skip redundant single-titles
                byline += f" · {clip(album, w // 4)}"
            put(self.scr, y + 1, 2, f"{icon} ", Pal.c(Pal.GREEN, bold=True))
            put(
                self.scr,
                y + 1,
                4,
                clip(item["name"], w // 2),
                Pal.c(Pal.BRIGHT, bold=True),
            )
            put(
                self.scr,
                y + 1,
                5 + min(len(item["name"]), w // 2),
                f"{byline}{shuffle}",
                Pal.c(Pal.DIM),
            )
            right = f"{dev.get('name', '?')}  vol {vol if vol is not None else '–'}%"
            put(self.scr, y + 1, max(2, w - len(right) - 2), right, Pal.c(Pal.YELLOW))
            # progress bricks
            prog = pb.get("progress_ms") or 0
            if playing and ts:
                prog += int((time.monotonic() - ts) * 1000)
            total = item.get("duration_ms") or 1
            prog = min(prog, total)
            bar_w = max(10, w - 20)
            filled = int(bar_w * prog / total)
            put(self.scr, y + 2, 2, fmt_ms(prog).rjust(5), Pal.c(Pal.DIM))
            put(self.scr, y + 2, 8, BRICK_FULL * filled, Pal.c(Pal.BAR, bold=True))
            put(
                self.scr,
                y + 2,
                8 + filled,
                BRICK_EMPTY * (bar_w - filled),
                Pal.c(Pal.DIM),
            )
            put(self.scr, y + 2, 9 + bar_w, fmt_ms(total), Pal.c(Pal.DIM))
        else:
            put(
                self.scr,
                y + 1,
                2,
                "⏹ nothing playing — pick a track and hit Enter",
                Pal.c(Pal.DIM),
            )
        # errors win the line; otherwise a short-lived notice (e.g. a
        # browser launch) fills the wait so the keypress feels answered
        if err and time.monotonic() - err_ts < 6:
            put(self.scr, y + 3, 2, f"✗ {err}", Pal.c(Pal.RED, bold=True))
        elif note and time.monotonic() - note_ts < 3:
            put(self.scr, y + 3, 2, f"↗ {note}", Pal.c(Pal.GREEN))

    def draw_footer(self, y, w):
        if self.global_searching:
            put(
                self.scr,
                y,
                0,
                f" ⌕ {self.global_query}▏ (all Spotify — ⏎ search)",
                Pal.c(Pal.YELLOW, bold=True),
            )
            return
        if self.searching:
            put(self.scr, y, 0, f" / {self.query}▏", Pal.c(Pal.YELLOW, bold=True))
            if self.query:
                with self.p.lock:
                    total = len(self.p.tracks)
                count = f"{len(self.visible_tracks())} / {total} "
                put(self.scr, y, max(0, w - len(count) - 1), count, Pal.c(Pal.DIM))
            return
        if self.all_keys:
            # ordered so a narrow terminal clips the already-learned
            # navigation keys, never "? less" / "q quit"
            keys = [
                ("⏎", "play"),
                ("space", "pause"),
                ("n/p", "skip"),
                ("s", "shuffle"),
                ("+/-", "vol"),
                ("·", ""),
                ("/", "search"),
                ("f", "find all"),
                ("a", "♥like"),
                ("o", "↗track"),
                ("d", "devices"),
                ("r", "reload"),
                ("·", ""),
                ("?", "less"),
                ("q", "quit"),
                ("↑↓/jk", "move"),
                ("g/G", "top/end"),
            ]
        else:
            keys = [
                ("⏎", "play"),
                ("space", "pause"),
                ("n/p", "skip"),
                ("/", "search"),
                ("f", "find all"),
                ("?", "more"),
                ("q", "quit"),
            ]
        # active-filter marker, pinned right; keys never draw under it
        kw = w
        if self.query:
            mark = f" ✂ {self.query} ({len(self.visible_tracks())}) "
            kw = max(0, w - len(mark) - 1)
            put(self.scr, y, kw, mark, Pal.c(Pal.YELLOW))
        x = 1
        for k, label in keys:
            if k == "·":  # group divider, not a key — dim, no chip
                if x + 3 > kw:
                    break
                put(self.scr, y, x, "·  ", Pal.c(Pal.DIM))
                x += 3
                continue
            if x + len(k) + len(label) + 4 > kw:
                break
            put(self.scr, y, x, f" {k} ", Pal.c(Pal.SEL, bold=True))
            put(self.scr, y, x + len(k) + 2, f"{label}  ", Pal.c(Pal.DIM))
            x += len(k) + len(label) + 5

    def draw_devices(self, h, w):
        bw, bh = min(50, w - 4), min(len(self.device_list) + 4, h - 4)
        x0, y0 = (w - bw) // 2, (h - bh) // 2
        win = self.scr
        for i in range(bh):
            put(win, y0 + i, x0, " " * bw, Pal.c(Pal.HEAD))
        put(win, y0, x0, "╔" + "═" * (bw - 2) + "╗", Pal.c(Pal.YELLOW, bold=True))
        put(
            win,
            y0 + bh - 1,
            x0,
            "╚" + "═" * (bw - 2) + "╝",
            Pal.c(Pal.YELLOW, bold=True),
        )
        for i in range(1, bh - 1):
            put(win, y0 + i, x0, "║", Pal.c(Pal.YELLOW, bold=True))
            put(win, y0 + i, x0 + bw - 1, "║", Pal.c(Pal.YELLOW, bold=True))
        put(
            win,
            y0,
            x0 + 3,
            "[ ◈ SPOTIFY CONNECT DEVICES ]",
            Pal.c(Pal.YELLOW, bold=True),
        )
        if self.device_loading and not self.device_list:
            spin = SPINNER[self.spin % len(SPINNER)]
            put(win, y0 + 2, x0 + 3, f"{spin} scanning devices…", Pal.c(Pal.GREEN))
        elif not self.device_list:
            put(
                win,
                y0 + 2,
                x0 + 3,
                "none found — open Spotify on a device",
                Pal.c(Pal.DIM),
            )
        for i, d in enumerate(self.device_list[: bh - 4]):
            mark = "●" if d.get("is_active") else "○"
            line = f"{mark} {d['name']}  ({d['type'].lower()})"
            attr = (
                Pal.c(Pal.SEL, bold=True) if i == self.device_sel else Pal.c(Pal.BRIGHT)
            )
            put(win, y0 + 2 + i, x0 + 3, clip(line, bw - 6), attr)
        put(win, y0 + bh - 1, x0 + 3, "[ ⏎ select · Esc close ]", Pal.c(Pal.DIM))

    # -- input --
    def handle(self, ch):
        if ch == -1:
            return True
        if ch in (curses.KEY_RESIZE, 12):  # resize or Ctrl-L → full repaint
            self.scr.clear()
            return True
        if self.device_mode:
            return self.handle_devices(ch)
        if self.searching:
            return self.handle_search(ch)
        if self.global_searching:
            return self.handle_global(ch)
        # any real keystroke means the user has taken the wheel — stop the
        # pending launch restore from yanking the cursor out from under them
        self._restored_saved = True
        tracks = self.visible_tracks()
        if ch in (ord("q"),):
            return False
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel -= 1
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel += 1
        elif ch == curses.KEY_PPAGE:
            self.sel -= 15
        elif ch == curses.KEY_NPAGE:
            self.sel += 15
        elif ch == ord("g"):
            self.sel = 0
        elif ch == ord("G"):
            self.sel = len(tracks) - 1
        elif ch in (curses.KEY_ENTER, 10, 13):
            if tracks:
                self.p.play_from(self.sel, tracks)
        elif ch == ord(" "):
            self.p.toggle_pause()
        elif ch == ord("n"):
            self.p.next()
        elif ch == ord("p"):
            self.p.prev()
        elif ch in (ord("+"), ord("=")):
            self.p.volume(+10)
        elif ch == ord("-"):
            self.p.volume(-10)
        elif ch == ord("s"):
            self.p.shuffle_toggle()
        elif ch == ord("a"):
            if tracks:
                self.p.toggle_like(tracks[self.sel])
        elif ch == ord("o"):
            if tracks:
                # spotify:track:ID → https://open.spotify.com/track/ID
                t = tracks[self.sel]
                tid = t["uri"].rsplit(":", 1)[-1]
                url = f"https://open.spotify.com/track/{tid}"
                # the launch is invisible for ~1s; name the track so the
                # flash connects the keypress to what it acted on (#3, #10)
                self.p.flag_notice(f"opening {clip(t['name'], 40)} in browser…")
                # browser launch can block — never on the UI thread
                threading.Thread(target=open_url, args=(url,), daemon=True).start()
        elif ch == ord("/"):
            self.searching = True
        elif ch == ord("f"):
            self.global_searching = True
            self.global_query = ""
        elif ch == ord("?"):
            self.all_keys = not self.all_keys
        elif ch == 27:  # Esc clears search filter, then global results
            if self.query:
                self.query = ""
            else:
                self.global_query = ""
                self.p.clear_search()
        elif ch == ord("d"):
            # open instantly; fetch in background — sp.devices() is a
            # network call and must never run on the UI thread
            self.device_sel = 0
            self.device_mode = True
            if not self.device_loading:
                self.device_loading = True

                def fetch():
                    self.device_list = self.p.devices()
                    self.device_loading = False

                threading.Thread(target=fetch, daemon=True).start()
        elif ch == ord("r"):
            # keep the cursor on the same track even if reload reorders the
            # list (e.g. a freshly liked song shifts everything down)
            self._pending_uri = self.selected_uri()
            with self.p.lock:
                self.p.loading = True
            threading.Thread(target=self.p.load_likes, daemon=True).start()
        self.sel = max(0, min(self.sel, max(0, len(tracks) - 1)))
        return True

    def handle_search(self, ch):
        if ch == 27:
            self.searching, self.query = False, ""
        elif ch in (curses.KEY_ENTER, 10, 13):
            self.searching = False
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
        elif ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            # browse results without leaving the search box (fzf-style)
            step = {
                curses.KEY_UP: -1,
                curses.KEY_DOWN: 1,
                curses.KEY_PPAGE: -15,
                curses.KEY_NPAGE: 15,
            }[ch]
            last = max(0, len(self.visible_tracks()) - 1)
            self.sel = max(0, min(self.sel + step, last))
        elif 32 <= ch < 256:
            self.query += chr(ch)
            self.sel, self.top = 0, 0
        return True

    def handle_global(self, ch):
        if ch == 27:
            self.global_searching, self.global_query = False, ""
        elif ch in (curses.KEY_ENTER, 10, 13):
            self.global_searching = False
            if self.global_query.strip():
                self.query = ""  # results arrive unfiltered
                self.sel, self.top = 0, 0
                self.p.search(self.global_query)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.global_query = self.global_query[:-1]
        elif 32 <= ch < 256:
            self.global_query += chr(ch)
            self.sel, self.top = 0, 0
        return True

    def handle_devices(self, ch):
        if ch == 27 or ch == ord("d"):
            self.device_mode = False
        elif ch in (curses.KEY_UP, ord("k")):
            self.device_sel = max(0, self.device_sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.device_sel = min(
                max(0, len(self.device_list) - 1), self.device_sel + 1
            )
        elif ch in (curses.KEY_ENTER, 10, 13) and self.device_list:
            self.p.transfer(self.device_list[self.device_sel])
            self.device_mode = False
        return True

    # -- loop --
    def run(self):
        curses.curs_set(0)
        try:
            curses.set_escdelay(25)  # default 1000ms makes Esc feel broken
        except AttributeError:
            pass
        self.scr.timeout(50)  # background updates surface within ~50ms
        Pal.init()
        running = True
        while running:
            self.spin += 1
            self.draw()
            ch = self.scr.getch()
            running = self.handle(ch)
            # drain any buffered keys (held arrows, fast typing) so we
            # redraw once per batch instead of once per keystroke
            if ch != -1:
                self.scr.nodelay(True)
                while running:
                    ch = self.scr.getch()
                    if ch == -1:
                        break
                    running = self.handle(ch)
                self.scr.timeout(50)
            # once per batch (not per keystroke) so held arrows = one write
            self._persist_position()


def setup_logging():
    """Route all logging (ours + spotipy/urllib3 + warnings) to a file.

    Nothing goes to stderr, so the curses screen stays clean (stray stderr
    text used to leave a ghost now-playing panel after NO_ACTIVE_DEVICE).
    The file at LOG_FILE is the place to look — or to hand to an agent —
    after a crash; it rotates so it never grows without bound.
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=512_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return  # never let logging setup stop the app from running
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()  # drop any default/stderr handler
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)  # warnings.warn() → py.warnings logger → file
    try:
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass

    def _log_uncaught(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _log_uncaught
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: log.critical(
            "uncaught exception in thread %s",
            a.thread.name if a.thread else "?",
            exc_info=(a.exc_type, a.exc_value, a.exc_traceback),
        )


def main():
    setup_logging()
    log.info("starting LEGO Radio")
    cfg = load_config()
    if not cfg.get("client_id"):
        cfg = first_run_wizard()

    print(f"\n  {G}{SPINNER[0]} connecting to Spotify…{X}")
    print(f"  {D}(a browser window may open for you to approve access){X}\n")
    try:
        sp = get_spotify(cfg)
        me = sp.current_user()
    except Exception as e:
        log.exception("auth failed")
        print(f"  {R}✗ auth failed: {e}{X}")
        print(
            f"  {D}check your Client ID and the Redirect URI "
            f"({REDIRECT_URI}) in the Spotify dashboard.{X}"
        )
        sys.exit(1)
    log.info("authenticated as %s", me.get("display_name") or me.get("id"))
    print(
        f"  {G}✓ hello, {B}{me.get('display_name') or me['id']}{X}{G} — building bricks…{X}"
    )

    player = Player(sp)
    threading.Thread(target=player.load_likes, daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=player.poll_loop, args=(stop,), daemon=True).start()

    saved_uri = load_state().get("sel_uri")
    try:
        # cursor position is saved continuously as it moves (App._persist_position)
        curses.wrapper(lambda scr: App(scr, player, saved_uri=saved_uri).run())
    except KeyboardInterrupt:
        pass  # Ctrl-C quits like [q] — curses.wrapper already restored the tty
    except Exception:
        # curses.wrapper has restored the tty; record the crash for later
        log.exception("crashed in the curses loop")
        print(f"  {R}✗ crashed — see {LOG_FILE}{X}")
        raise
    finally:
        stop.set()
        log.info("exiting")
        print(f"{Y}  ▘▘ ▘▘ ▘▘  thanks for listening — LEGO RADIO  ▘▘ ▘▘ ▘▘{X}")


if __name__ == "__main__":
    main()
