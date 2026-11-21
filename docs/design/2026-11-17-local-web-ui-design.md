# Local web UI design

Status: accepted on 2026-11-17. Revised on 2026-11-21 after first-use feedback.

## Why

The pipeline is CLI-only. Starting a run means remembering seven stage commands
or the portable runner, progress lives in `state/runtime_progress.jsonl` and
`state/*_summary.json`, and settings are edited in `subgen.toml` with no
feedback until the first stage fails.

The operator asked for a local web UI with four capabilities: create a job,
watch its progress, manage jobs, and edit the pipeline settings.

`subgen serve` already starts the translation model server, so the new verb is
`subgen web`. The UI targets the portable runner on Linux and macOS. It does not
replace the systemd supervisor, and it does not change any stage.

## Constraints found

- The pipeline has no attempt identity. Each stage process mints its own
  `run_id`, and summaries are last-writer-wins with no timestamp.
- Two concurrent runs on one work root race on summaries, the shared per-video
  scratch directory, and the publish `.part` file. The architecture doc assigns
  mutual exclusion to the scheduler, not the stages.
- `state/runtime_progress.jsonl` is append-only JSONL. It is the only per-video
  signal, but `extract` rows carry no `run_id`.
- `settings.py` has no TOML writer, and `tomllib` is read-only. Its loader
  rejects unknown sections and keys.
- `settings.load_settings` validates absolute paths only. The first stage later
  resolves both roots strictly and rejects missing or symlinked paths.
- Only files under `src/subtitle_pipeline/` ship in the wheel. Hatchling includes
  non-Python files under that package with no build-config change.
- The test suite uses `unittest` classes and temporary directories. It opens no
  sockets today and does no model work.
- The runtime dependency list is three packages. CI installs from `uv.lock`.

## Decisions

1. **`subgen web` is a scheduler with an HTTP face.** It calls
   `runner_cli.build_commands` for argv and `runner_cli.settings_environment`
   for the child environment, then runs the stage processes itself, one at a
   time. It does not spawn `subgen` as one child. Running stages directly makes
   the current stage, the per-stage exit code, and the cancel target facts
   instead of log inference.
2. **A job is the attempt identity the pipeline lacks.** One JSON record per
   attempt under `<work_root>/state/web/jobs/`, written temp plus `os.replace`.
   The record is frozen and `JobRecord.__post_init__` rejects shapes the state
   machine cannot produce.
3. **One active job per work root.** A process-lifetime `flock` on
   `state/web/server.lock` stops a second console. A create while a job runs
   answers `409`. No queue: the pipeline cannot overlap with itself, and a queue
   would hide that.
4. **Progress comes from the job's own slice of the progress log.** The runner
   records the log size before spawn and after exit. Rows in that window are the
   stage's own. Terminal counters and the last rows are copied into the record at
   stage end. Summary files are never read. `run_id` is captured when the row
   has one. `extract` and `inventory` rows without a `run_id` are attributed by
   the window.
5. **Cancel escalates SIGINT, then SIGTERM, then SIGKILL.** SIGINT lets the
   stage's existing `KeyboardInterrupt` path return 130 and run its `finally`
   cleanup. A cancel request wins over the exit code. Remaining stages are
   skipped.
6. **Settings are saved by a line-preserving rewrite with a proof.** Comments and
   ordering survive. The output is parsed back with `tomllib` and must equal the
   requested document, or the save is refused. A file the loader rejects can be
   repaired only with `force=true`, which writes canonical TOML and saves a
   `.bak` first.
7. **Stdlib HTTP server, JSON API, no-build static SPA, polling.** No new
   dependency. Polling is stateless and testable. The app uses a hash router so
   the static file server needs no rewrites.
8. **Loopback only, same-origin guarded.** No `--host` flag. Mutating requests
   must be JSON, the `Host` header must be loopback, and no CORS headers are sent.
   Job parameters are structured fields, never a command string.
9. **Dry-run jobs.** A dry-run job builds and records the seven commands without
   spawning anything. It ends in the terminal state `dry_run` with every stage
   `skipped`. The operator can validate configuration without a corpus or models,
   and the UI gets a green path that needs neither.

## Shape

### Modules

