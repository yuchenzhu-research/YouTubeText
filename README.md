# YouTubeText

English | [繁體中文](docs/README.zh-Hant.md) | [简体中文](docs/README.zh-Hans.md) | [Español](docs/README.es.md) | [日本語](docs/README.ja.md)

YouTubeText is a terminal tool for Apple Silicon macOS and Windows x64 that turns
YouTube and Bilibili videos and local audio/video files into clean, timestamped transcripts. It exports a timestamped
Markdown file, a clean Markdown file without timestamps, a text file,
SRT/WebVTT subtitles, structured transcript segments, and metadata.

YouTubeText extracts transcripts. It does not summarize videos, analyze
arguments, or require an LLM, Ollama, or a cloud AI API.

## Features

- Submit URLs, local paths, and `file://` inputs in one queue.
- Detect YouTube and Bilibili sources and read their metadata.
- Prefer manual or automatic platform captions when available.
- Try up to five advertised caption tracks before falling back to local recognition.
- Read adjacent SRT/WebVTT files as `local-captions` without copying source media.
- Read burned-in subtitles with Apple Vision on macOS or RapidOCR on Windows
  when no platform track is usable.
- Fall back to local MLX Whisper on macOS or faster-whisper on Windows when OCR
  is not usable.
- Export both timestamped and clean, no-timestamp Markdown transcripts.
- Export SRT and WebVTT subtitles and full transcript segments as JSON.
- Automatically choose input concurrency from physical memory and, on Windows,
  CPU capacity.
- Reuse completed transcripts, resume interrupted media downloads, and reuse
  completed OCR batches and Whisper chunks.
- Choose OCR sampling density and the subtitle region with `--ocr-profile` and `--ocr-region`.
- Inspect an input with `--plan` before transcription or media downloads.
- Isolate failures: one bad input does not cancel the rest of the queue.

## Pipeline

```text
INPUT queue (URLs, local paths, file://)
  └─ Read source metadata
       ├─ Usable platform caption / local sidecar → Clean cues → Export
       └─ No usable caption
            ├─ --resume cache hit ─────────────────────────────→ Export
            ├─ Local audio → Whisper → Export
            └─ Remote/local video → streamed OCR
                                      ├─ usable → Export
                                      └─ unusable → Whisper → Export
```

Caption and usable-OCR routes include the conditional gap checks below before export.
If a platform caption download or parse fails, up to five advertised language/kind
candidates are tried in the existing preference order. When all attempted tracks
fail, bounded warnings explain the fallback; no additional cloud API is used.

Platform captions with no suspicious timing gaps need no media download. Caption
files are small, temporary inputs. In `auto` mode, suspicious caption gaps can
trigger an audio-only download for verification and Whisper; this audio is kept
in a disposable temporary directory. Normal runs keep OCR and Whisper media in a supervised,
per-run temporary directory and remove it when the run ends. With `--resume`,
media is stored under the user cache's `tasks/` directory, retained after an
interruption, and removed after success unless OCR gap verification is still pending.

## Completeness checks

Before export, YouTubeText checks for long gaps before, between, and after
transcript segments. The gap threshold is 10% of the video duration,
with a minimum of 60 seconds and a maximum of 300 seconds. Videos
shorter than 90 seconds or with unknown duration are not judged by this rule.

In `auto` mode, a usable platform caption, local sidecar, or OCR result with suspicious gaps
receives an FFmpeg silence check. OCR reuses its analysis video; remote platform captions download only audio for
this check. Local sidecars use the original media in place. Quiet gaps keep the original text.
If the check finds non-silent audio or cannot verify the gap, Whisper runs and
only its cues overlapping suspicious gaps are merged, preserving the original text. If Whisper fails,
the original transcript is retained with an explicit uncertainty warning.
Forced `captions` and `ocr` modes retain the selected method and report long-gap
warnings without starting additional speech recognition.

Both JSON files and the `--json` result include a `completeness` report with `status`, `coverage_ratio`,
`duration_seconds`, `covered_seconds`, and `long_gaps` (each with `start_seconds`
and `end_seconds`). A status such as `needs-review` or `unverified` records the
assessment; no status guarantees that the transcript is complete. All warnings
are also shown in the terminal.

