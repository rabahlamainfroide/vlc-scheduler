#!/usr/bin/env python3
"""Regression tests for playback-state persistence.

Run with:  python3 test_state.py

These pin down the bug that froze every folder on the same episodes for
days: playback_state.json is rewritten whole, so a writer that snapshots it
early and saves late silently reverts whatever another writer committed in
between.  In production that was the next slot's play_videos() erasing the
finishing slot's rotation commit, every single hand-over.

Each test runs against a private copy of the module pointed at a throwaway
directory, so nothing here touches the real state file.  kill_vlc() is never
called for real either -- it runs `pkill vlc`, which would kill whatever the
operator is actually watching.
"""

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent / "vlc_scheduler.py"


@contextlib.contextmanager
def sandbox():
    """Import a private copy of the scheduler rooted in a temp directory.

    The module derives STATE_FILE from its own __file__, so copying it into
    a temp dir is what isolates the state file.
    """
    tmp = Path(tempfile.mkdtemp(prefix="vlc-sched-test-"))
    shutil.copy(SRC, tmp / "vlc_scheduler.py")
    (tmp / "config.json").write_text('{"schedules": []}')
    sys.path.insert(0, str(tmp))
    sys.modules.pop("vlc_scheduler", None)
    try:
        import vlc_scheduler as vs
        yield vs, tmp
    finally:
        sys.modules.pop("vlc_scheduler", None)
        if str(tmp) in sys.path:
            sys.path.remove(str(tmp))
        shutil.rmtree(tmp, ignore_errors=True)


def _session(**over):
    """A state entry for a slot that has launched but not been confirmed."""
    entry = {
        "last_played":           "ep01",
        "resume_offset":         0.0,
        "last_session_at":       "T-A",
        "session_completed":     False,
        "pending_folder_index":  0,
        "pending_last_played":   "ep02",
        "pending_resume_offset": 42.0,
    }
    entry.update(over)
    return entry


