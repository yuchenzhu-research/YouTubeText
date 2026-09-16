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
from .planning import PlanResult, ProcessingPlan
from .progress import ProgressEvent, discard_progress
from .resume import CacheCleanup, CacheUsage, LocalResumeStore
from .runtime import CapacityPlan, detect_host
from .sources import COOKIE_BROWSERS, YtDlpAuth

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
        "Export timestamped and clean transcripts from YouTube or Bilibili URLs.\n\n"
        "Run 'youtubetext doctor' to inspect local requirements, or "
        "'youtubetext cache' to inspect local resume storage."
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
    help="Source language hint; not translation or script conversion.",
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
@click.option(
    "--cookies-from-browser",
    type=click.Choice(COOKIE_BROWSERS, case_sensitive=False),
    help="Use login cookies from a local browser profile.",
)
@click.option(
    "--cookies-file",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        path_type=Path,
    ),
    help="Use a Netscape-format cookies file.",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Reuse a compatible completed transcript from the local cache.",
)
@click.option(
    "--plan",
    "plan_only",
    is_flag=True,
    help="Inspect metadata and forecast processing without downloading files.",
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
    cookies_from_browser: str | None,
    cookies_file: Path | None,
    resume: bool,
    plan_only: bool,
    json_output: bool,
    doctor_mode: bool,
) -> None:
    """Run YouTubeText or its offline environment doctor."""

    doctor_command = len(urls) == 1 and urls[0].casefold() == "doctor"
    cache_command = bool(urls) and urls[0].casefold() == "cache"
    if doctor_mode or doctor_command:
        if doctor_mode and urls:
            raise click.UsageError("--doctor does not accept URL arguments")
        _run_doctor(json_output=json_output)
        return
    if urls and urls[0].casefold() == "doctor":
        raise click.UsageError("the doctor command does not accept URL arguments")
    if cache_command:
        _run_cache(urls[1:], json_output=json_output)
        return
    if not urls:
        raise click.UsageError(
            "provide at least one URL, or run 'youtubetext doctor' / "
            "'youtubetext cache'"
        )
    if cookies_from_browser and cookies_file is not None:
        raise click.UsageError(
            "choose either --cookies-from-browser or --cookies-file, not both"
        )
    if resume and cookies_from_browser:
        raise click.UsageError(
            "--resume cannot be combined with --cookies-from-browser because "
            "browser account changes cannot be safely isolated; use --cookies-file "
            "or run without --resume"
        )
    if plan_only and resume:
        raise click.UsageError(
            "--plan does not inspect resume state; remove --resume to view the "
            "fresh-run plan"
        )

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
        auth = (
            YtDlpAuth(browser=cookies_from_browser or "", cookie_file=cookies_file)
            if cookies_from_browser or cookies_file is not None
            else None
        )
        engine = YouTubeTextEngine(_transcript_pipeline(auth, resume=resume), plan)
        console = Console(stderr=True, highlight=False)
        if plan_only:
            plan_results = asyncio.run(engine.plan(list(urls), options))
        else:
            progress = discard_progress if json_output else _progress_sink(console)
            results = asyncio.run(engine.process(list(urls), options, progress=progress))
    except click.ClickException:
        raise
    except Exception as exc:
        _fatal_error(exc, json_output=json_output)
        return

    if plan_only:
        ready = all(_plan_result_ready(result) for result in plan_results)
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "success": ready,
                        "results": [
                            _plan_result_payload(result) for result in plan_results
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            _render_plans(plan_results, console)
        if not ready:
            raise click.exceptions.Exit(1)
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
        plan_only = bool(context.params.get("plan_only"))
    finally:
        context.close()

    doctor_command = len(urls) == 1 and urls[0].casefold() == "doctor"
    cache_command = bool(urls) and urls[0].casefold() == "cache"
    return (
        bool(urls)
        and not doctor_mode
        and not doctor_command
        and not cache_command
        and not plan_only
    )


def _transcript_pipeline(
    auth: YtDlpAuth | None = None,
    *,
    resume: bool = False,
) -> TranscriptPipeline:
    run_root = os.environ.get(RUN_TEMP_ENV, "").strip()
    options: dict[str, object] = {}
    temporary_root = Path(run_root) if run_root else None
    if run_root:
        options["temp_root"] = temporary_root
    if auth is not None:
        options["auth"] = auth
    if resume:
        scope = auth.cache_scope() if auth is not None else "anonymous"
        options["resume_store"] = LocalResumeStore(
            auth_scope=scope,
            temp_root=temporary_root,
        )
    return TranscriptPipeline(**options)


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


def _run_cache(arguments: tuple[str, ...], *, json_output: bool) -> None:
    normalized = tuple(argument.casefold() for argument in arguments)
    if normalized not in {(), ("status",), ("clear-incomplete",)}:
        raise click.UsageError(
            "use 'youtubetext cache', 'youtubetext cache status', or "
            "'youtubetext cache clear-incomplete'"
        )
    store = LocalResumeStore()
    if normalized == ("clear-incomplete",):
        try:
            cleanup = store.clear_incomplete()
        except NotImplementedError as exc:
            _fatal_error(exc, json_output=json_output)
            return
        if json_output:
            click.echo(json.dumps(cleanup.as_dict(), ensure_ascii=False))
        else:
            _render_cache_cleanup(cleanup, Console(highlight=False))
        if cleanup.failed_tasks:
            raise click.exceptions.Exit(1)
        return

    usage = store.usage()
    if json_output:
        click.echo(json.dumps(usage.as_dict(), ensure_ascii=False))
        return
    _render_cache_usage(usage, Console(highlight=False))


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


def _plan_result_ready(result: PlanResult) -> bool:
    return result.succeeded and result.plan is not None and result.plan.processable


def _render_plans(results: list[PlanResult], console: Console) -> None:
    console.print("[bold]YouTubeText preflight plan[/bold]")
    for result in results:
        if not result.succeeded or result.plan is None:
            detail = result.error or "metadata inspection did not produce a plan"
            console.print(f"[red]✗[/red] {result.url}: {detail}")
            continue

        plan = result.plan
        mark = "[green]✓[/green]" if plan.processable else "[yellow]![/yellow]"
        console.print(f"{mark} {plan.metadata.title}")
        console.print(
            f"  Source: {plan.metadata.platform} · "
            f"{_format_duration(plan.metadata.duration_seconds)}"
        )
        if plan.advertised_subtitle is None:
            console.print("  Caption: none advertised")
        else:
            subtitle = plan.advertised_subtitle
            console.print(
                f"  Caption: {subtitle.kind.value} {subtitle.language} "
                "(advertised, not validated)"
            )
        console.print(f"  Route: {plan.route.value}")
        console.print(
            "  Downloads: subtitle "
            f"{plan.subtitle_download_requirement.value}; "
            f"media {_format_planned_media(plan)}"
        )
        console.print(f"  Cache assumption: {plan.cache_assumption.value}")
        console.print(f"  Note: {plan.reason}")


def _format_planned_media(plan: ProcessingPlan) -> str:
    requirement = plan.media_download_requirement.value
    if plan.media_purpose is None:
        return requirement
    detail = f"{requirement} ({plan.media_purpose.value}"
    if plan.fallback_media_purpose is not None:
        detail += f"; fallback {plan.fallback_media_purpose.value}"
    return detail + ")"


def _format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def _render_doctor(report: DoctorReport, console: Console) -> None:
    console.print("[bold]YouTubeText doctor[/bold]")
    for check in report.checks:
        mark = "[green]✓[/green]" if check.ok else "[red]✗[/red]"
        requirement = "" if check.required else " [dim](optional)[/dim]"
        console.print(f"{mark} {check.key}{requirement}: {check.detail}")
    if report.whisper_models:
        cached = [model.name for model in report.whisper_models if model.cached]
        model_detail = ", ".join(cached) if cached else "none (downloaded on first use)"
        console.print(f"Whisper cache: {model_detail}")
    status = "ready" if report.ready else "not ready"
    style = "green" if report.ready else "red"
    console.print(f"[{style}]{status}[/{style}]")


def _render_cache_usage(usage: CacheUsage, console: Console) -> None:
    console.print("[bold]YouTubeText cache[/bold]")
    console.print(f"Location: {usage.root}")
    console.print(
        "Completed transcripts: "
        f"{usage.transcript_count} ({_format_bytes(usage.transcript_bytes)})"
    )
    console.print(
        f"Incomplete tasks: {usage.task_count} ({_format_bytes(usage.task_bytes)})"
    )
    console.print(f"Total: {_format_bytes(usage.total_bytes)}")


def _render_cache_cleanup(cleanup: CacheCleanup, console: Console) -> None:
    console.print("[bold]YouTubeText cache cleanup[/bold]")
    console.print(f"Location: {cleanup.root}")
    console.print(
        "Removed incomplete tasks: "
        f"{cleanup.removed_tasks} ({_format_bytes(cleanup.removed_bytes)})"
    )
    console.print(f"Skipped active tasks: {cleanup.active_tasks}")
    console.print(f"Failed removals: {cleanup.failed_tasks}")
    console.print("Completed transcripts were not changed.")


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for index, unit in enumerate(units):
        if amount < 1024 or index == len(units) - 1:
            return f"{int(amount)} B" if index == 0 else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


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
                "warnings": list(result.transcript.warnings),
            }
        )
    if result.output is not None:
        payload["output"] = {
            "directory": str(result.output.directory),
            "markdown": str(result.output.markdown),
            "clean_markdown": str(result.output.clean_markdown),
            "text": str(result.output.text),
            "metadata": str(result.output.metadata),
        }
    return payload


def _plan_result_payload(result: PlanResult) -> dict[str, Any]:
    return {
        "url": result.url,
        "success": _plan_result_ready(result),
        "error": result.error or None,
        "plan": result.plan.as_dict() if result.plan is not None else None,
    }


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