| Path | Role |
| --- | --- |
| `src/subtitle_pipeline/web/__init__.py` | Package docstring |
| `src/subtitle_pipeline/web/jobs.py` | `JobParams`, `StageRun`, `JobRecord`, transitions, `JobStore`, work-root lock, reconciliation |
| `src/subtitle_pipeline/web/progress.py` | Stage ids, terminal events, row parsing, window attribution, counters, live counts |
| `src/subtitle_pipeline/web/runner.py` | `JobRunner` worker, command planning, subprocess spawning, cancellation |
| `src/subtitle_pipeline/web/settings_io.py` | `FIELD_SPECS`, `describe`, `validate_draft`, `save`, line-preserving writer, strict root check |
| `src/subtitle_pipeline/web/app.py` | `Request`, `Response`, routes, static files, same-origin guard, error envelope |
| `src/subtitle_pipeline/web/server.py` | `ThreadingHTTPServer` adapter |
| `src/subtitle_pipeline/web/cli.py` | `subgen web` argparse and lifecycle |
| `src/subtitle_pipeline/web/static/index.html` | One page, three views |
| `src/subtitle_pipeline/web/static/app.js` | Router, fetch, render, Chinese copy |
| `src/subtitle_pipeline/web/static/app.css` | Styles, no external assets |

Changes to existing files:

| File | Change |
| --- | --- |
| `src/subtitle_pipeline/cli.py` | Dispatch `web` to `web_cli.main(args[1:])` |
| `src/subtitle_pipeline/settings.py` | Add `SETTINGS_SECTIONS` and `SETTINGS_ENV`, make `_SECTIONS` derive from them, rename `_config_path` to public `config_path` |
| `tests/test_cli.py` | One dispatch case |
| `tests/test_settings.py` | Pins for the new public tables and the rename |
| `README.md`, `README.zh-CN.md`, `docs/README.md`, `docs/architecture.md`, `docs/operations.md`, `CHANGELOG.md` | Operator docs and design index |

### Job types

```python
JobState = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted",
    "dry_run",
]
StageStatus = Literal[
    "pending", "running", "succeeded", "failed", "cancelled", "interrupted",
    "skipped",
]
TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted", "dry_run"}
)
STAGE_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "skipped"})
RECORD_SCHEMA = "subgen-web-job-v1"
MAX_JOBS = 200

@dataclass(frozen=True)
class JobParams:
    limit: int | None = None
    video_ids: tuple[str, ...] = ()
    skip_inventory: bool = False
    skip_extract: bool = False
    asr_batch_size: int = 32
    alignment_batch_size: int = 16
    dry_run: bool = False

@dataclass(frozen=True)
class ChildHandle:
    pid: int
    pgid: int
    stage: str

@dataclass(frozen=True)
class StageRun:
    name: str                    # inventory, extract, asr, alignment,
                                 # translation_primary, translation_review, publish
    argv: tuple[str, ...]
    status: StageStatus
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    run_id: str | None = None
    counters: Mapping[str, int] | None = None
    events: tuple[Mapping[str, object], ...] = ()
    progress_start: int | None = None
    progress_end: int | None = None
    error: str | None = None

@dataclass(frozen=True)
class JobRecord:
    schema_version: str
    job_id: str
    state: JobState
    params: JobParams
    stages: tuple[StageRun, ...]
    current: int
    created_at: float
    updated_at: float
    ended_at: float | None
    exit_code: int | None
    child: ChildHandle | None
    error: str | None
    reason: str | None
    client_token: str | None
    origin_job_id: str | None
    settings_snapshot: Mapping[str, str]
    work_root: str
```

`JobParams.parse(mapping)` validates JSON. `limit` is null or an integer at least
zero, video ids are unique 64-character lowercase hex strings, batch sizes are at
least one, and every flag is a boolean. It raises `ParamsError` with a per-field
message.

`JobRecord.__post_init__` enforces these rules:

- A terminal state holds exactly when `ended_at` is set and `child` is None.
- `queued` has `current == -1` and every stage `pending`.
- `running` has a child, and stage `current` is `running`; earlier stages are
  terminal, later stages are `pending`.
- `succeeded` has every stage `succeeded`; `failed` has the failing stage failed
  and later stages `skipped`; `cancelled` and `interrupted` have the in-flight
  stage in the matching status and later stages `skipped`; `dry_run` has every
  stage `skipped` and no exit code.
