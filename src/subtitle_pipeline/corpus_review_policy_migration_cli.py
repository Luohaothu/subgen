from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .translation import _content, exact_required_translation
from .translation_runner import (
    CONFIRMED_INAUDIBLE_POLICY,
    INAUDIBLE_TRANSLATION,
    VALIDATED_DRAFT_FALLBACK_POLICY,
)
from .translation_runtime_cli import (
    FINAL_SCHEMA,
    REVIEW_CHECKPOINT_SCHEMA,
    WORK_ROOT,
    TranslationRuntimeError,
    _atomic_write,
    _json_bytes,
    _strict_json,
)

LEGACY_FALLBACK_KEY = "deterministic_validated_draft_fallbacks"
MIGRATION_SCHEMA = "review-policy-v2-migration-v1"
MIGRATION_HISTORY_SCHEMA = "review-policy-v2-migration-history-v1"


class ReviewPolicyMigrationError(RuntimeError):
    pass


def _message_has_legacy_fallback(message: object) -> bool:
    if not isinstance(message, str) or not message:
        return False
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and bool(payload.get(LEGACY_FALLBACK_KEY))
        and payload.get("validated_draft_fallback_policy")
        != VALIDATED_DRAFT_FALLBACK_POLICY
    )


def _artifact_has_legacy_fallback(artifact: object) -> bool:
    if not isinstance(artifact, dict):
        return False
    attempts = artifact.get("attempts")
    if not isinstance(attempts, list):
        return False
    if any(
        isinstance(attempt, dict)
        and _message_has_legacy_fallback(attempt.get("message"))
        for attempt in attempts
    ):
        return True
    texts = artifact.get("texts")
    if isinstance(texts, list) and any(
        isinstance(row, list)
        and len(row) == 2
        and isinstance(row[0], str)
        and isinstance(row[1], str)
        and (required := exact_required_translation("", cue_id=row[0])) is not None
        and _content(row[1]) != _content(required)
        for row in texts
    ):
        return True
    marker_ids = (
        {
            row[0]
            for row in texts
            if isinstance(row, list)
            and len(row) == 2
            and isinstance(row[0], str)
            and row[1] == INAUDIBLE_TRANSLATION
        }
        if isinstance(texts, list)
        else set()
    )
    if not marker_ids:
        return False
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        message = attempt.get("message")
        if not isinstance(message, str) or not message:
            continue
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        confirmed = (
            payload.get("confirmed_inaudible_markers")
            if isinstance(payload, dict)
            else None
        )
        if (
            isinstance(payload, dict)
            and payload.get("confirmed_inaudible_policy") == CONFIRMED_INAUDIBLE_POLICY
            and isinstance(confirmed, dict)
            and marker_ids <= set(confirmed)
        ):
            return False
    return True


def first_legacy_fallback_block(blocks: object) -> int | None:
    if not isinstance(blocks, list):
        raise ReviewPolicyMigrationError("review blocks are not a list")
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ReviewPolicyMigrationError(f"review block {index} is invalid")
        if _artifact_has_legacy_fallback(block.get("review_artifact")):
            return index
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _backup_bytes(
    source: Path,
    quarantine: Path,
    *,
    label: str,
) -> dict[str, object]:
    data = source.read_bytes()
    digest = _sha256(data)
    target = quarantine / f"{label}.{digest}.bak"
    if target.is_file():
        if target.read_bytes() != data:
            raise ReviewPolicyMigrationError(f"quarantine collision: {target}")
    else:
        _atomic_write(target, data)
    return {"path": str(target), "sha256": digest, "bytes": len(data)}


def _checkpoint_from_final(value: dict[str, Any], prefix: int) -> dict[str, object]:
    required = {
        "schema_version",
        "video_id",
        "cue_count",
        "timestamp_digest",
        "source_digest",
        "glossary_digest",
        "draft_json",
        "primary",
        "review",
        "blocks",
        "cues",
    }
    if set(value) != required or value.get("schema_version") != FINAL_SCHEMA:
        raise ReviewPolicyMigrationError("final translation schema is invalid")
    blocks = value["blocks"]
    if not isinstance(blocks, list) or not 0 <= prefix < len(blocks):
        raise ReviewPolicyMigrationError("final translation block prefix is invalid")
    checkpoint_blocks: list[dict[str, object]] = []
    for index, block in enumerate(blocks[:prefix]):
        if not isinstance(block, dict) or set(block) != {
            "commit_ids",
            "primary_artifact",
            "review_artifact",
        }:
            raise ReviewPolicyMigrationError(
                f"final translation block {index} is invalid"
            )
        checkpoint_blocks.append(
            {
                "commit_ids": block["commit_ids"],
                "review_artifact": block["review_artifact"],
            }
        )
    return {
        "schema_version": REVIEW_CHECKPOINT_SCHEMA,
        "video_id": value["video_id"],
        "cue_count": value["cue_count"],
        "timestamp_digest": value["timestamp_digest"],
        "source_digest": value["source_digest"],
        "glossary_digest": value["glossary_digest"],
        "draft_json": value["draft_json"],
        "primary": value["primary"],
        "review": value["review"],
        "blocks": checkpoint_blocks,
    }


