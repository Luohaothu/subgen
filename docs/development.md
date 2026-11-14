# Development

## Supported platform

Linux or macOS, Python 3.12 or newer. The stage code uses descriptor-relative
opens with `O_NOFOLLOW` for source identity. The only Linux and macOS branch is
the descriptor path: `/proc/self/fd` on Linux and `/dev/fd` on macOS. Publication
uses `os.replace`. Media probing and extraction use PyAV, so the package needs no
system `ffmpeg` or `ffprobe`. There are no file locks, extended attributes,
no-replace syscalls, or memory-backed snapshots. CI runs the suite on Ubuntu and macOS with
Python 3.12 and 3.13.

## Setup

Install uv, then sync the environment.

```bash
uv sync
```

`uv sync` creates `.venv` and installs the project, the development tools, and
the exact versions in `uv.lock`. The development tools live in a uv dependency
group, so they are installed by default. The GPU runtimes are optional:

```bash
uv sync --extra models
```

That extra installs `funasr`, `nagisa`, `torch`, and `transformers`. On Apple
Silicon, add the MLX translation server with `uv sync --extra models --extra mlx`.
Tests that need the model packages skip when they are absent. Run `uv lock` after
you change dependencies, and commit `uv.lock` with the change.

## Test suite

Run the suite from the repository root.

```bash
uv run pytest
```

The tests use `unittest` classes and `unittest.mock`. pytest is only the runner.
The suite is serial and uses temporary directories plus a small generated media
file, so it needs no database and no network.

| Test file | Covers |
| --- | --- |
| `test_config.py` | Frozen pipeline config, paths, digest coverage |
| `test_inventory_state.py` | Source scanning, probe parsing, inventory snapshots |
| `test_media.py` | PyAV probing, audio selection, extraction |
| `test_segments.py` | VAD normalization, WAV materialization |
| `test_asr.py` | Batch normalization, repetition trimming, quality flags |
| `test_alignment.py` | Token restore, cue building, aligner retries |
| `test_platform_io.py` | Descriptor paths |
| `test_settings.py` | TOML and environment settings, provider validation |
| `test_cli.py` | Single entry point dispatch |
| `test_runner_cli.py` | Portable pipeline command and filters |
| `test_serve_cli.py` | vLLM and MLX server selection |
| `test_translation.py` | Translation policy and Chinese output checks |
| `test_translation_runtime_cli.py` | Two-pass translation runtime and checkpoints |
| `test_srt_publish.py` | SRT parse and render, candidate validation, pre-check publication |
| `test_runtime_cli.py` | Inventory and extraction CLI |
| `test_corpus_*_cli.py` | Corpus stage drivers and migration commands |

The latest full run on Linux (Debian 12, Python 3.11) and macOS reported 278
passed and 2 skipped. The two skips are the production-root path check and the
real torch batch test, which needs the `models` extra.

### Platform notes

The descriptor path is tested on both platforms. Linux uses `/proc/self/fd` and
macOS uses `/dev/fd`. The tests select the path through `platform_io.py`, not
through platform checks scattered across the suite.

`tests/conftest.py` points temporary directories at `/private/tmp` on macOS. The
default `/var/folders` path sits behind a `/var` symlink, and the source path
validator rejects every temporary directory because of it.

Tests that need `torch`, `transformers`, or the real Qwen checkpoints skip when
the `models` extra is absent. Install it to run the full model path:

```bash
uv sync --extra models
```

## Lint and format

```bash
uv run ruff check .                 # lint
uv run ruff format .                # format
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Ruff selects `E4`, `E7`, `E9`, `F`, `I`, and `UP`. The rules catch real errors,
import order, and deprecated syntax. The wider rule sets stay off.

The formatter uses a line length of 88 and double quotes. Install the git hook
when you want the checks to run before each commit:

```bash
uv run pre-commit install
```

## Versioning

The version lives in `src/subtitle_pipeline/__init__.py` as `__version__`, and
`pyproject.toml` reads it through hatch. Bump it before a release and add the
entry to `CHANGELOG.md`. Stage output formats are versioned separately in
`SchemaVersions` and `PromptVersions` inside
`src/subtitle_pipeline/config.py`. Change a schema version whenever a persisted
payload changes shape.

## Commit style

Use a conventional prefix and a short imperative subject. The history reads as
a sequence of reviewable slices, so keep a formatting change separate from a
behaviour change.

## Continuous integration

`.github/workflows/ci.yml` installs uv, runs `uv sync --locked`, and then runs
`uv run ruff check`, `uv run ruff format --check`, and `uv run pytest` on Ubuntu
and macOS for Python 3.12 and 3.13. It needs no GPU, no model files, and no
network beyond the package index.