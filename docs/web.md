# Web console

`subgen web` serves a local console for starting jobs, watching progress,
managing finished jobs, and editing the pipeline settings. It binds `127.0.0.1`
only and has no authentication. Do not expose the port.

The console is aimed at someone who does not run the pipeline from a terminal.
It explains the product on the first screen, checks the three things a real run
needs, and keeps the pipeline terms behind a details fold.

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

The console needs a usable work root. A missing settings file loads the defaults,
so the first save can create the file. If an existing file does not load, the
console still starts so the settings page can repair it. It finds the work root
from `paths.work_root` in the file, then `SUBGEN_WORK_ROOT`, then the default.
Job creation stays disabled until the settings load.

One console owns one work root. A lock file at
`<work_root>/state/web/server.lock` keeps a second console out; the second exits
with status 2.

## Ready check

The first screen has a three-item checklist:

1. **Video folder.** The folder subgen scans, including its subfolders. It must
   exist and be a canonical absolute path. The settings page has a `浏览…`
   browser and quick picks for the home, Movies, Downloads, Desktop, Videos,
   and Documents folders.
2. **Translation service.** An OpenAI-compatible endpoint, a model name, and a
   revision. The API key is read from the environment variable named in the
   settings, not stored in the file.
3. **Model files.** Only real runs need them. A dry run skips every model. The
   checklist shows a warning when the directory is missing and lets you start a
   dry run anyway.

The console also reads the folder browser from a read-only local API. It lists
directories only, skips hidden names, and caps a listing at 200 entries.

## Create a job

The primary button opens one dialog.

- `全部视频` processes the folder in manifest order. Videos that already have a
  subtitle are skipped.
- `先试一部分` processes the first N videos. The default is 5.
- `更多选项` holds the dry run, the skip flags, a video ID filter, and the ASR
  and alignment batch sizes. Most users never open it.

A dry-run job builds and records the seven stage commands without starting a
stage. It ends as `dry_run`, every stage is `skipped`, and the detail page shows
the exact command for each stage. Use it to check the settings without a corpus
or model checkpoints.

The console runs one job at a time. While a job runs, the create dialog shows the
active job and refuses a second one. If another process wrote progress rows in
the last 120 seconds, the console asks you to acknowledge the overlap before it
starts.

## Watch progress

The detail page shows four plain phases in the main view:

| Phase | Pipeline stages |
| --- | --- |
| 准备 | Inventory and audio extraction |
| 识别 | ASR and forced alignment |
| 翻译 | Primary translation and review |
| 完成 | Publication |

The page also shows the current video, the finished and total counts, elapsed
time, and a bar for the active phase. Counters come from the stage's own
terminal progress row. The console reads only the byte range of
`state/runtime_progress.jsonl` that belongs to the attempt. It does not read
`state/*_summary.json`, because those files are last-writer-wins with no attempt
identity.

The `处理细节` fold holds the seven real stages, their exit codes, counters, and
exact argv, plus the per-video event tail and a following log panel. The failure
card names the phase and turns a known cause into a Chinese hint, with a link to
the matching settings. The raw log stays under `查看技术日志`.

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
original with `origin_job_id`. The UI shows the record's title as the local
creation time and puts the raw job id behind a copy button.

## Edit settings

The settings page is a single column with three sections.

1. **Video folder.** The path, a folder browser, quick picks, and
   `检查文件夹`. The check runs the same validation as a save.
2. **Translation service.** The endpoint, model, revision, and the name of the
   environment variable that holds the API key. The page never reads the key
   value. It says whether the variable is set.
3. **Models and runtime.** Collapsed by default. Holds `model_root`,
   `asr_model`, the device, and the ASR engine. Real runs need the model
   directories; a dry run does not.

The page validates every draft with the same loader the pipeline uses. It also
requires `source_root` and `work_root` to exist and to be canonical absolute
paths, which is the rule the first stage enforces. Missing model directories are
warnings.

A save rewrites only the changed lines, so comments and ordering survive. The
console proves the rewrite by parsing it back with `tomllib`; a file it cannot
reproduce is refused with an instruction to use the canonical write. The previous
bytes are copied to `subgen.toml.bak` before the replace. The preview shows the
exact text that will be written.

If the file does not load, use the repair action. It writes a canonical file
with the values in the form and keeps the backup. That drops unknown sections,
so check the diff first.

A save that changes `work_root` or `source_root` needs a console restart. The
next job creation returns an error until you restart. Environment overrides are
shown under the field, and the saved file value is what a process without that
variable would read.

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
- The folder browser is read-only. It lists directories, never file contents,
  and follows the same loopback and origin guards.
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