def migrate_final(
    final_path: Path,
    *,
    quarantine_root: Path,
    apply: bool,
) -> dict[str, object]:
    value = _strict_json(final_path)
    if not isinstance(value, dict):
        raise ReviewPolicyMigrationError("final translation is not an object")
    prefix = first_legacy_fallback_block(value.get("blocks"))
    if prefix is None:
        return {"status": "clean", "replay_from_block": None}
    video_id = value.get("video_id")
    if not isinstance(video_id, str) or final_path.parent.name != video_id:
        raise ReviewPolicyMigrationError("final translation video binding differs")
    srt_path = final_path.with_name(f"{video_id}.zh.srt")
    if not srt_path.is_file():
        raise ReviewPolicyMigrationError("final translation SRT is missing")
    checkpoint_path = final_path.with_name(f"{video_id}.review.checkpoint.json")
    checkpoint = _checkpoint_from_final(value, prefix)
    if not apply:
        return {"status": "migratable_final", "replay_from_block": prefix}

    quarantine = quarantine_root / video_id
    quarantine.mkdir(parents=True, exist_ok=True)
    final_backup = _backup_bytes(final_path, quarantine, label="translation.json")
    srt_backup = _backup_bytes(srt_path, quarantine, label="subtitle.zh.srt")
    _atomic_write(checkpoint_path, _json_bytes(checkpoint))
    final_path.unlink()
    srt_path.unlink()
    return {
        "status": "migrated_final",
        "replay_from_block": prefix,
        "preserved_blocks": prefix,
        "backups": {"final": final_backup, "srt": srt_backup},
    }


def migrate_checkpoint(
    checkpoint_path: Path,
    *,
    quarantine_root: Path,
    apply: bool,
) -> dict[str, object]:
    value = _strict_json(checkpoint_path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != REVIEW_CHECKPOINT_SCHEMA
    ):
        raise ReviewPolicyMigrationError("review checkpoint schema is invalid")
    blocks = value.get("blocks")
    prefix = first_legacy_fallback_block(blocks)
    if prefix is None:
        return {"status": "clean", "replay_from_block": None}
    video_id = value.get("video_id")
    if not isinstance(video_id, str) or checkpoint_path.parent.name != video_id:
        raise ReviewPolicyMigrationError("review checkpoint video binding differs")
    if not isinstance(blocks, list):
        raise ReviewPolicyMigrationError("review checkpoint blocks are invalid")
    if not apply:
        return {"status": "migratable_checkpoint", "replay_from_block": prefix}

    quarantine = quarantine_root / video_id
    quarantine.mkdir(parents=True, exist_ok=True)
    backup = _backup_bytes(checkpoint_path, quarantine, label="review.checkpoint.json")
    migrated = dict(value)
    migrated["blocks"] = blocks[:prefix]
    _atomic_write(checkpoint_path, _json_bytes(migrated))
    return {
        "status": "migrated_checkpoint",
        "replay_from_block": prefix,
        "preserved_blocks": prefix,
        "discarded_blocks": len(blocks) - prefix,
        "backup": backup,
    }


def _translation_client_running(root: Path) -> bool:
    pid_path = root / "state/translation_client.pid"
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def run_migration(root: Path, *, apply: bool) -> dict[str, object]:
    root = root.resolve()
    if apply and _translation_client_running(root):
        raise ReviewPolicyMigrationError(
            "translation request client must be stopped before applying migration"
        )
    corpus = root / "translation/corpus"
    quarantine = root / "state/review-policy-v2-quarantine"
    details: list[dict[str, object]] = []
    counts: dict[str, int] = {}

    finals = sorted(
        path
        for path in corpus.glob("*/*.translation.json")
        if not path.name.endswith(".draft.translation.json")
    )
    final_video_ids: set[str] = set()
    for path in finals:
        result = migrate_final(path, quarantine_root=quarantine, apply=apply)
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        if status != "clean":
            final_video_ids.add(path.parent.name)
            details.append(
                {"video_id": path.parent.name, "artifact": "final", **result}
            )

    checkpoints = sorted(corpus.glob("*/*.review.checkpoint.json"))
    for path in checkpoints:
        if path.parent.name in final_video_ids:
            continue
        result = migrate_checkpoint(path, quarantine_root=quarantine, apply=apply)
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        if status != "clean":
            details.append(
                {"video_id": path.parent.name, "artifact": "checkpoint", **result}
            )

    return {
        "schema_version": MIGRATION_SCHEMA,
        "apply": apply,
        "finals_scanned": len(finals),
        "checkpoints_scanned": len(checkpoints),
        "counts": counts,
        "affected_videos": len(details),
        "details": details,
    }


def write_migration_history(state: Path, report: dict[str, object]) -> None:
    runs: list[dict[str, object]] = []
    if state.is_file():
        existing = _strict_json(state)
        if not isinstance(existing, dict):
            raise ReviewPolicyMigrationError("migration history is not an object")
        schema = existing.get("schema_version")
        if schema == MIGRATION_SCHEMA:
            runs.append(existing)
        elif schema == MIGRATION_HISTORY_SCHEMA:
            raw_runs = existing.get("runs")
            if not isinstance(raw_runs, list) or not all(
                isinstance(run, dict) and run.get("schema_version") == MIGRATION_SCHEMA
                for run in raw_runs
            ):
                raise ReviewPolicyMigrationError("migration history runs are invalid")
            runs.extend(raw_runs)
        else:
            raise ReviewPolicyMigrationError("migration history schema is invalid")
    runs.append(report)
    history = {
        "schema_version": MIGRATION_HISTORY_SCHEMA,
        "run_count": len(runs),
        "runs": runs,
    }
    _atomic_write(state, _json_bytes(history))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay legacy review fallbacks under the corrected review policy"
    )
    parser.add_argument("--work-root", type=Path, default=WORK_ROOT)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_migration(args.work_root, apply=args.apply)
    except (
        OSError,
        TypeError,
        ValueError,
        ReviewPolicyMigrationError,
        TranslationRuntimeError,
    ) as exc:
        print(f"review policy migration failed: {exc}", flush=True)
        return 1
    if args.apply:
        state = args.work_root.resolve() / "state/review_policy_v2_migration.json"
        write_migration_history(state, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
