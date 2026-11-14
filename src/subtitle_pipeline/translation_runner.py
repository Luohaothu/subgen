from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import InitVar, dataclass
from typing import Any

from subtitle_pipeline.alignment import JapaneseCue, timestamp_digest
from subtitle_pipeline.translation import (
    CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS,
    DIRECT_KANA_NAME_REQUIRED_LATIN,
    INAUDIBLE_TRANSLATION_CONTENT,
    Glossary,
    GlossaryEntry,
    TranslationBlock,
    apply_translations,
    build_translation_blocks,
    exact_required_translation,
    glossary_digest,
    parse_glossary_response,
    parse_translation_response,
    source_cue_digest,
    translation_flags,
    validate_glossary_against_source,
    validate_translation_block,
)

PRIMARY_MODEL_ID = "Qwen/Qwen3.5-35B-A3B-FP8"
FALLBACK_MODEL_ID = "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8"
ESCALATION_MODEL_ID = "Qwen/Qwen3.6-27B-FP8"
PRIMARY_MODEL_IDS = frozenset({PRIMARY_MODEL_ID, FALLBACK_MODEL_ID})
DEFAULT_SEED = 20260710
ESCALATION_POLICY_VERSION = "qwen36-block-review-v1"
ESCALATION_SAMPLE_DENOMINATOR = 20
ESCALATION_BLOCK_SIZE = 120
_FINALIZER_TOKEN = object()
MAX_BLOCK_ATTEMPTS = 5
VALIDATED_DRAFT_FALLBACK_POLICY = "retry-before-fallback-v2"
CONFIRMED_INAUDIBLE_POLICY = "review-retry-confirmed-v1"
INAUDIBLE_TRANSLATION = f"（{INAUDIBLE_TRANSLATION_CONTENT}）"

PRIMARY_SYSTEM_PROMPT = """You are localizing authorized Japanese dialogue subtitles into natural Simplified Chinese.
The request separates READ_ONLY_CONTEXT from COMMIT_CUES. Use context only to understand the
dialogue; return translations for every COMMIT_CUE and for no context cue. Translate committed
cues faithfully and explicitly. Do not censor, euphemize, moralize, refuse, add dialogue, omit
repetitions, invent subjects, merge cues, split cues, or change cue IDs. Resolve omitted Japanese
subjects only from supplied context. Preserve names, relationships, register, vulgarity, sexual
terminology, hesitation, and speaker intent consistently with the video glossary. Return only
JSON: {"cues":[{"id":"...","zh":"..."}, ...]} in exact COMMIT_CUE order. Each committed cue
must have exactly one non-empty Simplified Chinese output; never return timing fields. Japanese
ASR can occasionally emit dialogue already in natural Simplified Chinese; preserve such Chinese
source text exactly instead of paraphrasing merely to look translated. Render Japanese
interjections and incomplete Japanese fragments as minimal faithful natural Chinese; never copy
kana into the Chinese output and never invent speech missing from an incomplete fragment. For an
isolated particle or untranslatable truncated sound, use a minimal Chinese interjection; if no
speech is recoverable, use the explicit Simplified Chinese marker （语音不清）, never bare
romanization or punctuation alone. For isolated particles, translate よ as
啊 or 哦, ね as 呢, な as 啊, and か as 吗; for an isolated です fragment, use 是的 or 嗯. Never
return a lone full stop. Translate an isolated なさい continuation as 照做 or 给我照做. Always
translate 失礼します by context, such as 打扰了, 那我先告辞, or 那我先挂了; never copy it as
Japanese. If a context-inconsistent ASR fragment is unintelligible, preserve the
recoverable proposition and conservatively omit only the unintelligible,
nonessential modifier instead of copying Japanese script into Chinese; if it contains no
recoverable proposition at all, use （语音不清）. If all or part of a
person name is supplied in kana with no
grounded kanji spelling, use faithful Latin-letter romanization for that kana part instead of
copying kana or inventing Chinese characters; preserve any grounded kanji surname, translate
honorifics, and keep the complete cue natural Chinese (for example, a romanized name plus a
Chinese title rather than a bare Latin-only cue). Translate -ちゃん/-さん/-くん into a Chinese
honorific instead of retaining -chan/-san/-kun: for example, use Ei酱 for えいちゃん, Ami酱 for
あみちゃん, Akari小姐 for あかりさん, and Yuto君 for ゆうとくん. Established brand labels may retain their
official source-grounded Latin spelling. When いじめ is present and the glossary requires
校园霸凌, explicitly include 校园霸凌; mentioning only perpetrators or victims does not translate
the term. Apply every mapping supplied in EXACT_KANA_NAME_ROMANIZATIONS and
EXACT_CONTEXTUAL_ASR_CORRECTIONS exactly to its affected cue. For
EXACT_CUE_TRANSLATIONS, replace the entire affected cue with the supplied Chinese string; do not
append or preserve any Japanese source text from that cue."""

REVIEW_SYSTEM_PROMPT = """Compare each Japanese source cue with its draft Simplified Chinese subtitle and the supplied
neighboring context and glossary. Correct omissions, additions, mistranslation, euphemism,
pronoun or subject errors, name drift, terminology drift, unnatural literal phrasing, and
traditional characters. Japanese ASR can occasionally emit dialogue already in natural
Simplified Chinese; preserve it exactly when it is already correct. Render Japanese
interjections and incomplete Japanese fragments as minimal faithful natural Chinese; never copy
kana into the Chinese output and never invent missing speech. For an isolated particle or
untranslatable truncated sound, use a minimal Chinese interjection; if no speech is recoverable,
use the explicit Simplified Chinese marker （语音不清）, never bare romanization or punctuation
alone. For isolated particles, translate よ as 啊 or 哦, ね as 呢,
な as 啊, and か as 吗; for an isolated です fragment, use 是的 or 嗯. Never return a lone full
stop. Translate an isolated なさい continuation as 照做 or 给我照做. Always translate 失礼します
by context, such as 打扰了, 那我先告辞, or 那我先挂了; never copy it as Japanese. If a
context-inconsistent ASR fragment is unintelligible, preserve the recoverable proposition and
conservatively omit only the unintelligible, nonessential modifier instead of
copying Japanese script into Chinese; if it contains no recoverable proposition at all, use
（语音不清）. If all or part of a person name is supplied in kana with no grounded kanji
spelling, use faithful Latin-letter romanization for that kana part instead of copying kana or
inventing Chinese characters; preserve any grounded kanji surname, translate honorifics, and
keep the complete cue natural Chinese. Translate -ちゃん/-さん/-くん into a Chinese honorific
instead of retaining -chan/-san/-kun (for example Ei酱, Ami酱, Akari小姐, or Yuto君). Established brand labels may retain their official
source-grounded Latin spelling. Do not
alter cue IDs or cue count. When いじめ is present and the glossary requires 校园霸凌,
explicitly include 校园霸凌 rather than only mentioning perpetrators or victims. Return only the
exact JSON schema. Never include Unicode control, format, or replacement characters (including
zero-width spaces), and never include a blank subtitle line. Apply every mapping supplied in
EXACT_KANA_NAME_ROMANIZATIONS and
EXACT_CONTEXTUAL_ASR_CORRECTIONS exactly to its affected cue. For
EXACT_CUE_TRANSLATIONS, replace the entire affected cue with the supplied Chinese string; do not
append or preserve any Japanese source text from that cue."""

GLOSSARY_SYSTEM_PROMPT = """Build a source-grounded Japanese-to-Simplified-Chinese subtitle glossary.
Return only strict JSON with version, entries, relationships, and register. Every entry must have
category, ja, preferred_zh, aliases, evidence_ids, and explicit. Cite only supplied cue IDs, do
not invent terms, and mark explicit sexual/vulgar terminology with explicit=true. Return at most
20 high-value recurring names, relationships, and terms; omit generic verbs and interjections,
list each Japanese term only once with consolidated evidence IDs, and use direct non-euphemistic
Chinese equivalents that preserve the source register. If all or part of a person's name is
grounded only in kana, preferred_zh must retain a faithful Latin-letter romanization for that
kana part, preserve any grounded kanji surname, and must translate an honorific into Chinese
rather than retain -chan/-san/-kun (for example Ei酱 or Akari小姐); never guess or
invent a kanji or Chinese-character spelling for the kana part."""

EMPTY_GLOSSARY_SYSTEM_PROMPT = """Return an auditable empty glossary because two strict extraction
attempts found no reliable source-grounded recurring terms. Return only JSON with version set to
fallback-empty-v1, entries=[], relationships=[], and register=source-matched. Do not add terms."""


