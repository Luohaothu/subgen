from __future__ import annotations

import argparse
import json
import os
import re
import sys
import types
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .alignment import JapaneseCue, timestamp_digest
from .settings import load_settings
from .srt import Cue, render_srt, validate_candidate
from .translation import (
    TranslationBlock,
    apply_translations,
    build_translation_blocks,
    glossary_digest,
    load_simplifier,
    source_cue_digest,
    translation_flags,
)
from .translation_runner import (
    DEFAULT_SEED,
    ESCALATION_MODEL_ID,
    PRIMARY_MODEL_ID,
    GlossaryArtifact,
    ModelConfig,
    TranslationArtifact,
    TranslationAttempt,
    _parse_model_glossary_response,
    _subtitle_text_flags,
    _text_digest,
    extract_glossary,
    review_block,
    translate_block,
)

WORK_ROOT = Path("/home/leo/.local/share/abc-subtitles-recovery-6")
DRAFT_SCHEMA = "translation-draft-v1"
FINAL_SCHEMA = "translation-final-v1"
PRIMARY_CHECKPOINT_SCHEMA = "translation-primary-checkpoint-v1"
REVIEW_CHECKPOINT_SCHEMA = "translation-review-checkpoint-v1"
SAFE_VIDEO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class TranslationRuntimeError(RuntimeError):
    pass


def _strict_json(path: Path) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TranslationRuntimeError(f"cannot read strict JSON {path}: {exc}") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            part,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while storing translation artifact")
            view = view[written:]
        os.close(descriptor)
        descriptor = None
        os.replace(part, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            part.unlink()
        except FileNotFoundError:
            pass


def _json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TranslationRuntimeError(
            f"translation evidence is not strict JSON: {exc}"
        ) from exc
    return (text + "\n").encode("utf-8")


def _inside(root: Path, path: Path, *, create: bool) -> Path:
    canonical_root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else canonical_root / path
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(canonical_root)
    except ValueError as exc:
        raise TranslationRuntimeError(
            f"translation path escapes local work root: {resolved}"
        ) from exc
    return resolved


def _safe_video_id(value: str) -> str:
    if type(value) is not str or SAFE_VIDEO_ID.fullmatch(value) is None:
        raise TranslationRuntimeError(
            "video ID must be a safe local filename component"
        )
    return value


def _is_nonlexical_small_tsu(text: str) -> bool:
    lexical = "".join(
        character
        for character in text
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )
    return lexical in {"っ", "ッ"}


def _load_alignment(path: Path, work_root: Path) -> tuple[JapaneseCue, ...]:
    canonical = _inside(work_root, path, create=False)
    value = _strict_json(canonical)
    if not isinstance(value, dict) or value.get("failure_count") != 0:
        raise TranslationRuntimeError("alignment must be a successful object")
    rows = value.get("cues")
    if type(rows) is not list or value.get("cue_count") != len(rows) or not rows:
        raise TranslationRuntimeError("alignment cues are empty or incomplete")
    cues: list[JapaneseCue] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "cue_id",
            "start_ms",
            "end_ms",
            "text",
        }:
            raise TranslationRuntimeError(f"alignment cue {index} has invalid fields")
        cue_id = row["cue_id"]
        start_ms = row["start_ms"]
        end_ms = row["end_ms"]
        text = row["text"]
        if (
            type(cue_id) is not str
            or not cue_id
            or type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms
            or type(text) is not str
            or not text.strip()
            or "\ufffd" in text
        ):
            raise TranslationRuntimeError(f"alignment cue {index} is invalid")
        cues.append(JapaneseCue(cue_id, start_ms, end_ms, text))
    ids = [cue.cue_id for cue in cues]
    if len(ids) != len(set(ids)):
        raise TranslationRuntimeError("alignment cue IDs are not unique")
    if any(
        current.start_ms < previous.end_ms for previous, current in zip(cues, cues[1:])
    ):
        raise TranslationRuntimeError("alignment cues overlap or are unordered")
    validated_cues = tuple(cues)
    digest = timestamp_digest(validated_cues)
    if value.get("timestamp_digest") != digest:
        raise TranslationRuntimeError("alignment timestamp digest differs")
    return tuple(
        cue for cue in validated_cues if not _is_nonlexical_small_tsu(cue.text)
    )


