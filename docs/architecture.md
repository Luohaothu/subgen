# Architecture

subgen is a sequence of stage commands over a local work root. One video is one
unit of work. Each stage writes a per-video JSON artifact and a stage summary. A
rerun reads those records and skips completed work.

The pipeline does not use extended attributes, no-replace renames, file locks, or
memory-backed snapshots. Scheduling owns mutual exclusion, the inventory
pre-check owns the existing-subtitle decision, and the per-video records own
recovery.

## Corpus assumptions

The production config targets the original recovery corpus. It has 580 videos,
579 with audio, one known no-audio file, and four existing `.zh.srt` targets.
The source root is read-only CIFS and the work root is local. The expected count
and the collision list live in `PipelineConfig.production`; a different corpus
needs its own values.

## Data flow

| Stage | Input | Per-video record | Summary |
| --- | --- | --- | --- |
| Inventory | Source corpus | Manifest: `state/runtime_inventory.json` | Inventory counts |
| Extract | Source video | `audio/<video_id>.flac` | Extraction counts |
| ASR | FLAC | `asr/corpus/<video_id>/asr.json` | `state/corpus_asr_summary.json` |
| Alignment | ASR text and FLAC | `asr/corpus/<video_id>/alignment.json` | `state/corpus_alignment_summary.json` |
| Primary translation | Alignment | `translation/corpus/<video_id>/*.draft.translation.json` | `state/corpus_translation_primary_summary.json` |
| Review translation | Draft | `translation/corpus/<video_id>/*.translation.json` | `state/corpus_translation_review_summary.json` |
| Publish | Reviewed SRT | `<video-stem>.zh.srt` beside the video | `state/corpus_publish_summary.json` |

`state/runtime_progress.jsonl` appends one event per video. It is a log, not the
source of truth. The per-video artifact is the completion record.

## Pre-check

Inventory records `target_exists` for each video. If `<video-stem>.zh.srt`
already exists, the ASR, alignment, translation, and publish stages skip the
video before doing any model work. The publish command repeats the check as a
last guard.

This is a scheduling decision, not a filesystem guarantee. A target created
between the check and the write can be replaced by `os.replace`. The pipeline
accepts that window because the operator controls the corpus and the target
names.

## Resume and recovery

The unit of work is one video at one stage. A stage follows one rule.

1. Read the inventory manifest.
2. Select videos that are not `target_exists`, not `no_audio`, and have their
   upstream artifact.
3. For each video, if the artifact exists and verifies, count it as existing and
   skip it.
4. Otherwise process the video and write the artifact with a temporary file
   plus `os.replace`.
5. Write the stage summary.

A crash loses at most the in-flight video. The next run reuses every completed
artifact. No global journal, lock, or fsync ordering is involved.

Translation commits per block inside a video. A crash between blocks keeps the
committed blocks in the draft artifact and resumes from the first uncommitted
block. The video-level artifact is written after the last block.

## Concurrency

The scheduler is either the Linux supervisor or `subgen`. The supervisor
starts extraction and ASR together; later stages run in sequence. `subgen`
runs every stage in sequence. Inside a stage, a thread pool assigns distinct
videos to workers. Two workers never receive the same video, so the pipeline
needs no file lock.

The `subgen` entry point passes the resolved settings to the child stage
commands. The stage commands read the same manifest and work root.

## Web console

`subgen web` is a scheduler in front of the stage commands. It builds the same
commands with `runner_cli.build_commands`, runs one stage at a time, and records
the attempt as `<work_root>/state/web/jobs/<job_id>.json`. Because it owns each
spawn, the current stage, the per-stage exit code, and the cancel target are
facts rather than log inferences.

The console reads per-video progress from the byte window of
`state/runtime_progress.jsonl` that belongs to the attempt: the runner records
the log size before spawn and after exit. The terminal row in that window holds
the stage counters. The console does not read `state/*_summary.json`, because
those files are last-writer-wins with no attempt identity. It never writes
pipeline state.