def _model_cue_id(cue_id: str) -> str:
    """Return a compact deterministic ID only for oversized model-boundary IDs."""
    if type(cue_id) is not str or not cue_id:
        raise ValueError("model cue IDs must be non-empty strings")
    if len(cue_id) <= 32:
        return cue_id
    try:
        _, suffix = cue_id.rsplit("-seg-", 1)
        segment, cue = suffix.split("-cue-", 1)
    except ValueError:
        pass
    else:
        if (
            len(segment) == 8
            and segment.isascii()
            and segment.isdigit()
            and len(cue) == 4
            and cue.isascii()
            and cue.isdigit()
        ):
            return f"s{segment}c{cue}"
    digest = hashlib.sha256(cue_id.encode("utf-8")).hexdigest()[:16]
    return f"c_{digest}"


def _model_cues(cues: Iterable[JapaneseCue]) -> tuple[JapaneseCue, ...]:
    ordered = tuple(cues)
    aliases = tuple(_model_cue_id(cue.cue_id) for cue in ordered)
    if len(aliases) != len(set(aliases)):
        raise ValueError("compact model cue IDs collide")
    return tuple(
        JapaneseCue(alias, cue.start_ms, cue.end_ms, cue.text)
        for cue, alias in zip(ordered, aliases, strict=True)
    )


def _restore_glossary_ids(
    glossary: Glossary,
    model_cues: tuple[JapaneseCue, ...],
    source_cues: tuple[JapaneseCue, ...],
) -> Glossary:
    if len(model_cues) != len(source_cues):
        raise ValueError("model/source glossary cue counts differ")
    restored = {
        model.cue_id: source.cue_id
        for model, source in zip(model_cues, source_cues, strict=True)
    }
    entries = tuple(
        GlossaryEntry(
            entry.category,
            entry.ja,
            entry.preferred_zh,
            entry.aliases,
            tuple(restored[cue_id] for cue_id in entry.evidence_ids),
            entry.explicit,
        )
        for entry in glossary.entries
    )
    result = Glossary(
        glossary.version,
        entries,
        glossary.relationships,
        glossary.register,
        source_cue_digest(source_cues),
    )
    validate_glossary_against_source(result, source_cues)
    return result


def _parse_model_glossary_response(
    raw: str,
    source_cues: tuple[JapaneseCue, ...],
) -> Glossary:
    model_cues = _model_cues(source_cues)
    if model_cues == source_cues:
        return parse_glossary_response(raw, source_cues)
    try:
        return parse_glossary_response(raw, source_cues)
    except ValueError as original_error:
        try:
            model_glossary = parse_glossary_response(raw, model_cues)
            return _restore_glossary_ids(model_glossary, model_cues, source_cues)
        except (KeyError, ValueError) as alias_error:
            raise alias_error from original_error


def _parse_model_translation_response(
    raw: str,
    original_ids: tuple[str, ...],
) -> dict[str, str]:
    model_ids = tuple(_model_cue_id(cue_id) for cue_id in original_ids)
    if len(model_ids) != len(set(model_ids)):
        raise ValueError("compact model cue IDs collide")
    if model_ids == original_ids:
        return parse_translation_response(raw, list(original_ids))
    try:
        return parse_translation_response(raw, list(original_ids))
    except ValueError as original_error:
        try:
            parsed = parse_translation_response(raw, list(model_ids))
        except ValueError as alias_error:
            raise alias_error from original_error
        return {
            original_id: parsed[model_id]
            for original_id, model_id in zip(original_ids, model_ids, strict=True)
        }


