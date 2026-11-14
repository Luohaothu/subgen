from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .corpus_publish_cli import _candidate_metadata, _target_path
from .corpus_translation_cli import (
    _alignment_relative,
    _draft_relative,
    _final_json_relative,
    _final_valid,
    _srt_relative,
)
from .domain import VideoRecord
from .runtime_cli import _read_manifest, _runtime_config
from .smoke_cli import _atomic_json
from .translation_runner import ESCALATION_MODEL_ID

PROTECTED_RELATIVE_PATH = Path("state/protected_targets.json")
PUBLISH_SUMMARY_RELATIVE_PATH = Path("state/corpus_publish_summary.json")
AUDIT_RELATIVE_PATH = Path("state/corpus_publish_audit.json")
FINAL_RELATIVE_PATH = Path("state/final_verification.json")


class CorpusAuditError(RuntimeError):
    pass


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusAuditError(f"cannot read audit evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusAuditError(f"audit evidence is not an object: {path}")
    return value


def _audio_records(records: tuple[VideoRecord, ...]) -> tuple[VideoRecord, ...]:
    return tuple(record for record in records if record.audio_stream_index is not None)


def _protected_records(records: tuple[VideoRecord, ...]) -> tuple[VideoRecord, ...]:
    return tuple(record for record in records if record.target_exists)


def _target_evidence(config, record: VideoRecord) -> dict[str, object]:
    target = _target_path(config.source_root, record)
    size, digest = _candidate_metadata(target)
    return {
        "video_id": record.video_id,
        "target_relative_path": record.target_relative_path,
        "bytes": size,
        "sha256": digest,
    }


def snapshot_protected(config, snapshot) -> dict[str, object]:
    records = tuple(snapshot.records)
    targets = [
        _target_evidence(config, record)
        for record in sorted(
            _protected_records(records), key=lambda item: item.target_relative_path
        )
    ]
    payload: dict[str, object] = {
        "schema_version": "protected-targets-v1",
        "snapshot_digest": snapshot.snapshot_digest,
        "target_count": len(targets),
        "targets": targets,
    }
    path = config.work_root / PROTECTED_RELATIVE_PATH
    if path.is_file():
        if _load_object(path) != payload:
            raise CorpusAuditError("protected target snapshot or files changed")
    else:
        _atomic_json(path, payload)
    return payload


def _verified_protected(config, snapshot) -> dict[str, dict[str, object]]:
    payload = _load_object(config.work_root / PROTECTED_RELATIVE_PATH)
    required = {"schema_version", "snapshot_digest", "target_count", "targets"}
    if (
        set(payload) != required
        or payload["schema_version"] != "protected-targets-v1"
        or payload["snapshot_digest"] != snapshot.snapshot_digest
        or not isinstance(payload["targets"], list)
        or payload["target_count"] != len(payload["targets"])
    ):
        raise CorpusAuditError("protected target snapshot is invalid")
    expected_records = _protected_records(tuple(snapshot.records))
    expected = {
        (record.video_id, record.target_relative_path) for record in expected_records
    }
    by_video: dict[str, dict[str, object]] = {}
    actual: set[tuple[str, str]] = set()
    for row in payload["targets"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"video_id", "target_relative_path", "bytes", "sha256"}
            or not isinstance(row["video_id"], str)
            or not isinstance(row["target_relative_path"], str)
            or type(row["bytes"]) is not int
            or row["bytes"] <= 0
            or not isinstance(row["sha256"], str)
            or len(row["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in row["sha256"])
        ):
            raise CorpusAuditError("protected target evidence row is invalid")
        key = (row["video_id"], row["target_relative_path"])
        actual.add(key)
        by_video[row["video_id"]] = row
    if actual != expected or len(by_video) != len(expected):
        raise CorpusAuditError("protected target evidence coverage differs")
    return by_video


def _verify_publish_summary(
    config, records: tuple[VideoRecord, ...]
) -> dict[str, object]:
    summary = _load_object(config.work_root / PUBLISH_SUMMARY_RELATIVE_PATH)
    expected = {
        "selected": len(records),
        "completed": sum(
            not record.target_exists and record.audio_stream_index is not None
            for record in records
        ),
        "target_exists": sum(record.target_exists for record in records),
        "no_audio": sum(
            not record.target_exists and record.audio_stream_index is None
            for record in records
        ),
        "target_collision": 0,
        "missing_candidate": 0,
        "invalid_candidate": 0,
        "failed": 0,
    }
    if any(summary.get(key) != value for key, value in expected.items()):
        raise CorpusAuditError(
            "corpus publish summary is incomplete: "
            + json.dumps(summary, ensure_ascii=False, sort_keys=True)[:2000]
        )
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise CorpusAuditError("corpus publish summary lacks a run ID")
    return summary


def audit_pre_cleanup(
    config,
    snapshot,
    *,
    review_model: str,
    review_revision: str,
) -> dict[str, object]:
    if not review_model or not review_revision:
        raise CorpusAuditError("review model and revision are required")
    records = tuple(snapshot.records)
    protected = _verified_protected(config, snapshot)
    publish_summary = _verify_publish_summary(config, records)
    entries: list[dict[str, object]] = []
    for record in sorted(_audio_records(records), key=lambda item: item.video_id):
        target = _target_evidence(config, record)
        if record.target_exists:
            baseline = protected.get(record.video_id)
            if baseline != target:
                raise CorpusAuditError(
                    f"protected target changed: {record.target_relative_path}"
                )
            target["protected"] = True
        else:
            alignment = _alignment_relative(record.video_id)
            draft = _draft_relative(record.video_id)
            final_json = _final_json_relative(record.video_id)
            srt = _srt_relative(record.video_id)
            if not _final_valid(
                config.work_root,
                alignment,
                draft,
                final_json,
                srt,
                video_id=record.video_id,
                review_model=review_model,
                review_revision=review_revision,
                video_duration_ms=record.duration_ms,
            ):
                raise CorpusAuditError(
                    f"local finalized translation is invalid: {record.video_id}"
                )
            candidate_size, candidate_digest = _candidate_metadata(
                config.work_root / srt
            )
            if (
                candidate_size != target["bytes"]
                or candidate_digest != target["sha256"]
            ):
                raise CorpusAuditError(
                    f"published target differs from candidate: {record.video_id}"
                )
            target["protected"] = False
        entries.append(target)
    payload: dict[str, object] = {
        "schema_version": "corpus-publish-audit-v1",
        "snapshot_digest": snapshot.snapshot_digest,
        "publish_run_id": publish_summary["run_id"],
        "status": "verified",
        "target_count": len(entries),
        "protected_count": len(protected),
        "targets": entries,
    }
    _atomic_json(config.work_root / AUDIT_RELATIVE_PATH, payload)
    return payload


def audit_post_cleanup(config, snapshot) -> dict[str, object]:
    path = config.work_root / AUDIT_RELATIVE_PATH
    audit_bytes = path.read_bytes()
    payload = _load_object(path)
    required = {
        "schema_version",
        "snapshot_digest",
        "publish_run_id",
        "status",
        "target_count",
        "protected_count",
        "targets",
    }
    if (
        set(payload) != required
        or payload["schema_version"] != "corpus-publish-audit-v1"
        or payload["snapshot_digest"] != snapshot.snapshot_digest
        or payload["status"] != "verified"
        or not isinstance(payload["targets"], list)
        or payload["target_count"] != len(payload["targets"])
    ):
        raise CorpusAuditError("pre-cleanup corpus audit is invalid")
    audio_records = _audio_records(tuple(snapshot.records))
    expected = {
        (record.video_id, record.target_relative_path) for record in audio_records
    }
    actual: set[tuple[str, str]] = set()
    protected_count = 0
    records_by_id = {record.video_id: record for record in audio_records}
    for row in payload["targets"]:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "video_id",
                "target_relative_path",
                "bytes",
                "sha256",
                "protected",
            }
            or not isinstance(row["video_id"], str)
            or not isinstance(row["target_relative_path"], str)
            or type(row["bytes"]) is not int
            or row["bytes"] <= 0
            or not isinstance(row["sha256"], str)
            or len(row["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in row["sha256"])
            or type(row["protected"]) is not bool
        ):
            raise CorpusAuditError("pre-cleanup target evidence row is invalid")
        record = records_by_id.get(row["video_id"])
        if record is None or record.target_relative_path != row["target_relative_path"]:
            raise CorpusAuditError("pre-cleanup target evidence coverage differs")
        if row["protected"] != record.target_exists:
            raise CorpusAuditError("pre-cleanup protected-target evidence differs")
        current = _target_evidence(config, record)
        if any(current[key] != row[key] for key in current):
            raise CorpusAuditError(
                f"published target changed after audit: {record.target_relative_path}"
            )
        actual.add((record.video_id, record.target_relative_path))
        protected_count += int(row["protected"])
    if (
        actual != expected
        or len(actual) != len(payload["targets"])
        or protected_count != payload["protected_count"]
    ):
        raise CorpusAuditError("pre-cleanup target evidence coverage differs")
    final: dict[str, object] = {
        "schema_version": "final-verification-v1",
        "snapshot_digest": snapshot.snapshot_digest,
        "status": "verified_after_cleanup",
        "audio_sources": len(audio_records),
        "published_targets": len(actual),
        "protected_targets": protected_count,
        "publish_audit_sha256": hashlib.sha256(audit_bytes).hexdigest(),
    }
    _atomic_json(config.work_root / FINAL_RELATIVE_PATH, final)
    return final


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot protected subtitles and audit corpus publication"
    )
    parser.add_argument(
        "--mode",
        choices=("snapshot-protected", "pre-cleanup", "post-cleanup"),
        required=True,
    )
    parser.add_argument("--review-model", default=ESCALATION_MODEL_ID)
    parser.add_argument("--review-revision", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _runtime_config()
        snapshot = _read_manifest(config)
        if args.mode == "snapshot-protected":
            result = snapshot_protected(config, snapshot)
        elif args.mode == "pre-cleanup":
            result = audit_pre_cleanup(
                config,
                snapshot,
                review_model=args.review_model,
                review_revision=args.review_revision,
            )
        else:
            result = audit_post_cleanup(config, snapshot)
    except (CorpusAuditError, OSError, ValueError) as exc:
        print(f"corpus audit failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
