#!/usr/bin/env python3
"""Unit tests for LEGO Radio — run with: .venv/bin/python -m unittest -v

No external test deps (stdlib unittest). The Spotify client is replaced by
FakeSP; curses is never initialized (draw paths are covered by the pty smoke
test, see CLAUDE.md). Command threads are real — tests synchronize on their
observable effects with wait_until().
"""

import curses
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import lego_radio as lr

# load_likes()/_persist_position() would otherwise write the user's real
# cache and cursor state; redirect both to a throwaway dir for the whole module
_module_tmp = tempfile.TemporaryDirectory()
_cache_patch = mock.patch.object(
    lr, "LIKES_CACHE_FILE", os.path.join(_module_tmp.name, "likes_cache.json")
)
_state_patch = mock.patch.object(
    lr, "STATE_FILE", os.path.join(_module_tmp.name, "state.json")
)


def setUpModule():
    _cache_patch.start()
    _state_patch.start()


def tearDownModule():
    _cache_patch.stop()
    _state_patch.stop()
    _module_tmp.cleanup()


def wait_until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def mk_track(i, name=None, artist=None, ms=200_000):
    return {
        "uri": f"spotify:track:{i:022d}",
        "name": name or f"Song {i}",
        "artists": [{"name": artist or f"Artist {i}"}],
        "album": {"name": f"Album {i}"},
        "duration_ms": ms,
    }