def _wait_for_confirmation(vs, key, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = vs.load_state().get(key, {})
        if entry.get("session_completed") is True:
            return entry
        time.sleep(0.02)
    raise AssertionError(f"{key} was never confirmed: {vs.load_state().get(key)}")


class _Proc:
    """Stand-in for a VLC process that exits when released."""

    def __init__(self, release=None):
        self._release = release

    def wait(self):
        if self._release is not None:
            self._release()


# ── The bug ───────────────────────────────────────────────────────────────────

def test_finalise_survives_the_next_slot_launching():
    """Slot A is confirmed while slot B writes its own entry. Both must land.

    This is the production sequence: B's kill_vlc() makes A's VLC exit, so
    A's _on_vlc_exit thread commits A's rotation at the same instant B saves.
    """
    with sandbox() as (vs, _tmp):
        vs.save_state({"A": _session()})
        gate = threading.Barrier(2)

        def slot_b_launch():
            gate.wait()
            time.sleep(0.10)          # before_play hook + ffprobe, as in a real slot
            with vs.state_transaction() as state:
                state["B"] = _session(last_session_at="T-B", last_played="x01")

        threads = [
            threading.Thread(target=vs._on_vlc_exit,
                             args=(_Proc(gate.wait), "A", "T-A",
                                   time.monotonic() - 3600)),
            threading.Thread(target=slot_b_launch),
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        a = vs.load_state()["A"]
        assert a["last_played"] == "ep02", f"A's rotation was reverted: {a}"
        assert a["resume_offset"] == 42.0, f"A's offset was reverted: {a}"
        assert a["session_completed"] is True, a
        assert not [k for k in a if k.startswith("pending_")], a
        assert vs.load_state()["B"]["last_played"] == "x01", "B's write was lost"


def test_stale_snapshot_write_reverts_the_finalise():
    """Guard on the test itself: the racy window above is genuinely hit.

    Reproduces the old snapshot-early/save-late pattern.  If this ever stops
    failing to finalise, the test above has gone blind and proves nothing.
    """
    with sandbox() as (vs, _tmp):
        vs.save_state({"A": _session()})
        gate = threading.Barrier(2)

        def slot_b_launch_old_way():
            stale = vs.load_state()      # snapshot taken BEFORE the kill
            gate.wait()
            time.sleep(0.10)
            stale["B"] = {"last_played": "x01"}
            vs.save_state(stale)         # ... saved after A has committed

        threads = [
            threading.Thread(target=vs._on_vlc_exit,
                             args=(_Proc(gate.wait), "A", "T-A",
                                   time.monotonic() - 3600)),
            threading.Thread(target=slot_b_launch_old_way),
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        a = vs.load_state()["A"]
        assert a["last_played"] == "ep01" and "pending_last_played" in a, \
            "expected the stale write to revert the finalise, but it did not"


def test_slot_handover_advances_rotation_end_to_end():
    """Drive the real play_videos() through a hand-over between two slots."""
    with sandbox() as (vs, tmp):
        # The crash guard would otherwise require the test to run for a minute;
        # it is covered directly by test_crash_on_launch_does_not_advance.
        vs.ABNORMAL_EXIT_SECONDS = 0.0

        fake_vlc = tmp / "fake-vlc"
        fake_vlc.write_text("#!/bin/sh\nexec sleep 300\n")
        fake_vlc.chmod(0o755)

        def kill_tracked_only():
            proc = vs._active_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait()
            vs._active_proc = None

        vs.kill_vlc = kill_tracked_only          # never run `pkill vlc` in a test
        vs.get_video_duration = lambda path: 1000.0

        folder_a, folder_b = tmp / "A", tmp / "B"
        for folder in (folder_a, folder_b):
            folder.mkdir()
            for name in ("1.mp4", "2.mp4", "3.mp4"):
                (folder / name).write_bytes(b"")

        exts = [".mp4"]
        try:
            # A 1500s window over 1000s episodes picks 1.mp4 + 2.mp4 and
            # overshoots by 500s, so 2.mp4 is cut off half way and must be
            # resumed there next time -- not skipped, not replayed from zero.
            vs.play_videos([{"path": str(folder_a), "count": 1}],
                           str(fake_vlc), exts, None, 1500.0)
            a = vs.load_state()[str(folder_a)]
            assert a["session_completed"] is False, a
            assert a["pending_last_played"] == "1.mp4", a
            assert a["pending_resume_offset"] == 500.0, a

            # The next slot kills A's player and writes its own entry.
            vs.play_videos([{"path": str(folder_b), "count": 1}],
                           str(fake_vlc), exts, None, 1500.0)

            a = _wait_for_confirmation(vs, str(folder_a))
            assert a["last_played"] == "1.mp4", f"rotation not advanced: {a}"
            assert a["resume_offset"] == 500.0, f"resume point lost: {a}"
            assert not [k for k in a if k.startswith("pending_")], a

            b = vs.load_state()[str(folder_b)]
            assert b["session_completed"] is False, "B's launch was lost"
            assert b["pending_last_played"] == "1.mp4", b
        finally:
            kill_tracked_only()


# ── Confirmation rules ────────────────────────────────────────────────────────

def test_crash_on_launch_does_not_advance():
    """VLC dying seconds in means nobody watched: the batch must replay."""
    with sandbox() as (vs, _tmp):
        vs.save_state({"A": _session()})
        vs._on_vlc_exit(_Proc(), "A", "T-A", time.monotonic() - 2.0)
        a = vs.load_state()["A"]
        assert a["last_played"] == "ep01" and "pending_last_played" in a, \
            f"a crashed session advanced the rotation: {a}"


def test_superseded_session_does_not_clobber_a_newer_one():
    with sandbox() as (vs, _tmp):
        vs.save_state({"A": _session(last_session_at="T-NEW",
                                     pending_last_played="ep09")})
        vs._on_vlc_exit(_Proc(), "A", "T-OLD", time.monotonic() - 3600)
        a = vs.load_state()["A"]
        assert a["last_played"] == "ep01", a
        assert a["last_session_at"] == "T-NEW", a
        assert a["session_completed"] is False, a


def test_manual_advance_clears_pending_and_keeps_history():
    """--advance must not leave pending_* that would later undo it."""
    with sandbox() as (vs, _tmp):
        vs.save_state({"A": _session(last_session_videos=["ep01", "ep02"],
                                     prev_session_folder="/somewhere")})
        with vs.state_transaction() as state:
            vs._apply_manual_advance(state, "A", 0, "ep07", 12.5)
        a = vs.load_state()["A"]
        assert a["last_played"] == "ep07" and a["resume_offset"] == 12.5, a
        assert not [k for k in a if k.startswith("pending_")], a
        assert a["last_session_videos"] == ["ep01", "ep02"], "history was wiped"
        assert a["prev_session_folder"] == "/somewhere", "mirror source was wiped"


# ── Durability ────────────────────────────────────────────────────────────────

def test_save_is_atomic_and_leaves_no_temp_file():
    with sandbox() as (vs, _tmp):
        vs.save_state({"k": {"v": 1}})
        assert not vs.STATE_TMP_FILE.exists(), "temp file left behind"
        assert json.loads(vs.STATE_FILE.read_text()) == {"k": {"v": 1}}


def test_two_processes_do_not_lose_updates():
    """The --play-now-while-the-daemon-is-running case."""
    with sandbox() as (vs, tmp):
        rounds = 50
        vs.save_state({"A": {"n": 0}, "B": {"n": 0}})
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1])\n"
             "import vlc_scheduler as vs\n"
             "for _ in range(int(sys.argv[2])):\n"
             "    with vs.state_transaction() as s: s['B']['n'] += 1\n",
             str(tmp), str(rounds)],
            cwd=tmp,
        )
        for _ in range(rounds):
            with vs.state_transaction() as state:
                state["A"]["n"] += 1
        assert child.wait() == 0, "child process failed"

        state = vs.load_state()
        assert state["A"]["n"] == rounds, f"lost updates in this process: {state}"
        assert state["B"]["n"] == rounds, f"lost updates in the child: {state}"