- Counters and events appear only on terminal stage statuses.

`advance(record, **changes)` is the only transition function. Writers call it,
then `JobStore.save`.

### State machine

| From | Event | To |
| --- | --- | --- |
| `queued` | worker claims | `running` |
| `queued` | server stops before claim | `interrupted` |
| `running` | all stages exit 0 | `succeeded` |
| `running` | a stage exits non-zero | `failed` |
| `running` | operator cancels | `cancelled` |
| `running` | server stops or crash recovery | `interrupted` |
| `queued`, `running` | dry-run job planned | `dry_run` |

Terminal states never change. A rerun creates a new record with
`origin_job_id`. Delete removes a terminal record and its log.

### Persistence

```
<work_root>/state/web/
  server.lock
  jobs/<job_id>.json
  jobs/<job_id>.log
```

A job id is `%Y%m%dT%H%M%S-<8 hex>`. Records are canonical JSON (`sort_keys`,
compact separators, trailing newline), mode `0600`, written to
`<name>.<pid>.<uuid>.part` and moved with `os.replace`. Listing reads the
directory and sorts by `created_at` descending. It prunes the oldest terminal
records beyond `MAX_JOBS`.

The log starts with a runner header, then one boundary line per stage with the
exact argv, then the child's stdout and stderr. The runner writes a header line
before each spawn and flushes it.

`JobStore.reconcile()` runs before the port is bound, under the work-root lock.
It walks non-terminal records. If a record has a child whose process group still
exists and `os.getpgid(pid) == pgid`, it sends SIGKILL to that group. It then
marks the running stage `interrupted`, later stages `skipped`, and the job
`interrupted` with reason `restart`.

### Command planning

`plan_commands(settings, params)` calls `build_commands(settings, python=...,
limit=..., video_ids=..., skip_inventory=..., skip_extract=...,
asr_batch_size=..., alignment_batch_size=...)` and maps each command to a stage
id with `stage_of`. `stage_of` matches the module name in `argv[2]` against a
frozen module table and raises `UnknownStageError` otherwise. A contract test
pins the mapping against every skip combination.

The child environment is `settings_environment(settings)` plus
`SUBGEN_JOB_ID=<job_id>`. The extra variable is informational and is not read by
any stage today.

### Progress attribution

`progress.py` owns the stage vocabulary:

```python
STAGE_TERMINAL_EVENTS = {
    "inventory": frozenset({"completed", "failed"}),
    "extract": frozenset({"batch_completed", "batch_failed"}),
    "asr": frozenset({"batch_completed", "batch_deferred", "batch_failed"}),
    "alignment": frozenset(
        {"batch_completed", "batch_incomplete", "batch_failed"}
    ),
    "translation_primary": frozenset(
        {"batch_completed", "batch_incomplete", "batch_failed"}
    ),
    "translation_review": frozenset(
        {"batch_completed", "batch_incomplete", "batch_failed"}
    ),
    "publish": frozenset(
        {"batch_completed", "batch_incomplete", "batch_failed"}
    ),
}
STAGE_ALIASES = {
    "translation_primary": frozenset(
        {"corpus_translation_primary", "primary"}
    ),
    "translation_review": frozenset({"corpus_translation_review", "review"}),
}
```

The runner records `progress_start = stat().st_size` before spawn and
`progress_end = stat().st_size` after exit. `read_rows(start, end)` reads only
that byte range, decodes UTF-8 with replacement, ignores a torn trailing line,
and skips lines that are not JSON objects. Rows whose `stage` is in the stage's
alias set are the stage's rows. The terminal row is the last row in the window
whose `event` is in `STAGE_TERMINAL_EVENTS[name]`.

Terminal counters are the integer values of the terminal row with these keys
removed: `timestamp`, `stage`, `event`, `run_id`, `progress`, `artifact_root`,
`source_root`. The inventory row's `count` stays. If no terminal row exists, the
stage has no counters and the UI shows the exit code and the log tail.

Live counts for the running stage:

- `inventory`: done is `observed` rows, total is the `discovered` count.
- `extract`: done is `completed`, `skipped_existing`, and `skipped_no_audio`;
  total is `batch_started.selected`.