class FakeSP:
    """Records every call; raises on demand; serves canned data."""

    def __init__(self, pages=None, playback=None, devices=None):
        self.calls = []
        self.pages = pages or [{"items": [], "next": None}]
        self.playback = playback
        self.device_payload = {"devices": devices or []}
        self.raises = {}  # method name -> Exception to raise
        self.devices_gate = None  # threading.Event to block devices()
        self.search_gate = None  # threading.Event to block search()
        self.search_items = []  # full result list; search() slices pages
        self.likes_gate = None  # threading.Event to block saved_tracks()

    def _hit(self, name, **kw):
        self.calls.append((name, kw))
        if name in self.raises:
            raise self.raises[name]

    def calls_to(self, name):
        return [kw for n, kw in self.calls if n == name]

    # -- spotipy surface used by the app --
    def current_user_saved_tracks(self, limit=50, offset=0):
        if self.likes_gate is not None:
            self.likes_gate.wait(3)
        self._hit("current_user_saved_tracks", limit=limit, offset=offset)
        return self.pages[min(offset // 50, len(self.pages) - 1)]

    def current_playback(self):
        self._hit("current_playback")
        return self.playback

    def start_playback(self, device_id=None, uris=None):
        self._hit("start_playback", device_id=device_id, uris=uris)

    def pause_playback(self):
        self._hit("pause_playback")

    def next_track(self):
        self._hit("next_track")

    def previous_track(self):
        self._hit("previous_track")

    def volume(self, volume_percent):
        self._hit("volume", volume_percent=volume_percent)

    def shuffle(self, state, device_id=None):
        self._hit("shuffle", state=state, device_id=device_id)

    def transfer_playback(self, device_id, force_play=False):
        self._hit("transfer_playback", device_id=device_id, force_play=force_play)

    def current_user_saved_tracks_add(self, tracks):
        self._hit("current_user_saved_tracks_add", tracks=tracks)

    def current_user_saved_tracks_delete(self, tracks):
        self._hit("current_user_saved_tracks_delete", tracks=tracks)

    def devices(self):
        if self.devices_gate is not None:
            self.devices_gate.wait(3)
        self._hit("devices")
        return self.device_payload

    def search(self, q, type="track", limit=10, offset=0):
        if self.search_gate is not None:
            self.search_gate.wait(3)
        self._hit("search", q=q, type=type, limit=limit, offset=offset)
        # dev-mode API rejects limit > 10 with 400 Invalid limit
        if limit > 10:
            raise RuntimeError("http status: 400 ... Invalid limit")
        return {"tracks": {"items": self.search_items[offset : offset + limit]}}


def playing_state(
    uri="spotify:track:x", is_playing=True, shuffle=False, volume=70, progress=60_000
):
    return {
        "is_playing": is_playing,
        "shuffle_state": shuffle,
        "progress_ms": progress,
        "item": {
            "uri": uri,
            "name": "X",
            "duration_ms": 200_000,
            "artists": [{"name": "A"}],
        },
        "device": {
            "id": "dev1",
            "name": "MacBook",
            "type": "Computer",
            "volume_percent": volume,
        },
    }


# ───────────────────────────── helpers ─────────────────────────────


class TestHelpers(unittest.TestCase):
    def test_fold_lowercases_and_strips_accents(self):
        self.assertEqual(lr.fold("Café ÉLÄN"), "cafe elan")
        self.assertEqual(lr.fold("l'école"), "l'ecole")

    def test_fmt_ms(self):
        self.assertEqual(lr.fmt_ms(0), "0:00")
        self.assertEqual(lr.fmt_ms(59_999), "0:59")
        self.assertEqual(lr.fmt_ms(60_000), "1:00")
        self.assertEqual(lr.fmt_ms(898_000), "14:58")

    def test_clip(self):
        self.assertEqual(lr.clip("abc", 3), "abc")
        self.assertEqual(lr.clip("abcd", 3), "ab…")
        self.assertEqual(lr.clip("abc", 0), "")
        self.assertEqual(lr.clip("abc", -1), "")

    def test_put_never_raises_outside_window(self):
        class FakeWin:
            def getmaxyx(self):
                return (5, 10)

            def addnstr(self, y, x, s, n, attr=0):
                assert 0 <= y < 5 and 0 <= x < 10 and n <= 10 - x - 1

        win = FakeWin()
        lr.put(win, 2, 3, "hello world", 0)  # clipped, ok
        lr.put(win, -1, 0, "off top", 0)  # ignored
        lr.put(win, 9, 0, "off bottom", 0)  # ignored
        lr.put(win, 2, 99, "off right", 0)  # ignored


class TestConfig(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        with mock.patch.object(lr, "CONFIG_FILE", "/nonexistent/nope.json"):
            self.assertEqual(lr.load_config(), {})

    def test_save_load_roundtrip_with_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = os.path.join(tmp, "config.json")
            with (
                mock.patch.object(lr, "CONFIG_DIR", tmp),
                mock.patch.object(lr, "CONFIG_FILE", cfg_file),
            ):
                lr.save_config({"client_id": "abc"})
                self.assertEqual(lr.load_config(), {"client_id": "abc"})
                self.assertEqual(os.stat(cfg_file).st_mode & 0o777, 0o600)


class TestLikesCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cache = os.path.join(self.tmp.name, "likes_cache.json")
        patcher = mock.patch.object(lr, "LIKES_CACHE_FILE", cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache_file = cache

    def test_successful_load_writes_cache(self):
        sp = FakeSP(
            pages=[{"items": [{"track": mk_track(i)} for i in range(2)], "total": 2}]
        )
        p = lr.Player(sp)
        p.load_likes()
        with open(self.cache_file) as f:
            cached = json.load(f)
        self.assertEqual(cached, p.tracks)
        self.assertEqual(os.stat(self.cache_file).st_mode & 0o777, 0o600)

    def test_cached_likes_show_before_network_completes(self):
        cached = [
            {
                "uri": f"u{i}",
                "name": f"C{i}",
                "artist": "A",
                "album": "",
                "ms": 1000,
                "key": f"c{i} a",
            }
            for i in range(5)
        ]
        with open(self.cache_file, "w") as f:
            json.dump(cached, f)
        sp = FakeSP(pages=[{"items": [{"track": mk_track(0)}], "total": 1}])
        sp.likes_gate = threading.Event()  # network blocked
        p = lr.Player(sp)
        t = threading.Thread(target=p.load_likes, daemon=True)
        t.start()
        # cache visible while the network is still hanging
        self.assertTrue(wait_until(lambda: len(p.tracks) == 5))
        self.assertEqual(p.tracks[0]["name"], "C0")
        sp.likes_gate.set()  # refresh lands, swaps in
        t.join(3)
        self.assertEqual([t_["name"] for t_ in p.tracks], ["Song 0"])
        self.assertFalse(p.loading)

    def test_fetch_failure_keeps_cached_likes(self):
        cached = [
            {
                "uri": "u1",
                "name": "C1",
                "artist": "A",
                "album": "",
                "ms": 1000,
                "key": "c1 a",
            }
        ]
        with open(self.cache_file, "w") as f:
            json.dump(cached, f)
        sp = FakeSP()
        sp.raises["current_user_saved_tracks"] = RuntimeError("offline")
        p = lr.Player(sp)
        p.load_likes()
        self.assertEqual([t["name"] for t in p.tracks], ["C1"])
        self.assertIn("likes", p.error)
        self.assertFalse(p.loading)

    def test_corrupt_cache_is_ignored(self):
        with open(self.cache_file, "w") as f:
            f.write("{not json")
        sp = FakeSP(pages=[{"items": [{"track": mk_track(0)}], "total": 1}])
        p = lr.Player(sp)
        p.load_likes()
        self.assertEqual([t["name"] for t in p.tracks], ["Song 0"])
        self.assertEqual(p.error, "")


# ───────────────────────────── Player ─────────────────────────────


class TestLoadLikes(unittest.TestCase):
    def test_paginates_and_precomputes_search_key(self):
        pages = [
            {
                "items": [{"track": mk_track(i, name=f"Sönг {i}")} for i in range(50)],
                "total": 51,
            },
            {
                "items": [
                    {
                        "track": mk_track(
                            50, name="Pour louper l'école", artist="Aldebert"
                        )
                    }
                ],
                "total": 51,
            },
        ]
        p = lr.Player(FakeSP(pages=pages))
        p.load_likes()
        self.assertFalse(p.loading)
        self.assertEqual(len(p.tracks), 51)
        self.assertEqual(p.load_count, 51)
        self.assertIn("l'ecole", p.tracks[50]["key"])  # folded
        self.assertIn("aldebert", p.tracks[50]["key"])  # artist searchable

    def test_remaining_pages_fetched_concurrently_in_order(self):
        pages = [
            {
                "items": [{"track": mk_track(50 * pg + i)} for i in range(50)],
                "total": 150,
            }
            for pg in range(3)
        ]
        p = lr.Player(FakeSP(pages=pages))
        p.load_likes()
        # all offsets requested, results stitched back in liked order
        offsets = sorted(
            kw["offset"] for kw in p.sp.calls_to("current_user_saved_tracks")
        )
        self.assertEqual(offsets, [0, 50, 100])
        self.assertEqual(len(p.tracks), 150)
        self.assertEqual(
            [t["uri"] for t in p.tracks],
            [f"spotify:track:{i:022d}" for i in range(150)],
        )

    def test_skips_null_tracks(self):
        pages = [{"items": [{"track": None}, {"track": mk_track(1)}], "total": 2}]
        p = lr.Player(FakeSP(pages=pages))
        p.load_likes()
        self.assertEqual(len(p.tracks), 1)

    def test_error_flags_and_clears_loading(self):
        sp = FakeSP()
        sp.raises["current_user_saved_tracks"] = RuntimeError("boom")
        p = lr.Player(sp)
        p.load_likes()
        self.assertFalse(p.loading)
        self.assertIn("boom", p.error)


class TestPlayFrom(unittest.TestCase):
    def setUp(self):
        self.sp = FakeSP(playback=playing_state())
        self.p = lr.Player(self.sp)
        self.p.playback = self.sp.playback
        self.tracks = [
            {
                "uri": f"spotify:track:{i}",
                "name": f"S{i}",
                "artist": "A",
                "album": "",
                "ms": 1000,
                "key": f"s{i} a",
            }
            for i in range(120)
        ]

    def test_sends_chunk_of_100_from_selection(self):
        self.p.play_from(5, self.tracks)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("start_playback")))
        kw = self.sp.calls_to("start_playback")[0]
        self.assertEqual(len(kw["uris"]), 100)
        self.assertEqual(kw["uris"][0], "spotify:track:5")
        self.assertEqual(kw["device_id"], "dev1")

    def test_disables_shuffle_first_when_shuffle_on(self):
        self.p.playback = playing_state(shuffle=True)
        self.p.play_from(0, self.tracks)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("start_playback")))
        names = [n for n, _ in self.sp.calls]
        self.assertIn("shuffle", names)
        self.assertLess(names.index("shuffle"), names.index("start_playback"))
        self.assertEqual(self.sp.calls_to("shuffle")[0]["state"], False)

    def test_no_shuffle_call_when_shuffle_off(self):
        self.p.play_from(0, self.tracks)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("start_playback")))
        self.assertEqual(self.sp.calls_to("shuffle"), [])

    def test_empty_list_is_noop(self):
        self.p.play_from(0, [])
        time.sleep(0.05)
        self.assertEqual(self.sp.calls, [])

    def test_play_is_optimistic_same_frame(self):
        self.p.play_from(7, self.tracks)
        # before the API round-trip: selected track already shown as playing
        item = self.p.playback["item"]
        self.assertEqual(item["uri"], "spotify:track:7")
        self.assertEqual(item["name"], "S7")
        self.assertTrue(self.p.playback["is_playing"])
        self.assertEqual(self.p.playback["progress_ms"], 0)
        # device info survives so the status bar doesn't flicker
        self.assertEqual(self.p.playback["device"]["id"], "dev1")
        self.assertTrue(wait_until(lambda: self.sp.calls_to("start_playback")))

    def test_falls_back_to_first_known_device(self):
        self.p.playback = None
        self.sp.device_payload = {
            "devices": [{"id": "devX", "name": "Phone", "type": "Smartphone"}]
        }
        self.p.play_from(0, self.tracks)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("start_playback")))
        self.assertEqual(self.sp.calls_to("start_playback")[0]["device_id"], "devX")