# ── Shifting a slot's position ────────────────────────────────────────────────

def _shift_fixture(vs, tmp, counts=(3, 2)):
    """A slot of len(counts) folders holding counts[i] episodes each."""
    folders = []
    for n, count in enumerate(counts):
        folder = tmp / f"f{n}"
        folder.mkdir()
        for i in range(1, count + 1):
            (folder / f"{i}.mp4").write_bytes(b"")
        folders.append({"path": str(folder), "count": 1})
    return folders, vs._slot_sequence(folders, [".mp4"])


def test_cursor_round_trips_through_state():
    """Every position must survive a conversion to state fields and back."""
    with sandbox() as (vs, tmp):
        folders, seq = _shift_fixture(vs, tmp)
        assert len(seq) == 5, seq
        for cursor in range(len(seq) + 1):
            folder_index, last_played = vs._cursor_to_state(seq, cursor)
            back = vs._state_to_cursor(seq, folder_index, last_played)
            assert back == cursor, (
                f"cursor {cursor} -> (folder_index={folder_index}, "
                f"last_played={last_played!r}) -> {back}"
            )


def test_shift_backward_crosses_into_the_previous_folder():
    """Stepping off the front of a folder lands on the tail of the one before."""
    with sandbox() as (vs, tmp):
        folders, seq = _shift_fixture(vs, tmp)
        boundary = next(i for i, (fi, _v) in enumerate(seq) if fi == 1)

        # sitting on the first episode of folder 1 ...
        folder_index, last_played = vs._cursor_to_state(seq, boundary)
        assert (folder_index, last_played) == (1, None)

        # ... one step back is the last episode of folder 0.
        folder_index, last_played = vs._cursor_to_state(seq, boundary - 1)
        assert folder_index == 0, f"folder_index did not follow: {folder_index}"
        assert last_played == "2.mp4", last_played
        assert seq[boundary - 1][1].name == "3.mp4"