def _model_glossary_payload(glossary: Glossary) -> dict[str, object]:
    payload = glossary.to_dict()
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise ValueError("glossary payload entries must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("glossary payload entry must be an object")
        evidence = entry.get("evidence_ids")
        if not isinstance(evidence, list):
            raise ValueError("glossary evidence IDs must be a list")
        entry["evidence_ids"] = [_model_cue_id(cue_id) for cue_id in evidence]
    return payload


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    revision: str
    pass_name: str
    temperature: float
    top_p: float
    seed: int
    n: int
    max_tokens: int
    enable_thinking: bool

    def __post_init__(self) -> None:
        if (
            type(self.model_id) is not str
            or type(self.revision) is not str
            or not self.model_id.strip()
            or not self.revision.strip()
        ):
            raise ValueError("model ID and revision must be pinned non-empty strings")
        if self.pass_name not in {"primary", "review", "escalation", "glossary"}:
            raise ValueError(f"invalid translation pass name: {self.pass_name}")
        if type(self.seed) is not int or type(self.n) is not int or self.n != 1:
            raise ValueError("translation requests require an integer seed and n=1")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if not 0 <= self.temperature <= 2 or not 0 < self.top_p <= 1:
            raise ValueError("invalid deterministic sampling settings")
        expected_temperature = 0.2 if self.pass_name in {"primary", "glossary"} else 0.1
        if self.temperature != expected_temperature or self.top_p != 0.8:
            raise ValueError("translation pass uses non-binding sampling settings")
        if self.seed != DEFAULT_SEED:
            raise ValueError("translation requests require the fixed seed")
        if self.enable_thinking is not False:
            raise ValueError("translation thinking must be disabled")

    @classmethod
    def primary(cls, *, revision: str, max_tokens: int) -> ModelConfig:
        return cls(
            PRIMARY_MODEL_ID,
            revision,
            "primary",
            0.2,
            0.8,
            DEFAULT_SEED,
            1,
            max_tokens,
            False,
        )

    @classmethod
    def review(
        cls,
        *,
        model_id: str,
        revision: str,
        max_tokens: int,
    ) -> ModelConfig:
        return cls(
            model_id,
            revision,
            "review",
            0.1,
            0.8,
            DEFAULT_SEED,
            1,
            max_tokens,
            False,
        )

    @classmethod
    def escalation(cls, *, revision: str, max_tokens: int) -> ModelConfig:
        return cls(
            ESCALATION_MODEL_ID,
            revision,
            "escalation",
            0.1,
            0.8,
            DEFAULT_SEED,
            1,
            max_tokens,
            False,
        )


@dataclass(frozen=True)
class TranslationAttempt:
    attempt: int
    code: str
    message: str
    raw: str | None

    def __post_init__(self) -> None:
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("translation attempt number must be positive")
        if self.code not in {
            "client_exception",
            "invalid_json",
            "schema_mismatch",
            "refusal",
            "quality_gate",
            "digest_mismatch",
            "ok",
        }:
            raise ValueError(f"unknown translation attempt code: {self.code}")
        if type(self.message) is not str:
            raise ValueError("translation attempt message must be a string")
        if self.raw is not None and type(self.raw) is not str:
            raise ValueError(
                "translation attempt raw evidence must be a string or None"
            )
        if self.code != "client_exception" and type(self.raw) is not str:
            raise ValueError("translation response attempts require raw evidence")


def _require_sha256(value: object, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be a canonical SHA-256")


def _validate_text_rows(
    rows: object,
    *,
    allow_empty: bool,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(rows, tuple):
        raise ValueError("translation artifact texts must be a tuple")
    if not allow_empty and not rows:
        raise ValueError("successful translation artifacts require texts")
    for row in rows:
        if (
            not isinstance(row, tuple)
            or len(row) != 2
            or any(type(value) is not str or not value.strip() for value in row)
        ):
            raise ValueError("translation artifact rows require non-empty ID/text")
    ids = [cue_id for cue_id, _ in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("translation artifact cue IDs must be unique")
    return rows


@dataclass(frozen=True)
class TranslationArtifact:
    texts: tuple[tuple[str, str], ...]
    timestamp_digest: str
    source_digest: str
    glossary_digest: str
    input_text_digest: str | None
    output_text_digest: str | None
    model_id: str
    revision: str
    pass_name: str
    status: str
    attempts: tuple[TranslationAttempt, ...]

    def __post_init__(self) -> None:
        expected_status = {
            "primary": "draft_ok",
            "review": "review_ok",
            "escalation": "escalation_ok",
        }
        if self.pass_name not in expected_status:
            raise ValueError("translation artifact has an invalid pass")
        if type(self.model_id) is not str or type(self.revision) is not str:
            raise ValueError("translation artifact model provenance must be strings")
        if not self.model_id.strip() or not self.revision.strip():
            raise ValueError("translation artifact model provenance must be pinned")
        for value, field in (
            (self.timestamp_digest, "translation timestamp digest"),
            (self.source_digest, "translation source digest"),
            (self.glossary_digest, "translation glossary digest"),
        ):
            _require_sha256(value, field)
        if (
            not isinstance(self.attempts, tuple)
            or not 1 <= len(self.attempts) <= MAX_BLOCK_ATTEMPTS
        ):
            raise ValueError(
                f"translation artifacts require one to {MAX_BLOCK_ATTEMPTS} attempts"
            )
        if any(
            not isinstance(attempt, TranslationAttempt) for attempt in self.attempts
        ):
            raise ValueError("translation artifact attempts have invalid values")
        if tuple(attempt.attempt for attempt in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("translation artifact attempts must be sequential")

        if self.pass_name == "primary":
            if self.input_text_digest is not None:
                raise ValueError("primary artifact cannot have an input text digest")
        else:
            _require_sha256(
                self.input_text_digest,
                "review input text digest",
            )

        if self.status == "blocked":
            rows = _validate_text_rows(self.texts, allow_empty=True)
            if rows or self.output_text_digest is not None:
                raise ValueError("blocked translation artifacts cannot contain output")
            if len(self.attempts) != MAX_BLOCK_ATTEMPTS or any(
                attempt.code == "ok" for attempt in self.attempts
            ):
                raise ValueError(
                    f"blocked translation artifact lacks {MAX_BLOCK_ATTEMPTS} failures"
                )
            return

        if self.status != expected_status[self.pass_name]:
            raise ValueError("translation artifact status does not match its pass")
        rows = _validate_text_rows(self.texts, allow_empty=False)
        _require_sha256(self.output_text_digest, "translation output text digest")
        if _text_digest(rows) != self.output_text_digest:
            raise ValueError("translation artifact output digest differs")
        if self.attempts[-1].code != "ok" or any(
            attempt.code == "ok" for attempt in self.attempts[:-1]
        ):
            raise ValueError("successful translation artifact has invalid attempts")
        raw = self.attempts[-1].raw
        if type(raw) is not str or not raw:
            raise ValueError("successful translation artifact lacks raw evidence")
        parsed = _parse_model_translation_response(
            raw, tuple(cue_id for cue_id, _ in rows)
        )
        if tuple(parsed.items()) != rows:
            raise ValueError("successful raw response differs from artifact texts")


@dataclass(frozen=True)
class EscalationSelection:
    source_digest: str
    glossary_digest: str
    baseline_text_digest: str
    policy_version: str
    flagged_ids: tuple[str, ...]
    ambiguous_ids: tuple[str, ...]
    explicit_ids: tuple[str, ...]
    sampled_ids: tuple[str, ...]
    sampled_blocks: tuple[tuple[str, ...], ...]
    required_blocks: tuple[tuple[str, ...], ...]
    required_ids: tuple[str, ...]
    sample_denominator: int
    block_size: int

    def __post_init__(self) -> None:
        _require_sha256(self.source_digest, "escalation source digest")
        _require_sha256(self.glossary_digest, "escalation glossary digest")
        _require_sha256(self.baseline_text_digest, "escalation baseline text digest")
        if (
            type(self.policy_version) is not str
            or self.policy_version != ESCALATION_POLICY_VERSION
        ):
            raise ValueError("escalation selection policy version is not approved")
        if (
            type(self.sample_denominator) is not int
            or self.sample_denominator != ESCALATION_SAMPLE_DENOMINATOR
        ):
            raise ValueError("escalation sample policy cannot be weakened")
        if type(self.block_size) is not int or self.block_size != ESCALATION_BLOCK_SIZE:
            raise ValueError("escalation block policy cannot be changed")
        for name, values in (
            ("flagged_ids", self.flagged_ids),
            ("ambiguous_ids", self.ambiguous_ids),
            ("explicit_ids", self.explicit_ids),
            ("sampled_ids", self.sampled_ids),
            ("required_ids", self.required_ids),
        ):
            if not isinstance(values, tuple) or any(
                type(value) is not str or not value for value in values
            ):
                raise ValueError(f"{name} must contain non-empty cue IDs")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique cue IDs")
        for name, blocks in (
            ("sampled_blocks", self.sampled_blocks),
            ("required_blocks", self.required_blocks),
        ):
            if not isinstance(blocks, tuple) or any(
                not isinstance(block, tuple)
                or not block
                or any(type(cue_id) is not str or not cue_id for cue_id in block)
                or len(block) != len(set(block))
                for block in blocks
            ):
                raise ValueError(f"{name} must contain non-empty unique-ID blocks")
        if not self.sampled_blocks or not self.required_blocks:
            raise ValueError(
                "escalation selection requires sampled and required blocks"
            )
        if len(self.required_blocks) != len(set(self.required_blocks)):
            raise ValueError("required escalation blocks must be unique")
        if any(block not in self.required_blocks for block in self.sampled_blocks):
            raise ValueError("sampled escalation blocks must be required")
        if len(self.sampled_ids) != len(self.sampled_blocks) or any(
            cue_id not in block
            for cue_id, block in zip(self.sampled_ids, self.sampled_blocks)
        ):
            raise ValueError("sampled escalation IDs must identify sampled blocks")
        flattened = tuple(cue_id for block in self.required_blocks for cue_id in block)
        if len(flattened) != len(set(flattened)) or flattened != self.required_ids:
            raise ValueError("required escalation IDs must exactly flatten its blocks")
        required = set(self.required_ids)
        if any(
            cue_id not in required
            for cue_id in self.flagged_ids + self.ambiguous_ids + self.explicit_ids
        ):
            raise ValueError("risk escalation IDs must belong to required blocks")


@dataclass(frozen=True)
class ReviewedTranslationArtifact:
    texts: tuple[tuple[str, str], ...]
    timestamp_digest: str
    source_digest: str
    glossary_digest: str
    selection: EscalationSelection
    primary_models: tuple[tuple[str, str], ...]
    baseline_models: tuple[tuple[str, str], ...]
    escalation_models: tuple[tuple[str, str], ...]
    status: str
    _verification_token: InitVar[object] = None

    def __post_init__(self, _verification_token: object) -> None:
        if _verification_token is not _FINALIZER_TOKEN:
            raise ValueError("reviewed translation artifacts are finalizer-only values")
        if self.status != "ok":
            raise ValueError("reviewed translation artifact status must be ok")
        rows = _validate_text_rows(self.texts, allow_empty=False)
        for value, field in (
            (self.timestamp_digest, "reviewed timestamp digest"),
            (self.source_digest, "reviewed source digest"),
            (self.glossary_digest, "reviewed glossary digest"),
        ):
            _require_sha256(value, field)
        if not isinstance(self.selection, EscalationSelection):
            raise ValueError("reviewed translation requires escalation selection")
        if (
            self.selection.source_digest != self.source_digest
            or self.selection.glossary_digest != self.glossary_digest
        ):
            raise ValueError("reviewed translation selection provenance differs")
        ids = {cue_id for cue_id, _ in rows}
        if any(cue_id not in ids for cue_id in self.selection.required_ids):
            raise ValueError("reviewed translation lacks required escalation IDs")
        if (
            not isinstance(self.primary_models, tuple)
            or not self.primary_models
            or any(
                not isinstance(provenance, tuple)
                or len(provenance) != 2
                or type(provenance[0]) is not str
                or not provenance[0].strip()
                or type(provenance[1]) is not str
                or not provenance[1].strip()
                for provenance in self.primary_models
            )
            or len(self.primary_models) != len(set(self.primary_models))
        ):
            raise ValueError("reviewed translation has invalid primary models")
        if (
            not isinstance(self.baseline_models, tuple)
            or not self.baseline_models
            or any(
                not isinstance(provenance, tuple)
                or len(provenance) != 2
                or type(provenance[0]) is not str
                or not provenance[0].strip()
                or type(provenance[1]) is not str
                or not provenance[1].strip()
                for provenance in self.baseline_models
            )
            or len(self.baseline_models) != len(set(self.baseline_models))
        ):
            raise ValueError("reviewed translation has invalid baseline models")
        if (
            not isinstance(self.escalation_models, tuple)
            or not self.escalation_models
            or any(
                not isinstance(provenance, tuple)
                or len(provenance) != 2
                or type(provenance[0]) is not str
                or not provenance[0].strip()
                or type(provenance[1]) is not str
                or not provenance[1].strip()
                for provenance in self.escalation_models
            )
            or len(self.escalation_models) != len(set(self.escalation_models))
        ):
            raise ValueError("reviewed translation has invalid escalation models")

    @property
    def required_escalation_ids(self) -> tuple[str, ...]:
        return self.selection.required_ids


@dataclass(frozen=True)
class GlossaryArtifact:
    glossary: Glossary | None
    source_digest: str
    content_digest: str | None
    model_id: str
    revision: str
    status: str
    attempts: tuple[TranslationAttempt, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.source_digest, "glossary artifact source digest")
        if (
            type(self.model_id) is not str
            or type(self.revision) is not str
            or not self.model_id.strip()
            or not self.revision.strip()
        ):
            raise ValueError("glossary artifact model provenance is invalid")
        if not isinstance(self.attempts, tuple) or not 1 <= len(self.attempts) <= 3:
            raise ValueError("glossary artifacts require one to three attempts")
        if any(
            not isinstance(attempt, TranslationAttempt) for attempt in self.attempts
        ) or tuple(attempt.attempt for attempt in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("glossary artifact attempts must be sequential")
        if self.status == "ok":
            if not isinstance(self.glossary, Glossary):
                raise ValueError("successful glossary artifact lacks a glossary")
            _require_sha256(self.content_digest, "glossary artifact content digest")
            if (
                self.glossary.source_digest != self.source_digest
                or glossary_digest(self.glossary) != self.content_digest
            ):
                raise ValueError("glossary artifact provenance differs")
            if self.attempts[-1].code != "ok" or any(
                attempt.code == "ok" for attempt in self.attempts[:-1]
            ):
                raise ValueError("successful glossary artifact has invalid attempts")
        elif self.status == "blocked":
            if (
                self.glossary is not None
                or self.content_digest is not None
                or len(self.attempts) != 3
                or any(attempt.code == "ok" for attempt in self.attempts)
            ):
                raise ValueError("blocked glossary artifact lacks three failures")
        else:
            raise ValueError("glossary artifact status is invalid")


def _validated_glossary_artifact(
    artifact: GlossaryArtifact,
    source_cues: tuple[JapaneseCue, ...],
) -> Glossary:
    if not isinstance(artifact, GlossaryArtifact):
        raise ValueError("translation requires a successful GlossaryArtifact")
    artifact.__post_init__()
    if artifact.status != "ok" or artifact.glossary is None:
        raise ValueError("translation requires a successful GlossaryArtifact")
    if source_cue_digest(source_cues) != artifact.source_digest:
        raise ValueError("glossary artifact source digest differs")
    raw = artifact.attempts[-1].raw
    if type(raw) is not str or not raw:
        raise ValueError("glossary artifact lacks successful raw evidence")
    parsed = _parse_model_glossary_response(raw, source_cues)
    if parsed != artifact.glossary:
        raise ValueError("glossary raw response differs from its artifact")
    validate_glossary_against_source(artifact.glossary, source_cues)
    if glossary_digest(artifact.glossary) != artifact.content_digest:
        raise ValueError("glossary artifact content digest differs")
    return artifact.glossary


def _cue_rows(cues: Iterable[JapaneseCue]) -> list[dict[str, str]]:
    return [{"id": cue.cue_id, "ja": cue.text} for cue in cues]


def _text_digest(rows: Iterable[tuple[str, str]]) -> str:
    payload = [[cue_id, text] for cue_id, text in rows]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _translation_response_format(expected_ids: tuple[str, ...]) -> dict[str, object]:
    item_schemas = [
        {
            "type": "object",
            "properties": {
                "id": {"type": "string", "const": cue_id},
                "zh": {"type": "string", "minLength": 1},
            },
            "required": ["id", "zh"],
            "additionalProperties": False,
        }
        for cue_id in expected_ids
    ]
    schema = {
        "type": "object",
        "properties": {
            "cues": {
                "type": "array",
                "prefixItems": item_schemas,
                "minItems": len(item_schemas),
                "maxItems": len(item_schemas),
                "items": False,
            }
        },
        "required": ["cues"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "cue_translations",
            "strict": True,
            "schema": schema,
        },
    }


def _glossary_response_format() -> dict[str, object]:
    entry = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "minLength": 1, "maxLength": 64},
            "ja": {"type": "string", "minLength": 1, "maxLength": 128},
            "preferred_zh": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "aliases": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string", "maxLength": 128},
            },
            "evidence_ids": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {"type": "string", "minLength": 1, "maxLength": 32},
            },
            "explicit": {"type": "boolean"},
        },
        "required": [
            "category",
            "ja",
            "preferred_zh",
            "aliases",
            "evidence_ids",
            "explicit",
        ],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "version": {"type": "string", "minLength": 1, "maxLength": 64},
            "entries": {"type": "array", "maxItems": 20, "items": entry},
            "relationships": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 256},
            },
            "register": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["version", "entries", "relationships", "register"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {"name": "video_glossary", "strict": True, "schema": schema},
    }