class TestPlaybackCommands(unittest.TestCase):
    def setUp(self):
        self.sp = FakeSP(playback=playing_state())
        self.p = lr.Player(self.sp)
        self.p.playback = playing_state()

    def test_toggle_pause_optimistic_and_calls_pause(self):
        self.p.toggle_pause()
        self.assertFalse(self.p.playback["is_playing"])  # same frame
        self.assertTrue(wait_until(lambda: self.sp.calls_to("pause_playback")))

    def test_toggle_resume_optimistic_and_calls_start(self):
        self.p.playback = playing_state(is_playing=False)
        self.p.toggle_pause()
        self.assertTrue(self.p.playback["is_playing"])
        self.assertTrue(wait_until(lambda: self.sp.calls_to("start_playback")))

    def test_volume_clamps_and_is_optimistic(self):
        self.p.playback = playing_state(volume=95)
        self.p.volume(+10)
        self.assertEqual(self.p.playback["device"]["volume_percent"], 100)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("volume")))
        self.assertEqual(self.sp.calls_to("volume")[0]["volume_percent"], 100)

    def test_volume_defaults_to_50_when_unknown(self):
        self.p.playback = playing_state(volume=None)
        self.p.volume(+10)
        self.assertEqual(self.p.playback["device"]["volume_percent"], 60)

    def test_volume_noop_without_playback(self):
        self.p.playback = None
        self.p.volume(+10)
        time.sleep(0.05)
        self.assertEqual(self.sp.calls_to("volume"), [])

    def test_shuffle_toggle_optimistic_flip(self):
        self.p.shuffle_toggle()
        self.assertTrue(self.p.playback["shuffle_state"])
        self.assertTrue(wait_until(lambda: self.sp.calls_to("shuffle")))
        self.assertEqual(self.sp.calls_to("shuffle")[0]["state"], True)

    def test_next_resets_progress_optimistically(self):
        self.p.next()
        self.assertEqual(self.p.playback["progress_ms"], 0)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("next_track")))

    def test_prev_resets_progress_optimistically(self):
        self.p.prev()
        self.assertEqual(self.p.playback["progress_ms"], 0)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("previous_track")))

    def test_transfer_optimistically_renames_device(self):
        self.p.transfer({"id": "devB", "name": "Speaker", "type": "Speaker"})
        self.assertEqual(self.p.playback["device"]["name"], "Speaker")
        self.assertTrue(wait_until(lambda: self.sp.calls_to("transfer_playback")))
        kw = self.sp.calls_to("transfer_playback")[0]
        self.assertEqual(kw, {"device_id": "devB", "force_play": True})

    def test_cmd_translates_no_active_device_without_opening_browser(self):
        # the app must never launch a browser on its own — it points the
        # user at [o]/[d] and lets them decide (trust beats magic)
        self.sp.raises["next_track"] = RuntimeError("NO_ACTIVE_DEVICE found")
        with mock.patch.object(lr, "open_url") as opener:
            self.p.next()
            self.assertTrue(wait_until(lambda: "no active device" in self.p.error))
        self.assertIn("[o]", self.p.error)
        opener.assert_not_called()

    def test_cmd_failure_reconciles_with_a_poll(self):
        # optimistic flip must be reverted promptly when the command fails,
        # not left wrong until the next 2.5s poll tick
        self.sp.raises["pause_playback"] = RuntimeError("boom")
        self.p.toggle_pause()  # optimistic flip to paused happens same-frame
        self.assertTrue(wait_until(lambda: self.sp.calls_to("current_playback")))
        self.assertTrue(wait_until(lambda: self.p.playback["is_playing"]))

    def test_cmd_translates_premium_required(self):
        self.sp.raises["pause_playback"] = RuntimeError("PREMIUM_REQUIRED")
        self.p.toggle_pause()
        self.assertTrue(wait_until(lambda: "Premium" in self.p.error))

    def test_devices_error_returns_empty_and_flags(self):
        self.sp.raises["devices"] = RuntimeError("nope")
        self.assertEqual(self.p.devices(), [])
        self.assertIn("devices", self.p.error)

    def test_poll_updates_playback_and_ts(self):
        self.p.playback, self.p.poll_ts = None, 0.0
        self.p.poll()
        self.assertTrue(self.p.playback["is_playing"])
        self.assertGreater(self.p.poll_ts, 0.0)


