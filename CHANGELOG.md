# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Linux and macOS support for the stage runners. Existing subtitles are skipped
  at inventory, and progress is recorded per video.
- Runtime settings from `subgen.toml` and `SUBGEN_*` variables.
- Device and engine selection for ASR, alignment, and translation.
- One `subgen` command for the full pipeline, plus `subgen serve` for vLLM on
  CUDA or MLX on Apple Silicon.
- `subgen web`, a guided local console with a readiness checklist, job
  creation, live progress, job management, a folder browser, and a validated
  settings editor.
- Any OpenAI-compatible translation model, with an optional separate reviewer.
- Chinese README.
- MIT license.

### Removed

- The unused append-only ledger, artifact store, generic runner, and their
  filesystem guarantees. Publication now uses a pre-check and `os.replace`.

### Changed

- Replaced the Makefile with uv-managed environments and a committed `uv.lock`.
- Moved development tools into a uv dependency group.
- Replaced the `ffmpeg` and `ffprobe` subprocesses with PyAV. The package no
  longer needs system media binaries. Re-run inventory once after upgrading
  because the probe backend, the timeout field names, and the schema versions
  changed.

## [0.1.0]

### Added

- Package metadata, console entry points, and a `src` layout.
- Project documentation for architecture, development, and operations.
- Continuous integration for lint, format, and tests.
- Ruff configuration and a formatted tree.

### Changed

- Deployment assets moved to `deploy/scripts` and `deploy/systemd`.
- Test fixtures no longer depend on the production work root, and optional
  dependency tests skip when the package is missing.
