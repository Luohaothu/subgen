from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .translation import (
    exact_required_translation,
    load_simplifier,
    translation_flags,
)
from .translation_runner import TranslationAttempt, _text_digest
from .translation_runtime_cli import (
    WORK_ROOT,
    TranslationRuntimeError,
    _artifact_from_dict,
    _atomic_write,
    _glossary_from_dict,
    _json_bytes,
    _load_alignment,
    _strict_json,
    _verified_draft,
)


def migrate_exact_draft(
    draft_path: Path,
    alignment_path: Path,
    work_root: Path,
    *,
    video_id: str,
    apply: bool,
) -> dict[str, object]:
    source_cues = _load_alignment(alignment_path, work_root)
    try:
        _verified_draft(draft_path, source_cues, video_id=video_id)
    except TranslationRuntimeError as exc:
        if not str(exc).startswith("draft translation fails current quality gates:"):
            return {"status": "invalid", "message": str(exc), "corrections": 0}
    else:
        return {"status": "valid", "message": "", "corrections": 0}

    value = _strict_json(draft_path)
    if not isinstance(value, dict):
        return {
            "status": "invalid",
            "message": "draft is not an object",
            "corrections": 0,
        }
    raw_blocks = value.get("blocks")
    raw_cues = value.get("cues")
    if not isinstance(raw_blocks, list) or not isinstance(raw_cues, list):
        return {
            "status": "invalid",
            "message": "draft coverage is invalid",
            "corrections": 0,
        }

    source_by_id = {cue.cue_id: cue for cue in source_cues}
    glossary = _glossary_from_dict(value["glossary_artifact"], source_cues).glossary
    final_texts: dict[str, str] = {}
    correction_count = 0
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            return {
                "status": "invalid",
                "message": "draft block is invalid",
                "corrections": 0,
            }
        artifact = _artifact_from_dict(raw_block.get("primary_artifact"))
        rows = dict(artifact.texts)
        corrections: dict[str, str] = {}
        for cue_id, current in artifact.texts:
            source = source_by_id[cue_id]
            required = exact_required_translation(source.text, cue_id=cue_id)
            if required is not None and current != required:
                rows[cue_id] = required
                corrections[cue_id] = required
        if corrections:
            ordered_rows = tuple((cue_id, rows[cue_id]) for cue_id, _ in artifact.texts)
            canonical_raw = json.dumps(
                {"cues": [{"id": cue_id, "zh": text} for cue_id, text in ordered_rows]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            last = artifact.attempts[-1]
            audit_message = json.dumps(
                {
                    "deterministic_exact_migration": corrections,
                    "original_message": last.message,
                    "original_raw": last.raw,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            attempts = artifact.attempts[:-1] + (
                TranslationAttempt(last.attempt, "ok", audit_message, canonical_raw),
            )
            artifact = replace(
                artifact,
                texts=ordered_rows,
                output_text_digest=_text_digest(ordered_rows),
                attempts=attempts,
            )
            raw_block["primary_artifact"] = asdict(artifact)
            correction_count += len(corrections)
        final_texts.update(artifact.texts)

    if correction_count == 0:
        return {
            "status": "invalid",
            "message": "no deterministic exact correction applies",
            "corrections": 0,
        }

    simplifier = load_simplifier()
    remaining_quality = {
        cue.cue_id: flags
        for cue in source_cues
        if (
            flags := translation_flags(
                cue.text,
                final_texts[cue.cue_id],
                glossary,
                simplifier=simplifier,
                cue_id=cue.cue_id,
            )
        )
    }
    if remaining_quality:
        return {
            "status": "remaining_quality",
            "message": json.dumps(
                remaining_quality,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:2000],
            "corrections": correction_count,
        }

    for source, raw_cue in zip(source_cues, raw_cues, strict=True):
        if not isinstance(raw_cue, dict) or raw_cue.get("id") != source.cue_id:
            return {
                "status": "invalid",
                "message": "draft cue order differs",
                "corrections": 0,
            }
        raw_cue["draft_zh"] = final_texts[source.cue_id]

    if not apply:
        return {
            "status": "migratable",
            "message": "",
            "corrections": correction_count,
        }

    candidate = draft_path.with_name(
        f".{draft_path.name}.{uuid.uuid4().hex}.migration-candidate"
    )
    try:
        _atomic_write(candidate, _json_bytes(value))
        _verified_draft(candidate, source_cues, video_id=video_id)
        os.replace(candidate, draft_path)
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    return {"status": "migrated", "message": "", "corrections": correction_count}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically apply audited deterministic exact-cue corrections to drafts"
    )
    parser.add_argument("--work-root", type=Path, default=WORK_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--active-seconds", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.work_root.resolve()
    drafts = sorted((root / "translation/corpus").glob("*/*.draft.translation.json"))
    counts: dict[str, int] = {}
    corrections = 0
    details: list[dict[str, object]] = []
    now = time.time()
    for draft in drafts:
        video_id = draft.parent.name
        checkpoint = draft.with_name(f"{video_id}.primary.checkpoint.json")
        if (
            checkpoint.is_file()
            and now - checkpoint.stat().st_mtime < args.active_seconds
        ):
            status = "active"
            result: dict[str, object] = {
                "status": status,
                "corrections": 0,
                "message": "",
            }
        else:
            result = migrate_exact_draft(
                draft,
                root / "asr/corpus" / video_id / "alignment.json",
                root,
                video_id=video_id,
                apply=args.apply,
            )
            status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        corrections += int(result["corrections"])
        if status not in {"valid", "migrated", "migratable"}:
            details.append({"video_id": video_id, **result})
    print(
        json.dumps(
            {
                "scanned": len(drafts),
                "apply": args.apply,
                "counts": counts,
                "corrections": corrections,
                "details": details,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