- `asr`, `alignment`, `translation_primary`, `translation_review`: done is
  `video_completed` plus `skipped_verified` (`video_existing` for translation);
  total is `batch_started.processable`, else `selected`.
- `publish`: done is `video_completed`; total is `batch_started.processable`,
  else `selected`.
- Failures are the `*_failed_*`, `deferred_*`, `video_missing_*`,
  `video_invalid_*`, and `video_target_collision` rows. Live counts are labelled
  approximate because the running stage knows its final split only at exit.

When the stage ends, the runner stores the last 40 in-window per-video rows in
`StageRun.events` and the terminal counters in `StageRun.counters`.

### Cancellation

The API sets a per-job `threading.Event` and returns the current record. The
worker polls the child every 0.2 seconds. On cancel it sends SIGINT to the child's
process group, waits up to 10 seconds, sends SIGTERM, waits 3 seconds, then sends
SIGKILL. It reaps the child and records the real exit status. It marks the
current stage `cancelled` (a bare exit 130 without a cancel request is
`interrupted`), later stages `skipped`, and the job `cancelled` with reason
`cancelled`. Between stages, the flag stops the loop before the next spawn.

Stopping the server with SIGINT or SIGTERM cancels the running job with reason
`server_shutdown`, joins the worker for up to 15 seconds, then closes the
listener. A forced kill leaves the reconcile path to clean up at the next start.

No pipeline artifact is deleted by the web layer. A killed stage leaves only the
temporary files the stage itself owns.

### Settings

`FIELD_SPECS` covers every key in `settings.SETTINGS_SECTIONS` exactly. Each spec
carries the section, key, environment variable, kind (`path`, `text`, `enum`,
`url`), and choices for enums. A contract test fails when the two tables drift.

`describe(environ, config_path)` returns:

```python
{
  "config_path": "/abs/subgen.toml",
  "exists": true,
  "settings_ok": true,
  "load_error": null,
  "base_sha256": "<sha256 of the bytes>",
  "api_key_configured": false,
  "fields": [
    {
      "section": "paths", "key": "source_root",
      "env": "SUBGEN_SOURCE_ROOT",
      "kind": "path",
      "file_value": "/Volumes/media",
      "effective_value": "/Volumes/media",
      "source": "file",
      "choices": [],
      "error": null
    }
  ],
  "warnings": []
}
```

`source` is `env` when the variable is present in `environ` (including an empty
value), else `file` when the key is in the document, else `derived` for
`model_root` and `asr_model` when their parent key is set, else `default`.
Values come from `load_settings(environ, config_path)`.

`validate_draft(values, environ, config_path)` accepts a flat map of
`"section.key"` to string. An empty string means the key is unset. It renders the
target document, writes it to a temporary file in the config directory, and calls
`load_settings(environ={}, config_path=temp)` so semantic validation is exactly
the loader's. It also checks that `source_root` and `work_root` exist and are
canonical (`resolve(strict=True) == path`), the rule the first stage enforces.
Missing `model_root` or `asr_model` directories are warnings, because a remote
translation provider makes those checkpoints optional.

`save(values, expected_sha256, force)` re-checks `base_sha256`, validates, then
writes:

- A missing or empty file, or `force=true`, gets canonical TOML with a header
  comment and only the non-empty keys, sections in `paths`, `runtime`,
  `translation` order.
- Otherwise, the writer scans the existing text line by line, tracks the current
  table, replaces the value on a `key = value` line while keeping indentation and
  a trailing comment, removes a line whose key is now empty, inserts a new key
  after the last key of its table, and appends a missing table at the end. The
  result must parse back to the target document with `tomllib`, or the save
  raises `RewriteError` and writes nothing.
- Before replacing a file that exists, the previous bytes are copied to
  `<config>.bak`.
- The file is replaced atomically and keeps its mode.

A file that fails to load blocks normal saves with `settings_load_error`. The UI
offers a repair save that requires `force=true`.

### HTTP API

