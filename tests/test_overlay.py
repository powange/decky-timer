import json
import os
import shutil
import subprocess
import sys
import time

import pytest

overlay = pytest.importorskip("overlay")  # needs libX11 + libcairo


def test_format_seconds():
    assert overlay.format_seconds(61.2) == "01:02"
    assert overlay.format_seconds(0) == "00:00"
    assert overlay.format_seconds(-0.5) == "00:00"
    assert overlay.format_seconds(-83.5) == "-01:23"
    assert overlay.format_seconds(3725) == "1:02:05"


def test_rows_hidden_and_truncated():
    now = 1000.0
    state = {"timers": [{"name": "x" * 40, "end_at": now + 30, "paused": False},
                        {"name": "Long", "end_at": now + 3600, "paused": False}],
             "settings": {"last_minutes": 5}}
    rows = overlay.build_rows(state, now)
    assert len(rows) == 1 and rows[0][0].endswith("…") and len(rows[0][0]) == overlay.NAME_MAX


def test_sleeps_until_next_change():
    now = 1000.0
    run = {"timers": [{"end_at": now + 10.3, "paused": False}], "settings": {}}
    assert 0.3 <= overlay.seconds_until_change(run, now) < 0.32
    paused = {"timers": [{"remaining": 5, "paused": True}], "settings": {}}
    assert overlay.seconds_until_change(paused, now) is None


@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb not installed")
def test_renders_on_a_virtual_display():
    xvfb = subprocess.Popen(["Xvfb", ":91", "-screen", "0", "1280x800x24"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        proc = subprocess.Popen([sys.executable, overlay.__file__], stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(os.environ, DISPLAY=":91"))
        msg = {"timers": [{"name": "T", "end_at": time.time() + 60, "paused": False}], "settings": {}}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        time.sleep(1.5)
        assert proc.poll() is None, proc.stderr.read().decode()
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        xvfb.kill()
