# Operations

The Linux deployment runs one long-lived supervisor on a host with an NVIDIA
GPU. This guide covers that deployment and the portable macOS or CPU hosts. The
file layout the scripts assume is in `deploy/README.md`.

## First-time setup

1. Clone the repository to `$WORK_ROOT/pipeline-src`.
2. Create the Python environment the scripts expect.

   ```bash
   uv venv --python 3.12 /home/leo/podcast/.venv
   uv pip install --python /home/leo/podcast/.venv/bin/python \
     -e "$WORK_ROOT/pipeline-src[models]"
   uv pip install --python /home/leo/podcast/.venv/bin/python vllm==0.24.0
   ```

3. Download the checkpoints.

   ```bash
   deploy/scripts/download_models.sh
   ```

   Each download writes a `.download-complete` sentinel. The supervisor waits
   for all four sentinels before the alignment stage.

4. Install the user service.

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/systemd/abc-subtitle-pipeline-supervisor.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable abc-subtitle-pipeline-supervisor.service
   ```

5. Start the run.

   ```bash
   systemctl --user start abc-subtitle-pipeline-supervisor.service
   ```

## Portable hosts

macOS and CPU-only Linux hosts use the portable entry points. They do not need
systemd or the supervisor.

1. Sync the environment, and add MLX on Apple Silicon.

   ```bash
   uv sync --extra models
   uv sync --extra models --extra mlx
   ```

2. Download the VAD, ASR, and aligner checkpoints into `model_root`. The
   `download_models.sh` script writes to the Linux host paths, so another host
   uses the ModelScope or Hugging Face CLI directly.

3. Write `subgen.toml` with the roots, the device, and a translation provider.
   The file format is in the [README](../README.md#configure).

4. Start a translation server, or point the provider at an existing API.

   ```bash
   subgen serve --model-path /path/to/model --model vendor/primary
   ```

5. Run the pipeline.

   ```bash
   subgen --dry-run
   subgen
   ```

`subgen` runs inventory, extraction, ASR, alignment, the primary pass, the
review pass, and publication. It stops after publication. Audit and cleanup stay
with the Linux supervisor.

The runtime derives model revisions from each checkpoint's config file when
`state/model_revisions.json` is absent. A remote translation provider supplies
the translation revisions, so the local translation checkpoints are optional.

## Upgrade from the ffmpeg backend

The media probe and extraction now use PyAV. Re-run inventory once after
upgrading. The probe mirrors the old ffprobe fields, but the stage settings
digest changed with the renamed timeout fields and the removed schema fields,
so the manifest is regenerated.
Per-video artifacts are reused when their provenance still matches. A video
whose probe hash changed is re-aligned, and its translation follows.

## Start and stop

```bash
systemctl --user start abc-subtitle-pipeline-supervisor.service
systemctl --user stop abc-subtitle-pipeline-supervisor.service
systemctl --user status abc-subtitle-pipeline-supervisor.service
```

The unit uses `Restart=on-failure` and exits with code 130 on `SIGINT` or
`SIGTERM`. `KillMode=control-group` stops the stage children with the
supervisor.

## Watch progress

| Source | What it shows |
| --- | --- |
| `journalctl --user -u abc-subtitle-pipeline-supervisor` | Supervisor start, stop, and exit |
| `$WORK_ROOT/logs/pipeline_supervisor.log` | Stage start, retry, and completion lines, and the alignment and translation output |
| `$WORK_ROOT/logs/extract_resume.log` | Audio extraction |
| `$WORK_ROOT/logs/corpus_asr.log` | ASR |
| `$WORK_ROOT/logs/model_download.log` | Checkpoint downloads |
| `$WORK_ROOT/logs/vllm_primary.log` | Primary model server |
| `$WORK_ROOT/logs/vllm_review.log` | Review model server |
| `$WORK_ROOT/state/runtime_progress.jsonl` | Per-video progress events |
| `$WORK_ROOT/state/*_summary.json` | Per-stage counters and failure lists |
| `$WORK_ROOT/state/pipeline_complete` | Written only after the final audit |

`deploy/scripts/vllm_service.sh status` prints the served model, the unit state,
and the health check result.

## Web console

The portable hosts also have an interactive console. It is not part of the
systemd supervisor and does not replace it.

```bash
uv run subgen web --open
```

The console prints its loopback URL. It runs one job at a time through the same
seven stage commands as `subgen`, shows per-stage progress, and edits
`subgen.toml`. Job records and logs live under
`$WORK_ROOT/state/web/jobs/`. The lock file is
`$WORK_ROOT/state/web/server.lock`; a second console on the same work root exits
2.

Stop the console with Ctrl-C or SIGTERM. It cancels the running job with reason
`server_shutdown`, waits for the worker, and releases the lock. If the process is
killed, the next start marks every non-terminal job `interrupted` and kills a
leftover stage process group before it binds.

The console binds `127.0.0.1` only and has no authentication. Do not expose the
port or put it behind a public proxy. The full operator guide is
[Web console](web.md).

## Stage order

Extraction and ASR run concurrently until every audio file exists. After that
the supervisor runs the remaining stages in order and reuses a stage when its
artifacts and summary already verify.

| Stage | Command | Gate |
| --- | --- | --- |
| Extract | `runtime_cli extract` | 579 FLAC files |
| ASR | `corpus_asr_cli` | 575 `asr.json` files plus 4 `target_exists` |
| Alignment | `corpus_alignment_cli` | 575 `alignment.json` files plus 4 `target_exists` |
| Primary translation | `corpus_translation_cli --pass primary` | 575 drafts plus 4 `target_exists` |
| Review translation | `corpus_translation_cli --pass review` | 575 reviewed artifacts plus 4 `target_exists` |
| Protected snapshot | `corpus_audit_cli --mode snapshot-protected` | Snapshot of existing targets |
| Publish | `corpus_publish_cli` | 579 targets, 575 new plus 4 existing |
| Pre-cleanup audit | `corpus_audit_cli --mode pre-cleanup` | 579 verified targets |
| Cleanup | `cleanup_owned_temporaries` | Local stage directories removed |
| Post-cleanup audit | `corpus_audit_cli --mode post-cleanup` | 579 targets verified after cleanup |

The final audit writes `state/final_verification.json` and the supervisor writes
`state/pipeline_complete`. Both must exist before you call a run finished.

## Tune translation workers

The supervisor reads the worker count from `state/translation_workers` and
passes it to the translation client. Change it without reloading the model
server:

```bash
deploy/scripts/set_translation_workers.sh 24
```

The script validates the range 1 to 36, replaces the file atomically, and
terminates the current request client so the supervisor starts a new one with the
new worker count. The vLLM server stays loaded.

## Recover from a failure

- The supervisor retries a failed stage with a delay. It does not retry a
  verification gate that never passes.
- Rerun one video with `subgen --video-id <sha256>`. `subgen` also accepts
  `--limit`.
- Force a stage to recompute one video by removing that video's artifact under
  `$WORK_ROOT/asr/corpus/<video_id>/` or
  `$WORK_ROOT/translation/corpus/<video_id>/`.
- If a `.part` file is left behind, the next publish overwrites it. A publish
  that finds the target already present counts it as an existing target and
  leaves it unchanged.
- If the GPU is stuck, stop the unit, check `vllm_service.sh status`, and stop
  the server with `vllm_service.sh stop`. The next `ensure` call waits for free
  GPU memory before it starts a new server.

The pipeline is resumable at every stage. Restarting the unit is safe.

## Cleanup and audit

The supervisor cleans `audio/`, `segments/`, `asr/`, `align/`, `translate/`,
`translation/`, `final/`, `scratch/`, and `models/` after the pre-cleanup audit
passes. It also removes `/tmp/abc-subtitle-dev` when that path exists.

Cleanup deletes the downloaded checkpoints. A later run downloads them again, so
keep a copy outside the work root if the download is slow.

The audit writes three files that together prove the publication.

- `state/protected_targets.json` records the subtitles that already existed and
  must not be replaced.
- `state/corpus_publish_audit.json` records the published, skipped, and blocked
  targets.
- `state/final_verification.json` records the post-cleanup verification.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Supervisor exits 1 at startup | The source or work root is missing, or the NAS mount is not ready |
| Stage retries forever | The stage log names the failing video and error |
| Publish reports a target exists | The target was published by another tool, or a protected file must not be replaced |
| vLLM never becomes healthy | Check `logs/vllm_*.log`, free GPU memory, and the port 8123 listener |
| Translation worker changes do nothing | `state/translation_workers` was edited while no request client was running |
| Audit reports a missing target | The NAS write failed or the source record changed after extraction |
| `subgen serve` prints guidance instead of starting | No CUDA GPU and no `mlx-lm` on Apple Silicon. Use an external endpoint |
| `subgen` cannot find a provider | `subgen.toml` has no `[translation]` section and the `SUBGEN_TRANSLATION_*` variables are unset |

## Limits

- The GPU stages run one at a time. ASR and alignment load their models in
  process, and the supervisor swaps the vLLM server between the primary and
  review translation models.
- The supervisor expects 580 inventory entries and 579 audio tracks. A different
  corpus size needs edits to the expected count in the scripts and
  `PipelineConfig.production`.
- The portable runner stops after publication. The Linux supervisor is the only
  path with the protected-target snapshot, the pre-cleanup audit, and cleanup.
- MLX serves translation only. ASR and alignment still use FunASR and
  transformers.
- Publication skips an existing `<stem>.zh.srt`. The pre-check is not atomic,
  so a target created in the race window can be replaced.