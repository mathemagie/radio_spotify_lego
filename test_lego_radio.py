#!/usr/bin/env python3
"""Unit tests for LEGO Radio — run with: .venv/bin/python -m unittest -v

No external test deps (stdlib unittest). The Spotify client is replaced by
FakeSP; curses is never initialized (draw paths are covered by the pty smoke
test, see CLAUDE.md). Command threads are real — tests synchronize on their
observable effects with wait_until().
"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import lego_radio as lr


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
        self.raises = {}          # method name -> Exception to raise
        self.devices_gate = None  # threading.Event to block devices()

    def _hit(self, name, **kw):
        self.calls.append((name, kw))
        if name in self.raises:
            raise self.raises[name]

    def calls_to(self, name):
        return [kw for n, kw in self.calls if n == name]

    # -- spotipy surface used by the app --
    def current_user_saved_tracks(self, limit=50, offset=0):
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

    def devices(self):
        if self.devices_gate is not None:
            self.devices_gate.wait(3)
        self._hit("devices")
        return self.device_payload


def playing_state(uri="spotify:track:x", is_playing=True, shuffle=False,
                  volume=70, progress=60_000):
    return {
        "is_playing": is_playing,
        "shuffle_state": shuffle,
        "progress_ms": progress,
        "item": {"uri": uri, "name": "X", "duration_ms": 200_000,
                 "artists": [{"name": "A"}]},
        "device": {"id": "dev1", "name": "MacBook", "type": "Computer",
                   "volume_percent": volume},
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
        lr.put(win, 2, 3, "hello world", 0)   # clipped, ok
        lr.put(win, -1, 0, "off top", 0)      # ignored
        lr.put(win, 9, 0, "off bottom", 0)    # ignored
        lr.put(win, 2, 99, "off right", 0)    # ignored


class TestConfig(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        with mock.patch.object(lr, "CONFIG_FILE", "/nonexistent/nope.json"):
            self.assertEqual(lr.load_config(), {})

    def test_save_load_roundtrip_with_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = os.path.join(tmp, "config.json")
            with mock.patch.object(lr, "CONFIG_DIR", tmp), \
                 mock.patch.object(lr, "CONFIG_FILE", cfg_file):
                lr.save_config({"client_id": "abc"})
                self.assertEqual(lr.load_config(), {"client_id": "abc"})
                self.assertEqual(os.stat(cfg_file).st_mode & 0o777, 0o600)


# ───────────────────────────── Player ─────────────────────────────


class TestLoadLikes(unittest.TestCase):
    def test_paginates_and_precomputes_search_key(self):
        pages = [
            {"items": [{"track": mk_track(i, name=f"Sönг {i}")} for i in range(50)],
             "next": "url"},
            {"items": [{"track": mk_track(50, name="Pour louper l'école",
                                          artist="Aldebert")}], "next": None},
        ]
        p = lr.Player(FakeSP(pages=pages))
        p.load_likes()
        self.assertFalse(p.loading)
        self.assertEqual(len(p.tracks), 51)
        self.assertEqual(p.load_count, 51)
        self.assertIn("l'ecole", p.tracks[50]["key"])   # folded
        self.assertIn("aldebert", p.tracks[50]["key"])  # artist searchable

    def test_skips_null_tracks(self):
        pages = [{"items": [{"track": None}, {"track": mk_track(1)}], "next": None}]
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
        self.tracks = [{"uri": f"spotify:track:{i}", "name": f"S{i}",
                        "artist": "A", "album": "", "ms": 1000,
                        "key": f"s{i} a"} for i in range(120)]

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

    def test_falls_back_to_first_known_device(self):
        self.p.playback = None
        self.sp.device_payload = {"devices": [{"id": "devX", "name": "Phone",
                                               "type": "Smartphone"}]}
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

    def test_cmd_translates_no_active_device(self):
        self.sp.raises["next_track"] = RuntimeError("NO_ACTIVE_DEVICE found")
        self.p.next()
        self.assertTrue(wait_until(lambda: "no active device" in self.p.error))

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
        self.p.tracks = [{"uri": f"spotify:track:{i}",
                          "name": f"Söng {i}", "artist": f"Ärtist {i}",
                          "album": "", "ms": 1000,
                          "key": lr.fold(f"Söng {i} Ärtist {i}")}
                         for i in range(n_tracks)]
        return lr.App(None, self.p)  # scr unused outside draw paths


class TestNavigation(AppTestBase):
    def test_down_up_and_clamping(self):
        import curses
        app = self.make_app(5)
        app.handle(curses.KEY_UP)
        self.assertEqual(app.sel, 0)            # clamped at top
        for _ in range(10):
            app.handle(curses.KEY_DOWN)
        self.assertEqual(app.sel, 4)            # clamped at bottom
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
        app.handle(KEY_ESC)                     # normal-mode Esc clears
        self.assertEqual(app.query, "")

    def test_backspace_and_esc_inside_search(self):
        app = self.make_app(12)
        self.type_query(app, "ab")
        app.handle(127)
        self.assertEqual(app.query, "a")
        app.handle(KEY_ESC)
        self.assertFalse(app.searching)
        self.assertEqual(app.query, "")

    def test_enter_plays_from_filtered_list(self):
        app = self.make_app(12)
        played = []
        self.p.play_from = lambda idx, lst: played.append((idx, len(lst)))
        self.type_query(app, "song 1")
        app.handle(KEY_ENTER)                   # leave search
        app.handle(curses_down())
        app.handle(KEY_ENTER)                   # play 2nd filtered track
        self.assertEqual(played, [(1, 3)])


def curses_down():
    import curses
    return curses.KEY_DOWN


class TestDevicePicker(AppTestBase):
    def test_d_opens_instantly_and_loads_in_background(self):
        app = self.make_app(devices=[{"id": "d1", "name": "Mac",
                                      "type": "Computer", "is_active": True}])
        app.handle(ord("d"))
        self.assertTrue(app.device_mode)        # no waiting for the network
        self.assertTrue(wait_until(lambda: app.device_list))
        self.assertFalse(app.device_loading)
        self.assertEqual(app.device_list[0]["id"], "d1")

    def test_inflight_guard_prevents_duplicate_fetch(self):
        app = self.make_app(devices=[{"id": "d1", "name": "Mac",
                                      "type": "Computer"}])
        self.sp.devices_gate = threading.Event()    # block the fetch
        app.handle(ord("d"))                        # opens + starts fetch
        app.handle(ord("d"))                        # modal toggles closed
        app.handle(ord("d"))                        # reopens; fetch in flight
        self.sp.devices_gate.set()
        self.assertTrue(wait_until(lambda: not app.device_loading))
        self.assertEqual(len(self.sp.calls_to("devices")), 1)

    def test_enter_transfers_selected_device_and_closes(self):
        app = self.make_app(playback=playing_state(),
                            devices=[{"id": "d1", "name": "Mac", "type": "Computer"},
                                     {"id": "d2", "name": "Phone", "type": "Smartphone"}])
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
        self.sp.pages = [{"items": [{"track": mk_track(i)} for i in range(3)],
                          "next": None}]
        app.handle(ord("r"))
        self.assertTrue(wait_until(lambda: not self.p.loading and
                                   len(self.p.tracks) == 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
