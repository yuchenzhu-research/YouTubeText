from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

import youtubetext.cli as cli
from youtubetext._supervisor import RUN_TEMP_ENV
from youtubetext.doctor import CachedModelStatus, DiagnosticCheck, DoctorReport
from youtubetext.domain import (
    OutputFiles,
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    TaskResult,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.progress import ProgressEvent, Stage
from youtubetext.runtime import HostProfile

URL_1 = "https://youtu.be/first"
URL_2 = "https://www.bilibili.com/video/BV1second"


def successful_result(url: str, root: Path, title: str = "Video") -> TaskResult:
    platform = "bilibili" if "bilibili" in url else "youtube"
    transcript = Transcript(
        metadata=SourceMetadata(url, platform, "id", title),
        language="zh-Hant",
        method=TranscriptMethod.PLATFORM_CAPTIONS,
        segments=(TranscriptSegment(0, 1, "完整文字"),),
    )
    output = OutputFiles(
        directory=root / "video",
        markdown=root / "video" / "transcript.md",
        text=root / "video" / "transcript.txt",
        metadata=root / "video" / "metadata.json",
    )
    return TaskResult(url=url, transcript=transcript, output=output)


class FakeEngine:
    created: list["FakeEngine"] = []
    results: list[TaskResult] = []

    def __init__(self, pipeline, plan):
        self.pipeline = pipeline
        self.plan = plan
        self.urls: list[str] = []
        self.options: TaskOptions | None = None
        FakeEngine.created.append(self)

    async def process(self, urls, options, *, progress):
        self.urls = list(urls)
        self.options = options
        await progress(ProgressEvent(urls[0], Stage.METADATA, "Metadata read", 0.5))
        return list(self.results)


def install_fake_runtime(monkeypatch, results):
    FakeEngine.created = []
    FakeEngine.results = list(results)
    pipeline = object()
    monkeypatch.setattr(cli, "TranscriptPipeline", lambda: pipeline)
    monkeypatch.setattr(cli, "YouTubeTextEngine", FakeEngine)
    monkeypatch.setattr(
        cli,
        "detect_host",
        lambda: HostProfile("Darwin", "arm64", 48 * 1024**3, 12),
    )
    return pipeline


def test_help_and_version_do_not_require_a_url():
    runner = CliRunner()

    help_result = runner.invoke(cli.main, ["--help"])
    version_result = runner.invoke(cli.main, ["--version"])

    assert help_result.exit_code == 0
    assert "youtubetext [OPTIONS] URL [URL...]" in help_result.output
    assert "YouTube or Bilibili" in help_result.output
    assert "summary" not in help_result.output.lower()
    assert version_result.exit_code == 0
    assert "0.1.0" in version_result.output


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ((URL_1,), True),
        ((URL_1, URL_2, "--json"), True),
        ((), False),
        (("--help",), False),
        (("-h",), False),
        (("--version",), False),
        (("doctor",), False),
        (("--json", "doctor"), False),
        (("--doctor", "--json"), False),
    ],
)
def test_only_url_work_requires_a_supervised_worker(arguments, expected):
    assert cli.requires_supervised_worker(arguments) is expected


def test_supervised_worker_injects_its_run_temp_root(monkeypatch, tmp_path):
    run_root = tmp_path / "supervised-run"
    captured: dict[str, object] = {}

    def pipeline_factory(**options):
        captured.update(options)
        return object()

    install_fake_runtime(monkeypatch, [successful_result(URL_1, tmp_path)])
    monkeypatch.setattr(cli, "TranscriptPipeline", pipeline_factory)
    monkeypatch.setenv(RUN_TEMP_ENV, str(run_root))

    result = CliRunner().invoke(cli.main, [URL_1])

    assert result.exit_code == 0, result.output
    assert captured == {"temp_root": run_root}


def test_command_entrypoint_preserves_the_supervised_worker_exit_code(monkeypatch):
    captured: list[list[str]] = []

    def supervise(command):
        captured.append(list(command))
        return 7

    monkeypatch.delenv(cli.SUPERVISED_ENV, raising=False)
    monkeypatch.setattr(cli, "run_supervised", supervise)
    monkeypatch.setattr(sys, "argv", ["youtubetext", URL_1, "--json"])

    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()

    assert stopped.value.code == 7
    assert captured == [
        [sys.executable, "-m", "youtubetext.cli", URL_1, "--json"]
    ]


@pytest.mark.parametrize("arguments", [("--help",), ("--version",), ("doctor",)])
def test_command_entrypoint_runs_lightweight_commands_inline(monkeypatch, arguments):
    report = DoctorReport(
        checks=(DiagnosticCheck("macos", True, True, "macOS detected"),),
        whisper_models=(),
    )
    monkeypatch.delenv(cli.SUPERVISED_ENV, raising=False)
    monkeypatch.setattr(cli, "diagnose", lambda: report)
    monkeypatch.setattr(
        cli,
        "run_supervised",
        lambda _command: (_ for _ in ()).throw(AssertionError("must stay inline")),
    )
    monkeypatch.setattr(sys, "argv", ["youtubetext", *arguments])

    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()

    assert stopped.value.code == 0


