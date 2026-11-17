# Web console

`subgen web` serves a local console for starting runs, watching progress,
managing jobs, and editing the pipeline settings. It binds `127.0.0.1` only and
has no authentication. Do not expose the port.

## Start the console

```bash
uv run subgen web
```

The console prints its URL, for example `http://127.0.0.1:8765`. Open the URL in
a browser, or pass `--open` to open it after the listener is ready.

| Flag | Meaning |
| --- | --- |
| `--config PATH` | Settings file to read and write. Defaults to `SUBGEN_CONFIG`, then `./subgen.toml`. |
| `--port PORT` | Listen port. Defaults to 8765. Use `0` for an ephemeral port. |
| `--open` | Open the URL in the default browser. |
| `--dry-run` | Print the resolved config path, work root, jobs root, and listen address, then exit. |
| `--verbose` | Log one line per request to stderr. |

The console needs a usable work root. If the settings file does not load, the
console still starts so you can repair it on the settings page. It finds the
work root from `paths.work_root` in the file, then `SUBGEN_WORK_ROOT`, then the
default. Job creation stays disabled until the settings load.

One console owns one work root. A lock file at
`<work_root>/state/web/server.lock` keeps a second console out; the second exits
with status 2.

## Create a job

The create panel sets the run scope:

- Limit: process the first N videos. Leave it empty for the whole manifest.
- Video IDs: one 64-character lowercase hex ID per line or comma-separated.
- Skip inventory and skip extract: reuse the current manifest and audio.
- ASR batch size and alignment batch size: passed to the stage commands.
- Dry run: build and record the seven commands without starting a stage.

The console runs one job at a time. While a job runs, the create panel shows the
active job and refuses a second one. If another process wrote progress rows in
the last 120 seconds, the console asks you to acknowledge the overlap before it
starts.

A dry-run job ends as `dry_run`. Every stage is `skipped`, and the detail page
shows the exact command for each stage. Use it to check the settings without a
corpus or model checkpoints.

## Watch progress

A job detail page shows three views of the same attempt:

- The seven-stage rail with the status, exit code, and counters for each stage.
- The live counters for the running stage, marked approximate while the stage
  runs.
- The newest per-video rows from `state/runtime_progress.jsonl`, and a log
  panel that follows the stage output.

Counters come from the stage's own terminal progress row. The console reads only
the byte range of the progress log that belongs to the attempt. It does not read
`state/*_summary.json`, because those files are last-writer-wins with no attempt
identity.

The page polls once a second while a job runs and once every five seconds when
idle. It stops polling while the tab is hidden.

## Manage jobs

| Action | Effect |
| --- | --- |
| Cancel | Sends SIGINT to the stage process group, then SIGTERM after 10 seconds, then SIGKILL after 3 more. Later stages are skipped. |
| Rerun | Creates a new job with the same scope and the current settings. |
| Delete | Removes a finished job's record and log. Pipeline artifacts are never deleted. |

A job record lives at `<work_root>/state/web/jobs/<job_id>.json`, and its log at
`<work_root>/state/web/jobs/<job_id>.log`. The console keeps the newest 200
records and deletes the oldest finished records beyond that.

Terminal states are final. A rerun creates a new record and points at the
original with `origin_job_id`.

## Edit settings

The settings page shows one row per key in `subgen.toml`:

- The value in the file.
- The effective value after environment overrides.
- A badge for the source: `file`, `env`, `derived`, or `default`.

The page validates every draft with the same loader the pipeline uses. It also
requires `source_root` and `work_root` to exist and to be canonical absolute
paths, which is the rule the first stage enforces. Missing model directories are
warnings, because a remote translation provider makes the local checkpoints
optional.

A save rewrites only the changed lines, so comments and ordering survive. The
console proves the rewrite by parsing it back with `tomllib`; a file it cannot
reproduce is refused with an instruction to use the canonical write. The previous
bytes are copied to `subgen.toml.bak` before the replace.

If the file does not load, use the repair action. It writes a canonical file
with the values in the form and keeps the backup. That drops unknown sections,
so check the diff first.

A save that changes `work_root` or `source_root` needs a console restart. The
next job creation returns an error until you restart.

## Recovery

- **Stop with Ctrl-C or SIGTERM.** The console cancels the running job with
  reason `server_shutdown`, waits for the worker, closes the listener, and
  releases the lock.
- **The console is killed.** The next start reconciles every non-terminal
  record. It sends SIGKILL to a leftover stage process group when the group is
  still the recorded one, then marks the stage `interrupted` and the job
  `interrupted` with reason `restart`. A new job can start immediately.
- **A job fails.** The record keeps the failing stage and exit code, and the log
  keeps the pipeline's own error line. The console never deletes partial
  pipeline artifacts; the next run reuses or replaces them under the pipeline's
  own rules.

## Safety

- The server binds loopback only. It has no `--host` flag.
- Every mutating request must send `Content-Type: application/json`. The `Host`
  header must name a loopback address, and a request with an `Origin` header must
  match the console's own origin.
- Job creation accepts structured fields only. No command string crosses the
  API.
- The page loads no external font, script, image, or stylesheet.

## Tests

The console tests use `unittest` and temporary directories. They need no model,
no network, and no corpus, except for one test that binds `127.0.0.1:0`.

```bash
uv run pytest tests/test_web_jobs.py tests/test_web_progress.py tests/test_web_settings.py \
  tests/test_web_runner.py tests/test_web_app.py tests/test_web_server.py
```

For a manual check without a corpus, start the console against a temporary work
root and create a dry-run job. A real run against a temporary source root fails
at the first stage with the pipeline's own message, which the job log shows.