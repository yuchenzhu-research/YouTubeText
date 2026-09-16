from __future__ import annotations

import os
from pathlib import Path

import pytest

from youtubetext.resume import CacheUsage, LocalResumeStore


@pytest.mark.parametrize("create_root", [False, True], ids=["missing", "empty"])
def test_usage_is_empty_for_missing_or_empty_root(
    tmp_path: Path,
    create_root: bool,
) -> None:
    root = tmp_path / "resume"
    if create_root:
        root.mkdir()

    usage = LocalResumeStore(root).usage()

    assert usage == CacheUsage(root=root)
    assert root.exists() is create_root


def test_usage_counts_direct_transcript_json_and_recursive_task_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    transcripts = root / "transcripts"
    transcripts.mkdir(parents=True)
    transcript_payloads = (b'{"transcript": 1}', b"{broken-json")
    (transcripts / "complete.json").write_bytes(transcript_payloads[0])
    (transcripts / "corrupt.json").write_bytes(transcript_payloads[1])
    (transcripts / "unfinished.partial").write_bytes(b"not-complete")
    nested_transcripts = transcripts / "nested"
    nested_transcripts.mkdir()
    (nested_transcripts / "ignored.json").write_bytes(b"nested")

    tasks = root / "tasks"
    first_task = tasks / "task-a"
    checkpoint_directory = first_task / "ocr" / "checkpoints"
    checkpoint_directory.mkdir(parents=True)
    task_payloads = (b"partial-media", b"checkpoint", b"metadata")
    (first_task / "video.mp4.part").write_bytes(task_payloads[0])
    (checkpoint_directory / "0001.json").write_bytes(task_payloads[1])
    second_task = tasks / "task-b"
    second_task.mkdir()
    (second_task / "state.bin").write_bytes(task_payloads[2])
    (tasks / "orphan-file").write_bytes(b"not-a-task")

    locks = root / "locks"
    locks.mkdir()
    (locks / "active.lock").write_bytes(b"lock-bytes-are-unrelated")

    usage = LocalResumeStore(root).usage()

    assert isinstance(usage, CacheUsage)
    assert usage.root == root
    assert usage.transcript_count == 2
    assert usage.transcript_bytes == sum(map(len, transcript_payloads))
    assert usage.task_count == 2
    assert usage.task_bytes == sum(map(len, task_payloads))
    assert usage.total_bytes == usage.transcript_bytes + usage.task_bytes


def test_usage_skips_symlinks_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    transcripts = root / "transcripts"
    transcripts.mkdir(parents=True)
    real_transcript = b"{}"
    (transcripts / "real.json").write_bytes(real_transcript)
    outside_transcript = tmp_path / "outside.json"
    outside_transcript.write_bytes(b"outside-transcript")
    (transcripts / "linked.json").symlink_to(outside_transcript)

    tasks = root / "tasks"
    real_task = tasks / "real-task"
    real_task.mkdir(parents=True)
    real_task_payload = b"real-task-data"
    (real_task / "media.bin").write_bytes(real_task_payload)

    outside_file = tmp_path / "outside-media.bin"
    outside_file.write_bytes(b"outside-file")
    (real_task / "linked-media.bin").symlink_to(outside_file)
    outside_directory = tmp_path / "outside-task-data"
    outside_directory.mkdir()
    (outside_directory / "checkpoint.bin").write_bytes(b"outside-directory")
    (real_task / "linked-directory").symlink_to(
        outside_directory,
        target_is_directory=True,
    )
    (real_task / "dangling-link").symlink_to(tmp_path / "does-not-exist")
    (tasks / "linked-task").symlink_to(
        outside_directory,
        target_is_directory=True,
    )

    usage = LocalResumeStore(root).usage()

    assert usage.transcript_count == 1
    assert usage.transcript_bytes == len(real_transcript)
    assert usage.task_count == 1
    assert usage.task_bytes == len(real_task_payload)
    assert usage.total_bytes == len(real_transcript) + len(real_task_payload)


def test_usage_tolerates_unreadable_cache_directories(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    transcripts = root / "transcripts"
    tasks = root / "tasks"
    transcripts.mkdir(parents=True)
    tasks.mkdir()
    (transcripts / "hidden.json").write_bytes(b"hidden-transcript")
    hidden_task = tasks / "hidden-task"
    hidden_task.mkdir()
    (hidden_task / "hidden.bin").write_bytes(b"hidden-task-data")

    transcripts.chmod(0o000)
    tasks.chmod(0o000)
    try:
        for directory in (transcripts, tasks):
            try:
                with os.scandir(directory):
                    pass
            except PermissionError:
                continue
            pytest.skip("the current user can still read mode-000 directories")

        usage = LocalResumeStore(root).usage()
    finally:
        transcripts.chmod(0o700)
        tasks.chmod(0o700)

    assert usage == CacheUsage(root=root)
