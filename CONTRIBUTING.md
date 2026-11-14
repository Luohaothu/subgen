# Contributing

Read `docs/development.md` before the first change.

## Workflow

1. Install the project with `uv sync`.
2. Make the change on a branch.
3. Run `uv run ruff check . && uv run ruff format --check . && uv run pytest`.
4. Commit with a conventional prefix. `feat`, `fix`, `refactor`, `docs`, `test`,
   `build`, or `chore` is enough. Commit `uv.lock` with any dependency change.

## Rules

- Do not change stage output formats without bumping the schema version in
  `src/subtitle_pipeline/config.py`.
- Keep Chinese cue timestamps derived from the accepted Japanese cues.
- Keep publication atomic. A new write path must use the helpers in
  `src/subtitle_pipeline/publish.py`.
- Add a test for every behaviour change. The suite runs on Linux and pins POSIX
  filesystem semantics.