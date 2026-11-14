from __future__ import annotations

import hashlib
import json
from pathlib import Path

from subtitle_pipeline.corpus_review_policy_migration_cli import (
    LEGACY_FALLBACK_KEY,
    MIGRATION_HISTORY_SCHEMA,
    MIGRATION_SCHEMA,
    first_legacy_fallback_block,
    migrate_checkpoint,
    migrate_final,
    write_migration_history,
)
from subtitle_pipeline.translation_runtime_cli import (
    FINAL_SCHEMA,
    REVIEW_CHECKPOINT_SCHEMA,
)


def _artifact(*, fallback: bool = False) -> dict[str, object]:
    message = ""
    if fallback:
        message = json.dumps(
            {LEGACY_FALLBACK_KEY: {"cue-1": {"draft_zh": "初译"}}},
            ensure_ascii=False,
        )
    return {"attempts": [{"attempt": 1, "code": "ok", "message": message}]}


def _blocks() -> list[dict[str, object]]:
    return [
        {"commit_ids": ["c0"], "review_artifact": _artifact()},
        {"commit_ids": ["c1"], "review_artifact": _artifact(fallback=True)},
        {"commit_ids": ["c2"], "review_artifact": _artifact()},
    ]


def _top(video_id: str, schema: str) -> dict[str, object]:
    return {
        "schema_version": schema,
        "video_id": video_id,
        "cue_count": 3,
        "timestamp_digest": "1" * 64,
        "source_digest": "2" * 64,
        "glossary_digest": "3" * 64,
        "draft_json": f"translation/corpus/{video_id}/{video_id}.draft.translation.json",
        "primary": {"model": "primary", "revision": "p"},
        "review": {"model": "review", "revision": "r"},
    }


def _write_json(path: Path, value: object) -> bytes:
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def test_migrate_final_preserves_clean_prefix_and_quarantines_original(tmp_path):
    video_id = "a" * 64
    directory = tmp_path / "translation/corpus" / video_id
    final = directory / f"{video_id}.translation.json"
    srt = directory / f"{video_id}.zh.srt"
    value = {
        **_top(video_id, FINAL_SCHEMA),
        "blocks": [
            {**block, "primary_artifact": {"block": index}}
            for index, block in enumerate(_blocks())
        ],
        "cues": [],
    }
    original = _write_json(final, value)
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text("1\n00:00:00,000 --> 00:00:00,800\n测试\n", encoding="utf-8")
    quarantine = tmp_path / "state/quarantine"

    dry = migrate_final(final, quarantine_root=quarantine, apply=False)
    assert dry == {"status": "migratable_final", "replay_from_block": 1}
    assert final.is_file()

    result = migrate_final(final, quarantine_root=quarantine, apply=True)

    assert result["status"] == "migrated_final"
    assert result["preserved_blocks"] == 1
    assert not final.exists()
    assert not srt.exists()
    checkpoint = directory / f"{video_id}.review.checkpoint.json"
    migrated = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == REVIEW_CHECKPOINT_SCHEMA
    assert migrated["blocks"] == [
        {"commit_ids": ["c0"], "review_artifact": _artifact()}
    ]
    backup = (
        quarantine
        / video_id
        / (f"translation.json.{hashlib.sha256(original).hexdigest()}.bak")
    )
    assert backup.read_bytes() == original


def test_migrate_checkpoint_truncates_at_first_legacy_fallback(tmp_path):
    video_id = "b" * 64
    checkpoint = (
        tmp_path
        / "translation/corpus"
        / video_id
        / f"{video_id}.review.checkpoint.json"
    )
    value = {**_top(video_id, REVIEW_CHECKPOINT_SCHEMA), "blocks": _blocks()}
    original = _write_json(checkpoint, value)
    quarantine = tmp_path / "state/quarantine"

    result = migrate_checkpoint(checkpoint, quarantine_root=quarantine, apply=True)

    assert result["status"] == "migrated_checkpoint"
    assert result["preserved_blocks"] == 1
    assert result["discarded_blocks"] == 2
    migrated = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert migrated["blocks"] == [_blocks()[0]]
    backup = (
        quarantine
        / video_id
        / (f"review.checkpoint.json.{hashlib.sha256(original).hexdigest()}.bak")
    )
    assert backup.read_bytes() == original


def test_current_retry_before_fallback_policy_is_not_legacy():
    message = json.dumps(
        {
            LEGACY_FALLBACK_KEY: {"cue-1": {"draft_zh": "初译"}},
            "validated_draft_fallback_policy": "retry-before-fallback-v2",
        },
        ensure_ascii=False,
    )
    blocks = [
        {
            "commit_ids": ["c0"],
            "review_artifact": {
                "attempts": [{"attempt": 5, "code": "ok", "message": message}]
            },
        }
    ]

    assert first_legacy_fallback_block(blocks) is None


def test_unconfirmed_inaudible_marker_is_legacy_but_confirmed_marker_is_not():
    unconfirmed = [
        {
            "commit_ids": ["c0"],
            "review_artifact": {
                "attempts": [{"attempt": 1, "code": "ok", "message": ""}],
                "texts": [["c0", "（语音不清）"]],
            },
        }
    ]
    confirmed_message = json.dumps(
        {
            "confirmed_inaudible_policy": "review-retry-confirmed-v1",
            "confirmed_inaudible_markers": {"c0": {"source_ja": "。"}},
        },
        ensure_ascii=False,
    )
    confirmed = [
        {
            "commit_ids": ["c0"],
            "review_artifact": {
                "attempts": [
                    {"attempt": 5, "code": "ok", "message": confirmed_message}
                ],
                "texts": [["c0", "（语音不清）"]],
            },
        }
    ]

    assert first_legacy_fallback_block(unconfirmed) == 0
    assert first_legacy_fallback_block(confirmed) is None


def test_migration_history_preserves_prior_runs(tmp_path):
    state = tmp_path / "state/review_policy_v2_migration.json"
    first = {"schema_version": MIGRATION_SCHEMA, "affected_videos": 35}
    second = {"schema_version": MIGRATION_SCHEMA, "affected_videos": 26}
    _write_json(state, first)

    write_migration_history(state, second)

    history = json.loads(state.read_text(encoding="utf-8"))
    assert history == {
        "schema_version": MIGRATION_HISTORY_SCHEMA,
        "run_count": 2,
        "runs": [first, second],
    }


def test_exact_cue_policy_mismatch_requires_review_replay():
    cue_id = (
        "805fb20e2129e8527d2ba5650db0b4fcef78c38e0c1d5d0df478cadd9901e205"
        "-seg-00000577-cue-0000"
    )
    blocks = [
        {
            "commit_ids": [cue_id],
            "review_artifact": {
                "attempts": [{"attempt": 5, "code": "ok", "message": ""}],
                "texts": [[cue_id, "和信"]],
            },
        }
    ]

    assert first_legacy_fallback_block(blocks) == 0
    blocks[0]["review_artifact"]["texts"] = [[cue_id, "Kazushin"]]
    assert first_legacy_fallback_block(blocks) is None

    punctuation_id = (
        "e61b6979e9c481ce0af46bcf37e41cc32a835fe5fcec2817f256938ae719a132"
        "-seg-00000026-cue-0000"
    )
    blocks[0]["review_artifact"]["texts"] = [[punctuation_id, "做"]]
    assert first_legacy_fallback_block(blocks) is None
