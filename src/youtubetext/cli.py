"""Terminal interface for YouTubeText."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from . import __version__
from ._supervisor import RUN_TEMP_ENV, SUPERVISED_ENV, run_supervised
from .acquisition import TranscriptPipeline
from .doctor import DoctorReport, diagnose
from .domain import ProcessingMode, TaskOptions, TaskResult
from .engine import YouTubeTextEngine
from .progress import ProgressEvent, discard_progress
from .runtime import CapacityPlan, detect_host

LANGUAGES = (
    "auto",
    "en",
    "zh-Hans",
    "zh-Hant",
    "es",
    "ja",
    "ko",
    "fr",
    "de",
    "pt",
    "it",
    "ru",
    "ar",
    "hi",
    "vi",
)
WHISPER_MODELS = ("auto", "base", "small", "large-v3-turbo")


@click.command(
    name="youtubetext",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Extract timestamped text from one or more YouTube or Bilibili URLs.\n\n"
        "Run 'youtubetext doctor' to inspect local requirements without a URL."
    ),
)
@click.argument("urls", nargs=-1, metavar="URL [URL...]")
@click.option(
    "--output",
    "output_dir",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("YouTubeText-output"),
    show_default=True,
    help="Directory for transcript files.",
)
@click.option(
    "--mode",
    type=click.Choice(tuple(mode.value for mode in ProcessingMode), case_sensitive=False),
    default=ProcessingMode.AUTO.value,
    show_default=True,
    help="Transcript acquisition mode.",
)
@click.option(
    "--language",
    type=click.Choice(LANGUAGES, case_sensitive=False),
    default="auto",
    show_default=True,
    help="Spoken or visible text language.",
)
@click.option(
    "--caption-language",
    "caption_languages",
    type=click.Choice(LANGUAGES[1:], case_sensitive=False),
    multiple=True,
    help="Preferred platform-caption language; repeat to add fallbacks.",
)
@click.option(
    "--whisper-model",
    type=click.Choice(WHISPER_MODELS, case_sensitive=False),
    default="auto",
    show_default=True,
    help="Local Whisper model. Auto follows the host capacity plan.",
)
@click.option(
    "--jobs",
    "-j",
    type=click.IntRange(0, 8),
    default=0,
    show_default=True,
    help="Concurrent URL jobs; 0 selects a safe value automatically.",
)
@click.option("--json", "json_output", is_flag=True, help="Write machine-readable JSON.")
@click.option(
    "--doctor",
    "doctor_mode",
    is_flag=True,
    is_eager=True,
    help="Inspect local requirements without processing a URL.",
)
@click.version_option(__version__, prog_name="YouTubeText")
def main(
    urls: tuple[str, ...],
    output_dir: Path,
    mode: str,
    language: str,
    caption_languages: tuple[str, ...],
    whisper_model: str,
    jobs: int,
    json_output: bool,
    doctor_mode: bool,
) -> None:
    """Run YouTubeText or its offline environment doctor."""

    doctor_command = len(urls) == 1 and urls[0].casefold() == "doctor"
    if doctor_mode or doctor_command:
        if doctor_mode and urls:
            raise click.UsageError("--doctor does not accept URL arguments")
        _run_doctor(json_output=json_output)
        return
    if urls and urls[0].casefold() == "doctor":
        raise click.UsageError("the doctor command does not accept URL arguments")
    if not urls:
        raise click.UsageError("provide at least one URL, or run 'youtubetext doctor'")

    try:
        plan = CapacityPlan.for_host(detect_host(), requested_jobs=jobs)
        selected_model = plan.whisper_model if whisper_model == "auto" else whisper_model
        options = TaskOptions(
            mode=ProcessingMode(mode),
            language=language,
            preferred_caption_languages=tuple(caption_languages),
            whisper_model=selected_model,
            output_dir=output_dir,
        )
        engine = YouTubeTextEngine(_transcript_pipeline(), plan)
        console = Console(stderr=True, highlight=False)
        progress = discard_progress if json_output else _progress_sink(console)
        results = asyncio.run(engine.process(list(urls), options, progress=progress))
    except click.ClickException:
        raise
    except Exception as exc:
        _fatal_error(exc, json_output=json_output)
        return

    succeeded = all(result.succeeded for result in results)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "success": succeeded,
                    "results": [_result_payload(result) for result in results],
                },
                ensure_ascii=False,
            )
        )
    else:
        _render_results(results, console)
    if not succeeded:
        raise click.exceptions.Exit(1)


def requires_supervised_worker(arguments: tuple[str, ...]) -> bool:
    """Return whether command-line arguments describe URL processing work."""

    if any(argument in {"-h", "--help", "--version"} for argument in arguments):
        return False

    parsed_arguments = list(arguments)
    try:
        context = main.make_context(
            "youtubetext",
            parsed_arguments,
            resilient_parsing=True,
        )
    except click.ClickException:
        return False
    try:
        urls = tuple(context.params.get("urls") or ())
        doctor_mode = bool(context.params.get("doctor_mode"))
    finally:
        context.close()

    doctor_command = len(urls) == 1 and urls[0].casefold() == "doctor"
    return bool(urls) and not doctor_mode and not doctor_command


def _transcript_pipeline() -> TranscriptPipeline:
    run_root = os.environ.get(RUN_TEMP_ENV, "").strip()
    if run_root:
        return TranscriptPipeline(temp_root=Path(run_root))
    return TranscriptPipeline()


def _run_doctor(*, json_output: bool) -> None:
    try:
        report = diagnose()
    except Exception as exc:
        _fatal_error(exc, json_output=json_output)
        return

    if json_output:
        click.echo(json.dumps(report.as_dict(), ensure_ascii=False))
    else:
        _render_doctor(report, Console(highlight=False))
    if not report.ready:
        raise click.exceptions.Exit(report.exit_code)


def _progress_sink(console: Console):
    async def emit(event: ProgressEvent) -> None:
        fraction = ""
        if event.fraction is not None:
            fraction = f" {max(0, min(100, round(event.fraction * 100)))}%"
        console.print(
            f"[cyan]{event.stage.value:>9}[/cyan] "
            f"[dim]{event.url}[/dim] {event.message}{fraction}"
        )

    return emit


def _render_results(results: list[TaskResult], console: Console) -> None:
    for result in results:
        if result.succeeded:
            assert result.transcript is not None
            assert result.output is not None
            console.print(
                f"[green]✓[/green] {result.transcript.metadata.title} "
                f"→ {result.output.directory}"
            )
        else:
            detail = result.error or "transcript was not produced"
            console.print(f"[red]✗[/red] {result.url}: {detail}")


def _render_doctor(report: DoctorReport, console: Console) -> None:
    console.print("[bold]YouTubeText doctor[/bold]")
    for check in report.checks:
        mark = "[green]✓[/green]" if check.ok else "[red]✗[/red]"
        requirement = "" if check.required else " [dim](optional)[/dim]"
        console.print(f"{mark} {check.key}{requirement}: {check.detail}")
    cached = [model.name for model in report.whisper_models if model.cached]
    model_detail = ", ".join(cached) if cached else "none (downloaded on first use)"
    console.print(f"Whisper cache: {model_detail}")
    status = "ready" if report.ready else "not ready"
    style = "green" if report.ready else "red"
    console.print(f"[{style}]{status}[/{style}]")


def _result_payload(result: TaskResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": result.url,
        "success": result.succeeded,
        "error": result.error or None,
    }
    if result.transcript is not None:
        payload.update(
            {
                "title": result.transcript.metadata.title,
                "platform": result.transcript.metadata.platform,
                "language": result.transcript.language,
                "method": result.transcript.method.value,
            }
        )
    if result.output is not None:
        payload["output"] = {
            "directory": str(result.output.directory),
            "markdown": str(result.output.markdown),
            "text": str(result.output.text),
            "metadata": str(result.output.metadata),
        }
    return payload


def _fatal_error(exc: Exception, *, json_output: bool) -> None:
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    if json_output:
        click.echo(json.dumps({"success": False, "error": detail}, ensure_ascii=False))
        raise click.exceptions.Exit(1)
    raise click.ClickException(detail)


def entrypoint() -> None:
    """Run lightweight commands inline and isolate URL work in a child process."""

    arguments = tuple(sys.argv[1:])
    already_supervised = os.environ.get(SUPERVISED_ENV) == "1"
    if already_supervised or not requires_supervised_worker(arguments):
        main()
        return

    exit_code = run_supervised(
        [sys.executable, "-m", "youtubetext.cli", *arguments]
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