def test_shift_past_the_end_leaves_the_slot_exhausted():
    """A cursor past the last episode must read back as 'folder exhausted'."""
    with sandbox() as (vs, tmp):
        folders, seq = _shift_fixture(vs, tmp)
        folder_index, last_played = vs._cursor_to_state(seq, len(seq))
        assert (folder_index, last_played) == (1, "2.mp4"), (folder_index, last_played)
        # which is exactly what makes the selector roll over to the first folder
        state = {folders[0]["path"]: {"folder_index": folder_index,
                                      "last_played": last_played}}
        videos, new_index, _path = vs.get_next_videos(folders, state, [".mp4"])
        assert new_index == 0 and videos[0].name == "1.mp4", (new_index, videos)


def test_shift_tolerates_a_deleted_last_played():
    """A last_played that is no longer on disk falls back to its folder's start."""
    with sandbox() as (vs, tmp):
        folders, seq = _shift_fixture(vs, tmp)
        cursor = vs._state_to_cursor(seq, 1, "gone-from-disk.mp4")
        assert cursor == next(i for i, (fi, _v) in enumerate(seq) if fi == 1)


def test_find_schedule_redirects_a_mirror_to_its_primary():
    with sandbox() as (vs, _tmp):
        config = {"schedules": [
            {"time": "15:00", "end_time": "16:00", "folders": [{"path": "/v/fa"}]},
            {"time": "08:00", "end_time": "09:00", "mirror": "15:00"},
        ]}
        primary, mirror = vs._find_schedule(config, "08:00")
        assert primary["time"] == "15:00", primary
        assert mirror["time"] == "08:00", mirror

        primary, mirror = vs._find_schedule(config, "15:00")
        assert primary["time"] == "15:00" and mirror is None

        # addressable by folder path too
        primary, mirror = vs._find_schedule(config, "/v/fa")
        assert primary["time"] == "15:00" and mirror is None

        assert vs._find_schedule(config, "03:00") == (None, None)


def test_window_seconds():
    with sandbox() as (vs, _tmp):
        assert vs._window_seconds({"time": "13:00", "end_time": "15:00"}) == 7200.0
        assert vs._window_seconds({"time": "13:00"}) is None
        # an end_time before the start is not a window
        assert vs._window_seconds({"time": "15:00", "end_time": "13:00"}) is None


# ── Status endpoint ───────────────────────────────────────────────────────────

def _status_config():
    return {"schedules": [
        {"time": "15:00", "end_time": "16:00", "folders": [{"path": "/v/fa"}]},
        {"time": "08:00", "end_time": "09:00", "mirror": "15:00"},
        {"time": "07:00", "mirror": "99:99"},
    ]}


def test_status_row_for_a_mirror_reports_its_primary():
    """A mirror row used to come back with an empty folder and a null episode."""
    with sandbox() as (vs, _tmp):
        config = _status_config()
        state = {"/v/fa": {"folder_index": 0, "last_played": "EP27.mkv",
                           "resume_offset": 1981.8}}
        row = vs._schedule_status(config["schedules"][1], config, state)

        assert row["active_folder"] == "/v/fa", row
        assert row["last_played"] == "EP27.mkv", row
        assert row["resume_offset"] == 1981.8, row
        assert row["mirrors"] == "15:00", row
        # the row keeps its own clock, not the primary's
        assert row["time"] == "08:00" and row["end_time"] == "09:00", row


def test_status_row_for_a_primary_is_unchanged():
    with sandbox() as (vs, _tmp):
        config = _status_config()
        state = {"/v/fa": {"folder_index": 0, "last_played": "EP27.mkv"}}
        row = vs._schedule_status(config["schedules"][0], config, state)
        assert row["active_folder"] == "/v/fa" and row["time"] == "15:00", row
        assert "mirrors" not in row, "a primary must not be labelled a mirror"


def test_status_row_survives_a_dangling_mirror_target():
    """A misconfigured mirror must not take the whole endpoint down."""
    with sandbox() as (vs, _tmp):
        config = _status_config()
        row = vs._schedule_status(config["schedules"][2], config, {})
        assert row["time"] == "07:00" and "error" in row, row