# ───────────────────────────── App input ─────────────────────────────


KEY_ENTER, KEY_ESC = 10, 27


class AppTestBase(unittest.TestCase):
    def make_app(self, n_tracks=30, playback=None, devices=None):
        self.sp = FakeSP(playback=playback, devices=devices)
        self.p = lr.Player(self.sp)
        self.p.loading = False
        self.p.playback = playback
        # ö/Ä are NFD-decomposable so fold() strips them (ø would not be)
        self.p.tracks = [
            {
                "uri": f"spotify:track:{i}",
                "name": f"Söng {i}",
                "artist": f"Ärtist {i}",
                "album": "",
                "ms": 1000,
                "key": lr.fold(f"Söng {i} Ärtist {i}"),
            }
            for i in range(n_tracks)
        ]
        return lr.App(None, self.p)  # scr unused outside draw paths


class TestNavigation(AppTestBase):
    def test_down_up_and_clamping(self):
        import curses

        app = self.make_app(5)
        app.handle(curses.KEY_UP)
        self.assertEqual(app.sel, 0)  # clamped at top
        for _ in range(10):
            app.handle(curses.KEY_DOWN)
        self.assertEqual(app.sel, 4)  # clamped at bottom
        app.handle(ord("g"))
        self.assertEqual(app.sel, 0)
        app.handle(ord("G"))
        self.assertEqual(app.sel, 4)

    def test_page_keys_move_by_15(self):
        import curses

        app = self.make_app(40)
        app.handle(curses.KEY_NPAGE)
        self.assertEqual(app.sel, 15)
        app.handle(curses.KEY_PPAGE)
        self.assertEqual(app.sel, 0)

    def test_q_quits_others_keep_running(self):
        app = self.make_app()
        self.assertTrue(app.handle(ord("x")))
        self.assertTrue(app.handle(-1))
        self.assertFalse(app.handle(ord("q")))

    def test_question_mark_toggles_footer(self):
        app = self.make_app()
        self.assertFalse(app.all_keys)
        app.handle(ord("?"))
        self.assertTrue(app.all_keys)


class TestSearch(AppTestBase):
    def type_query(self, app, q):
        app.handle(ord("/"))
        for c in q:
            app.handle(ord(c))

    def test_filter_is_accent_and_case_insensitive(self):
        app = self.make_app(12)
        self.type_query(app, "song 1")
        names = [t["name"] for t in app.visible_tracks()]
        self.assertEqual(names, ["Söng 1", "Söng 10", "Söng 11"])

    def test_filter_matches_artist(self):
        app = self.make_app(12)
        self.type_query(app, "artist 11")
        self.assertEqual([t["name"] for t in app.visible_tracks()], ["Söng 11"])

    def test_typing_resets_selection(self):
        app = self.make_app(12)
        app.sel = 9
        self.type_query(app, "s")
        self.assertEqual(app.sel, 0)

    def test_enter_keeps_filter_esc_clears(self):
        app = self.make_app(12)
        self.type_query(app, "song 1")
        app.handle(KEY_ENTER)
        self.assertFalse(app.searching)
        self.assertEqual(app.query, "song 1")
        app.handle(KEY_ESC)  # normal-mode Esc clears
        self.assertEqual(app.query, "")

    def test_backspace_and_esc_inside_search(self):
        app = self.make_app(12)
        self.type_query(app, "ab")
        app.handle(127)
        self.assertEqual(app.query, "a")
        app.handle(KEY_ESC)
        self.assertFalse(app.searching)
        self.assertEqual(app.query, "")

    def test_arrows_browse_filtered_list_while_searching(self):
        app = self.make_app(12)
        self.type_query(app, "song 1")  # matches Söng 1, 10, 11
        app.handle(curses.KEY_DOWN)
        self.assertEqual(app.sel, 1)
        app.handle(curses.KEY_DOWN)
        app.handle(curses.KEY_DOWN)  # clamps at last match (idx 2)
        self.assertEqual(app.sel, 2)
        app.handle(curses.KEY_UP)
        self.assertEqual(app.sel, 1)
        self.assertTrue(app.searching)  # still in search mode
        app.handle(curses.KEY_PPAGE)  # page keys clamp too
        self.assertEqual(app.sel, 0)
        app.handle(curses.KEY_NPAGE)
        self.assertEqual(app.sel, 2)

    def test_filter_result_is_memoized_per_query_and_tracks(self):
        app = self.make_app(12)
        self.type_query(app, "song 1")
        first = app.visible_tracks()
        self.assertIs(app.visible_tracks(), first)  # same query → cached
        app.handle(ord("1"))  # query changed
        self.assertIsNot(app.visible_tracks(), first)
        with self.p.lock:  # loader replaces list
            self.p.tracks = list(self.p.tracks)
        stale = app.visible_tracks()
        self.assertIsNot(stale, first)
        self.assertEqual([t["name"] for t in stale], ["Söng 11"])

    def test_filter_narrows_from_previous_results_when_query_extends(self):
        class PoisonKey:
            def __contains__(self, _needle):
                raise AssertionError("extended search scanned unrelated tracks")

        app = self.make_app(120)
        app.query = "song 9"
        previous = app.visible_tracks()
        previous_uris = {t["uri"] for t in previous}

        # Tracks that did not match "song 9" cannot match "song 99" either.
        # If the extended query scans them again, this test fails.
        for track in self.p.tracks:
            if track["uri"] not in previous_uris:
                track["key"] = PoisonKey()

        app.query = "song 99"
        self.assertEqual([t["name"] for t in app.visible_tracks()], ["Söng 99"])

    def test_enter_plays_from_filtered_list(self):
        app = self.make_app(12)
        played = []
        self.p.play_from = lambda idx, lst: played.append((idx, len(lst)))
        self.type_query(app, "song 1")
        app.handle(KEY_ENTER)  # leave search
        app.handle(curses_down())
        app.handle(KEY_ENTER)  # play 2nd filtered track
        self.assertEqual(played, [(1, 3)])


