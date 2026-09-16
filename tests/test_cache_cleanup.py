from __future__ import annotations

import fcntl
from pathlib import Path

import youtubetext.resume as resume_module
from youtubetext.resume import CacheCleanup, LocalResumeStore


def test_clear_incomplete_removes_task_trees_and_preserves_transcripts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    transcripts = root / "transcripts"
    transcripts.mkdir(parents=True)
    transcript_payload = b'{"completed": true}'
    transcript = transcripts / "completed.json"
    transcript.write_bytes(transcript_payload)

    tasks = root / "tasks"
    first_task = tasks / ("a" * 64)
    first_nested = first_task / "ocr" / "checkpoints"
    first_nested.mkdir(parents=True)
    task_payloads = (b"partial-video", b"ocr-checkpoint", b"task-state")
    (first_task / "video.mp4.part").write_bytes(task_payloads[0])
    (first_nested / "0001.json").write_bytes(task_payloads[1])
    second_task = tasks / ("b" * 64)
    second_task.mkdir()
    (second_task / "state.json").write_bytes(task_payloads[2])

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(
        root=root,
        removed_tasks=2,
        removed_bytes=sum(map(len, task_payloads)),
    )
    assert cleanup.as_dict() == {
        "root": str(root),
        "removed_tasks": 2,
        "removed_bytes": sum(map(len, task_payloads)),
        "active_tasks": 0,
        "failed_tasks": 0,
    }
    assert not first_task.exists()
    assert not second_task.exists()
    assert transcript.read_bytes() == transcript_payload


def test_clear_incomplete_skips_non_task_names_and_task_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    tasks = root / "tasks"
    non_task = tasks / "not-a-resume-task"
    non_task.mkdir(parents=True)
    (non_task / "keep.bin").write_bytes(b"keep-non-task")

    outside = tmp_path / "outside-task"
    outside.mkdir()
    outside_payload = b"keep-outside"
    outside_file = outside / "state.bin"
    outside_file.write_bytes(outside_payload)
    linked_task = tasks / ("c" * 64)
    linked_task.symlink_to(outside, target_is_directory=True)

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(root=root)
    assert (non_task / "keep.bin").read_bytes() == b"keep-non-task"
    assert linked_task.is_symlink()
    assert outside_file.read_bytes() == outside_payload


def test_clear_incomplete_does_not_follow_tasks_root_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    root.mkdir()
    outside_tasks = tmp_path / "outside-tasks"
    outside_task = outside_tasks / ("a" * 64)
    outside_task.mkdir(parents=True)
    outside_state = outside_task / "state.bin"
    outside_state.write_bytes(b"preserve-outside-task")
    (root / "tasks").symlink_to(outside_tasks, target_is_directory=True)

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(root=root, failed_tasks=1)
    assert outside_state.read_bytes() == b"preserve-outside-task"


def test_clear_incomplete_does_not_follow_locks_root_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    task_key = "b" * 64
    task = root / "tasks" / task_key
    task.mkdir(parents=True)
    state = task / "state.bin"
    state.write_bytes(b"preserve-task")
    outside_locks = tmp_path / "outside-locks"
    outside_locks.mkdir()
    (root / "locks").symlink_to(outside_locks, target_is_directory=True)

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(root=root, failed_tasks=1)
    assert state.read_bytes() == b"preserve-task"
    assert not (outside_locks / f"{task_key}.lock").exists()


def test_clear_incomplete_counts_locked_task_as_active(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    task_key = "d" * 64
    task = root / "tasks" / task_key
    task.mkdir(parents=True)
    payload = b"active-task"
    (task / "state.bin").write_bytes(payload)
    locks = root / "locks"
    locks.mkdir()
    lock_path = locks / f"{task_key}.lock"

    with lock_path.open("a+b") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            cleanup = LocalResumeStore(root).clear_incomplete()
        finally:
            fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

    assert cleanup == CacheCleanup(root=root, active_tasks=1)
    assert (task / "state.bin").read_bytes() == payload


def test_clear_incomplete_does_not_follow_lock_symlink(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    task_key = "f" * 64
    task = root / "tasks" / task_key
    task.mkdir(parents=True)
    state = task / "state.bin"
    state.write_bytes(b"preserve-task")
    locks = root / "locks"
    locks.mkdir()
    outside_lock = tmp_path / "outside.lock"
    outside_payload = b"preserve-outside"
    outside_lock.write_bytes(outside_payload)
    (locks / f"{task_key}.lock").symlink_to(outside_lock)

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(root=root, failed_tasks=1)
    assert state.read_bytes() == b"preserve-task"
    assert outside_lock.read_bytes() == outside_payload


def test_clear_incomplete_counts_task_when_deletion_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "resume"
    task = root / "tasks" / ("e" * 64)
    task.mkdir(parents=True)
    payload = b"undeletable-task"
    state = task / "state.bin"
    state.write_bytes(payload)
    monkeypatch.setattr(
        resume_module,
        "_remove_tree_at",
        lambda _parent_descriptor, _name: False,
    )

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(root=root, failed_tasks=1)
    assert state.read_bytes() == payload


def test_clear_incomplete_does_not_create_missing_storage(tmp_path: Path) -> None:
    root = tmp_path / "missing-resume"

    cleanup = LocalResumeStore(root).clear_incomplete()

    assert cleanup == CacheCleanup(root=root)
    assert cleanup.as_dict() == {
        "root": str(root),
        "removed_tasks": 0,
        "removed_bytes": 0,
        "active_tasks": 0,
        "failed_tasks": 0,
    }
    assert not root.exists()