def test_installed_command_uses_the_supervising_entrypoint():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert project["project"]["scripts"]["youtubetext"] == "youtubetext.cli:entrypoint"


def test_missing_url_is_a_usage_error():
    result = CliRunner().invoke(cli.main, [])

    assert result.exit_code == 2
    assert "provide at least one URL" in result.output


def test_options_are_forwarded_and_auto_model_comes_from_capacity_plan(
    monkeypatch, tmp_path
):
    output = tmp_path / "exports"
    pipeline = install_fake_runtime(
        monkeypatch,
        [successful_result(URL_1, output), successful_result(URL_2, output)],
    )

    result = CliRunner().invoke(
        cli.main,
        [
            URL_1,
            URL_2,
            "--output",
            str(output),
            "--mode",
            "hybrid",
            "--language",
            "zh-Hant",
            "--caption-language",
            "zh-Hant",
            "--caption-language",
            "en",
            "--whisper-model",
            "auto",
            "--jobs",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    engine = FakeEngine.created[0]
    assert engine.pipeline is pipeline
    assert engine.urls == [URL_1, URL_2]
    assert engine.plan.task_slots == 2
    assert engine.options == TaskOptions(
        mode=ProcessingMode.HYBRID,
        language="zh-Hant",
        preferred_caption_languages=("zh-Hant", "en"),
        whisper_model="large-v3-turbo",
        output_dir=output,
    )
    assert result.output.index(URL_1) < result.output.index("Video")
    assert result.output.count("✓") == 2


def test_explicit_whisper_model_is_preserved(monkeypatch, tmp_path):
    install_fake_runtime(monkeypatch, [successful_result(URL_1, tmp_path)])

    result = CliRunner().invoke(
        cli.main,
        [URL_1, "--whisper-model", "base", "--mode", "whisper"],
    )

    assert result.exit_code == 0, result.output
    assert FakeEngine.created[0].options.whisper_model == "base"


def test_partial_failure_preserves_order_and_exits_nonzero(monkeypatch, tmp_path):
    install_fake_runtime(
        monkeypatch,
        [
            successful_result(URL_1, tmp_path, "First title"),
            TaskResult(url=URL_2, error="subtitle extraction failed"),
        ],
    )

    result = CliRunner().invoke(cli.main, [URL_1, URL_2])

    assert result.exit_code == 1
    assert result.output.index("First title") < result.output.index(URL_2)
    assert "subtitle extraction failed" in result.output


def test_json_output_is_ordered_and_contains_no_progress(monkeypatch, tmp_path):
    install_fake_runtime(
        monkeypatch,
        [
            successful_result(URL_1, tmp_path, "第一条"),
            TaskResult(url=URL_2, error="failed"),
        ],
    )

    result = CliRunner().invoke(cli.main, [URL_1, URL_2, "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert [item["url"] for item in payload["results"]] == [URL_1, URL_2]
    assert payload["results"][0]["title"] == "第一条"
    assert payload["results"][0]["output"]["text"].endswith("transcript.txt")
    assert payload["results"][1]["error"] == "failed"
    assert "Metadata read" not in result.output


def test_doctor_command_calls_diagnose_without_url(monkeypatch, tmp_path):
    called = 0
    report = DoctorReport(
        checks=(DiagnosticCheck("macos", True, True, "macOS detected"),),
        whisper_models=(
            CachedModelStatus("small", True, tmp_path / "small", 1, 2),
        ),
    )

    def fake_diagnose():
        nonlocal called
        called += 1
        return report

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)

    result = CliRunner().invoke(cli.main, ["doctor"])

    assert result.exit_code == 0
    assert called == 1
    assert "YouTubeText doctor" in result.output
    assert "small" in result.output


def test_doctor_json_and_failed_requirement_exit_nonzero(monkeypatch):
    report = DoctorReport(
        checks=(DiagnosticCheck("ffmpeg", False, True, "not found"),),
        whisper_models=(),
    )
    monkeypatch.setattr(cli, "diagnose", lambda: report)

    result = CliRunner().invoke(cli.main, ["--doctor", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["ready"] is False


def test_invalid_jobs_is_rejected_before_runtime(monkeypatch):
    monkeypatch.setattr(
        cli,
        "detect_host",
        lambda: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    result = CliRunner().invoke(cli.main, [URL_1, "--jobs", "9"])

    assert result.exit_code == 2
    assert "not in the range 0<=x<=8" in result.output