def _request_payload(
    block: TranslationBlock,
    glossary: Glossary,
    config: ModelConfig,
    draft: dict[str, str] | None,
) -> dict[str, object]:
    before = _model_cues(block.before_context)
    commit = _model_cues(block.commit_cues)
    after = _model_cues(block.after_context)
    payload: dict[str, object] = {
        "schema_version": 1,
        "model_revision": config.revision,
        "source_digest": block.source_digest,
        "glossary_source_digest": glossary.source_digest,
        "glossary_digest": glossary_digest(glossary),
        "READ_ONLY_CONTEXT": {
            "before": _cue_rows(before),
            "after": _cue_rows(after),
        },
        "COMMIT_CUES": _cue_rows(commit),
        "glossary": _model_glossary_payload(glossary),
    }
    kana_name_requirements = {
        source.cue_id: {
            name: required_latin
            for name, required_latin in DIRECT_KANA_NAME_REQUIRED_LATIN.items()
            if name in source.text
        }
        for source in block.commit_cues
        if any(name in source.text for name in DIRECT_KANA_NAME_REQUIRED_LATIN)
    }
    if kana_name_requirements:
        payload["EXACT_KANA_NAME_ROMANIZATIONS"] = kana_name_requirements
    contextual_requirements = {
        source.cue_id: {
            artifact: required
            for artifact, required in CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS.items()
            if artifact in source.text
        }
        for source in block.commit_cues
        if any(
            artifact in source.text for artifact in CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS
        )
    }
    if contextual_requirements:
        payload["EXACT_CONTEXTUAL_ASR_CORRECTIONS"] = contextual_requirements
    exact_cue_requirements = {
        source.cue_id: required
        for source in block.commit_cues
        if (
            required := exact_required_translation(
                source.text,
                cue_id=source.cue_id,
            )
        )
        is not None
    }
    if exact_cue_requirements:
        payload["EXACT_CUE_TRANSLATIONS"] = exact_cue_requirements
    if draft is not None:
        payload["DRAFT_CUES"] = [
            {"id": _model_cue_id(cue_id), "zh": draft[cue_id]}
            for cue_id in block.commit_ids
        ]
    return payload


def _request_kwargs(
    system_prompt: str,
    payload: dict[str, object],
    config: ModelConfig,
    response_format: dict[str, object],
) -> dict[str, object]:
    return {
        "model": config.model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": config.temperature,
        "top_p": config.top_p,
        "seed": config.seed,
        "n": config.n,
        "max_tokens": config.max_tokens,
        "response_format": response_format,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }


def _response_text(response: object) -> str:
    try:
        choices = response.choices
        content = choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "local translation response is missing message content"
        ) from exc
    if type(content) is not str:
        raise RuntimeError("local translation response content must be a string")
    return content


def _response_finish_reason(response: object) -> str | None:
    try:
        reason = response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    return reason if type(reason) is str and reason else None


def _schema_code(exc: ValueError) -> str:
    message = str(exc)
    if (
        "invalid JSON" in message
        or "duplicate JSON key" in message
        or "non-finite JSON" in message
    ):
        return "invalid_json"
    return "schema_mismatch"


def _subtitle_text_flags(text: str) -> list[str]:
    """Return SRT-safety failures that model output must repair before commit."""
    if type(text) is not str:
        raise ValueError("subtitle text must be a string")
    flags: list[str] = []
    if any(
        char == "\ufffd"
        or (char != "\n" and unicodedata.category(char).startswith("C"))
        for char in text
    ):
        flags.append("forbidden_character")
    if any(not line.strip() for line in text.split("\n")):
        flags.append("empty_subtitle_line")
    return flags


