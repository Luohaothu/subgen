# deploy

Deployment assets for the Linux host that runs the corpus pipeline. The scripts
assume this layout.

| Path | Contents |
| --- | --- |
| `/home/leo/nas/Other/abc` | Read-only video corpus |
| `/home/leo/.local/share/abc-subtitles-recovery-6` | Local work root |
| `$WORK_ROOT/pipeline-src` | Checkout of this repository |
| `/home/leo/podcast/.venv` | Python environment with `subgen`, vLLM, and the model runtimes |
| `$WORK_ROOT/models` | Model checkpoints |
| `$WORK_ROOT/state` | Inventory, revisions, summaries, progress |
| `$WORK_ROOT/logs` | Stage logs |

Edit the constants at the top of each script when the host layout changes.

## Scripts

| Script | Purpose |
| --- | --- |
| `run_pipeline_supervisor.sh` | Runs the corpus stages, retries a failed stage, verifies the result, and stops the vLLM server |
| `vllm_service.sh` | Starts, stops, and checks the single local vLLM server on port 8123 |
| `download_models.sh` | Downloads the aligner and translation checkpoints with ModelScope |
| `set_translation_workers.sh` | Sets the translation worker count for the next request client restart |
| `reload_supervisor_after_client.sh` | Waits for a request client to exit, then restarts the supervisor |

The supervisor is Linux-only. On macOS or a CPU host, use `subgen serve` and
`subgen` from the package instead. They do not need systemd.

## Install the systemd unit

The unit runs as a user service because it needs the user's mount namespace and
GPU access.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/abc-subtitle-pipeline-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now abc-subtitle-pipeline-supervisor.service
```

Check the unit and the stage logs with `journalctl --user -u
abc-subtitle-pipeline-supervisor` and `$WORK_ROOT/logs/`.

The `RequiresMountsFor` line waits for the NAS mount and the work root before
the first stage. Keep both paths in sync with the scripts.