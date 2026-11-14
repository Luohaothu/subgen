# subgen

[English](README.md) | [简体中文](README.zh-CN.md)

Resumable Japanese-to-Chinese subtitle pipeline for a local video corpus.

subgen inventories a directory of videos, extracts the Japanese audio track,
transcribes it with FunASR, force-aligns the Japanese text, translates and
reviews it into Simplified Chinese, and publishes `<video-stem>.zh.srt` beside
each video. Translation can point at any OpenAI-compatible API.

Each stage writes a per-video record under a local work root. A rerun reuses
completed records, and a failed publish never leaves a partial subtitle in the
corpus.

## Requirements

- Linux or macOS.
- Python 3.12 or newer.
- `uv` for installs and development.

## Install

```bash
uv sync
```

`uv sync` creates `.venv`, installs the project and the development tools, and
pins every package to `uv.lock`. Media probing and audio extraction use PyAV,
so no system `ffmpeg` or `ffprobe` is required.

Activate the environment before running the commands below, or prefix a command
with `uv run`.

Install the local model runtimes on the machine that runs the GPU stages:

```bash
uv sync --extra models
```

On Apple Silicon, add the MLX translation server:

```bash
uv sync --extra models --extra mlx
```

## Configure

Override the defaults in `subgen.toml` in the repository root, in the file
named by `SUBGEN_CONFIG`, or with environment variables.

```toml
[paths]
source_root = "/Volumes/media/videos"
work_root = "/Users/me/.local/share/subgen"
model_root = "/Users/me/.local/share/subgen/models"
asr_model = "/Users/me/.local/share/subgen/models/Fun-ASR-Nano-2512"

[runtime]
device = "auto"
asr_engine = "auto"

[translation]
endpoint = "http://127.0.0.1:8123/v1"
model = "vendor/primary"
revision = "2026-11-01"
review_model = "vendor/reviewer"
review_revision = "2026-11-01"
api_key_env = "OPENAI_API_KEY"
```

| Variable | Meaning |
| --- | --- |
| `SUBGEN_CONFIG` | Path to the settings file |
| `SUBGEN_SOURCE_ROOT` | Read-only video corpus |
| `SUBGEN_WORK_ROOT` | Local work root |
| `SUBGEN_MODEL_ROOT` | Checkpoint directory |
| `SUBGEN_ASR_MODEL` | ASR checkpoint directory |
| `SUBGEN_DEVICE` | `auto`, `cpu`, `cuda`, or `mps` |
| `SUBGEN_ASR_ENGINE` | `auto`, `vllm`, or `funasr` |
| `SUBGEN_TRANSLATION_ENDPOINT` | OpenAI-compatible base URL |
| `SUBGEN_TRANSLATION_MODEL` | Primary translation model ID |
| `SUBGEN_TRANSLATION_REVISION` | Primary model revision |
| `SUBGEN_TRANSLATION_REVIEW_MODEL` | Review model ID |
| `SUBGEN_TRANSLATION_REVIEW_REVISION` | Review model revision |
| `SUBGEN_API_KEY_ENV` | Environment variable that holds the API key |

`model_root` holds the VAD, ASR, aligner, and translation checkpoints in the
directory names the download scripts use. The runtime reads
`state/model_revisions.json` when it exists. Otherwise it derives a revision
from each checkpoint's config file. With a remote translation provider, the
translation revisions come from the provider settings, so the translation
checkpoints are optional.

The default roots are `/home/leo/nas/Other/abc` and
`/home/leo/.local/share/abc-subtitles-recovery-6`. `PipelineConfig.production()`
in `src/subtitle_pipeline/config.py` still describes the original host. A new
host uses `subgen.toml` or the `SUBGEN_*` variables.

## Run the pipeline

Start a local translation server, or point `[translation]` at an endpoint you
already run.

```bash
subgen serve --model-path /path/to/translation/model --model vendor/primary
```

`subgen serve` picks vLLM on CUDA and the MLX server on Apple Silicon when
`mlx-lm` is installed. It prints guidance when the host has no local GPU engine.
It does not start a CPU server. Use llama.cpp, Ollama, or a remote provider
there.

Then run the full pipeline:

```bash
subgen --dry-run
subgen
```

`subgen` runs inventory, extraction, ASR, alignment, both translation passes,
and publication. It accepts `--limit`, `--video-id`, `--skip-inventory`, and
`--skip-extract`. It does not run the Linux audit and cleanup steps.

## Commands

| Command | Purpose |
| --- | --- |
| `subgen` | Run the full corpus pipeline |
| `subgen serve` | Start the vLLM or MLX translation server, or print guidance |

## Runtime matrix

| Host | ASR | Alignment | Translation |
| --- | --- | --- | --- |
| Linux with CUDA | FunASR through vLLM | transformers, bf16, CUDA | vLLM or any OpenAI-compatible API |
| Apple Silicon | FunASR on CPU | transformers, float16, MPS | MLX server or any OpenAI-compatible API |
| CPU only | FunASR on CPU | transformers, float32, CPU | external OpenAI-compatible API |

`device = "auto"` picks CUDA, then MPS, then CPU. `asr_engine = "auto"` picks
vLLM on CUDA and FunASR elsewhere. An explicit value overrides the choice.

## Work root layout

| Path | Contents |
| --- | --- |
| `audio/<video_id>.flac` | 16 kHz mono audio extracted from the source |
| `asr/corpus/<video_id>/` | `vad.json`, `segments.json`, `asr.json`, `alignment.json`, and traces |
| `translation/corpus/<video_id>/` | Draft and reviewed translation evidence |
| `state/` | Inventory snapshot, model revisions, per-stage summaries, progress log |
| `models/` | Local checkpoints |
| `logs/`, `scratch/` | Stage logs and per-run scratch files |

## Develop

```bash
uv sync                              # create .venv from uv.lock
uv run pytest                        # tests
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run ruff check . && uv run ruff format --check . && uv run pytest
uv run pre-commit install            # optional git hook
```

Run `uv lock` after changing dependencies, and commit `uv.lock` with the change.

## Documentation

- `docs/architecture.md` covers the stage flow, corpus assumptions, state, and
  the runtime matrix.
- `docs/development.md` covers setup, tests, lint, versioning, and CI.
- `docs/operations.md` covers deployment, monitoring, recovery, and cleanup.
- `docs/design/2026-11-13-portable-runtime-design.md` records the platform,
  engine, media backend, and provider decisions.

## License

No license has been chosen. Add a `LICENSE` file before distributing this code.