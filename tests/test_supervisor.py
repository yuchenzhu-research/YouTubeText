from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from youtubetext._supervisor import RUN_TEMP_ENV, SUPERVISED_ENV, run_supervised


def test_successful_supervised_worker_returns_zero(tmp_path: Path) -> None:
    assert run_supervised(
        [sys.executable, "-c", "raise SystemExit(0)"],
        temp_parent=tmp_path,
    ) == 0


def test_supervised_worker_preserves_exit_code_and_cleans_its_temp_root(
    tmp_path: Path,
) -> None:
    state = tmp_path / "worker-state.json"
    program = """
import json
import os
import sys
from pathlib import Path

state = Path(sys.argv[1])
run_root = Path(os.environ[sys.argv[2]])
(run_root / "temporary-media.part").write_text("partial", encoding="utf-8")
state.write_text(json.dumps({
    "run_root": str(run_root),
    "tmpdir": os.environ.get("TMPDIR"),
    "supervised": os.environ.get(sys.argv[3]),
}), encoding="utf-8")
raise SystemExit(7)
"""

    exit_code = run_supervised(
        [sys.executable, "-c", program, str(state), RUN_TEMP_ENV, SUPERVISED_ENV],
        temp_parent=tmp_path,
    )

    details = json.loads(state.read_text(encoding="utf-8"))
    assert exit_code == 7
    assert details["tmpdir"] == details["run_root"]
    assert details["supervised"] == "1"
    assert not Path(details["run_root"]).exists()
    assert os.environ.get(SUPERVISED_ENV) != "1"


def test_interrupt_terminates_the_worker_process_group(tmp_path: Path) -> None:
    state = tmp_path / "process-tree.json"
    worker_program = """
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
Path(sys.argv[1]).write_text(json.dumps({
    "worker": os.getpid(),
    "grandchild": grandchild.pid,
    "run_root": os.environ[sys.argv[2]],
}), encoding="utf-8")
time.sleep(60)
"""
    supervisor_program = """
import sys
from youtubetext._supervisor import run_supervised

temp_parent, *command = sys.argv[1:]
raise SystemExit(run_supervised(command, temp_parent=temp_parent, grace_seconds=0.1))
"""
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            supervisor_program,
            str(tmp_path),
            sys.executable,
            "-c",
            worker_program,
            str(state),
            RUN_TEMP_ENV,
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    details: dict[str, object] = {}
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                details = json.loads(state.read_text(encoding="utf-8"))
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
        assert details, "worker process tree did not start"

        os.kill(supervisor.pid, signal.SIGINT)
        assert supervisor.wait(timeout=5) == 130

        deadline = time.monotonic() + 2
        process_ids = (int(details["worker"]), int(details["grandchild"]))
        while time.monotonic() < deadline and any(
            _process_exists(process_id) for process_id in process_ids
        ):
            time.sleep(0.02)
        assert not any(_process_exists(process_id) for process_id in process_ids)
        assert not Path(str(details["run_root"])).exists()
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        if details:
            try:
                os.killpg(int(details["worker"]), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True