def _normalize_endpoint(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("endpoint must be non-empty")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be an HTTP(S) URL without credentials/query")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = (
            f"{path}/chat/completions"
            if path.endswith("/v1")
            else f"{path}/v1/chat/completions"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class _Completions:
    def __init__(self, owner: OpenAICompatibleClient) -> None:
        self._owner = owner

    def create(self, **kwargs: object) -> object:
        return self._owner._create(kwargs)


class OpenAICompatibleClient:
    """Small OpenAI-compatible /v1/chat/completions adapter for local servers."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str,
        timeout: float,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if type(api_key) is not str or not api_key:
            raise ValueError("API key must be non-empty")
        if type(timeout) not in {int, float} or not 0 < timeout < float("inf"):
            raise ValueError("timeout must be finite and positive")
        self._endpoint = _normalize_endpoint(endpoint)
        self._api_key = api_key
        self._timeout = float(timeout)
        self._opener = opener
        self.chat = types.SimpleNamespace(completions=_Completions(self))

    def _create(self, kwargs: Mapping[str, object]) -> object:
        body = dict(kwargs)
        extra = body.pop("extra_body", {})
        if not isinstance(extra, Mapping):
            raise ValueError("extra_body must be an object")
        collisions = set(body).intersection(extra)
        if collisions:
            raise ValueError(
                f"extra_body duplicates request fields: {sorted(collisions)}"
            )
        body.update(extra)
        request = Request(
            self._endpoint,
            data=_json_bytes(body).rstrip(b"\n"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read()
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            if len(detail) > 2000:
                detail = detail[:2000] + "..."
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"translation endpoint returned HTTP {exc.code}{suffix}"
            ) from exc
        if type(status) is not int or not 200 <= status < 300:
            raise RuntimeError(f"translation endpoint returned HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
            finish_reason = choices[0].get("finish_reason")
        except (
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise RuntimeError(
                "translation endpoint returned an invalid envelope"
            ) from exc
        if type(content) is not str:
            raise RuntimeError("translation endpoint content is not a string")
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content),
                    finish_reason=finish_reason,
                )
            ]
        )


def _primary_config(model: str, revision: str, max_tokens: int) -> ModelConfig:
    return ModelConfig(
        model,
        revision,
        "primary",
        0.2,
        0.8,
        DEFAULT_SEED,
        1,
        max_tokens,
        False,
    )


def _review_config(model: str, revision: str, max_tokens: int) -> ModelConfig:
    if model == ESCALATION_MODEL_ID:
        return ModelConfig.escalation(revision=revision, max_tokens=max_tokens)
    return ModelConfig.review(model_id=model, revision=revision, max_tokens=max_tokens)


def _require_success(artifact: TranslationArtifact, stage: str) -> None:
    if artifact.status == "blocked":
        attempts = "; ".join(
            f"{attempt.code}: {attempt.message}" for attempt in artifact.attempts
        )
        raise TranslationRuntimeError(f"{stage} blocked after attempts: {attempts}")


def _attempt_from_dict(value: object) -> TranslationAttempt:
    if not isinstance(value, dict) or set(value) != {
        "attempt",
        "code",
        "message",
        "raw",
    }:
        raise TranslationRuntimeError("stored translation attempt is invalid")
    return TranslationAttempt(
        value["attempt"], value["code"], value["message"], value["raw"]
    )


def _artifact_from_dict(value: object) -> TranslationArtifact:
    fields = {
        "texts",
        "timestamp_digest",
        "source_digest",
        "glossary_digest",
        "input_text_digest",
        "output_text_digest",
        "model_id",
        "revision",
        "pass_name",
        "status",
        "attempts",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise TranslationRuntimeError("stored translation artifact is invalid")
    texts = value["texts"]
    attempts = value["attempts"]
    if type(texts) is not list or type(attempts) is not list:
        raise TranslationRuntimeError("stored translation artifact rows are invalid")
    try:
        return TranslationArtifact(
            texts=tuple(tuple(row) for row in texts),
            timestamp_digest=value["timestamp_digest"],
            source_digest=value["source_digest"],
            glossary_digest=value["glossary_digest"],
            input_text_digest=value["input_text_digest"],
            output_text_digest=value["output_text_digest"],
            model_id=value["model_id"],
            revision=value["revision"],
            pass_name=value["pass_name"],
            status=value["status"],
            attempts=tuple(_attempt_from_dict(row) for row in attempts),
        )
    except (TypeError, ValueError) as exc:
        raise TranslationRuntimeError(
            f"stored translation artifact failed validation: {exc}"
        ) from exc


def _glossary_from_dict(
    value: object,
    source_cues: tuple[JapaneseCue, ...],
) -> GlossaryArtifact:
    fields = {
        "glossary",
        "source_digest",
        "content_digest",
        "model_id",
        "revision",
        "status",
        "attempts",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise TranslationRuntimeError("stored glossary artifact is invalid")
    attempts_value = value["attempts"]
    if type(attempts_value) is not list or not attempts_value:
        raise TranslationRuntimeError("stored glossary attempts are invalid")
    attempts = tuple(_attempt_from_dict(row) for row in attempts_value)
    raw = attempts[-1].raw
    if type(raw) is not str:
        raise TranslationRuntimeError("stored glossary lacks raw evidence")
    try:
        glossary = _parse_model_glossary_response(raw, source_cues)
        if value["glossary"] != glossary.to_dict():
            raise ValueError("stored glossary structure differs from raw evidence")
        artifact = GlossaryArtifact(
            glossary=glossary,
            source_digest=value["source_digest"],
            content_digest=value["content_digest"],
            model_id=value["model_id"],
            revision=value["revision"],
            status=value["status"],
            attempts=attempts,
        )
        if value["glossary"] != glossary.to_dict():
            raise ValueError("stored glossary payload differs from raw evidence")
    except (TypeError, ValueError) as exc:
        raise TranslationRuntimeError(
            f"stored glossary failed validation: {exc}"
        ) from exc
    return artifact


@dataclass(frozen=True)
class VerifiedDraft:
    primary_model: str
    primary_revision: str
    commit_size: int
    glossary: GlossaryArtifact
    artifacts: tuple[TranslationArtifact, ...]
    texts: dict[str, str]


def _primary_checkpoint_payload(
    *,
    video_id: str,
    source_cues: tuple[JapaneseCue, ...],
    commit_size: int,
    config: ModelConfig,
    glossary: GlossaryArtifact,
    blocks: Sequence[Any],
    artifacts: Sequence[TranslationArtifact],
) -> dict[str, object]:
    return {
        "schema_version": PRIMARY_CHECKPOINT_SCHEMA,
        "video_id": video_id,
        "cue_count": len(source_cues),
        "timestamp_digest": timestamp_digest(source_cues),
        "source_digest": source_cue_digest(source_cues),
        "commit_size": commit_size,
        "primary": {"model": config.model_id, "revision": config.revision},
        "glossary_artifact": asdict(glossary),
        "blocks": [
            {
                "commit_ids": list(block.commit_ids),
                "primary_artifact": asdict(artifact),
            }
            for block, artifact in zip(blocks, artifacts)
        ],
    }


def _verified_primary_checkpoint(
    path: Path,
    source_cues: tuple[JapaneseCue, ...],
    *,
    video_id: str,
    commit_size: int,
    config: ModelConfig,
) -> tuple[GlossaryArtifact, list[TranslationArtifact], dict[str, str]]:
    value = _strict_json(path)
    required = {
        "schema_version",
        "video_id",
        "cue_count",
        "timestamp_digest",
        "source_digest",
        "commit_size",
        "primary",
        "glossary_artifact",
        "blocks",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise TranslationRuntimeError("primary checkpoint has invalid fields")
    source_ts = timestamp_digest(source_cues)
    source_digest = source_cue_digest(source_cues)
    expected_primary = {"model": config.model_id, "revision": config.revision}
    if (
        value["schema_version"] != PRIMARY_CHECKPOINT_SCHEMA
        or value["video_id"] != video_id
        or value["cue_count"] != len(source_cues)
        or value["timestamp_digest"] != source_ts
        or value["source_digest"] != source_digest
        or value["commit_size"] != commit_size
        or value["primary"] != expected_primary
    ):
        raise TranslationRuntimeError("primary checkpoint provenance differs")
    blocks = build_translation_blocks(source_cues, commit_size=commit_size)
    raw_blocks = value["blocks"]
    if type(raw_blocks) is not list or len(raw_blocks) > len(blocks):
        raise TranslationRuntimeError("primary checkpoint block coverage is invalid")
    glossary = _glossary_from_dict(value["glossary_artifact"], source_cues)
    expected_glossary_digest = glossary_digest(glossary.glossary)
    artifacts: list[TranslationArtifact] = []
    texts: dict[str, str] = {}
    for block, raw_block in zip(blocks, raw_blocks):
        if not isinstance(raw_block, dict) or set(raw_block) != {
            "commit_ids",
            "primary_artifact",
        }:
            raise TranslationRuntimeError("primary checkpoint block is invalid")
        if raw_block["commit_ids"] != list(block.commit_ids):
            raise TranslationRuntimeError("primary checkpoint cue IDs differ")
        artifact = _artifact_from_dict(raw_block["primary_artifact"])
        if (
            artifact.pass_name != "primary"
            or artifact.status != "draft_ok"
            or artifact.model_id != config.model_id
            or artifact.revision != config.revision
            or artifact.source_digest != source_digest
            or artifact.glossary_digest != expected_glossary_digest
            or artifact.timestamp_digest != timestamp_digest(block.commit_cues)
            or tuple(cue_id for cue_id, _ in artifact.texts) != block.commit_ids
        ):
            raise TranslationRuntimeError("primary checkpoint artifact differs")
        artifacts.append(artifact)
        texts.update(artifact.texts)
    return glossary, artifacts, texts


def _review_checkpoint_payload(
    *,
    video_id: str,
    source_cues: tuple[JapaneseCue, ...],
    draft_relative: str,
    draft: VerifiedDraft,
    config: ModelConfig,
    blocks: Sequence[Any],
    artifacts: Sequence[TranslationArtifact],
) -> dict[str, object]:
    return {
        "schema_version": REVIEW_CHECKPOINT_SCHEMA,
        "video_id": video_id,
        "cue_count": len(source_cues),
        "timestamp_digest": timestamp_digest(source_cues),
        "source_digest": source_cue_digest(source_cues),
        "glossary_digest": glossary_digest(draft.glossary.glossary),
        "draft_json": draft_relative,
        "primary": {
            "model": draft.primary_model,
            "revision": draft.primary_revision,
        },
        "review": {"model": config.model_id, "revision": config.revision},
        "blocks": [
            {
                "commit_ids": list(block.commit_ids),
                "review_artifact": asdict(artifact),
            }
            for block, artifact in zip(blocks, artifacts)
        ],
    }


def _verified_review_checkpoint(
    path: Path,
    source_cues: tuple[JapaneseCue, ...],
    *,
    video_id: str,
    draft_relative: str,
    draft: VerifiedDraft,
    config: ModelConfig,
) -> tuple[list[TranslationArtifact], dict[str, str]]:
    value = _strict_json(path)
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
    }
    if not isinstance(value, dict) or set(value) != required:
        raise TranslationRuntimeError("review checkpoint has invalid fields")
    source_ts = timestamp_digest(source_cues)
    source_digest = source_cue_digest(source_cues)
    expected_glossary_digest = glossary_digest(draft.glossary.glossary)
    expected_primary = {
        "model": draft.primary_model,
        "revision": draft.primary_revision,
    }
    expected_review = {"model": config.model_id, "revision": config.revision}
    if (
        value["schema_version"] != REVIEW_CHECKPOINT_SCHEMA
        or value["video_id"] != video_id
        or value["cue_count"] != len(source_cues)
        or value["timestamp_digest"] != source_ts
        or value["source_digest"] != source_digest
        or value["glossary_digest"] != expected_glossary_digest
        or value["draft_json"] != draft_relative
        or value["primary"] != expected_primary
        or value["review"] != expected_review
    ):
        raise TranslationRuntimeError("review checkpoint provenance differs")
    blocks = build_translation_blocks(source_cues, commit_size=draft.commit_size)
    raw_blocks = value["blocks"]
    if type(raw_blocks) is not list or len(raw_blocks) > len(blocks):
        raise TranslationRuntimeError("review checkpoint block coverage is invalid")
    artifacts: list[TranslationArtifact] = []
    texts: dict[str, str] = {}
    for block, raw_block in zip(blocks, raw_blocks):
        if not isinstance(raw_block, dict) or set(raw_block) != {
            "commit_ids",
            "review_artifact",
        }:
            raise TranslationRuntimeError("review checkpoint block is invalid")
        if raw_block["commit_ids"] != list(block.commit_ids):
            raise TranslationRuntimeError("review checkpoint cue IDs differ")
        artifact = _artifact_from_dict(raw_block["review_artifact"])
        block_draft_rows = tuple(
            (cue_id, draft.texts[cue_id]) for cue_id in block.commit_ids
        )
        expected_status = {
            "review": "review_ok",
            "escalation": "escalation_ok",
        }[config.pass_name]
        if (
            artifact.pass_name != config.pass_name
            or artifact.status != expected_status
            or artifact.model_id != config.model_id
            or artifact.revision != config.revision
            or artifact.source_digest != source_digest
            or artifact.glossary_digest != expected_glossary_digest
            or artifact.timestamp_digest != timestamp_digest(block.commit_cues)
            or artifact.input_text_digest != _text_digest(block_draft_rows)
            or tuple(cue_id for cue_id, _ in artifact.texts) != block.commit_ids
        ):
            raise TranslationRuntimeError("review checkpoint artifact differs")
        if any(_subtitle_text_flags(text) for _, text in artifact.texts):
            print(
                f"REVIEW checkpoint truncated before unsafe block {len(artifacts) + 1}",
                flush=True,
            )
            break
        artifacts.append(artifact)
        texts.update(artifact.texts)
    return artifacts, texts


def _verified_draft(
    path: Path,
    source_cues: tuple[JapaneseCue, ...],
    *,
    video_id: str,
) -> VerifiedDraft:
    value = _strict_json(path)
    required = {
        "schema_version",
        "video_id",
        "cue_count",
        "timestamp_digest",
        "source_digest",
        "commit_size",
        "primary",
        "glossary_artifact",
        "blocks",
        "cues",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise TranslationRuntimeError("draft has invalid top-level fields")
    source_ts = timestamp_digest(source_cues)
    source_digest = source_cue_digest(source_cues)
    if (
        value["schema_version"] != DRAFT_SCHEMA
        or value["video_id"] != video_id
        or value["cue_count"] != len(source_cues)
        or value["timestamp_digest"] != source_ts
        or value["source_digest"] != source_digest
    ):
        raise TranslationRuntimeError("draft source provenance differs from alignment")
    primary = value["primary"]
    if not isinstance(primary, dict) or set(primary) != {"model", "revision"}:
        raise TranslationRuntimeError("draft primary provenance is invalid")
    commit_size = value["commit_size"]
    if type(commit_size) is not int:
        raise TranslationRuntimeError("draft commit size is invalid")
    blocks = build_translation_blocks(source_cues, commit_size=commit_size)
    raw_blocks = value["blocks"]
    raw_cues = value["cues"]
    if type(raw_blocks) is not list or len(raw_blocks) != len(blocks):
        raise TranslationRuntimeError("draft block coverage is incomplete")
    if type(raw_cues) is not list or len(raw_cues) != len(source_cues):
        raise TranslationRuntimeError("draft cue coverage is incomplete")
    glossary = _glossary_from_dict(value["glossary_artifact"], source_cues)
    expected_glossary_digest = glossary_digest(glossary.glossary)
    texts: dict[str, str] = {}
    artifacts: list[TranslationArtifact] = []
    for block, raw_block in zip(blocks, raw_blocks):
        if not isinstance(raw_block, dict) or set(raw_block) != {
            "commit_ids",
            "primary_artifact",
        }:
            raise TranslationRuntimeError("draft block evidence is invalid")
        if raw_block["commit_ids"] != list(block.commit_ids):
            raise TranslationRuntimeError("draft block cue IDs differ")
        artifact = _artifact_from_dict(raw_block["primary_artifact"])
        if (
            artifact.pass_name != "primary"
            or artifact.status != "draft_ok"
            or artifact.model_id != primary["model"]
            or artifact.revision != primary["revision"]
            or artifact.source_digest != source_digest
            or artifact.glossary_digest != expected_glossary_digest
            or artifact.timestamp_digest != timestamp_digest(block.commit_cues)
            or tuple(cue_id for cue_id, _ in artifact.texts) != block.commit_ids
        ):
            raise TranslationRuntimeError("draft primary artifact provenance differs")
        artifacts.append(artifact)
        texts.update(artifact.texts)
    expected_ids = [cue.cue_id for cue in source_cues]
    if list(texts) != expected_ids:
        raise TranslationRuntimeError("draft does not cover every source cue in order")
    for source, row in zip(source_cues, raw_cues):
        if not isinstance(row, dict) or row != {
            "id": source.cue_id,
            "start_ms": source.start_ms,
            "end_ms": source.end_ms,
            "ja": source.text,
            "draft_zh": texts[source.cue_id],
        }:
            raise TranslationRuntimeError("draft cue evidence differs from alignment")
    simplifier = load_simplifier()
    quality = {
        source.cue_id: flags
        for source in source_cues
        if (
            flags := translation_flags(
                source.text,
                texts[source.cue_id],
                glossary.glossary,
                simplifier=simplifier,
                cue_id=source.cue_id,
            )
        )
    }
    if quality:
        raise TranslationRuntimeError(
            "draft translation fails current quality gates: "
            + json.dumps(
                quality,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:2000]
        )
    return VerifiedDraft(
        primary_model=primary["model"],
        primary_revision=primary["revision"],
        commit_size=commit_size,
        glossary=glossary,
        artifacts=tuple(artifacts),
        texts=texts,
    )


def _single_cue_primary_fallback(
    client: Any,
    block: TranslationBlock,
    blocked: TranslationArtifact,
    glossary: GlossaryArtifact,
    config: ModelConfig,
    *,
    source_cues: tuple[JapaneseCue, ...],
    expected_source_digest: str,
    expected_glossary_digest: str,
    simplifier: Callable[[str], str],
) -> TranslationArtifact:
    """Retry only a blocked batch as single cues, retaining its block layout."""
    fallback_config = replace(config, max_tokens=min(config.max_tokens, 1024))
    single_blocks = {
        candidate.commit_ids[0]: candidate
        for candidate in build_translation_blocks(source_cues, commit_size=1)
    }
    rows: list[tuple[str, str]] = []
    for cue in block.commit_cues:
        single = single_blocks[cue.cue_id]
        artifact = translate_block(
            client,
            single,
            glossary,
            fallback_config,
            source_cues=source_cues,
            expected_digest=timestamp_digest(single.commit_cues),
            expected_source_digest=expected_source_digest,
            expected_glossary_digest=expected_glossary_digest,
            simplifier=simplifier,
        )
        _require_success(artifact, f"single-cue fallback {cue.cue_id}")
        rows.extend(artifact.texts)
    texts = tuple(rows)
    raw = json.dumps(
        {"cues": [{"id": cue_id, "zh": text} for cue_id, text in texts]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    retained = blocked.attempts[-4:]
    attempts = tuple(
        TranslationAttempt(index, attempt.code, attempt.message, attempt.raw)
        for index, attempt in enumerate(retained, 1)
    ) + (
        TranslationAttempt(
            len(retained) + 1,
            "ok",
            "combined from individually model-validated single-cue responses",
            raw,
        ),
    )
    return TranslationArtifact(
        texts=texts,
        timestamp_digest=timestamp_digest(block.commit_cues),
        source_digest=expected_source_digest,
        glossary_digest=expected_glossary_digest,
        input_text_digest=None,
        output_text_digest=_text_digest(texts),
        model_id=config.model_id,
        revision=config.revision,
        pass_name="primary",
        status="draft_ok",
        attempts=attempts,
    )


def _single_cue_review_fallback(
    client: Any,
    block: TranslationBlock,
    blocked: TranslationArtifact,
    draft: dict[str, str],
    glossary: GlossaryArtifact,
    config: ModelConfig,
    *,
    source_cues: tuple[JapaneseCue, ...],
    expected_source_digest: str,
    expected_glossary_digest: str,
    simplifier: Callable[[str], str],
) -> TranslationArtifact:
    """Retry a length-truncated review batch cue by cue without changing layout."""
    fallback_config = replace(config, max_tokens=min(config.max_tokens, 1024))
    single_blocks = {
        candidate.commit_ids[0]: candidate
        for candidate in build_translation_blocks(source_cues, commit_size=1)
    }
    rows: list[tuple[str, str]] = []
    for cue in block.commit_cues:
        single = single_blocks[cue.cue_id]
        artifact = review_block(
            client,
            single,
            {cue.cue_id: draft[cue.cue_id]},
            glossary,
            fallback_config,
            source_cues=source_cues,
            expected_digest=timestamp_digest(single.commit_cues),
            expected_source_digest=expected_source_digest,
            expected_glossary_digest=expected_glossary_digest,
            simplifier=simplifier,
        )
        _require_success(artifact, f"single-cue review fallback {cue.cue_id}")
        rows.extend(artifact.texts)
    texts = tuple(rows)
    raw = json.dumps(
        {"cues": [{"id": cue_id, "zh": text} for cue_id, text in texts]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    retained = blocked.attempts[-4:]
    attempts = tuple(
        TranslationAttempt(index, attempt.code, attempt.message, attempt.raw)
        for index, attempt in enumerate(retained, 1)
    ) + (
        TranslationAttempt(
            len(retained) + 1,
            "ok",
            "combined from individually model-validated single-cue review responses",
            raw,
        ),
    )
    input_rows = tuple((cue_id, draft[cue_id]) for cue_id in block.commit_ids)
    return TranslationArtifact(
        texts=texts,
        timestamp_digest=timestamp_digest(block.commit_cues),
        source_digest=expected_source_digest,
        glossary_digest=expected_glossary_digest,
        input_text_digest=_text_digest(input_rows),
        output_text_digest=_text_digest(texts),
        model_id=config.model_id,
        revision=config.revision,
        pass_name=config.pass_name,
        status={
            "review": "review_ok",
            "escalation": "escalation_ok",
        }[config.pass_name],
        attempts=attempts,
    )


def run_primary(
    alignment: Path,
    *,
    video_id: str,
    output_dir: Path,
    client: Any,
    model: str,
    revision: str,
    commit_size: int,
    max_tokens: int,
    work_root: Path = WORK_ROOT,
) -> dict[str, object]:
    video_id = _safe_video_id(video_id)
    cues = _load_alignment(alignment, work_root)
    output = _inside(work_root, output_dir, create=True)
    config = _primary_config(model, revision, max_tokens)
    simplifier = load_simplifier()
    source_digest = source_cue_digest(cues)
    global_timestamp_digest = timestamp_digest(cues)
    blocks = build_translation_blocks(cues, commit_size=commit_size)
    checkpoint_path = output / f"{video_id}.primary.checkpoint.json"
    checkpoint = None
    if checkpoint_path.is_file():
        try:
            checkpoint = _verified_primary_checkpoint(
                checkpoint_path,
                cues,
                video_id=video_id,
                commit_size=commit_size,
                config=config,
            )
        except TranslationRuntimeError as exc:
            print(f"PRIMARY checkpoint ignored: {exc}", flush=True)
    if checkpoint is None:
        glossary = extract_glossary(client, cues, config)
        if glossary.status != "ok" or glossary.glossary is None:
            attempts = ", ".join(attempt.code for attempt in glossary.attempts)
            raise TranslationRuntimeError(
                f"glossary blocked after attempts: {attempts}"
            )
        artifacts: list[TranslationArtifact] = []
        draft: dict[str, str] = {}
        _atomic_write(
            checkpoint_path,
            _json_bytes(
                _primary_checkpoint_payload(
                    video_id=video_id,
                    source_cues=cues,
                    commit_size=commit_size,
                    config=config,
                    glossary=glossary,
                    blocks=blocks,
                    artifacts=artifacts,
                )
            ),
        )
    else:
        glossary, artifacts, draft = checkpoint
        print(f"PRIMARY resume {len(artifacts)}/{len(blocks)}", flush=True)
    content_digest = glossary_digest(glossary.glossary)
    for index, block in enumerate(blocks[len(artifacts) :], len(artifacts) + 1):
        artifact = translate_block(
            client,
            block,
            glossary,
            config,
            source_cues=cues,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_digest,
            expected_glossary_digest=content_digest,
            simplifier=simplifier,
        )
        if artifact.status == "blocked" and len(block.commit_cues) > 1:
            print(
                f"PRIMARY block {index} single-cue fallback "
                f"count={len(block.commit_cues)}",
                flush=True,
            )
            artifact = _single_cue_primary_fallback(
                client,
                block,
                artifact,
                glossary,
                config,
                source_cues=cues,
                expected_source_digest=source_digest,
                expected_glossary_digest=content_digest,
                simplifier=simplifier,
            )
        _require_success(artifact, f"primary block {index}")
        artifacts.append(artifact)
        draft.update(artifact.texts)
        _atomic_write(
            checkpoint_path,
            _json_bytes(
                _primary_checkpoint_payload(
                    video_id=video_id,
                    source_cues=cues,
                    commit_size=commit_size,
                    config=config,
                    glossary=glossary,
                    blocks=blocks,
                    artifacts=artifacts,
                )
            ),
        )
        print(f"PRIMARY {index}/{len(blocks)}", flush=True)
    apply_translations(cues, draft, global_timestamp_digest)
    evidence = {
        "schema_version": DRAFT_SCHEMA,
        "video_id": video_id,
        "cue_count": len(cues),
        "timestamp_digest": global_timestamp_digest,
        "source_digest": source_digest,
        "commit_size": commit_size,
        "primary": {"model": config.model_id, "revision": config.revision},
        "glossary_artifact": asdict(glossary),
        "blocks": [
            {
                "commit_ids": list(block.commit_ids),
                "primary_artifact": asdict(artifact),
            }
            for block, artifact in zip(blocks, artifacts)
        ],
        "cues": [
            {
                "id": cue.cue_id,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "ja": cue.text,
                "draft_zh": draft[cue.cue_id],
            }
            for cue in cues
        ],
    }
    path = output / f"{video_id}.draft.translation.json"
    _atomic_write(path, _json_bytes(evidence))
    checkpoint_path.unlink(missing_ok=True)
    return {
        "stage": "primary",
        "video_id": video_id,
        "cue_count": len(cues),
        "block_count": len(blocks),
        "draft_json": str(path),
        "timestamp_digest": global_timestamp_digest,
        "glossary_digest": content_digest,
    }


def run_review(
    alignment: Path,
    draft_json: Path,
    *,
    video_id: str,
    output_dir: Path,
    client: Any,
    model: str,
    revision: str,
    max_tokens: int,
    video_duration_ms: int | None,
    work_root: Path = WORK_ROOT,
    block_executor: Executor | None = None,
) -> dict[str, object]:
    video_id = _safe_video_id(video_id)
    cues = _load_alignment(alignment, work_root)
    output = _inside(work_root, output_dir, create=True)
    draft_path = _inside(work_root, draft_json, create=False)
    draft = _verified_draft(draft_path, cues, video_id=video_id)
    config = _review_config(model, revision, max_tokens)
    simplifier = load_simplifier()
    source_digest = source_cue_digest(cues)
    global_timestamp_digest = timestamp_digest(cues)
    content_digest = glossary_digest(draft.glossary.glossary)
    blocks = build_translation_blocks(cues, commit_size=draft.commit_size)
    draft_relative = str(draft_path.relative_to(work_root.resolve(strict=True)))
    checkpoint_path = output / f"{video_id}.review.checkpoint.json"
    checkpoint = None
    if checkpoint_path.is_file():
        try:
            checkpoint = _verified_review_checkpoint(
                checkpoint_path,
                cues,
                video_id=video_id,
                draft_relative=draft_relative,
                draft=draft,
                config=config,
            )
        except TranslationRuntimeError as exc:
            print(f"REVIEW checkpoint ignored: {exc}", flush=True)
    if checkpoint is None:
        review_artifacts: list[TranslationArtifact] = []
        final: dict[str, str] = {}
        _atomic_write(
            checkpoint_path,
            _json_bytes(
                _review_checkpoint_payload(
                    video_id=video_id,
                    source_cues=cues,
                    draft_relative=draft_relative,
                    draft=draft,
                    config=config,
                    blocks=blocks,
                    artifacts=review_artifacts,
                )
            ),
        )
    else:
        review_artifacts, final = checkpoint
        print(f"REVIEW resume {len(review_artifacts)}/{len(blocks)}", flush=True)
    remaining = tuple(
        enumerate(blocks[len(review_artifacts) :], len(review_artifacts) + 1)
    )

    def review_one(index: int, block: TranslationBlock) -> TranslationArtifact:
        block_draft = {cue_id: draft.texts[cue_id] for cue_id in block.commit_ids}
        artifact = review_block(
            client,
            block,
            block_draft,
            draft.glossary,
            config,
            source_cues=cues,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_digest,
            expected_glossary_digest=content_digest,
            simplifier=simplifier,
        )
        if artifact.status == "blocked" and any(
            "finish_reason=length" in attempt.message for attempt in artifact.attempts
        ):
            print(
                f"REVIEW length fallback block={index} cues={len(block.commit_ids)}",
                flush=True,
            )
            artifact = _single_cue_review_fallback(
                client,
                block,
                artifact,
                block_draft,
                draft.glossary,
                config,
                source_cues=cues,
                expected_source_digest=source_digest,
                expected_glossary_digest=content_digest,
                simplifier=simplifier,
            )
        _require_success(artifact, f"review block {index}")
        return artifact

    submitted: list[tuple[int, TranslationBlock, Future[TranslationArtifact]]] = []
    if block_executor is None:
        completed = (
            (index, block, review_one(index, block)) for index, block in remaining
        )
    else:
        submitted = [
            (index, block, block_executor.submit(review_one, index, block))
            for index, block in remaining
        ]
        completed = (
            (index, block, future.result()) for index, block, future in submitted
        )
    try:
        for index, block, artifact in completed:
            review_artifacts.append(artifact)
            final.update(artifact.texts)
            _atomic_write(
                checkpoint_path,
                _json_bytes(
                    _review_checkpoint_payload(
                        video_id=video_id,
                        source_cues=cues,
                        draft_relative=draft_relative,
                        draft=draft,
                        config=config,
                        blocks=blocks,
                        artifacts=review_artifacts,
                    )
                ),
            )
            print(f"REVIEW {index}/{len(blocks)}", flush=True)
    finally:
        for _, _, future in submitted:
            future.cancel()
    translated = apply_translations(cues, final, global_timestamp_digest)
    srt_cues = tuple(
        Cue(cue.cue_id, cue.start_ms, cue.end_ms, cue.text) for cue in translated
    )
    srt = render_srt(srt_cues)
    duration_ms = (
        max(cue.end_ms for cue in cues)
        if video_duration_ms is None
        else video_duration_ms
    )
    if type(duration_ms) is not int or duration_ms < max(cue.end_ms for cue in cues):
        raise TranslationRuntimeError("video duration is shorter than aligned cues")
    flags = validate_candidate(srt, cues, duration_ms, simplifier=simplifier)
    if flags:
        raise TranslationRuntimeError(f"final SRT validation failed: {flags}")
    evidence = {
        "schema_version": FINAL_SCHEMA,
        "video_id": video_id,
        "cue_count": len(cues),
        "timestamp_digest": global_timestamp_digest,
        "source_digest": source_digest,
        "glossary_digest": content_digest,
        "draft_json": draft_relative,
        "primary": {
            "model": draft.primary_model,
            "revision": draft.primary_revision,
        },
        "review": {"model": config.model_id, "revision": config.revision},
        "blocks": [
            {
                "commit_ids": list(block.commit_ids),
                "primary_artifact": asdict(primary),
                "review_artifact": asdict(review),
            }
            for block, primary, review in zip(blocks, draft.artifacts, review_artifacts)
        ],
        "cues": [
            {
                "id": source.cue_id,
                "start_ms": source.start_ms,
                "end_ms": source.end_ms,
                "ja": source.text,
                "draft_zh": draft.texts[source.cue_id],
                "zh": final[source.cue_id],
            }
            for source in cues
        ],
    }
    translation_path = output / f"{video_id}.translation.json"
    srt_path = output / f"{video_id}.zh.srt"
    _atomic_write(translation_path, _json_bytes(evidence))
    _atomic_write(srt_path, srt.encode("utf-8"))
    checkpoint_path.unlink(missing_ok=True)
    return {
        "stage": "review",
        "video_id": video_id,
        "cue_count": len(cues),
        "block_count": len(blocks),
        "translation_json": str(translation_path),
        "srt": str(srt_path),
        "timestamp_digest": global_timestamp_digest,
        "glossary_digest": content_digest,
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _commit_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 120:
        raise argparse.ArgumentTypeError("must be at most 120")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 < parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m subtitle_pipeline.translation_runtime_cli",
        description="Staged local Japanese-to-Chinese subtitle translation runtime",
    )
    parser.add_argument(
        "--pass", dest="pass_name", choices=("primary", "review"), required=True
    )
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--revision")
    parser.add_argument("--draft-json", type=Path)
    parser.add_argument("--commit-size", type=_commit_size, default=120)
    parser.add_argument("--max-tokens", type=_positive_int, default=8192)
    parser.add_argument("--video-duration-ms", type=_positive_int)
    parser.add_argument("--timeout", type=_positive_float, default=600.0)
    parser.add_argument("--api-key-env")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        video_id = _safe_video_id(args.video_id)
        output_dir = args.output_dir or Path("translation/smoke") / video_id
        provider = load_settings().translation
        endpoint = args.endpoint or (provider.endpoint if provider else None)
        revision = args.revision or (provider.revision if provider else None)
        api_key_env = args.api_key_env or (
            provider.api_key_env if provider is not None else "OPENAI_API_KEY"
        )
        if not endpoint or not revision:
            raise TranslationRuntimeError(
                "translation requires --endpoint and --revision, "
                "or a [translation] section in subgen.toml"
            )
        api_key = os.environ.get(api_key_env, "local")
        client = OpenAICompatibleClient(
            endpoint,
            api_key=api_key,
            timeout=args.timeout,
        )
        if args.pass_name == "primary":
            if args.draft_json is not None:
                raise TranslationRuntimeError(
                    "primary pass does not accept --draft-json"
                )
            summary = run_primary(
                args.alignment,
                video_id=video_id,
                output_dir=output_dir,
                client=client,
                model=args.model
                or (provider.model if provider is not None else PRIMARY_MODEL_ID),
                revision=revision,
                commit_size=args.commit_size,
                max_tokens=args.max_tokens,
            )
        else:
            if args.draft_json is None:
                raise TranslationRuntimeError("review pass requires --draft-json")
            summary = run_review(
                args.alignment,
                args.draft_json,
                video_id=video_id,
                output_dir=output_dir,
                client=client,
                model=args.model
                or (provider.model if provider is not None else ESCALATION_MODEL_ID),
                revision=revision,
                max_tokens=args.max_tokens,
                video_duration_ms=args.video_duration_ms,
            )
    except KeyboardInterrupt:
        print("translation runtime interrupted", file=sys.stderr, flush=True)
        return 130
    except BaseException as exc:
        print(
            f"translation runtime failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