def curses_down():
    import curses

    return curses.KEY_DOWN


class TestDevicePicker(AppTestBase):
    def test_d_opens_instantly_and_loads_in_background(self):
        app = self.make_app(
            devices=[{"id": "d1", "name": "Mac", "type": "Computer", "is_active": True}]
        )
        app.handle(ord("d"))
        self.assertTrue(app.device_mode)  # no waiting for the network
        self.assertTrue(wait_until(lambda: app.device_list))
        self.assertFalse(app.device_loading)
        self.assertEqual(app.device_list[0]["id"], "d1")

    def test_inflight_guard_prevents_duplicate_fetch(self):
        app = self.make_app(devices=[{"id": "d1", "name": "Mac", "type": "Computer"}])
        self.sp.devices_gate = threading.Event()  # block the fetch
        app.handle(ord("d"))  # opens + starts fetch
        app.handle(ord("d"))  # modal toggles closed
        app.handle(ord("d"))  # reopens; fetch in flight
        self.sp.devices_gate.set()
        self.assertTrue(wait_until(lambda: not app.device_loading))
        self.assertEqual(len(self.sp.calls_to("devices")), 1)

    def test_enter_transfers_selected_device_and_closes(self):
        app = self.make_app(
            playback=playing_state(),
            devices=[
                {"id": "d1", "name": "Mac", "type": "Computer"},
                {"id": "d2", "name": "Phone", "type": "Smartphone"},
            ],
        )
        self.p.playback = playing_state()
        app.handle(ord("d"))
        self.assertTrue(wait_until(lambda: len(app.device_list) == 2))
        app.handle(curses_down())
        app.handle(KEY_ENTER)
        self.assertFalse(app.device_mode)
        self.assertTrue(wait_until(lambda: self.sp.calls_to("transfer_playback")))
        self.assertEqual(self.sp.calls_to("transfer_playback")[0]["device_id"], "d2")

    def test_esc_closes_without_transfer(self):
        app = self.make_app(devices=[{"id": "d1", "name": "Mac", "type": "Computer"}])
        app.handle(ord("d"))
        app.handle(KEY_ESC)
        self.assertFalse(app.device_mode)
        time.sleep(0.05)
        self.assertEqual(self.sp.calls_to("transfer_playback"), [])

    def test_device_navigation_clamps(self):
        app = self.make_app()
        app.device_mode = True
        app.device_list = [{"id": "a"}, {"id": "b"}]
        for _ in range(5):
            app.handle(curses_down())
        self.assertEqual(app.device_sel, 1)
        import curses

        for _ in range(5):
            app.handle(curses.KEY_UP)
        self.assertEqual(app.device_sel, 0)


class TestReload(AppTestBase):
    def test_r_reloads_likes_in_background(self):
        app = self.make_app(n_tracks=2)
        self.sp.pages = [
            {"items": [{"track": mk_track(i)} for i in range(3)], "total": 3}
        ]
        app.handle(ord("r"))
        self.assertTrue(
            wait_until(lambda: not self.p.loading and len(self.p.tracks) == 3)
        )


class TestToggleLike(AppTestBase):
    def _search_track(self, suffix="new"):
        uri = f"spotify:track:{suffix}"
        return {
            "uri": uri,
            "name": "Fresh Find",
            "artist": "Someone",
            "album": "",
            "ms": 1000,
            "key": lr.fold("Fresh Find Someone"),
        }

    def test_like_adds_unliked_track_to_top(self):
        app = self.make_app(3)
        track = self._search_track()
        self.assertFalse(self.p.is_liked(track["uri"]))
        app.p.toggle_like(track)
        # optimistic: lands on top immediately
        self.assertEqual(self.p.tracks[0]["uri"], "spotify:track:new")
        self.assertIn("♥ added", self.p.notice)
        self.assertTrue(
            wait_until(lambda: self.sp.calls_to("current_user_saved_tracks_add"))
        )
        kw = self.sp.calls_to("current_user_saved_tracks_add")[0]
        self.assertEqual(kw, {"tracks": ["new"]})

    def test_unlike_removes_liked_track(self):
        app = self.make_app(3)
        track = self.p.tracks[1]  # already liked
        app.p.toggle_like(track)
        self.assertNotIn(track["uri"], [t["uri"] for t in self.p.tracks])
        self.assertIn("♡ removed", self.p.notice)
        self.assertTrue(
            wait_until(lambda: self.sp.calls_to("current_user_saved_tracks_delete"))
        )
        self.assertEqual(
            self.sp.calls_to("current_user_saved_tracks_delete")[0], {"tracks": ["1"]}
        )

    def test_failed_like_reverts_optimistic_add(self):
        app = self.make_app(3)
        self.sp.raises["current_user_saved_tracks_add"] = RuntimeError("boom")
        track = self._search_track()
        app.p.toggle_like(track)
        self.assertTrue(wait_until(lambda: "like:" in self.p.error))
        # the optimistic add must be undone
        self.assertNotIn("spotify:track:new", [t["uri"] for t in self.p.tracks])

    def test_failed_unlike_restores_track(self):
        app = self.make_app(3)
        self.sp.raises["current_user_saved_tracks_delete"] = RuntimeError("boom")
        track = self.p.tracks[1]
        app.p.toggle_like(track)
        self.assertTrue(wait_until(lambda: "like:" in self.p.error))
        self.assertIn(track["uri"], [t["uri"] for t in self.p.tracks])

    def test_a_key_toggles_selected_track(self):
        app = self.make_app(3)
        app.sel = 2
        app.handle(ord("a"))
        self.assertTrue(
            wait_until(lambda: self.sp.calls_to("current_user_saved_tracks_delete"))
        )
        self.assertEqual(
            self.sp.calls_to("current_user_saved_tracks_delete")[0], {"tracks": ["2"]}
        )


