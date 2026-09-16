from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import youtubetext._supervisor as supervisor_module
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group integration test")
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
        stderr=subprocess.PIPE,
        text=True,
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
        _stdout, stderr = supervisor.communicate(timeout=5)
        assert supervisor.returncode == 130, stderr

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


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree integration test")
def test_windows_interrupt_reaps_child_after_worker_exits(tmp_path: Path) -> None:
    state = tmp_path / "windows-process-tree.json"
    stop = tmp_path / "stop-worker"
    worker_program = r"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

state, stop, run_temp_env = sys.argv[1:]
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(state).write_text(json.dumps({
    "worker": os.getpid(),
    "grandchild": child.pid,
    "run_root": os.environ[run_temp_env],
}), encoding="utf-8")
while not Path(stop).exists():
    time.sleep(0.01)
"""
    supervisor_program = r"""
import signal
import subprocess
import sys
import time
from pathlib import Path

import youtubetext._supervisor as supervisor

state, stop, temp_parent, *command = sys.argv[1:]
original_wait = subprocess.Popen.wait
original_send_signal = subprocess.Popen.send_signal
original_run = subprocess.run
interrupted = False

def interrupt_once(self, timeout=None):
    global interrupted
    if not interrupted and timeout is None:
        deadline = time.monotonic() + 5
        while not Path(state).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("worker did not start")
            time.sleep(0.01)
        interrupted = True
        raise KeyboardInterrupt
    return original_wait(self, timeout=timeout)

def simulated_break(self, event):
    if event == signal.CTRL_BREAK_EVENT:
        Path(stop).write_text("stop", encoding="utf-8")
        return
    return original_send_signal(self, event)

def unavailable_taskkill(command, **kwargs):
    if command and command[0] == "taskkill":
        return subprocess.CompletedProcess(command, 1)
    return original_run(command, **kwargs)

subprocess.Popen.wait = interrupt_once
subprocess.Popen.send_signal = simulated_break
subprocess.run = unavailable_taskkill
raise SystemExit(supervisor.run_supervised(
    command, temp_parent=temp_parent, grace_seconds=0.2,
))
"""
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            supervisor_program,
            str(state),
            str(stop),
            str(tmp_path),
            sys.executable,
            "-c",
            worker_program,
            str(state),
            str(stop),
            RUN_TEMP_ENV,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    details: dict[str, object] = {}
    try:
        _stdout, stderr = supervisor.communicate(timeout=10)
        assert supervisor.returncode == 130, stderr
        details = json.loads(state.read_text(encoding="utf-8"))

        grandchild = int(details["grandchild"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _windows_process_exists(grandchild):
            time.sleep(0.02)
        assert not _windows_process_exists(grandchild)
        assert not Path(str(details["run_root"])).exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if not details:
            try:
                details = json.loads(state.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        if details and _windows_process_exists(int(details["grandchild"])):
            subprocess.run(
                ["taskkill", "/PID", str(details["grandchild"]), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )


def test_popen_options_are_platform_specific(monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module.os, "name", "nt")
    assert supervisor_module._popen_options() == {
        "creationflags": supervisor_module._CREATE_NEW_PROCESS_GROUP
    }

    monkeypatch.setattr(supervisor_module.os, "name", "posix")
    assert supervisor_module._popen_options() == {"start_new_session": True}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group probe")
def test_process_group_probe_treats_permission_denied_as_existing(monkeypatch) -> None:
    def denied(_group: int, _signal: int) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(supervisor_module.os, "killpg", denied)

    assert supervisor_module._process_group_exists(321)


def test_windows_termination_escalates_from_break_to_taskkill(monkeypatch) -> None:
    class FakeProcess:
        pid = 321

        def __init__(self) -> None:
            self.signals: list[int] = []
            self.done = False
            self.killed = False

        def send_signal(self, value: int) -> None:
            self.signals.append(value)

        def wait(self, timeout=None) -> int:
            if timeout is not None and not self.done:
                raise subprocess.TimeoutExpired("worker", timeout)
            return 0

        def poll(self) -> int | None:
            return 0 if self.done else None

        def kill(self) -> None:
            self.killed = True
            self.done = True

    process = FakeProcess()
    taskkill_calls: list[list[str]] = []

    def taskkill(command, **_kwargs):
        taskkill_calls.append(command)
        process.done = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(supervisor_module.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(supervisor_module.subprocess, "run", taskkill)

    supervisor_module._terminate_windows_process_tree(
        process,  # type: ignore[arg-type]
        grace_seconds=0.01,
    )

    assert process.signals == [1]
    assert taskkill_calls == [["taskkill", "/PID", "321", "/T", "/F"]]
    assert not process.killed


def test_windows_termination_keeps_cleaning_when_parent_exits_after_break(
    monkeypatch,
) -> None:
    class FakeProcess:
        pid = 654

        def __init__(self) -> None:
            self.done = False
            self.descendant_alive = True

        def send_signal(self, _event: int) -> None:
            self.done = True

        def wait(self, timeout=None) -> int:
            return 0

        def poll(self) -> int | None:
            return 0 if self.done else None

        def kill(self) -> None:
            self.done = True

    class FakeJob:
        def __init__(self, process: FakeProcess) -> None:
            self.process = process
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self.process.descendant_alive = False

    process = FakeProcess()
    job = FakeJob(process)
    taskkill_calls: list[list[str]] = []

    def taskkill(command, **_kwargs):
        taskkill_calls.append(command)
        process.descendant_alive = False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(supervisor_module.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(supervisor_module.subprocess, "run", taskkill)

    supervisor_module._terminate_windows_process_tree(
        process,  # type: ignore[arg-type]
        grace_seconds=0.01,
        job=job,  # type: ignore[arg-type]
    )

    assert job.closed
    assert taskkill_calls == []
    assert not process.descendant_alive


def test_windows_without_job_does_not_taskkill_an_exited_parent(monkeypatch) -> None:
    class FakeProcess:
        pid = 765

        def send_signal(self, _event: int) -> None:
            pass

        def wait(self, timeout=None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    calls: list[list[str]] = []

    def taskkill(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(supervisor_module.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(supervisor_module.subprocess, "run", taskkill)

    supervisor_module._terminate_windows_process_tree(
        FakeProcess(),  # type: ignore[arg-type]
        grace_seconds=0.01,
    )

    assert calls == []


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _windows_process_exists(process_id: int) -> bool:
    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return any(
        len(row) > 1 and row[1] == str(process_id)
        for row in csv.reader(completed.stdout.splitlines())
    )