All responses are JSON except the static assets and the log endpoint. Errors use
`{"error": {"code", "message", "fields", "detail"}}`. Codes are stable; the UI
owns the Chinese copy for known codes, and `message` and `detail` are English
diagnostics that the UI shows in a disclosure. Mutating requests require
`Content-Type: application/json`. The `Host` header must be `127.0.0.1:<port>` or
`localhost:<port>`; a request with an `Origin` header must match the same origin.
The body limit is 64 KiB.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | `index.html` |
| GET | `/static/<name>` | `app.js` or `app.css` only |
| GET | `/api/meta` | server, work root, setup checklist, quick places, inventory, active job, external activity |
| GET | `/api/browse?path=` | read-only folder browser, directories only |
| GET | `/api/jobs` | job summaries, newest first |
| POST | `/api/jobs` | create `{params, client_token?, acknowledge_external?}` |
| GET | `/api/jobs/<id>` | full record with stages and stored events |
| GET | `/api/jobs/<id>/progress?since=&log_since=` | live view, new events, log chunk |
| GET | `/api/jobs/<id>/log?since=` | plain-text log from a byte offset |
| POST | `/api/jobs/<id>/cancel` | request cancellation |
| POST | `/api/jobs/<id>/rerun` | new job with the same params, current settings |
| DELETE | `/api/jobs/<id>` | remove a terminal job and its log |
| GET | `/api/settings` | field view |
| POST | `/api/settings/validate` | validate `{values}` and return the preview |
| PUT | `/api/settings` | save `{values, base_sha256, force?}` |
| GET | `/api/health` | `{"ok": true}` |

Create returns `201` with the full record. It answers `409 job_running` when a
job is active, `409 external_activity` when the progress log moved in the last
120 seconds and no web job is active and `acknowledge_external` is false,
`409 restart_required` when a settings save changed `work_root` or
`source_root`, `409 settings_invalid` when `load_settings` fails, and `422` with
field messages when the parameters are invalid.

`client_token` makes create idempotent. A repeat with the same token returns
`200` and `"deduplicated": true` with the existing record.

`GET /api/jobs` returns summaries:

```json
{"jobs": [{"job_id": "20261117T101500-1a2b3c4d", "state": "running",
  "created_at": 1763352900.0, "updated_at": 1763352912.0,
  "ended_at": null, "current_stage": "asr", "exit_code": null,
  "params": {"limit": 1, "video_ids": [], "skip_inventory": false,
    "skip_extract": false, "asr_batch_size": 32,
    "alignment_batch_size": 16, "dry_run": false},
  "progress": {"done": 12, "total": 580, "failed": 0, "exact": false}}],
 "active_job_id": "20261117T101500-1a2b3c4d"}
```

`GET /api/jobs/<id>/progress` returns the record view, a bounded list of new
rows, and a log chunk:

```json
{"job": {"job_id": "...", "state": "running", "current": 2,
  "stages": [{"name": "inventory", "status": "succeeded",
    "counters": {"count": 580}, "exit_code": 0}, "..."]},
 "events": [{"timestamp": 1763352912.0, "stage": "corpus_asr",
   "event": "video_completed", "video_id": "...", "relative_path": "A/B.mp4"}],
 "next_offset": 918273, "log": "...", "next_log_offset": 4096,
 "log_reset": false}
```

`since` is a byte offset into `state/runtime_progress.jsonl`. When it is absent
the server returns rows from the current stage's `progress_start`. The server
reads at most 256 KiB per call. `log_since` behaves the same for the job log.

`GET /api/settings` returns the field view above. `POST
/api/settings/validate` returns `{"valid": true, "preview": "<toml>",
"fields": [...], "warnings": [...]}` or `422` with per-field errors. `PUT
/api/settings` returns the new view; `409 settings_changed` when `base_sha256`
does not match; `409 cannot_rewrite` with an instruction to use `force` when the
line-preserving rewrite cannot prove its output.

### CLI

```
subgen web [--config PATH] [--port PORT] [--open] [--dry-run] [--verbose]
```

The server always binds `127.0.0.1`. The default port is 8765. `--port 0` binds
an ephemeral port and prints it. `--open` opens the URL in the default browser.
`--dry-run` prints the config path, work root, jobs root, and listen address and
exits without binding. `--verbose` logs one line per request to stderr.

Startup order: resolve the config path, load settings, acquire the work-root
lock (exit 2 if held), run `store.reconcile()`, build the runner and app, bind,
start the worker, print the URL, then wait for a shutdown event. SIGINT and
SIGTERM trigger the shutdown path described under cancellation.

