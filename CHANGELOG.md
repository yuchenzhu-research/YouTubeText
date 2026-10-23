# Changelog

## 0.2.0 — 2026-10-23

### Added

- Local audio/video paths and `file://` inputs, read in place with SHA-256 content
  identities; adjacent SRT/WebVTT sidecars use the `local-captions` method.
- SRT, WebVTT, and full `segments.json` exports alongside Markdown, clean
  Markdown, TXT, and metadata. CLI JSON reports all seven paths and completeness.
- Long-gap completeness reports and warnings. Automatic mode checks audio
  silence and supplements suspicious intervals with Whisper while preserving
  existing text; forced captions/OCR modes retain the selected method.
- `--ocr-profile fast|balanced|accurate` and normalized `--ocr-region` controls.
  OCR streams at most 32 cropped frames at a time with fixed sampling density,
  including long videos, and reuses exactly identical crops without losing times.
- Resumable Whisper chunks of up to 300 seconds with 2-second overlap, private
  atomic checkpoints, content/model/backend/recipe invalidation, and conservative
  boundary deduplication. Only the active temporary audio chunk is kept on disk.
- macOS ARM64 wheel packaging with a bundled Apple Vision helper, source archive
  packaging, and CI builds with fresh-install smoke checks for macOS/Windows wheels.
- [MIT license](LICENSE) and refreshed English, Traditional Chinese, Simplified
  Chinese, Spanish, and Japanese documentation.

### Changed

- Caption acquisition tries up to five advertised language/kind candidates in
  preference order before reporting bounded fallback warnings.
- Local preflight reads FFprobe metadata and source content without network or
  media/model downloads. OCR/hybrid reject audio-only inputs.
- Pending OCR-gap verification preserves incomplete resume state for retry.
  Original local media is never removed by cache cleanup.

### Validation and limits

- A live 1,203-second TED video yielded 427 manual English caption segments and
  all seven exports. Reparsed SRT/WebVTT text matched the segment JSON, with at
  most 0.51 milliseconds of serialization timing error. The timing-only fixture
  retains no caption text and covers original and injected omission cases.
- Actual MLX Whisper `base` recognized a generated 17-word English clip with
  zero word edits. A generated 307.5-second clip was interrupted after its first
  completed 300-second chunk and resumed only the remaining chunk. Native Apple
  Vision in `accurate` mode recognized all three 2-second captions in a generated
  6-second video.
- These are platform-caption and small generated-media checks. Broader real-video
  OCR/Whisper CER/WER accuracy remains unmeasured; timing/silence checks do not
  prove that speech is missing. Even 0.25-second OCR sampling can miss brief captions.
- The macOS wheel passed a local fresh install outside the checkout. Both macOS
  and Windows wheels also passed CI builds and fresh isolated installation in the
  [release workflow](https://github.com/yuchenzhu-research/YouTubeText/actions/runs/39875296716).
  Installation checks run `--help` and `doctor` without downloading speech models
  or running ASR. PyPI and GitHub Release publication have not been performed.