Live validation used a [TED video](https://www.youtube.com/watch?v=iG9CE55wbtY)
(1,203 seconds, 427 manual English caption segments) and exported all seven files.
Re-parsing SRT and WebVTT reproduced the `segments.json` text with at most
0.51 milliseconds of timing error. The [timing-only fixture](tests/fixtures/ted_caption_timing.json)
retains no caption text and supports four offline regressions: original timings
and injected leading, internal, and trailing omissions. This covers platform-caption
export; broader real-video OCR/Whisper CER/WER benchmarks remain unmeasured.

Generated-media smoke tests also ran actual MLX Whisper `base` on a 17-word
English clip (zero word edits) and native Apple Vision with `accurate` on a
6-second video (all three 2-second captions recognized). These small generated
fixtures do not establish broader real-video accuracy.

An actual 307.5-second generated-audio run was interrupted after its first
300-second chunk and resumed only the remaining chunk; temporary chunks were removed.

## Requirements

- Apple Silicon Mac (`arm64`) with macOS 13 or later, or 64-bit x86 Windows
- Python 3.11–3.14
- FFmpeg and FFprobe available on `PATH`
- For Mac source builds only: Xcode Command Line Tools to build the Apple Vision OCR helper
- `uv` is recommended

On macOS, install FFmpeg with Homebrew if it is missing:

```bash
brew install ffmpeg
```

For Mac source builds, install Apple's command-line tools if they are missing:

```bash
xcode-select --install
```

## Installation

Source checkout remains supported on both platforms. A macOS ARM64 wheel for
0.2.0 has been built and smoke-tested in a fresh environment outside the checkout;
it includes the Apple Vision helper and requires no Swift compiler to run.
Windows CI is configured to build a portable wheel; that new wheel has not yet
been verified on Windows. Neither PyPI nor a GitHub Release has been published.

If you have the built macOS wheel, install it with:

```bash
python -m pip install ./youtubetext-0.2.0-py3-none-macosx_13_0_arm64.whl
```

For a built Windows wheel, use PowerShell:

```powershell
python -m pip install .\youtubetext-0.2.0-py3-none-any.whl
```

FFmpeg/FFprobe and Python 3.11–3.14 remain separate requirements. Speech models
are downloaded on first use. Mac source installations, including the
`youtubetext-0.2.0.tar.gz` source archive, still require Xcode Command Line Tools.
The source checkout workflow follows.

On macOS, run:

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

If GitHub SSH is already configured:

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

On Windows x64, install 64-bit Python and FFmpeg first, add FFmpeg's `bin`
directory to `PATH`, then run in PowerShell:

```powershell
git clone https://github.com/yuchenzhu-research/YouTubeText.git
Set-Location YouTubeText
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Both installers create `.venv` and install the host-specific Python dependencies.
The macOS installer also builds the Apple Vision OCR helper; the Windows installer
does not use Swift. Neither installer installs FFmpeg. The macOS installer does not
use `sudo` or install Homebrew automatically.

Check the local environment after installation on macOS:

```bash
./.venv/bin/youtubetext doctor
```

## Quick start

Process one video:

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

Queue YouTube and Bilibili URLs with two concurrent URL tasks:

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

Choose an output directory and hint that the source text is Traditional Chinese:

```bash
./.venv/bin/youtubetext INPUT \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

Return machine-readable results:

```bash
./.venv/bin/youtubetext INPUT --json
```

Show every option:

```bash
./.venv/bin/youtubetext --help
```

On Windows, use the executable in `.venv\Scripts` from PowerShell; for example:

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe INPUT_1 INPUT_2 --jobs 2
.\.venv\Scripts\youtubetext.exe INPUT --plan
.\.venv\Scripts\youtubetext.exe INPUT --resume
.\.venv\Scripts\youtubetext.exe cache
```

The remaining command examples use the macOS executable path. On Windows,
replace `./.venv/bin/youtubetext` with `.\.venv\Scripts\youtubetext.exe`.

## Local inputs

The CLI accepts `INPUT [INPUT...]`: remote URLs, local audio/video paths, or
`file://` URLs on this computer. Paths containing spaces must be quoted.

```bash
./.venv/bin/youtubetext "/path/to/video.mp4" "/path/to/audio.wav" --resume
./.venv/bin/youtubetext "file:///path/to/video.mp4" --plan
```

Original files are read in place and are never copied into task storage or deleted.
A SHA-256 content identity invalidates cached results when source bytes change.
An adjacent file with the same stem, such as `video.vtt` or `video.srt`, is used as
`local-captions` in `auto` or `captions` mode; WebVTT is preferred when both exist.
Without a usable sidecar, `auto` sends audio to Whisper and applies the normal
OCR-then-Whisper policy to video. `ocr` and `hybrid` require a video stream.
Local `--plan` uses FFprobe and reads a content hash without network access,
media downloads, or model downloads.

## Preflight plan

Use `--plan` to inspect what a fresh run would do before any subtitle or media
file is downloaded:

```bash
./.venv/bin/youtubetext INPUT --plan
./.venv/bin/youtubetext INPUT_1 INPUT_2 --plan --json
```

For remote URLs, preflight makes a network metadata request; local inputs are
inspected in place. It reports:

- source metadata and duration;
- the best caption track advertised by the platform;
- the expected caption, OCR, and Whisper route;
- whether subtitle or media downloads are `required`, `conditional`, or `none`;
- the primary media type and any audio fallback.

An advertised track is marked `advertised-unvalidated`: preflight does not
download or parse it, so availability is not yet guaranteed. Preflight does not
download subtitles, audio, video, or Whisper models; does not sample frames or
run OCR/Whisper; does not create an output directory; and does not read or write
resume state.

Plans use the cache assumption `no-resume-reuse`. A later execution with
`--resume` may therefore skip work shown as required. `--plan` and `--resume`
cannot be used together. Multiple inputs are inspected concurrently, results stay
in input order, and one metadata failure does not hide the other plans.

## Resume and cache

Enable reusable local state explicitly:

```bash
./.venv/bin/youtubetext INPUT --resume
```

Remote URLs still check fresh platform captions first; local sidecars are reread.
On a cache miss, yt-dlp can continue an interrupted `.part` download; a completed
media file is reused directly; and the local OCR backend saves raw observations after
each successfully recognized batch of up to 32 frames. A batch containing frame
errors is not checkpointed and is retried on the next run. Sampled images are
deleted batch by batch after recognition; when a checkpoint is written, it is
saved before deletion.

With `--resume`, Whisper checkpoints each completed chunk of up to 300 seconds,
including a 2-second overlap with the next chunk. Only one temporary audio chunk
exists at a time. Retries reuse completed chunks when the media content, model,
language, backend revision, and chunk recipe match. The interrupted chunk is run
again; duplicate overlap cues are removed conservatively without merging different
text or different-time repetitions. Short files can run directly; if local media
duration cannot be determined, Whisper runs directly without chunk checkpoints.

If optional Whisper verification of OCR gaps fails, the original transcript is
still exported, but its media and OCR checkpoints remain incomplete under
`--resume`; no completed transcript is cached. The next `--resume` run retries
verification while reusing successfully downloaded media and completed OCR
batches. Caption verification always uses a disposable temporary directory and
checks fresh platform captions, including when `--resume` is enabled.

On macOS, completed structured transcripts are stored under:

```text
~/Library/Caches/YouTubeText/transcripts/
```

On Windows, the cache is in the user's platform cache directory; run
`youtubetext cache` to see its exact location. Incomplete tasks are stored in a
sibling `tasks/` directory on either platform. On macOS, directories and files
use `0700` and `0600` permissions; Windows uses its own filesystem permissions. Two
processes requesting the same task use one lock: one performs the work and the
other waits to reuse its result. Anonymous requests and different cookie files
use separate cache scopes.

Inspect cache usage without modifying it:

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

On macOS, remove only failed or interrupted task media and OCR/Whisper checkpoints while
retaining all completed transcripts:

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

Active, locked tasks are skipped. Removed temporary media cannot be recovered,
but it can be downloaded again from the original URL. On Windows,
`cache clear-incomplete` is not yet supported and does not delete task data;
`cache` and `cache --json` remain read-only and available. To remove everything
on macOS, including completed transcripts, delete
`~/Library/Caches/YouTubeText/` manually.

## Authentication and cookies

Use the login state from a local browser for restricted videos:

```bash
./.venv/bin/youtubetext INPUT --cookies-from-browser safari
```

Or provide a Netscape-format cookie file:

```bash
./.venv/bin/youtubetext INPUT --cookies-file /path/to/cookies.txt
```

The two options are mutually exclusive. Cookie contents are never written to
the output directory, metadata, JSON results, or resume cache. Cookie files are
copied into memory for each yt-dlp call; the original file is not modified.

Because a browser name cannot reliably identify the active account,
`--cookies-from-browser` cannot be combined with `--resume`. Use
`--cookies-file` when authentication and resume are both required. Safari may
need Full Disk Access for the terminal in macOS Privacy & Security settings.
The Safari example is macOS-specific; on Windows, select an installed browser
supported by yt-dlp.

## Modes

| Mode | Behavior |
| --- | --- |
| `auto` | Prefer platform captions or a local sidecar, then OCR for video and Whisper for audio/fallback. |
| `captions` | Require a usable platform caption track or local SRT/VTT sidecar. |
| `ocr` | Ignore platform captions and read burned-in subtitles only. |
| `whisper` | Skip captions and OCR; run local speech recognition directly. |
| `hybrid` | Run OCR and Whisper; visible captions take priority when their time ranges overlap. |

Force OCR, for example:

```bash
./.venv/bin/youtubetext INPUT --mode ocr --language zh-Hant
```

## Languages

`--language` supports:

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

`--language` hints at the source speech or visible-text language; it does not
select an output language. It guides platform-caption preference, OCR where
available, and Whisper's speech-language choice. Whisper passes both `zh-Hans`
and `zh-Hant` as `zh`, so the result may report `zh` and contain Simplified
Chinese even when `zh-Hant` was selected. It does not translate or convert
between Simplified and Traditional Chinese.

On macOS, Apple Vision uses title and author context to prioritize recognition
languages when `auto` is selected. On Windows, RapidOCR's default PP-OCRv6 small
model recognizes `en`, `zh-Hans`, `zh-Hant`, `es`, `ja`, `fr`, `de`, `pt`, `it`, and
`vi` without switching models; `--language` does not restrict its output to one
language. It does not support OCR for `ko`, `ru`, `ar`, or `hi`: forced OCR mode
reports an error, while `auto` and `hybrid` warn and use Whisper. Platform
captions and Whisper can still use those languages. Both Whisper backends can
detect spoken language automatically.

Set an ordered list of preferred platform-caption languages separately:

```bash
./.venv/bin/youtubetext INPUT \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## Output files

The default output root is `YouTubeText-output/` in the current directory:

```text
YouTubeText-output/
  VIDEO_ID-video-title/
    transcript.md
    transcript-clean.md
    transcript.txt
    transcript.srt
    transcript.vtt
    segments.json
    metadata.json
```

- `transcript.md`: complete Markdown transcript with per-segment timestamps.
- `transcript-clean.md`: complete Markdown transcript without timeline markers.
- `transcript.txt`: plain text with timestamps.
- `transcript.srt`: numbered subtitle cues for players and editors that support SRT.
- `transcript.vtt`: WebVTT subtitle cues for web players and other compatible tools.
- `segments.json`: full transcript text with each segment's start and end times.
- `metadata.json`: source, language, extraction method, segment count, warnings, and completeness assessment.

`segments.json` uses `schema_version: 1` and includes source metadata, language,
extraction method, warnings, completeness assessment, and an ordered `segments` list. Each segment has
`start_seconds`, `end_seconds`, `text`, and `confidence`; times retain their
original precision in seconds. SRT and WebVTT use millisecond timestamps.
`--json` reports the result and output file paths; read `segments.json` for the
transcript itself.

## OCR controls

`--ocr-profile` sets a fixed sampling interval: `fast` uses 1 second, `balanced`
(default) uses 0.5 seconds, and `accurate` uses 0.25 seconds. Two- and three-hour
videos retain the same density; there is no global 2,400-frame cap. Frames stream
in batches of at most 32 and are removed after recognition. Exactly identical
cropped images can reuse OCR results while preserving every sampled timestamp.

`--ocr-region X Y WIDTH HEIGHT` uses coordinates normalized to the frame, measured
from the top-left corner. The default `0 0.55 1 0.45` covers the bottom 45%.
Width and height must be positive, and the region must stay inside the frame.

```bash
./.venv/bin/youtubetext INPUT --ocr-profile accurate --ocr-region 0 0.55 1 0.45
```

This is fixed-density sampling with identical-image reuse. Captions shorter than
the chosen interval can still be missed; a denser profile increases processing work.

## Concurrency and resource control

`--jobs 0` is the default and derives a memory-based input concurrency ceiling:

| Physical memory | Automatic input jobs |
| --- | ---: |
| Less than 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB or more | 4 |

On Windows, CPU capacity can lower the automatic input count further. Override it
with `--jobs 1` through `--jobs 8`. Network work uses at most four slots;
Apple Vision OCR uses at most two on macOS, while RapidOCR uses one on Windows.
Whisper is serialized on both platforms to limit memory and compute contention.

The local OCR backend receives streamed batches of up to 32 cropped frames.
With `--resume`, a successfully recognized batch is checkpointed before deletion; a batch containing frame errors
is not checkpointed and will be retried on the next run. Checkpoints store
timestamps, content hashes, and raw text boxes, never frame images or temporary
paths.

## Whisper models

Whisper runs when captions and burned-in subtitles are unusable, when `auto`
needs to supplement suspicious gaps, or when `whisper` / `hybrid` mode is
selected. A model is downloaded on first use and then reused from a local model cache. On macOS, compatible MLX weights already
present in the standard Hugging Face cache are reused as well. Windows uses
faster-whisper with CTranslate2 weights in a separate cache.

The following download and memory estimates apply to the macOS MLX backend;
Windows model sizes and runtime memory can differ:

| Model | Approximate download | Approximate runtime memory |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

Automatic selection uses `small` on lower-memory Macs and `large-v3-turbo` on
Macs with at least 16 GiB. Windows defaults to `small`. Override the choice
with `--whisper-model`. The Windows doctor checks backend availability but does
not yet inspect the CTranslate2 model cache.

## Development and architecture

Run the complete development workflow:

```bash
./scripts/dev.sh
```

Pass pytest arguments through the script when needed:

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

On Windows, run the development script from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

Primary modules:

- `sources/`: YouTube/Bilibili metadata and bounded platform-caption candidate fallback.
- `inputs.py`: local file metadata, content identity, and sidecar captions.
- `planning.py`: read-only preflight routes and download requirements.
- `media.py`: resumable media downloads and bounded frame sampling.
- `resume.py`: transcript reuse, task locks, OCR/Whisper checkpoints, and task cleanup.
- `ocr/`: Apple Vision or RapidOCR and burned-in caption assembly.
- `asr/`: MLX Whisper or faster-whisper, local model selection, and resumable audio chunks.
- `acquisition.py`: the authoritative captions/OCR/Whisper routing policy.
- `runtime.py`: host resource detection and ordered multi-input scheduling.
- `export.py`: grouped atomic writes for Markdown, TXT, SRT, WebVTT, and JSON outputs.
- `cli.py`: terminal interface.

## Current limitations

- macOS Apple Silicon and Windows x64 pass the automated CI suite. Windows CI
  also runs real local OCR and Whisper models plus an offline CLI-to-export
  transcript. A live-video end-to-end run on a physical Windows machine is
  still pending; public URLs can reject hosted CI runners.
- Windows `cache clear-incomplete` remains unavailable because safe deletion
  across Windows junctions and reparse points needs additional validation.
- Platform access depends on yt-dlp and may be affected by region, account,
  cookies, or platform changes.
- The default OCR crop targets the bottom 45% of the frame. Other layouts can
  use `--ocr-region`; heavily stylized text can still require Whisper mode.
- Bilibili end-to-end transcription has been exercised live on macOS; platform
  caption compatibility still needs broader testing across public videos.
- Whisper resumes at completed chunk boundaries; an interrupted chunk restarts.
- OCR uses fixed sampling intervals, so subtitles shorter than 0.25 seconds can
  still be missed even in `accurate` mode.
- Timing-gap and silence checks do not prove transcription accuracy or missing
  speech. Long pauses and music can require review; no real-video accuracy
  benchmark has yet been established for these checks.

## Reference and notices

The design was informed by [MediaBrief](https://github.com/EvilIrving/mediabrief).
A local reference checkout may be kept under `reference/mediabrief/`; the entire
`reference/` directory is intentionally ignored by Git and is not part of this
repository. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for third-party
software notices.

See [CHANGELOG.md](CHANGELOG.md) for the 0.2.0 changes.

YouTubeText is licensed under the [MIT license](LICENSE).
