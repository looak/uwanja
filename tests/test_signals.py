"""A stop signal ends a run as `stopped`, with the document saying so, as Ctrl-C does."""
import json
import signal
import subprocess
import sys
import time
from pathlib import Path


def test_sigterm_reports_stopped(tmp_path):
    config = tmp_path / "g.toml"
    config.write_text('tc = "1+1"\n[subject]\ncmd = "a"\n[[opponent]]\ncmd = "b"\n', encoding="utf-8")
    (tmp_path / "fc.log").write_text("", encoding="utf-8")
    (tmp_path / "fc.pgn").write_text("", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "uwanja", "follow", str(config), "--log", str(tmp_path / "fc.log"),
         "--pgn", str(tmp_path / "fc.pgn"), "--out", str(tmp_path / "runs"), "--no-report", "--idle-exit", "300"],
        stderr=subprocess.PIPE, creationflags=flags, cwd=Path(__file__).parent.parent)
    try:
        match = None
        for _ in range(100):
            found = list((tmp_path / "runs").glob("*/match.json"))
            if found:
                match = found[0]
                break
            time.sleep(0.1)
        assert match is not None, "the run never started"
        time.sleep(0.5)                                      # in the loop, handlers installed
        # Windows has no SIGTERM to send a process, Ctrl-Break reaches the same handler
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        assert process.wait(timeout=20) == 130
    finally:
        if process.poll() is None:
            process.kill()
    document = json.loads(match.read_text(encoding="utf-8"))
    assert document["match"]["state"] == "stopped"
    assert document["match"]["finished_at"]