def _translation_output_flags(
    source_ja: str,
    text: str,
    glossary: Glossary,
    *,
    simplifier: Callable[[str], str] | None,
    cue_id: str,
) -> list[str]:
    flags = _subtitle_text_flags(text)
    flags.extend(
        flag
        for flag in translation_flags(
            source_ja,
            text,
            glossary,
            simplifier=simplifier,
            cue_id=cue_id,
        )
        if flag not in flags
    )
    return flags


def _run_block(
    client: Any,
    block: TranslationBlock,
    source_cues: tuple[JapaneseCue, ...],
    glossary_artifact: GlossaryArtifact,
    config: ModelConfig,
    *,
    draft: dict[str, str] | None,
    expected_digest: str,
    expected_source_digest: str,
    expected_glossary_digest: str,
    simplifier: Callable[[str], str] | None,
) -> TranslationArtifact:
    if not isinstance(block, TranslationBlock) or not block.commit_cues:
        raise ValueError("translation requires a non-empty TranslationBlock")
    glossary = _validated_glossary_artifact(glossary_artifact, source_cues)
    validate_translation_block(block, source_cues)
    if block.source_digest != expected_source_digest:
        raise ValueError("translation block source digest differs from expected")
    if glossary.source_digest != expected_source_digest:
        raise ValueError("translation glossary is not tied to the source cue set")
    actual_digest = timestamp_digest(block.commit_cues)
    if actual_digest != expected_digest:
        raise ValueError(
            f"timestamp digest differs before {config.pass_name}: "
            f"expected {expected_digest}, got {actual_digest}"
        )
    actual_glossary_digest = glossary_digest(glossary)
    if expected_glossary_digest not in {
        actual_glossary_digest,
        expected_source_digest,
    }:
        raise ValueError("translation glossary content digest differs from expected")
    if draft is not None:
        if not isinstance(draft, dict) or list(draft) != list(block.commit_ids):
            raise ValueError("draft cue IDs or order differ from commit cues")
        if any(type(value) is not str or not value.strip() for value in draft.values()):
            raise ValueError("draft values must be non-empty strings")

    system_prompt = REVIEW_SYSTEM_PROMPT if draft is not None else PRIMARY_SYSTEM_PROMPT
    payload = _request_payload(block, glossary, config, draft)
    model_commit_ids = tuple(_model_cue_id(cue_id) for cue_id in block.commit_ids)
    if len(model_commit_ids) != len(set(model_commit_ids)):
        raise ValueError("compact model cue IDs collide")
    attempts: list[TranslationAttempt] = []
    validation_feedback: str | None = None
    input_text_digest = (
        None
        if draft is None
        else _text_digest((cue_id, draft[cue_id]) for cue_id in block.commit_ids)
    )
    exact_translations = {
        source.cue_id: required
        for source in block.commit_cues
        if (
            required := exact_required_translation(
                source.text,
                cue_id=source.cue_id,
            )
        )
        is not None
    }
    if len(exact_translations) == len(block.commit_cues):
        quality = {
            source.cue_id: flags
            for source in block.commit_cues
            if (
                flags := _translation_output_flags(
                    source.text,
                    exact_translations[source.cue_id],
                    glossary,
                    simplifier=simplifier,
                    cue_id=source.cue_id,
                )
            )
        }
        if quality:
            raise ValueError(
                "deterministic exact translations failed quality gates: "
                + json.dumps(quality, ensure_ascii=False, sort_keys=True)
            )
        translated = apply_translations(
            block.commit_cues,
            exact_translations,
            expected_digest,
        )
        output_rows = tuple((cue.cue_id, cue.text) for cue in translated)
        raw = json.dumps(
            {
                "cues": [
                    {
                        "id": _model_cue_id(source.cue_id),
                        "zh": exact_translations[source.cue_id],
                    }
                    for source in block.commit_cues
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return TranslationArtifact(
            texts=output_rows,
            timestamp_digest=expected_digest,
            source_digest=expected_source_digest,
            glossary_digest=expected_glossary_digest,
            input_text_digest=input_text_digest,
            output_text_digest=_text_digest(output_rows),
            model_id=config.model_id,
            revision=config.revision,
            pass_name=config.pass_name,
            status={
                "primary": "draft_ok",
                "review": "review_ok",
                "escalation": "escalation_ok",
            }[config.pass_name],
            attempts=(
                TranslationAttempt(
                    1,
                    "ok",
                    json.dumps(
                        {"deterministic_exact_short_circuit": exact_translations},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    raw,
                ),
            ),
        )

    for attempt_number in range(1, MAX_BLOCK_ATTEMPTS + 1):
        request_payload = dict(payload)
        if validation_feedback is not None:
            request_payload["VALIDATION_FEEDBACK"] = validation_feedback
            request_payload["VALIDATION_ATTEMPT"] = attempt_number
        kwargs = _request_kwargs(
            system_prompt,
            request_payload,
            config,
            _translation_response_format(model_commit_ids),
        )
        raw: str | None = None
        response: object | None = None
        try:
            response = client.chat.completions.create(**kwargs)
            raw = _response_text(response)
        except Exception as exc:
            if raw is None and response is not None:
                try:
                    raw = repr(response)
                except Exception:
                    raw = f"<{type(response).__name__} response>"
            attempts.append(
                TranslationAttempt(
                    attempt_number,
                    "client_exception",
                    str(exc),
                    raw,
                )
            )
            continue

        try:
            translations = _parse_model_translation_response(raw, block.commit_ids)
        except ValueError as exc:
            finish_reason = _response_finish_reason(response)
            diagnostic = f"{exc}; raw_length={len(raw)}"
            if finish_reason is not None:
                diagnostic += f"; finish_reason={finish_reason}"
            length_feedback = (
                " The response was cut off because generation reached its length limit. "
                "Keep every zh value concise, never repeat a word more times than its "
                "source cue, and never continue an ASR repetition beyond the supplied text."
                if finish_reason == "length"
                else ""
            )
            validation_feedback = (
                f"Previous response failed strict JSON validation: {diagnostic[:500]}. "
                "Regenerate the complete JSON for every COMMIT_CUE in exact order."
                f"{length_feedback}"
            )
            attempts.append(
                TranslationAttempt(
                    attempt_number,
                    _schema_code(exc),
                    diagnostic,
                    raw,
                )
            )
            continue

        exact_overrides: dict[str, str] = {}
        for source in block.commit_cues:
            required = exact_required_translation(
                source.text,
                cue_id=source.cue_id,
            )
            if required is not None and translations[source.cue_id] != required:
                translations[source.cue_id] = required
                exact_overrides[source.cue_id] = required
        audit_evidence: dict[str, object] = {}
        if exact_overrides:
            original_raw = raw
            raw = json.dumps(
                {
                    "cues": [
                        {
                            "id": _model_cue_id(source.cue_id),
                            "zh": translations[source.cue_id],
                        }
                        for source in block.commit_cues
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            audit_evidence = {
                "deterministic_exact_overrides": exact_overrides,
                "original_raw": original_raw,
            }

        quality: dict[str, list[str]] = {}
        for source in block.commit_cues:
            flags = _translation_output_flags(
                source.text,
                translations[source.cue_id],
                glossary,
                simplifier=simplifier,
                cue_id=source.cue_id,
            )
            if flags:
                quality[source.cue_id] = flags
        inaudible_sources = tuple(
            source
            for source in block.commit_cues
            if translations[source.cue_id] == INAUDIBLE_TRANSLATION
        )
        if config.pass_name in {"review", "escalation"} and inaudible_sources:
            if attempt_number < MAX_BLOCK_ATTEMPTS:
                for source in inaudible_sources:
                    quality.setdefault(source.cue_id, []).append(
                        "inaudible_marker_unconfirmed"
                    )
            else:
                audit_evidence["confirmed_inaudible_markers"] = {
                    source.cue_id: {
                        "source_ja": source.text,
                        "draft_zh": draft[source.cue_id] if draft is not None else None,
                    }
                    for source in inaudible_sources
                }
                audit_evidence["confirmed_inaudible_policy"] = (
                    CONFIRMED_INAUDIBLE_POLICY
                )
        if quality and draft is not None and attempt_number == MAX_BLOCK_ATTEMPTS:
            validated_draft_fallbacks: dict[str, dict[str, object]] = {}
            for source in block.commit_cues:
                flags = quality.get(source.cue_id)
                if flags is None:
                    continue
                draft_flags = _translation_output_flags(
                    source.text,
                    draft[source.cue_id],
                    glossary,
                    simplifier=simplifier,
                    cue_id=source.cue_id,
                )
                if draft_flags:
                    break
                validated_draft_fallbacks[source.cue_id] = {
                    "draft_zh": draft[source.cue_id],
                    "rejected_zh": translations[source.cue_id],
                    "review_flags": flags,
                }
            if len(validated_draft_fallbacks) == len(quality):
                if "original_raw" not in audit_evidence:
                    audit_evidence["original_raw"] = raw
                audit_evidence["deterministic_validated_draft_fallbacks"] = (
                    validated_draft_fallbacks
                )
                audit_evidence["validated_draft_fallback_policy"] = (
                    VALIDATED_DRAFT_FALLBACK_POLICY
                )
                for cue_id in validated_draft_fallbacks:
                    translations[cue_id] = draft[cue_id]
                raw = json.dumps(
                    {
                        "cues": [
                            {
                                "id": _model_cue_id(source.cue_id),
                                "zh": translations[source.cue_id],
                            }
                            for source in block.commit_cues
                        ]
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                quality = {}
        confirmed_markers = audit_evidence.get("confirmed_inaudible_markers")
        if isinstance(confirmed_markers, dict):
            remaining_confirmed_markers = {
                cue_id: evidence
                for cue_id, evidence in confirmed_markers.items()
                if translations.get(cue_id) == INAUDIBLE_TRANSLATION
            }
            if remaining_confirmed_markers:
                audit_evidence["confirmed_inaudible_markers"] = (
                    remaining_confirmed_markers
                )
            else:
                audit_evidence.pop("confirmed_inaudible_markers", None)
                audit_evidence.pop("confirmed_inaudible_policy", None)
        if quality:
            code = (
                "refusal"
                if any("refusal" in flags for flags in quality.values())
                else "quality_gate"
            )
            failure_json = json.dumps(quality, ensure_ascii=False, sort_keys=True)
            rejected_json = json.dumps(
                {cue_id: translations[cue_id] for cue_id in quality},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            glossary_requirements = {
                source.cue_id: [
                    f"{entry.ja}=>{entry.preferred_zh}"
                    for entry in glossary.entries
                    if any(term in source.text for term in (entry.ja, *entry.aliases))
                ]
                for source in block.commit_cues
                if source.cue_id in quality
                and any(
                    flag in {"glossary_term_missing", "explicit_term_missing"}
                    for flag in quality[source.cue_id]
                )
            }
            glossary_feedback = (
                " Exact glossary mappings for affected cues: "
                + json.dumps(
                    glossary_requirements,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )[:1000]
                + "."
                if glossary_requirements
                else ""
            )
            kana_name_requirements = {
                source.cue_id: [
                    f"{name}=>{required_latin}+Chinese honorific"
                    for name, required_latin in DIRECT_KANA_NAME_REQUIRED_LATIN.items()
                    if name in source.text
                ]
                for source in block.commit_cues
                if source.cue_id in quality
                and "kana_name_romanization_missing" in quality[source.cue_id]
            }
            kana_name_feedback = (
                " Exact kana-name Latin stems for affected cues: "
                + json.dumps(
                    kana_name_requirements,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )[:1000]
                + "."
                if kana_name_requirements
                else ""
            )
            contextual_requirements = {
                source.cue_id: {
                    artifact: required
                    for artifact, required in CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS.items()
                    if artifact in source.text
                }
                for source in block.commit_cues
                if source.cue_id in quality
                and any(
                    flag in quality[source.cue_id]
                    for flag in (
                        "contextual_asr_correction_missing",
                        "contextual_asr_name_invented",
                    )
                )
            }
            contextual_feedback = (
                " Exact context-inconsistent ASR corrections for affected cues: "
                + json.dumps(
                    contextual_requirements,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )[:1000]
                + "."
                if contextual_requirements
                else ""
            )
            validation_feedback = (
                "Previous response failed deterministic subtitle quality gates: "
                f"{failure_json[:1000]}. Regenerate the complete JSON for every "
                "COMMIT_CUE. Fix only the identified problems; for "
                "forbidden_character, remove every Unicode control, format, and replacement "
                "character (including zero-width spaces) without deleting spoken words; for "
                "empty_subtitle_line, return cue text without blank lines. For "
                "japanese_remaining or unchanged_source on a cue containing kana, "
                "replace every kana substring, render a minimal faithful Chinese "
                "translation, use a Chinese interjection for an isolated particle or "
                "truncated sound, use （语音不清） only when no speech is recoverable, "
                "and romanize kana-only names with Latin "
                "letters while translating honorifics so the full cue remains Chinese; "
                "if a context-inconsistent ASR fragment is unintelligible, preserve the "
                "recoverable proposition and omit only the unintelligible nonessential "
                "modifier rather than copying Japanese script. "
                "For inaudible_marker_unconfirmed, the previous （语音不清） marker is "
                "not accepted yet: re-evaluate the Japanese and neighboring context, "
                "translate every recoverable word, proposition, negation, object, name, "
                "or English phrase, and do not use the marker merely because a cue is "
                "short, incomplete, colloquial, or ungrammatical. "
                "For not_simplified_chinese, never return bare romanization or "
                "punctuation-only ellipsis; return a minimal faithful Chinese cue or "
                "（语音不清） when no speech is recoverable, except for "
                "an established source-grounded brand's official Latin label; a lone "
                "full stop is never acceptable; an isolated よ should become 啊 or 哦, "
                "ね should become 呢, な should become 啊, か should become 吗, and "
                "です should become 是的 or 嗯 rather than punctuation alone; an "
                "isolated なさい continuation should become 照做 or 给我照做; "
                "失礼します should become 打扰了, 那我先告辞, or 那我先挂了 according "
                "to context and must never be copied. For "
                "unchanged_source without kana, preserve it only when it is already "
                "natural context-consistent Simplified Chinese; if it is garbled or "
                "context-inconsistent ASR text, use neighboring Japanese context for "
                "a conservative Chinese correction instead of repeating the garble; "
                "for kana_name_romanization_missing, replace any guessed Chinese-character "
                "name spelling with a faithful Latin-letter romanization plus a Chinese "
                "honorific, never bare -chan/-san/-kun (for example Ei酱, Ami酱, "
                "Akari小姐, or Yuto君); when not_simplified_chinese rejects a bare "
                "romanized name, add the translated Chinese honorific; for "
                "contextual_asr_correction_missing or contextual_asr_name_invented, apply "
                "the exact source-artifact correction listed below; when the artifact is "
                "いかゆさん, never render it as a person or title; "
                f"rejected outputs were {rejected_json[:1000]}. For "
                "glossary_term_missing or explicit_term_missing, include the exact "
                "preferred_zh from VIDEO_GLOSSARY while preserving natural grammar; when "
                "いじめ requires 校园霸凌, the affected cue must literally include 校园霸凌 "
                "and cannot substitute perpetrator/victim wording alone."
                f"{glossary_feedback}{kana_name_feedback}{contextual_feedback}"
            )
            attempts.append(
                TranslationAttempt(
                    attempt_number,
                    code,
                    f"flags={failure_json[:1000]}; rejected={rejected_json[:1000]}",
                    raw,
                )
            )
            continue

        try:
            translated = apply_translations(
                block.commit_cues,
                translations,
                expected_digest,
            )
        except ValueError as exc:
            attempts.append(
                TranslationAttempt(
                    attempt_number,
                    "digest_mismatch",
                    str(exc),
                    raw,
                )
            )
            continue

        success_message = (
            json.dumps(
                audit_evidence,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if audit_evidence
            else ""
        )
        attempts.append(TranslationAttempt(attempt_number, "ok", success_message, raw))
        output_rows = tuple((cue.cue_id, cue.text) for cue in translated)
        return TranslationArtifact(
            texts=output_rows,
            timestamp_digest=expected_digest,
            source_digest=expected_source_digest,
            glossary_digest=expected_glossary_digest,
            input_text_digest=input_text_digest,
            output_text_digest=_text_digest(output_rows),
            model_id=config.model_id,
            revision=config.revision,
            pass_name=config.pass_name,
            status={
                "primary": "draft_ok",
                "review": "review_ok",
                "escalation": "escalation_ok",
            }[config.pass_name],
            attempts=tuple(attempts),
        )

    return TranslationArtifact(
        texts=(),
        timestamp_digest=expected_digest,
        source_digest=expected_source_digest,
        glossary_digest=expected_glossary_digest,
        input_text_digest=input_text_digest,
        output_text_digest=None,
        model_id=config.model_id,
        revision=config.revision,
        pass_name=config.pass_name,
        status="blocked",
        attempts=tuple(attempts),
    )


def translate_block(
    client: Any,
    block: TranslationBlock,
    glossary: GlossaryArtifact,
    config: ModelConfig,
    *,
    source_cues: tuple[JapaneseCue, ...],
    expected_digest: str,
    expected_source_digest: str,
    expected_glossary_digest: str,
    simplifier: Callable[[str], str] | None = None,
) -> TranslationArtifact:
    if config.pass_name not in {"primary"}:
        raise ValueError("translate_block requires a primary model configuration")
    return _run_block(
        client,
        block,
        source_cues,
        glossary,
        config,
        draft=None,
        expected_digest=expected_digest,
        expected_source_digest=expected_source_digest,
        expected_glossary_digest=expected_glossary_digest,
        simplifier=simplifier,
    )


def review_block(
    client: Any,
    block: TranslationBlock,
    draft: dict[str, str],
    glossary: GlossaryArtifact,
    config: ModelConfig,
    *,
    source_cues: tuple[JapaneseCue, ...],
    expected_digest: str,
    expected_source_digest: str,
    expected_glossary_digest: str,
    simplifier: Callable[[str], str] | None = None,
) -> TranslationArtifact:
    if config.pass_name not in {"review", "escalation"}:
        raise ValueError("review_block requires a review model configuration")
    return _run_block(
        client,
        block,
        source_cues,
        glossary,
        config,
        draft=draft,
        expected_digest=expected_digest,
        expected_source_digest=expected_source_digest,
        expected_glossary_digest=expected_glossary_digest,
        simplifier=simplifier,
    )


def extract_glossary(
    client: Any,
    cues: Iterable[JapaneseCue],
    config: ModelConfig,
) -> GlossaryArtifact:
    if config.pass_name not in {"primary", "glossary"}:
        raise ValueError("glossary extraction requires a primary/glossary model")
    ordered = tuple(cues)
    if not ordered:
        raise ValueError("glossary extraction requires source cues")
    ids = [cue.cue_id for cue in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("glossary source cue IDs must be unique")
    digest = source_cue_digest(ordered)
    model_cues = _model_cues(ordered)
    payload = {
        "schema_version": 1,
        "model_revision": config.revision,
        "source_digest": digest,
        "SOURCE_CUES": _cue_rows(model_cues),
    }
    attempts: list[TranslationAttempt] = []
    validation_feedback: str | None = None
    for attempt_number in range(1, 4):
        fallback_empty = attempt_number == 3 and validation_feedback is not None
        if fallback_empty:
            request_payload = {
                "schema_version": 1,
                "model_revision": config.revision,
                "source_digest": digest,
                "FALLBACK_EMPTY_GLOSSARY": True,
                "reason": validation_feedback,
            }
            system_prompt = EMPTY_GLOSSARY_SYSTEM_PROMPT
        else:
            request_payload = dict(payload)
            if validation_feedback is not None:
                request_payload["VALIDATION_FEEDBACK"] = validation_feedback
            system_prompt = GLOSSARY_SYSTEM_PROMPT
        kwargs = _request_kwargs(
            system_prompt,
            request_payload,
            config,
            _glossary_response_format(),
        )
        raw: str | None = None
        try:
            raw = _response_text(client.chat.completions.create(**kwargs))
            glossary = _parse_model_glossary_response(raw, ordered)
        except ValueError as exc:
            message = str(exc)
            attempts.append(
                TranslationAttempt(
                    attempt_number,
                    _schema_code(exc),
                    message,
                    raw,
                )
            )
            validation_feedback = (
                f"{message[:500]} Regenerate the complete JSON. Omit any entry "
                "whose ja cannot be copied verbatim from a cited cue."
            )
            continue
        except Exception as exc:
            attempts.append(
                TranslationAttempt(attempt_number, "client_exception", str(exc), raw)
            )
            continue
        attempts.append(TranslationAttempt(attempt_number, "ok", "", raw))
        return GlossaryArtifact(
            glossary=glossary,
            source_digest=digest,
            content_digest=glossary_digest(glossary),
            model_id=config.model_id,
            revision=config.revision,
            status="ok",
            attempts=tuple(attempts),
        )
    return GlossaryArtifact(
        glossary=None,
        source_digest=digest,
        content_digest=None,
        model_id=config.model_id,
        revision=config.revision,
        status="blocked",
        attempts=tuple(attempts),
    )


def build_escalation_selection(
    cues: Iterable[JapaneseCue],
    glossary_artifact: GlossaryArtifact,
    baseline_texts: Iterable[tuple[str, str]],
    *,
    expected_glossary_digest: str,
    flagged_ids: Iterable[str],
    ambiguous_ids: Iterable[str],
) -> EscalationSelection:
    ordered = tuple(cues)
    ids = [cue.cue_id for cue in ordered]
    if not ordered or len(ids) != len(set(ids)):
        raise ValueError("escalation selection requires unique source cues")
    digest = source_cue_digest(ordered)
    glossary = _validated_glossary_artifact(glossary_artifact, ordered)
    if glossary_digest(glossary) != expected_glossary_digest:
        raise ValueError("escalation glossary differs from upstream expected digest")
    baseline_rows = _validate_text_rows(tuple(baseline_texts), allow_empty=False)
    if tuple(cue_id for cue_id, _ in baseline_rows) != tuple(ids):
        raise ValueError("escalation baseline must cover source cues in order")
    id_set = set(ids)
    flagged = tuple(flagged_ids)
    ambiguous = tuple(ambiguous_ids)
    if any(
        type(cue_id) is not str or cue_id not in id_set
        for cue_id in flagged + ambiguous
    ):
        raise ValueError("flagged/ambiguous escalation IDs must exist in source")
    if len(flagged) != len(set(flagged)) or len(ambiguous) != len(set(ambiguous)):
        raise ValueError("flagged/ambiguous escalation IDs must be unique")
    flagged_set = set(flagged)
    ambiguous_set = set(ambiguous)
    flagged = tuple(cue_id for cue_id in ids if cue_id in flagged_set)
    ambiguous = tuple(cue_id for cue_id in ids if cue_id in ambiguous_set)

    explicit: list[str] = []
    for cue in ordered:
        if any(
            entry.explicit
            and any(term in cue.text for term in (entry.ja, *entry.aliases))
            for entry in glossary.entries
        ):
            explicit.append(cue.cue_id)

    blocks = build_translation_blocks(
        ordered,
        commit_size=ESCALATION_BLOCK_SIZE,
    )
    scores = {
        block.commit_start: int.from_bytes(
            hashlib.sha256(
                (
                    f"{ESCALATION_POLICY_VERSION}:{DEFAULT_SEED}:"
                    f"{digest}:{block.commit_start}:"
                    f"{','.join(block.commit_ids)}"
                ).encode()
            ).digest()[:8],
            "big",
        )
        for block in blocks
    }
    sampled_starts = {
        start
        for start, score in scores.items()
        if score % ESCALATION_SAMPLE_DENOMINATOR == 0
    }
    if not sampled_starts:
        sampled_starts.add(min(scores, key=scores.__getitem__))

    risk_ids = set(flagged + ambiguous + tuple(explicit))
    sampled_blocks = tuple(
        block.commit_ids for block in blocks if block.commit_start in sampled_starts
    )
    sampled_ids = tuple(
        min(
            block,
            key=lambda cue_id: hashlib.sha256(
                (f"{ESCALATION_POLICY_VERSION}:{digest}:{cue_id}").encode()
            ).digest(),
        )
        for block in sampled_blocks
    )
    required_blocks = tuple(
        block.commit_ids
        for block in blocks
        if block.commit_start in sampled_starts
        or any(cue_id in risk_ids for cue_id in block.commit_ids)
    )
    required_ids = tuple(cue_id for block in required_blocks for cue_id in block)
    return EscalationSelection(
        source_digest=digest,
        glossary_digest=glossary_digest(glossary),
        baseline_text_digest=_text_digest(baseline_rows),
        policy_version=ESCALATION_POLICY_VERSION,
        flagged_ids=flagged,
        ambiguous_ids=ambiguous,
        explicit_ids=tuple(explicit),
        sampled_ids=sampled_ids,
        sampled_blocks=sampled_blocks,
        required_blocks=required_blocks,
        required_ids=required_ids,
        sample_denominator=ESCALATION_SAMPLE_DENOMINATOR,
        block_size=ESCALATION_BLOCK_SIZE,
    )


def select_escalation_ids(
    cues: Iterable[JapaneseCue],
    glossary: GlossaryArtifact,
    baseline_texts: Iterable[tuple[str, str]],
    *,
    expected_glossary_digest: str,
    flagged_ids: Iterable[str],
    ambiguous_ids: Iterable[str],
) -> tuple[str, ...]:
    return build_escalation_selection(
        cues,
        glossary,
        baseline_texts,
        expected_glossary_digest=expected_glossary_digest,
        flagged_ids=flagged_ids,
        ambiguous_ids=ambiguous_ids,
    ).required_ids


def _collect_artifacts(
    artifacts: Iterable[TranslationArtifact],
    *,
    allowed_passes: set[str],
    source_map: dict[str, JapaneseCue],
    expected_source_digest: str,
    expected_glossary_digest: str,
    expected_input: dict[str, str] | None,
    glossary: Glossary,
    simplifier: Callable[[str], str],
) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    texts: dict[str, str] = {}
    models: list[tuple[str, str]] = []
    for artifact in artifacts:
        expected_status = {
            "primary": "draft_ok",
            "review": "review_ok",
            "escalation": "escalation_ok",
        }
        if not isinstance(artifact, TranslationArtifact):
            raise ValueError("review workflow contains a non-translation artifact")
        artifact.__post_init__()
        if artifact.status != expected_status.get(artifact.pass_name):
            raise ValueError("review workflow contains a non-ok translation artifact")
        if artifact.pass_name not in allowed_passes:
            raise ValueError("review workflow contains an artifact from the wrong pass")
        if (
            artifact.source_digest != expected_source_digest
            or artifact.glossary_digest != expected_glossary_digest
        ):
            raise ValueError("review workflow artifact provenance differs")
        rows = artifact.texts
        ids = [cue_id for cue_id, _ in rows]
        if (
            not rows
            or len(ids) != len(set(ids))
            or any(cue_id not in source_map for cue_id in ids)
        ):
            raise ValueError("review workflow artifact has invalid cue IDs")
        if any(cue_id in texts for cue_id in ids):
            raise ValueError("review workflow artifacts overlap committed cue IDs")
        source_subset = tuple(source_map[cue_id] for cue_id in ids)
        if timestamp_digest(source_subset) != artifact.timestamp_digest:
            raise ValueError("review workflow artifact timestamp digest differs")
        if _text_digest(rows) != artifact.output_text_digest:
            raise ValueError("review workflow artifact output digest differs")
        if expected_input is None:
            if artifact.input_text_digest is not None:
                raise ValueError("primary artifact unexpectedly records review input")
        else:
            expected_rows = tuple((cue_id, expected_input[cue_id]) for cue_id in ids)
            if artifact.input_text_digest != _text_digest(expected_rows):
                raise ValueError("review artifact is not linked to the prior pass")
        quality = {
            cue_id: flags
            for cue_id, text in rows
            if (
                flags := _translation_output_flags(
                    source_map[cue_id].text,
                    text,
                    glossary,
                    simplifier=simplifier,
                    cue_id=cue_id,
                )
            )
        }
        if quality:
            raise ValueError(
                "review workflow artifact fails quality replay: "
                f"{json.dumps(quality, ensure_ascii=False, sort_keys=True)}"
            )
        texts.update(rows)
        provenance = (artifact.model_id, artifact.revision)
        if provenance not in models:
            models.append(provenance)
    return texts, tuple(models)


def finalize_reviewed_video(
    source_cues: Iterable[JapaneseCue],
    primary_artifacts: Iterable[TranslationArtifact],
    baseline_artifacts: Iterable[TranslationArtifact],
    escalation_artifacts: Iterable[TranslationArtifact],
    glossary_artifact: GlossaryArtifact,
    selection: EscalationSelection,
    *,
    expected_timestamp_digest: str,
    expected_source_digest: str,
    expected_glossary_digest: str,
    simplifier: Callable[[str], str],
) -> ReviewedTranslationArtifact:
    ordered = tuple(source_cues)
    ids = [cue.cue_id for cue in ordered]
    if not ordered or len(ids) != len(set(ids)):
        raise ValueError("review finalization requires unique source cues")
    if source_cue_digest(ordered) != expected_source_digest:
        raise ValueError("review finalization source digest differs")
    if timestamp_digest(ordered) != expected_timestamp_digest:
        raise ValueError("review finalization timestamp digest differs")
    if not callable(simplifier):
        raise ValueError("review finalization requires a Simplified-Chinese converter")
    glossary = _validated_glossary_artifact(glossary_artifact, ordered)
    actual_glossary_digest = glossary_digest(glossary)
    if actual_glossary_digest != expected_glossary_digest:
        raise ValueError(
            "review finalization glossary differs from upstream expected digest"
        )
    if not isinstance(selection, EscalationSelection):
        raise ValueError("review finalization requires escalation selection evidence")
    selection.__post_init__()
    if selection.source_digest != expected_source_digest:
        raise ValueError("review finalization selection source digest differs")
    if selection.glossary_digest != expected_glossary_digest:
        raise ValueError("review finalization selection glossary digest differs")
    source_map = {cue.cue_id: cue for cue in ordered}

    primary, primary_models = _collect_artifacts(
        primary_artifacts,
        allowed_passes={"primary"},
        source_map=source_map,
        expected_source_digest=expected_source_digest,
        expected_glossary_digest=expected_glossary_digest,
        expected_input=None,
        glossary=glossary,
        simplifier=simplifier,
    )
    if list(primary) != ids:
        raise ValueError(
            "primary translation does not cover every source cue exactly once"
        )
    if len(primary_models) != 1:
        raise ValueError("primary translation mixes model IDs or revisions")
    baseline, baseline_models = _collect_artifacts(
        baseline_artifacts,
        allowed_passes={"review"},
        source_map=source_map,
        expected_source_digest=expected_source_digest,
        expected_glossary_digest=expected_glossary_digest,
        expected_input=primary,
        glossary=glossary,
        simplifier=simplifier,
    )
    if list(baseline) != ids:
        raise ValueError("baseline review does not cover every source cue exactly once")
    if len(baseline_models) != 1:
        raise ValueError("baseline review mixes model IDs or revisions")
    baseline_rows = tuple(baseline.items())
    if selection.baseline_text_digest != _text_digest(baseline_rows):
        raise ValueError("review finalization selection baseline digest differs")
    recomputed_selection = build_escalation_selection(
        ordered,
        glossary_artifact,
        baseline_rows,
        expected_glossary_digest=expected_glossary_digest,
        flagged_ids=selection.flagged_ids,
        ambiguous_ids=selection.ambiguous_ids,
    )
    if recomputed_selection != selection:
        raise ValueError("review finalization escalation selection evidence differs")

    required = selection.required_ids
    escalation_artifacts = tuple(escalation_artifacts)
    if any(
        not isinstance(artifact, TranslationArtifact)
        for artifact in escalation_artifacts
    ):
        raise ValueError("review workflow contains a non-translation artifact")
    artifact_blocks = tuple(
        tuple(cue_id for cue_id, _ in artifact.texts)
        for artifact in escalation_artifacts
    )
    if len(artifact_blocks) != len(selection.required_blocks) or set(
        artifact_blocks
    ) != set(selection.required_blocks):
        raise ValueError(
            "mandatory escalation coverage must use complete selected blocks"
        )
    escalated, escalation_models = _collect_artifacts(
        escalation_artifacts,
        allowed_passes={"escalation"},
        source_map=source_map,
        expected_source_digest=expected_source_digest,
        expected_glossary_digest=expected_glossary_digest,
        expected_input=baseline,
        glossary=glossary,
        simplifier=simplifier,
    )
    if any(cue_id not in escalated for cue_id in required):
        missing = [cue_id for cue_id in required if cue_id not in escalated]
        raise ValueError(f"mandatory escalation coverage is missing: {missing}")
    if len(escalation_models) != 1:
        raise ValueError("mandatory escalation mixes reviewer revisions")

    final_map = dict(baseline)
    final_map.update(escalated)
    final_rows = {cue_id: final_map[cue_id] for cue_id in ids}
    final_cues = apply_translations(
        ordered,
        final_rows,
        expected_timestamp_digest,
    )
    return ReviewedTranslationArtifact(
        texts=tuple((cue.cue_id, cue.text) for cue in final_cues),
        timestamp_digest=expected_timestamp_digest,
        source_digest=expected_source_digest,
        glossary_digest=expected_glossary_digest,
        selection=selection,
        primary_models=primary_models,
        baseline_models=baseline_models,
        escalation_models=escalation_models,
        status="ok",
        _verification_token=_FINALIZER_TOKEN,
    )