class TestRestoreCursor(AppTestBase):
    def test_selected_uri_reports_current_selection(self):
        app = self.make_app(5)
        app.sel = 3
        self.assertEqual(app.selected_uri(), "spotify:track:3")

    def test_launch_restores_saved_cursor(self):
        app = self.make_app(5)
        app._saved_uri = "spotify:track:4"
        app._restore_cursor(app.visible_tracks())
        self.assertEqual(app.sel, 4)
        self.assertTrue(app._restored_saved)

    def test_restore_ignores_playback_keeps_scroll_position(self):
        # scroll position wins; a playing track must not move the cursor
        app = self.make_app(5)
        app._saved_uri = "spotify:track:3"
        app.p.playback = {"item": {"uri": "spotify:track:1"}}
        app._restore_cursor(app.visible_tracks())
        self.assertEqual(app.sel, 3)

    def test_keystroke_cancels_pending_launch_restore(self):
        app = self.make_app(5)
        app._saved_uri = "spotify:track:4"
        app.handle(ord("j"))  # user takes over before the list settles
        self.assertTrue(app._restored_saved)
        app.sel = 0
        app._restore_cursor(app.visible_tracks())
        self.assertEqual(app.sel, 0)  # restore no longer fires

    def test_active_filter_suppresses_restore(self):
        app = self.make_app(5)
        app._saved_uri = "spotify:track:4"
        app.query = "söng 2"
        app._restore_cursor(app.visible_tracks())
        self.assertFalse(app._restored_saved)

    def test_reload_reanchors_cursor_by_uri(self):
        app = self.make_app(5)
        app.sel = 2
        app.handle(ord("r"))  # captures spotify:track:2 as pending
        self.assertEqual(app._pending_uri, "spotify:track:2")
        # simulate reload shifting the list: track 2 is now at index 4
        shifted = list(reversed(app.p.tracks))
        app._restore_cursor(shifted)
        self.assertEqual(shifted[app.sel]["uri"], "spotify:track:2")
        self.assertIsNone(app._pending_uri)

    def test_persist_position_saves_only_on_change(self):
        app = self.make_app(5)
        app.sel = 2
        with mock.patch.object(lr, "save_state") as save:
            app._persist_position()
            save.assert_called_once_with({"sel_uri": "spotify:track:2"})
            app._persist_position()  # unchanged → no second write
            save.assert_called_once()
            app.sel = 4
            app._persist_position()
            self.assertEqual(save.call_count, 2)
            self.assertEqual(save.call_args[0][0], {"sel_uri": "spotify:track:4"})


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = os.path.join(self.tmp.name, "state.json")
        patcher = mock.patch.object(lr, "STATE_FILE", state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state_file = state

    def test_roundtrip_with_0600(self):
        lr.save_state({"sel_uri": "spotify:track:abc"})
        self.assertEqual(lr.load_state(), {"sel_uri": "spotify:track:abc"})
        self.assertEqual(os.stat(self.state_file).st_mode & 0o777, 0o600)

    def test_load_missing_returns_empty(self):
        self.assertEqual(lr.load_state(), {})

    def test_load_corrupt_returns_empty(self):
        with open(self.state_file, "w") as f:
            f.write("not json{")
        self.assertEqual(lr.load_state(), {})


class TestPlayerSearch(unittest.TestCase):
    def setUp(self):
        self.sp = FakeSP()
        self.p = lr.Player(self.sp)

    def test_search_queries_api_and_parses_tracks(self):
        self.sp.search_items = [mk_track(1), mk_track(2)]
        self.p.search("daft punk")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        results = self.p.search_results
        self.assertEqual(len(results), 2)
        # same shape as liked tracks, including the precomputed fold key
        self.assertEqual(results[0]["name"], "Song 1")
        self.assertEqual(results[0]["artist"], "Artist 1")
        self.assertIn("song 1 artist 1", results[0]["key"])

    def test_search_paginates_at_limit_10(self):
        # dev-mode apps 400 on limit > 10, so 50 results = 5 pages of 10
        self.sp.search_items = [mk_track(i) for i in range(50)]
        self.p.search("daft punk")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        calls = self.sp.calls_to("search")
        self.assertEqual({c["limit"] for c in calls}, {10})
        self.assertEqual(sorted(c["offset"] for c in calls), [0, 10, 20, 30, 40])
        self.assertEqual({c["q"] for c in calls}, {"daft punk"})
        # pages land in offset order regardless of fetch concurrency
        self.assertEqual(
            [t["name"] for t in self.p.search_results], [f"Song {i}" for i in range(50)]
        )

    def test_search_dedupes_repeats_across_pages(self):
        # Spotify search repeats tracks across adjacent pages; keep first
        self.sp.search_items = [mk_track(i) for i in range(20)] + [
            mk_track(i) for i in range(15, 45)
        ]
        self.p.search("x")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        uris = [t["uri"] for t in self.p.search_results]
        self.assertEqual(len(uris), len(set(uris)))
        self.assertEqual(len(uris), 45)

    def test_search_loading_flag_during_fetch(self):
        self.sp.search_gate = threading.Event()
        self.p.search("x")
        self.assertTrue(self.p.search_loading)
        self.sp.search_gate.set()
        self.assertTrue(wait_until(lambda: not self.p.search_loading))

    def test_search_error_flags_and_clears_loading(self):
        self.sp.raises["search"] = RuntimeError("boom")
        self.p.search("x")
        self.assertTrue(wait_until(lambda: self.p.error))
        self.assertIn("search", self.p.error)
        self.assertFalse(self.p.search_loading)
        self.assertIsNone(self.p.search_results)

    def test_stale_search_cannot_overwrite_newer_results(self):
        slow_gate = threading.Event()

        class SlowOldSP(FakeSP):
            def search(self, q, type="track", limit=10, offset=0):
                if q == "old":
                    slow_gate.wait(3)  # first search hangs
                self.search_items = (
                    [mk_track(1, name="Old")]
                    if q == "old"
                    else [mk_track(2, name="New")]
                )
                return super().search(q, type=type, limit=limit, offset=offset)

        self.sp = SlowOldSP()
        self.p = lr.Player(self.sp)
        self.p.search("old")  # in flight, blocked
        self.p.search("new")  # finishes first
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        self.assertEqual([t["name"] for t in self.p.search_results], ["New"])
        slow_gate.set()  # stale search lands late
        time.sleep(0.2)
        self.assertEqual([t["name"] for t in self.p.search_results], ["New"])
        self.assertFalse(self.p.search_loading)

    def test_clear_search_discards_inflight_results(self):
        self.sp.search_gate = threading.Event()
        self.sp.search_items = [mk_track(1)]
        self.p.search("x")  # blocked in flight
        self.p.clear_search()  # user pressed Esc
        self.sp.search_gate.set()  # response lands after Esc
        self.assertTrue(wait_until(lambda: not self.p.search_loading))
        time.sleep(0.1)
        self.assertIsNone(self.p.search_results)

    def test_clear_search_resets_results(self):
        self.sp.search_items = [mk_track(1)]
        self.p.search("x")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        self.p.clear_search()
        self.assertIsNone(self.p.search_results)


class TestGlobalSearch(AppTestBase):
    def search_for(self, app, q):
        app.handle(ord("f"))
        for c in q:
            app.handle(ord(c))
        app.handle(KEY_ENTER)

    def test_f_opens_global_input_and_typing_builds_query(self):
        app = self.make_app()
        app.handle(ord("f"))
        self.assertTrue(app.global_searching)
        for c in "abba":
            app.handle(ord(c))
        self.assertEqual(app.global_query, "abba")

    def test_enter_fires_api_search_and_shows_results(self):
        app = self.make_app(n_tracks=3)
        self.sp.search_items = [mk_track(90), mk_track(91)]
        self.search_for(app, "abba")
        self.assertFalse(app.global_searching)  # input closes, results stay
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        names = [t["name"] for t in app.visible_tracks()]
        self.assertEqual(names, ["Song 90", "Song 91"])

    def test_enter_with_empty_query_searches_nothing(self):
        app = self.make_app()
        app.handle(ord("f"))
        app.handle(KEY_ENTER)
        time.sleep(0.05)
        self.assertEqual(self.sp.calls_to("search"), [])
        self.assertFalse(app.global_searching)

    def test_typing_resets_selection(self):
        app = self.make_app()
        app.sel = 9
        app.handle(ord("f"))
        app.handle(ord("a"))
        self.assertEqual(app.sel, 0)

    def test_esc_inside_input_cancels_and_keeps_likes(self):
        app = self.make_app(n_tracks=3)
        app.handle(ord("f"))
        app.handle(ord("a"))
        app.handle(KEY_ESC)
        self.assertFalse(app.global_searching)
        self.assertEqual(app.global_query, "")
        self.assertEqual(len(app.visible_tracks()), 3)

    def test_backspace_edits_global_query(self):
        app = self.make_app()
        app.handle(ord("f"))
        for c in "ab":
            app.handle(ord(c))
        app.handle(127)
        self.assertEqual(app.global_query, "a")

    def test_esc_in_normal_mode_clears_results_back_to_likes(self):
        app = self.make_app(n_tracks=3)
        self.sp.search_items = [mk_track(90)]
        self.search_for(app, "abba")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        app.handle(KEY_ESC)
        self.assertIsNone(self.p.search_results)
        self.assertEqual(app.global_query, "")
        self.assertEqual(len(app.visible_tracks()), 3)

    def test_enter_plays_from_results_list(self):
        app = self.make_app()
        self.sp.search_items = [mk_track(90), mk_track(91)]
        played = []
        self.p.play_from = lambda idx, lst: played.append(
            (idx, [t["name"] for t in lst])
        )
        self.search_for(app, "abba")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        app.handle(curses_down())
        app.handle(KEY_ENTER)
        self.assertEqual(played, [(1, ["Song 90", "Song 91"])])

    def test_slash_filter_narrows_results_locally(self):
        app = self.make_app()
        self.sp.search_items = [mk_track(1, name="Alpha"), mk_track(2, name="Beta")]
        self.search_for(app, "abba")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        app.handle(ord("/"))
        for c in "beta":
            app.handle(ord(c))
        self.assertEqual([t["name"] for t in app.visible_tracks()], ["Beta"])

    def test_new_search_replaces_previous_results(self):
        app = self.make_app()
        self.sp.search_items = [mk_track(1)]
        self.search_for(app, "one")
        self.assertTrue(wait_until(lambda: self.p.search_results is not None))
        self.sp.search_items = [mk_track(2), mk_track(3)]
        self.search_for(app, "two")
        self.assertTrue(
            wait_until(
                lambda: self.p.search_results and len(self.p.search_results) == 2
            )
        )
        self.assertEqual(self.sp.calls_to("search")[-1]["q"], "two")


class TestRunLoop(AppTestBase):
    def test_main_loop_uses_low_latency_input_timeout(self):
        class FakeScreen:
            def __init__(self):
                self.timeouts = []

            def timeout(self, ms):
                self.timeouts.append(ms)

            def nodelay(self, _flag):
                pass

            def getch(self):
                return ord("q")

        scr = FakeScreen()
        app = self.make_app()
        app.scr = scr
        app.draw = lambda: None
        with (
            mock.patch.object(lr.curses, "curs_set"),
            mock.patch.object(lr.Pal, "init"),
        ):
            app.run()

        self.assertLessEqual(scr.timeouts[0], 50)


class TestOpenInBrowser(AppTestBase):
    def test_o_opens_selected_track_url(self):
        app = self.make_app(5)
        app.sel = 3
        opened = []
        with mock.patch.object(lr, "open_url", opened.append):
            app.handle(ord("o"))
            self.assertTrue(wait_until(lambda: opened))
        self.assertEqual(opened, ["https://open.spotify.com/track/3"])
        # a flash names the track so the keypress feels answered
        self.assertIn("Söng 3", self.p.notice)

    def test_o_respects_active_filter(self):
        app = self.make_app(5)
        app.query = "söng 2"  # visible_tracks shrinks to one
        opened = []
        with mock.patch.object(lr, "open_url", opened.append):
            app.handle(ord("o"))
            self.assertTrue(wait_until(lambda: opened))
        self.assertEqual(opened, ["https://open.spotify.com/track/2"])

    def test_o_with_no_tracks_is_a_noop(self):
        app = self.make_app(0)
        with mock.patch.object(lr, "open_url") as opener:
            app.handle(ord("o"))
            time.sleep(0.05)
        opener.assert_not_called()

    def test_open_url_prefers_chrome(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(lr.sys, "platform", "darwin"),
            mock.patch.object(lr.subprocess, "run", fake_run),
            mock.patch.object(lr.webbrowser, "open") as default,
        ):
            lr.open_url("https://open.spotify.com/track/x")
        self.assertEqual(calls[0], ["open", "-Ra", "Google Chrome"])
        self.assertEqual(
            calls[1],
            ["open", "-a", "Google Chrome", "https://open.spotify.com/track/x"],
        )
        default.assert_not_called()

    def test_open_url_falls_back_without_chrome(self):
        # `open -Ra` failing means Chrome is not installed
        fake_run = mock.Mock(return_value=mock.Mock(returncode=1))
        with (
            mock.patch.object(lr.sys, "platform", "darwin"),
            mock.patch.object(lr.subprocess, "run", fake_run),
            mock.patch.object(lr.webbrowser, "open") as default,
        ):
            lr.open_url("https://open.spotify.com/track/x")
        default.assert_called_once_with("https://open.spotify.com/track/x")

    def test_open_url_linux_uses_webbrowser_get(self):
        chrome = mock.Mock()
        chrome.open.return_value = True

        def get(name):
            if name == "google-chrome":
                return chrome
            raise lr.webbrowser.Error(name)

        with (
            mock.patch.object(lr.sys, "platform", "linux"),
            mock.patch.object(lr.webbrowser, "get", get),
            mock.patch.object(lr.webbrowser, "open") as default,
        ):
            lr.open_url("https://open.spotify.com/track/x")
        chrome.open.assert_called_once_with("https://open.spotify.com/track/x")
        default.assert_not_called()

    def test_open_url_linux_falls_back_without_chrome(self):
        def get(name):
            raise lr.webbrowser.Error(name)

        with (
            mock.patch.object(lr.sys, "platform", "linux"),
            mock.patch.object(lr.webbrowser, "get", get),
            mock.patch.object(lr.webbrowser, "open") as default,
        ):
            lr.open_url("https://open.spotify.com/track/x")
        default.assert_called_once_with("https://open.spotify.com/track/x")


class TestRedraw(AppTestBase):
    def test_resize_and_ctrl_l_force_full_repaint(self):
        app = self.make_app(3)
        cleared = []

        class FakeScr:
            def clear(self):
                cleared.append(True)

        app.scr = FakeScr()
        self.assertTrue(app.handle(curses.KEY_RESIZE))
        self.assertTrue(app.handle(12))  # Ctrl-L
        self.assertEqual(len(cleared), 2)


class TestLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, val in (
            ("CONFIG_DIR", self.tmp.name),
            ("LOG_FILE", os.path.join(self.tmp.name, "lego_radio.log")),
        ):
            p = mock.patch.object(lr, attr, val)
            p.start()
            self.addCleanup(p.stop)
        # setup_logging mutates global logging + excepthooks; restore them
        root = logging.getLogger()
        saved = (root.handlers[:], root.level, sys.excepthook, threading.excepthook)

        def restore():
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])
            sys.excepthook = saved[2]
            threading.excepthook = saved[3]

        self.addCleanup(restore)

    def test_setup_logging_writes_to_file_not_stderr(self):
        import contextlib
        import io

        lr.setup_logging()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            # spotipy logs every HTTP error like this (client.py:288)
            logging.getLogger("spotipy.client").error("HTTP Error for PUT /x")
            lr.log.error("boom from app")
        self.assertEqual(buf.getvalue(), "")  # nothing corrupts the curses screen
        with open(lr.LOG_FILE, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("HTTP Error for PUT /x", content)
        self.assertIn("boom from app", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