A missing config file loads defaults, so a first run starts normally and the
first save creates the file. If `load_settings` fails for another reason, the
server still starts so the settings screen can repair the file. It locates the lock and store with a fallback work root, in
this order: `paths.work_root` from a lenient `tomllib` read of the same file
when the value is an absolute path, then `SUBGEN_WORK_ROOT`, then
`DEFAULT_WORK_ROOT`. In that state `/api/meta` reports `settings_ok: false` and
the loader message, and job creation answers `409 settings_invalid`.
`--dry-run` with invalid settings prints the loader error and exits 2 without
binding.

### UI

The interface is written for a first-time user. It uses `任务` for a job,
`视频文件夹` for the source root, and `识别语音`/`对齐时间`/`翻译和校对`/
`写入字幕` for the work. Pipeline stage names appear only in the technical
fold.

Three hash-routed views:

- `#/jobs`: a hero that says `把视频变成中文字幕`, a primary action, and a
  three-item readiness checklist for the video folder, the translation service,
  and the model files. When jobs exist, the hero becomes a `最近的任务` header,
  a setup notice, and a card list with view, cancel, rerun, and delete actions.
- `#/jobs/<id>`: four plain phases (准备, 识别, 翻译, 完成) mapped from the seven
  stages, one progress sentence, the current video, and a failure card that
  turns a known cause into a Chinese hint with a link to the matching settings.
  The seven stages, argv, per-video tail, and log live under `处理细节`.
- `#/settings`: three sections (video folder, translation service, models and
  runtime), a read-only folder browser, quick places, validation with Chinese
  messages, a TOML preview, a comment-preserving save, and a repair path for a
  file that does not load.

The create dialog asks one question (`全部视频` or `先试一部分`) and hides the
dry run, skip flags, video filter, and batch sizes under `更多选项`. A help
dialog explains the three first steps. The page polls `/api/meta` every 5
seconds and the active detail every second, and stops while the tab is hidden.
The UI uses no external asset and no build step.

The first version was a dark ops console with the seven stage names, the
manifest terms, and every batch setting on the first screen. It failed the
first-use test: the user could not tell what to do and called it ugly. The
revision replaced it with a warm, light editorial interface, a readiness
checklist, plain-language phases, and progressive disclosure. The request path,
job state machine, storage, and progress attribution did not change. The new
read-only `/api/browse` endpoint and the `setup` and `places` fields on
`/api/meta` exist only to support the checklist and the folder picker.

## Synthesis decision

The arena produced two candidates that agreed on the transport, the stdlib
server, the per-stage subprocess execution, the job store under the work root,
and polling. Candidate B became the base for four reasons: the process-lifetime
work-root lock closes the reconcile race candidate A leaves open, `client_token`
deduplication makes create safe to retry, its job invariants are stated as
constructive rules, and its line-preserving settings editor keeps the operator's
comments without adding a dependency.

Grafted from candidate A:

- the SIGINT, SIGTERM, SIGKILL cancel ladder and the rule that a cancel request
  wins over the exit code;
- the shutdown-cancels contract with reason `server_shutdown`;
- the complete terminal-event set, including `batch_deferred` and
  `batch_incomplete`;
- persisted per-stage argv as evidence;
- the external-activity acknowledgement gate;
- the forced repair save for a settings file the loader rejects.

Added after the comparison: the segment-window progress rule, because
`extract` rows carry no `run_id` and the `completed` event name collides with
per-video rows; dry-run jobs, so the full lifecycle is provable without a
corpus; and `SUBGEN_JOB_ID` in the child environment.

The cross-model judge did not run: this harness exposes no provider other than
the parent model. The comparison was a same-model evidence pass and the pick is
the author's.

## Tradeoffs accepted

- One active job, no queue. A queue would hide that the pipeline cannot overlap
  with itself.
- The runner loop duplicates about 20 lines of `runner_cli.main` sequencing. argv
  and environment still come from one place.
- Progress attribution uses a byte window, not a cross-process job id. An
  external stage appended inside the window would be attributed to the job; the
  acknowledgement gate makes that deliberate.
- Summary files are not read. Their last-writer-wins counters never reach the UI,
  and a stage without a terminal progress row shows no counters.