def test_status_payload_covers_every_schedule():
    with sandbox() as (vs, _tmp):
        config = _status_config()
        rows = [vs._schedule_status(e, config, {}) for e in config["schedules"]]
        assert len(rows) == 3
        assert [r["time"] for r in rows] == ["15:00", "08:00", "07:00"]


# ── Surviving a power cut ─────────────────────────────────────────────────────

def test_corrupt_state_falls_back_to_the_backup():
    """A torn state file must not take the kiosk down."""
    with sandbox() as (vs, _tmp):
        vs.save_state({"A": {"last_played": "ep01"}})
        vs.save_state({"A": {"last_played": "ep02"}})   # ep01 becomes the backup
        vs.STATE_FILE.write_text('{"A": {"last_pla')     # a torn write

        state = vs.load_state()
        assert state == {"A": {"last_played": "ep01"}}, state
        assert list(vs.STATE_FILE.parent.glob("*.corrupt-*")), "bad file not quarantined"
        assert not vs.STATE_FILE.exists(), "the corrupt file should have been moved aside"


def test_corrupt_state_with_no_backup_starts_fresh_without_raising():
    with sandbox() as (vs, _tmp):
        vs.STATE_FILE.write_text("not json at all")
        assert vs.load_state() == {}
        assert list(vs.STATE_FILE.parent.glob("*.corrupt-*"))


def test_state_that_is_valid_json_but_not_an_object_is_rejected():
    with sandbox() as (vs, _tmp):
        vs.STATE_FILE.write_text("[1, 2, 3]")
        assert vs.load_state() == {}


def test_missing_state_file_is_not_an_error():
    with sandbox() as (vs, _tmp):
        assert vs.load_state() == {}
        assert not list(vs.STATE_FILE.parent.glob("*.corrupt-*")), \
            "nothing to quarantine when there was never a state file"


def test_backup_trails_the_live_file_by_one_generation():
    with sandbox() as (vs, _tmp):
        vs.save_state({"n": 1})
        assert not vs.STATE_BAK_FILE.exists(), "nothing to back up on the first write"
        vs.save_state({"n": 2})
        assert json.loads(vs.STATE_BAK_FILE.read_text()) == {"n": 1}
        vs.save_state({"n": 3})
        assert json.loads(vs.STATE_BAK_FILE.read_text()) == {"n": 2}
        assert json.loads(vs.STATE_FILE.read_text()) == {"n": 3}


def _catchup_config(vs, tmp, now, mirror_offset_h, primary_offset_h):
    """A primary later today plus a mirror `mirror_offset_h` hours ago."""
    import datetime
    primary_at = (now + datetime.timedelta(hours=primary_offset_h)).replace(second=0, microsecond=0)
    mirror_at  = (now - datetime.timedelta(hours=mirror_offset_h)).replace(second=0, microsecond=0)
    folder = tmp / "series"
    if not folder.exists():
        folder.mkdir()
        for i in range(1, 4):
            (folder / f"{i}.mp4").write_bytes(b"")
    config = {"video_extensions": [".mp4"], "schedules": [
        {"time": primary_at.strftime("%H:%M"),
         "end_time": (primary_at + datetime.timedelta(hours=1)).strftime("%H:%M"),
         "folders": [{"path": str(folder)}]},
        {"time": mirror_at.strftime("%H:%M"),
         "end_time": (mirror_at + datetime.timedelta(hours=2)).strftime("%H:%M"),
         "mirror": primary_at.strftime("%H:%M")},
    ]}
    # the primary ran cleanly yesterday, at its own time
    ran = (primary_at - datetime.timedelta(days=1)).replace(second=5, microsecond=0)
    vs.save_state({str(folder): {
        "folder_index": 0, "last_played": "1.mp4", "resume_offset": 0.0,
        "last_session_at": ran.isoformat(), "session_completed": True,
        "last_session_folder": str(folder), "last_session_videos": ["1.mp4"],
        "last_session_resume_offset": 0.0,
    }})
    return config, folder