One console owns one work root through `state/web/server.lock`. That moves the
mutual-exclusion rule above to an interactive scheduler. A second console exits
2. A job runs one stage at a time and cancellation sends SIGINT, SIGTERM, then
SIGKILL to the stage process group.

The console also serves two read-only helpers for first-run setup: a readiness
summary in `/api/meta` (folder, translation provider, model directories) and a
directory-only browser at `/api/browse`. Neither writes state.

## Publication

`publish.py` checks the target, copies the validated candidate to
`<target>.part`, verifies the bytes and SHA-256, checks the target again, and
calls `os.replace`. A stale
`.part` file is overwritten. There is no directory lock, no fsync, and no
no-replace syscall.

## Source identity

Inventory binds the source path to a size, mtime, device, inode, and media probe
digest. The extraction and probe paths pass the open descriptor through
`platform_io.fd_path`, which is `/proc/self/fd` on Linux and `/dev/fd` on macOS.
This keeps the media probe on the same file the inventory observed. It is the only
platform branch in the stage code.

## Devices and engines

| Host | ASR | Alignment | Translation |
| --- | --- | --- | --- |
| Linux with CUDA | FunASR through vLLM | transformers, bf16, CUDA | vLLM or any OpenAI-compatible API |
| Apple Silicon | FunASR on CPU | transformers, float16, MPS | MLX server or any OpenAI-compatible API |
| CPU only | FunASR on CPU | transformers, float32, CPU | external OpenAI-compatible API |

`settings.py` resolves `auto` to CUDA, then MPS, then CPU. The ASR runner picks
vLLM on CUDA and FunASR elsewhere. The aligner picks bf16, float16, or float32
from the device. MLX serves translation only.

## Modules

| Module | Role |
| --- | --- |
| `runtime_cli.py` | Inventory and audio extraction |
| `media.py` | PyAV probing and extraction, audio selection |
| `segments.py` | VAD normalization and WAV materialization |
| `vad_runner.py` | FunASR VAD |
| `asr_runner.py` | FunASR through vLLM on CUDA, FunASR AutoModel elsewhere |
| `asr.py` | Batch normalization, repetition trimming, quality flags |
| `align_runner.py` | Qwen3 forced aligner on CUDA, MPS, or CPU |
| `alignment.py` | Transcript restore, cue building, timestamp digest |
| `translation_runner.py` | Prompt and glossary construction, OpenAI-compatible calls |
| `translation.py` | Chinese output checks, name and glossary policy |
| `translation_runtime_cli.py` | Single-video primary and review passes |
| `srt.py` | SRT parse, render, and candidate validation |
| `publish.py` | Pre-check and `.part` plus `os.replace` publication |
| `corpus_*_cli.py` | Corpus drivers for one stage |
| `smoke_cli.py` | One-file VAD, ASR, and alignment smoke |
| `settings.py` | TOML and environment settings |
| `platform_io.py` | Descriptor paths for libavformat |
| `runner_cli.py` | Portable corpus runner |
| `serve_cli.py` | vLLM, MLX, or external endpoint selection |
| `web/jobs.py` | Job records, state machine, store, work-root lock |
| `web/runner.py` | Stage plan, subprocess supervision, cancellation |
| `web/progress.py` | Progress-log vocabulary and byte-window attribution |
| `web/settings_io.py` | Settings view, validation, line-preserving save |
| `web/app.py`, `web/server.py` | HTTP routes, guards, and the socket adapter |

## Known gaps

- The Linux supervisor remains the only path with the protected-target snapshot,
  the pre-cleanup audit, and cleanup. `subgen` stops after publication.
- MLX serves translation only. ASR and alignment still use FunASR and
  transformers.
- ASR and alignment cannot use a remote API yet. Only translation accepts an
  external OpenAI-compatible provider.
- The default roots remain tied to the original Linux host. Another host
  overrides them in `subgen.toml` or the `SUBGEN_*` variables.
- The stage commands share private helpers across modules, notably
  `runtime_cli._runtime_config` and `smoke_cli._atomic_json`. They are a common
  seam candidate but are not a public API.