- The settings writer is hand-rolled. The parse-back proof contains the risk,
  and layouts it cannot reproduce are refused instead of rewritten.
- Job records live under the work root. Deleting the work root deletes its jobs.
- Logs are not truncated by the web layer. The terminal-event and payload
  pruning keeps runtime memory bounded; disk growth is bounded by the stage
  output and the 200-record prune.
- The Chinese UI copy is a product surface; code identifiers and docs stay
  English. The UI says `任务` while the code, API, and docs say job.
- The main detail view collapses seven stages into four phases. The exact stages
  are one disclosure away, not gone.

## Alternatives considered

- **Run one `subgen` child.** Smaller, but the UI could not name the current
  stage, attribute a failure, or cancel one stage. It would parse a human error
  string.
- **Import the stage modules and run them in-process.** No subprocess boundary,
  but model state, exit codes, and KeyboardInterrupt semantics would change, and
  a model OOM would take the web server with it.
- **A worker process and queue.** Correct for many jobs, but the pipeline cannot
  overlap on one work root, so the queue would serialize anyway and add a second
  process to reconcile.
- **FastAPI or Flask with a build-step frontend.** Faster to write the routing,
  but adds a dependency, a lockfile change, and a toolchain for a single-user
  local tool.
- **Canonical TOML rewrite.** Simpler writer, but deletes the comments in a file
  the README documents as a commented template.

## Verification

Unit tests, no models, no network, no real corpus:

- `tests/test_web_jobs.py`: transitions, invariants, store atomicity, list
  order, prune, delete rules, token dedupe, reconciliation kill and state, lock
  refusal with a second lock.
- `tests/test_web_progress.py`: window parsing, torn line, extract and inventory
  attribution without `run_id`, translation aliasing, terminal counters, live
  counts.
- `tests/test_web_runner.py`: `stage_of` over every skip combination, plan argv,
  dry-run lifecycle, failure stops the loop, cancel sends the signal ladder and
  records the exit status, shutdown cancels.
- `tests/test_web_settings.py`: field specs match `SETTINGS_SECTIONS`, comment
  and order preservation, key removal, unset defaults, parse-back refusal,
  forced repair with backup, strict roots, base hash conflict, source
  attribution.
- `tests/test_web_app.py`: every route, status codes, same-origin and content
  type guards, body limit, static whitelist, log offsets.
- `tests/test_web_server.py`: one loopback bind on port 0 fetches `/` and
  `/api/health`; one real subprocess cancel proves the group dies.
- `tests/test_cli.py`: `web` dispatch passes the remaining argv through.

End-to-end on the real server:

```bash
uv run subgen web --dry-run
uv run subgen web --port 8765 --open
```

The browser pass opens the empty state and its readiness checklist, creates a
dry-run job, checks the four phases and the seven stages under `处理细节`,
browses folders, edits and saves one settings field while confirming the comment
survives, creates a real job against a temporary source root, checks the
human-readable failure hint and the technical log, deletes the job, opens the
help dialog, and checks the offline state. Screenshots and the Playwright script
are kept outside the repository as review artifacts.

Packaging: `uv build && unzip -l dist/subgen-*.whl | grep 'web/static'` lists
`index.html`, `app.js`, and `app.css`.

Full gate: `uv run ruff check . && uv run ruff format --check . && uv run pytest`.

## Risks and open questions

- **PID reuse at reconcile.** The `getpgid(pid) == pgid` guard accepts a reused
  pid that happens to be a session leader. A start-time token would be exact but
  adds a second platform branch. Accepted.
- **External CLI overlap.** The lock cannot stop a `subgen` run in another
  terminal. The 120-second progress check and the acknowledgement gate make it
  deliberate, not impossible.
- **Dry-run state.** It is a terminal state only dry-run jobs can reach. If it
  proves awkward in the UI it can become a boolean field with state `succeeded`,
  at the cost of an honest status.
- **Log growth.** A full run is about 1 MiB today. If a stage ever prints far
  more, the tail view still works but disk use grows until the 200-record prune.
- **Port collision.** The default 8765 exits 2 when taken. An operator can pass
  `--port 0` or another port.
- **Chinese vocabulary.** The UI uses 作业 for job, 运行 for run, 重跑 for rerun,
  参数 for settings fields, and 设置 for the settings page.