def test_catchup_replays_a_mirror_whose_window_is_still_open():
    """A power cut during a morning mirror used to leave the screen dark."""
    import datetime
    with sandbox() as (vs, tmp):
        now = datetime.datetime.now()
        config, _folder = _catchup_config(vs, tmp, now, mirror_offset_h=1, primary_offset_h=4)
        played = []
        vs.play_videos = lambda *a, **k: played.append((a, k))
        vs.startup_catchup(config, "/bin/true", [".mp4"])
        assert played, "mirror inside its window was not replayed"
        assert played[0][0][5] is True, "must be played as a mirror (writes no state)"


def test_catchup_ignores_a_mirror_whose_window_has_closed():
    import datetime
    with sandbox() as (vs, tmp):
        now = datetime.datetime.now()
        # window is 2h; 3h ago means it closed an hour back
        config, _folder = _catchup_config(vs, tmp, now, mirror_offset_h=3, primary_offset_h=4)
        played = []
        vs.play_videos = lambda *a, **k: played.append((a, k))
        vs.startup_catchup(config, "/bin/true", [".mp4"])
        assert not played, "a closed mirror window must not trigger playback"


def _interrupted_primary(vs, tmp, now, started_h_ago, window_h, end_time=True):
    """A primary that started `started_h_ago` hours ago and was cut off."""
    import datetime
    started = (now - datetime.timedelta(hours=started_h_ago)).replace(second=0, microsecond=0)
    folder = tmp / "series"
    if not folder.exists():
        folder.mkdir()
        for i in range(1, 4):
            (folder / f"{i}.mp4").write_bytes(b"")
    slot = {"time": started.strftime("%H:%M"), "folders": [{"path": str(folder)}]}
    if end_time:
        slot["end_time"] = (started + datetime.timedelta(hours=window_h)).strftime("%H:%M")
    vs.save_state({str(folder): {
        "folder_index": 0, "last_played": "1.mp4", "resume_offset": 0.0,
        "last_session_at": started.isoformat(), "session_completed": False,
        "last_session_folder": str(folder), "last_session_videos": ["2.mp4"],
        "last_session_resume_offset": 0.0,
        "pending_folder_index": 0, "pending_last_played": "2.mp4",
    }})
    return {"video_extensions": [".mp4"], "schedules": [slot]}


def test_catchup_resumes_an_interrupted_primary_inside_its_window():
    import datetime
    with sandbox() as (vs, tmp):
        now = datetime.datetime.now()
        config = _interrupted_primary(vs, tmp, now, started_h_ago=1, window_h=2)
        played = []
        vs.play_videos = lambda *a, **k: played.append(a)
        vs.startup_catchup(config, "/bin/true", [".mp4"])
        assert played, "a slot still on air must be resumed"


def test_catchup_ignores_an_interrupted_primary_past_its_window():
    """The 21:00 slot cut at 22:00 must not start playing at 05:00."""
    import datetime
    with sandbox() as (vs, tmp):
        now = datetime.datetime.now()
        # started 8h ago with a 2h window: closed 6h back
        config = _interrupted_primary(vs, tmp, now, started_h_ago=8, window_h=2)
        played = []
        vs.play_videos = lambda *a, **k: played.append(a)
        vs.startup_catchup(config, "/bin/true", [".mp4"])
        assert not played, "a slot whose window closed must not be replayed late"


def test_catchup_still_resumes_a_count_based_slot_with_no_window():
    """Slots without end_time have no window, so they keep the old behaviour."""
    import datetime
    with sandbox() as (vs, tmp):
        now = datetime.datetime.now()
        config = _interrupted_primary(vs, tmp, now, started_h_ago=8, window_h=2,
                                      end_time=False)
        played = []
        vs.play_videos = lambda *a, **k: played.append(a)
        vs.startup_catchup(config, "/bin/true", [".mp4"])
        assert played, "a count-based slot has no window to fall outside of"


def main() -> int:
    tests = [fn for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failures = []
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"FAIL  {fn.__name__}\n        {exc}")
        else:
            print(f"ok    {fn.__name__}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
