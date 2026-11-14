# Portable runtime design

Status: accepted on 2026-11-13, revised after an architecture review.

## Why

The pipeline started as a single Linux host with an NVIDIA GPU. The runner, the
model paths, and the publication path assumed that host. The new requirement is
broader.

- Run on Linux and macOS.
- Run on CPU only, or use a GPU when one is present.
- Pick the best inference engine for the platform. vLLM on CUDA, MLX on Apple
  Silicon, FunASR or transformers on CPU.
- Let a user point translation at any OpenAI-compatible model API.

## Constraints found

- `asr_runner.py` hard-coded `device="cuda"` and one ModelScope cache path.
- `align_runner.py` hard-coded `cuda:0` and bf16.
- `translation_runner.py` rejected any model outside three Qwen IDs.
- `PipelineConfig.production()` and `runtime_cli` hard-coded `/home/leo` roots.
- The Linux supervisor is a systemd and vLLM script.
- A separate control plane (append-only ledger, artifact store, and generic
  runner) was tested but not called by production. It used `flock`, `fsync`,
  native no-replace renames, extended attributes, and `memfd_create` to enforce
  its guarantees.

## Review decisions

The review rejected the control plane's premises.

1. Existing subtitles are detected during inventory and skipped before any model
   work. Publication repeats the check. It does not need an atomic no-replace
   rename.
2. Mutual exclusion belongs to the scheduler. The supervisor or `subgen`
   runs stages in sequence, and each stage gives distinct videos to its workers.
   It does not need file locks.
3. Schema evidence in extended attributes is not needed. The per-video JSON
   record and its content are enough.
4. Recovery comes from the tool's own state machine, not from filesystem
   guarantees. The unit of work is one video at one stage. The per-video JSON
   artifact is the completion record. The stage summary is the aggregate.

The control plane and its tests were removed. So were the xattr, no-replace,
memfd, and lock code paths. `platform_io.py` now holds only the descriptor path
used to hand an open source file to libavformat.

## Candidate A: thread a RuntimeProfile through PipelineConfig

Add a frozen `RuntimeProfile` with the OS, accelerator, engine per stage, roots,
and provider. Put it on `PipelineConfig` and pass it down to every factory.

- Strength: one object, no second configuration surface.
- Weakness: `PipelineConfig` is already large, and every test that builds a
  config must build a profile.

## Candidate B: settings boundary plus small platform helpers

- `settings.py` loads `subgen.toml` and `SUBGEN_*` variables into a frozen
  `RuntimeSettings`.
- `platform_io.py` holds the descriptor path for libavformat.
- `runner_cli.py` and `serve_cli.py` are thin entry points over the stage
  commands.

Chosen. The durable pipeline keeps its per-video records, and the host concerns
stay at the edge.

## Media backend

Probing and extraction use PyAV against libavformat, libavfilter, and
libswresample. Extraction keeps the original `aresample=async=1000:first_pts=0`
filter graph and trims with `atrim`, so the timeline matches the old ffmpeg
command. No system media binary is required.

## State machine

| Item | Value |
| --- | --- |
| Unit of work | One video at one stage |
| Completion record | Per-video JSON artifact |
| Aggregate | `state/<stage>_summary.json` |
| Event log | `state/runtime_progress.jsonl` |
| Skip rule | `target_exists`, no audio, missing upstream artifact, or an existing verified artifact |

A stage reads the inventory manifest, selects unfinished videos, writes each
artifact through a temporary file plus `os.replace`, and writes the summary. A
crash loses the in-flight video. The next run reuses the rest.

## Selection matrix

| Host | ASR | Alignment | Translation |
| --- | --- | --- | --- |
| Linux with CUDA | FunASR through vLLM | transformers, bf16, CUDA | vLLM or any OpenAI-compatible API |
| Apple Silicon | FunASR on CPU | transformers, float16, MPS | MLX server or any OpenAI-compatible API |
| CPU only | FunASR on CPU | transformers, float32, CPU | external OpenAI-compatible API |

`device = "auto"` picks CUDA, then MPS, then CPU. `asr_engine = "auto"` picks
vLLM on CUDA and FunASR elsewhere.

## What is not in this design

- MLX Whisper or another MLX ASR model.
- An alignment API contract.
- Replacing the Linux supervisor. The portable runner covers the corpus stages
  on both platforms. The supervisor still owns systemd, vLLM lifecycle, audit,
  and cleanup on the Linux host.

## Verification

- Linux container, `pytest` with the test dependencies installed: 278 passed,
  2 skipped.
- macOS, `uv run pytest`: 278 passed, 2 skipped.
- `subgen --dry-run` prints the seven stage commands from provider settings.
- `subgen serve` selects vLLM on CUDA, MLX on Apple Silicon with `mlx-lm`, and
  prints guidance for an external endpoint elsewhere.