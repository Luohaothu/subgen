"""Settings presentation, validation, and the comment-preserving writer.

The loader in :mod:`subtitle_pipeline.settings` remains the only semantic
validator. A draft is rendered to a temporary file and parsed by that loader.
Saving either rewrites the existing lines in place, keeping comments and order,
or writes canonical TOML when the file is missing, empty, or repaired with
``force``. Every rewrite is parsed back and compared before anything is
replaced.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
import uuid
from collections.abc import Mapping
from pathlib import Path

from ..settings import (
    ASR_ENGINES,
    CONFIG_NAME,
    DEFAULT_API_KEY_ENV,
    DEVICES,
    SETTINGS_ENV,
    SETTINGS_SECTIONS,
    RuntimeSettings,
    SettingsError,
    load_settings,
)
from ..settings import (
    resolve_config_path as resolve_setting_path,
)

SECTION_ORDER = ("paths", "runtime", "translation")

_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "paths": ("source_root", "work_root", "model_root", "asr_model"),
    "runtime": ("device", "asr_engine"),
    "translation": (
        "endpoint",
        "model",
        "revision",
        "api_key_env",
        "review_model",
        "review_revision",
    ),
}

_DERIVED_PARENTS = {
    "paths.model_root": "paths.work_root",
    "paths.asr_model": "paths.model_root",
}

_TABLE_RE = re.compile(r"^[ \t]*\[(?P<name>[A-Za-z0-9_-]+)\][ \t]*(?:#.*)?$")
_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+)"
    r"(?P<sep>[ \t]*=[ \t]*)(?P<rest>.*)$"
)

CANONICAL_HEADER = (
    "# subgen settings. `subgen web` keeps comments when it rewrites this file."
)


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    """One editable settings key."""

    section: str
    key: str
    env: str
    kind: str
    choices: tuple[str, ...] = ()

    @property
    def full_key(self) -> str:
        return f"{self.section}.{self.key}"


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("paths", "source_root", SETTINGS_ENV["paths.source_root"], "path"),
    FieldSpec("paths", "work_root", SETTINGS_ENV["paths.work_root"], "path"),
    FieldSpec("paths", "model_root", SETTINGS_ENV["paths.model_root"], "path"),
    FieldSpec("paths", "asr_model", SETTINGS_ENV["paths.asr_model"], "path"),
    FieldSpec(
        "runtime",
        "device",
        SETTINGS_ENV["runtime.device"],
        "enum",
        tuple(sorted(DEVICES)),
    ),
    FieldSpec(
        "runtime",
        "asr_engine",
        SETTINGS_ENV["runtime.asr_engine"],
        "enum",
        tuple(sorted(ASR_ENGINES)),
    ),
    FieldSpec(
        "translation",
        "endpoint",
        SETTINGS_ENV["translation.endpoint"],
        "url",
    ),
    FieldSpec("translation", "model", SETTINGS_ENV["translation.model"], "text"),
    FieldSpec("translation", "revision", SETTINGS_ENV["translation.revision"], "text"),
    FieldSpec(
        "translation",
        "api_key_env",
        SETTINGS_ENV["translation.api_key_env"],
        "text",
    ),
    FieldSpec(
        "translation",
        "review_model",
        SETTINGS_ENV["translation.review_model"],
        "text",
    ),
    FieldSpec(
        "translation",
        "review_revision",
        SETTINGS_ENV["translation.review_revision"],
        "text",
    ),
)

_FIELD_BY_KEY = {spec.full_key: spec for spec in FIELD_SPECS}
_SECTION_KEYS = {
    section: frozenset(keys) for section, keys in SETTINGS_SECTIONS.items()
}


class SettingsIOError(RuntimeError):
    """A settings read, validation, or save operation failed."""


class DraftInvalid(SettingsIOError):
    """The draft is semantically invalid; ``fields`` names each problem."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        self.fields = dict(fields)
        super().__init__("settings draft is invalid")


class BaseHashMismatch(SettingsIOError):
    """The file changed since the client read it."""


class SettingsLoadError(SettingsIOError):
    """The file on disk does not load and a normal save cannot repair it."""


class RewriteError(SettingsIOError):
    """The line-preserving rewrite could not prove its output."""


class _RewriteFailure(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class DraftResult:
    """The validation outcome and preview for one settings draft."""

    valid: bool
    preview: str
    fields: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    errors: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class SaveResult:
    """Where a save wrote and which keys it wrote or omitted."""

    path: Path
    backup: Path | None
    wrote_keys: tuple[str, ...]
    omitted_keys: tuple[str, ...]


def resolve_config_path(environ: Mapping[str, str], config_path: Path | None) -> Path:
    """The settings path the console reads and writes, always absolute."""
    resolved = resolve_setting_path(environ, config_path)
    if resolved is None:
        resolved = Path.cwd() / CONFIG_NAME
    else:
        resolved = Path(resolved).expanduser()
        if not resolved.is_absolute():
            resolved = Path.cwd() / resolved
    return Path(os.path.abspath(resolved))


def _read_document(path: Path) -> tuple[dict, bytes, str | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, b"", None
    except OSError as exc:
        return {}, b"", f"cannot read settings file {path}: {exc}"
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {}, raw, f"settings file is not valid TOML: {path}: {exc}"
    if not isinstance(document, dict):
        return {}, raw, f"settings file must contain a table: {path}"
    return document, raw, None


def _file_values(document: Mapping[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    for section, keys in SETTINGS_SECTIONS.items():
        table = document.get(section)
        if not isinstance(table, Mapping):
            continue
        for key in keys:
            value = table.get(key)
            if isinstance(value, str):
                values[f"{section}.{key}"] = value
    return values


def _settings_values(settings: RuntimeSettings) -> dict[str, str]:
    provider = settings.translation
    return {
        "paths.source_root": str(settings.source_root),
        "paths.work_root": str(settings.work_root),
        "paths.model_root": str(settings.model_root),
        "paths.asr_model": str(settings.asr_model_path),
        "runtime.device": settings.device,
        "runtime.asr_engine": settings.asr_engine,
        "translation.endpoint": provider.endpoint if provider else "",
        "translation.model": provider.model if provider else "",
        "translation.revision": provider.revision if provider else "",
        "translation.api_key_env": (
            provider.api_key_env if provider else DEFAULT_API_KEY_ENV
        ),
        "translation.review_model": provider.review_model if provider else "",
        "translation.review_revision": (provider.review_revision if provider else ""),
    }


def snapshot(settings: RuntimeSettings) -> dict[str, str]:
    """The resolved settings a job records as its evidence."""
    return _settings_values(settings)


def _source(
    spec: FieldSpec,
    environ: Mapping[str, str],
    file_values: Mapping[str, str],
) -> str:
    if spec.env in environ:
        return "env"
    if spec.full_key in file_values:
        return "file"
    parent = _DERIVED_PARENTS.get(spec.full_key)
    if parent is not None:
        parent_spec = _FIELD_BY_KEY[parent]
        if _source(parent_spec, environ, file_values) != "default":
            return "derived"
    return "default"


def _field_rows(
    environ: Mapping[str, str],
    file_values: Mapping[str, str],
    effective_values: Mapping[str, str],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for spec in FIELD_SPECS:
        file_value = file_values.get(spec.full_key)
        rows.append(
            {
                "section": spec.section,
                "key": spec.key,
                "env": spec.env,
                "kind": spec.kind,
                "file_value": file_value,
                "effective_value": effective_values.get(spec.full_key, ""),
                "source": _source(spec, environ, file_values),
                "choices": list(spec.choices),
                "error": None,
            }
        )
    return tuple(rows)


def _directory_warnings(settings: RuntimeSettings) -> list[str]:
    warnings: list[str] = []
    for value, label in (
        (settings.model_root, "model root"),
        (settings.asr_model_path, "ASR model root"),
    ):
        if not value.is_dir():
            warnings.append(f"{label} is not a directory: {value}")
    return warnings


def describe(environ: Mapping[str, str], config_path: Path | None) -> dict[str, object]:
    """The settings view: file values, effective values, and their sources."""
    path = resolve_config_path(environ, config_path)
    document, raw, read_error = _read_document(path)
    file_values = _file_values(document)
    loaded: RuntimeSettings | None = None
    load_error = read_error
    try:
        loaded = load_settings(environ=environ, config_path=path, missing_ok=True)
    except SettingsError as exc:
        load_error = str(exc)
    if loaded is not None:
        effective_values = _settings_values(loaded)
        warnings = _directory_warnings(loaded)
    else:
        effective_values = dict(file_values)
        warnings = []
    if loaded is None:
        for spec in FIELD_SPECS:
            effective_values.setdefault(spec.full_key, "")
    provider = loaded.translation if loaded is not None else None
    api_key_configured = bool(
        provider is not None and environ.get(provider.api_key_env)
    )
    return {
        "config_path": str(path),
        "exists": path.is_file(),
        "settings_ok": load_error is None,
        "load_error": load_error,
        "base_sha256": hashlib.sha256(raw).hexdigest(),
        "api_key_configured": api_key_configured,
        "fields": list(_field_rows(environ, file_values, effective_values)),
        "warnings": warnings,
    }


def _document_from_values(values: Mapping[str, str]) -> dict[str, dict[str, str]]:
    document: dict[str, dict[str, str]] = {}
    for spec in FIELD_SPECS:
        value = values.get(spec.full_key, "")
        if value:
            document.setdefault(spec.section, {})[spec.key] = value
    return document


def _parse_values(value: object) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(value, Mapping):
        return {}, {"values": "values must be an object"}
    clean: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _FIELD_BY_KEY:
            errors[str(key)] = "unknown settings field"
            continue
        if not isinstance(item, str):
            errors[key] = "value must be a string"
            continue
        clean[key] = item
    return clean, errors


def _toml_string(value: str) -> str:
    # JSON string escaping is a subset of TOML basic-string escaping.
    return json.dumps(value, ensure_ascii=False)


def _render_canonical(document: Mapping[str, Mapping[str, str]]) -> str:
    lines = [CANONICAL_HEADER, ""]
    for section in SECTION_ORDER:
        table = document.get(section)
        if not table:
            continue
        lines.append(f"[{section}]")
        for key in _FIELD_ORDER[section]:
            if key in table:
                lines.append(f"{key} = {_toml_string(table[key])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _document_or_empty(
    document: Mapping[str, object], section: str
) -> Mapping[str, str]:
    table = document.get(section)
    if not isinstance(table, Mapping):
        return {}
    return {key: value for key, value in table.items() if isinstance(value, str)}


def _strip_empty_tables(document: Mapping[str, object]) -> dict[str, dict]:
    return {
        section: dict(table)
        for section, table in document.items()
        if isinstance(table, Mapping) and table
    }


def _prove(text: str, document: Mapping[str, Mapping[str, str]]) -> None:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _RewriteFailure(f"rewritten settings are not valid TOML: {exc}")
    if not isinstance(parsed, dict):
        raise _RewriteFailure("rewritten settings do not contain a table")
    if _strip_empty_tables(parsed) != dict(document):
        raise _RewriteFailure(
            "rewritten settings do not parse back to the requested document"
        )


def _value_span(text: str) -> int | None:
    if text.startswith('"""') or text.startswith("'''"):
        return None
    if text.startswith('"'):
        index = 1
        while index < len(text):
            character = text[index]
            if character == "\\":
                index += 2
                continue
            if character == '"':
                return index + 1
            index += 1
        return None
    if text.startswith("'"):
        index = text.find("'", 1)
        return None if index < 0 else index + 1
    if text.startswith(("[", "{")):
        return None
    index = 0
    while index < len(text) and not text[index].isspace() and text[index] != "#":
        index += 1
    return index or None


def _replace_value(match: re.Match[str], wanted: str) -> str | None:
    rest = match.group("rest")
    leading = rest[: len(rest) - len(rest.lstrip(" \t"))]
    value_text = rest[len(leading) :]
    span = _value_span(value_text)
    if span is None:
        return None
    tail = value_text[span:]
    if tail.strip() and not tail.lstrip().startswith("#"):
        return None
    return (
        f"{match.group('indent')}{match.group('key')}{match.group('sep')}"
        f"{leading}{_toml_string(wanted)}{tail}"
    )


def _rewrite_preserving(text: str, document: Mapping[str, Mapping[str, str]]) -> str:
    output: list[str] = []
    current: str | None = None
    headers: dict[str, int] = {}
    last_key: dict[str, int] = {}
    handled: set[tuple[str, str]] = set()
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        header = _TABLE_RE.match(body)
        if header:
            current = header.group("name")
            headers[current] = len(output)
            output.append(line)
            continue
        assignment = _KEY_RE.match(body)
        if assignment is not None and current is not None:
            key = assignment.group("key")
            if key in _SECTION_KEYS.get(current, frozenset()):
                wanted = _document_or_empty(document, current).get(key)
                if wanted is None:
                    handled.add((current, key))
                    continue
                replacement = _replace_value(assignment, wanted)
                if replacement is None:
                    raise _RewriteFailure(f"cannot rewrite [{current}] {key} in place")
                output.append(replacement + ending)
                handled.add((current, key))
                last_key[current] = len(output) - 1
                continue
            output.append(line)
            last_key[current] = len(output) - 1
            continue
        output.append(line)

    insertions: dict[int, list[str]] = {}
    appended: list[str] = []
    for section in SECTION_ORDER:
        table = _document_or_empty(document, section)
        missing = [
            key
            for key in _FIELD_ORDER[section]
            if key in table and (section, key) not in handled
        ]
        if not missing:
            continue
        if section in headers:
            base = last_key.get(section, headers[section])
            lines = [f"{key} = {_toml_string(table[key])}\n" for key in missing]
            insertions.setdefault(base + 1, []).extend(lines)
        else:
            appended.append(f"[{section}]\n")
            for key in missing:
                appended.append(f"{key} = {_toml_string(table[key])}\n")
            appended.append("\n")
    for position in sorted(insertions, reverse=True):
        output[position:position] = insertions[position]
    if appended:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += "\n"
        if output and output[-1].strip():
            output.append("\n")
        output.extend(appended)
    return "".join(output)


def _attribute_error(message: str) -> dict[str, str]:
    lowered = message.lower()
    if "endpoint, model, and revision" in lowered:
        return {
            "translation.endpoint": message,
            "translation.model": message,
            "translation.revision": message,
        }
    pairs = (
        ("source root", "paths.source_root"),
        ("work root", "paths.work_root"),
        ("model root", "paths.model_root"),
        ("asr model path", "paths.asr_model"),
        ("asr engine", "runtime.asr_engine"),
        ("device", "runtime.device"),
        ("translation endpoint", "translation.endpoint"),
        ("translation api key variable", "translation.api_key_env"),
        ("translation model", "translation.model"),
        ("translation revision", "translation.revision"),
        ("review model", "translation.review_model"),
        ("review revision", "translation.review_revision"),
    )
    for token, key in pairs:
        if token in lowered:
            return {key: message}
    return {"_": message}


def _check_roots(settings: RuntimeSettings) -> dict[str, str]:
    errors: dict[str, str] = {}
    for value, key in (
        (settings.source_root, "paths.source_root"),
        (settings.work_root, "paths.work_root"),
    ):
        try:
            resolved = value.resolve(strict=True)
        except OSError as exc:
            errors[key] = f"root does not exist: {value} ({exc})"
            continue
        if resolved != value:
            errors[key] = (
                f"root must be a canonical absolute path without symlinks: {value}"
            )
    return errors


def _preview(
    path: Path,
    document: Mapping[str, Mapping[str, str]],
) -> tuple[str, list[str]]:
    canonical = _render_canonical(document)
    warnings: list[str] = []
    if not path.is_file():
        return canonical, warnings
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return canonical, [f"cannot read the existing file: {exc}"]
    if not raw.strip():
        return canonical, warnings
    if not _current_file_loads(path):
        return canonical, [
            "the existing settings file does not load; saving needs force"
        ]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return canonical, [
            "the existing settings file is not UTF-8; saving needs force"
        ]
    try:
        rewritten = _rewrite_preserving(text, document)
        _prove(rewritten, document)
    except _RewriteFailure as exc:
        return canonical, [f"cannot rewrite in place ({exc}); saving needs force"]
    return rewritten, warnings


def _current_file_loads(path: Path) -> bool:
    try:
        load_settings(environ={}, config_path=path)
    except SettingsError:
        return False
    return True


def validate_draft(
    values: object,
    *,
    environ: Mapping[str, str],
    config_path: Path | None,
) -> DraftResult:
    """Validate a flat draft exactly as the loader would see it."""
    path = resolve_config_path(environ, config_path)
    clean, errors = _parse_values(values)
    document = _document_from_values(clean)
    canonical = _render_canonical(document)
    if errors:
        return DraftResult(False, canonical, (), (), errors)
    parent = path.parent
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".subgen-draft-", suffix=".toml", dir=parent
        )
    except OSError as exc:
        return DraftResult(
            False,
            canonical,
            (),
            (),
            {"_": f"cannot validate settings in {parent}: {exc}"},
        )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical.encode("utf-8"))
        try:
            loaded = load_settings(environ={}, config_path=temporary)
        except SettingsError as exc:
            return DraftResult(
                False,
                canonical,
                (),
                (),
                _attribute_error(str(exc)),
            )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    errors = _check_roots(loaded)
    warnings = _directory_warnings(loaded)
    file_values = _file_values(document)
    effective_values = _settings_values(loaded)
    fields = _field_rows(environ, file_values, effective_values)
    if errors:
        return DraftResult(False, canonical, fields, tuple(warnings), errors)
    preview, preview_warnings = _preview(path, document)
    return DraftResult(
        True,
        preview,
        fields,
        tuple(warnings + preview_warnings),
        {},
    )


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
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
            mode,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while storing {path}")
            view = view[written:]
        os.fsync(descriptor)
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


def save(
    values: object,
    *,
    environ: Mapping[str, str],
    config_path: Path | None,
    expected_sha256: str | None = None,
    force: bool = False,
) -> SaveResult:
    """Validate and write the settings file, keeping comments when possible."""
    path = resolve_config_path(environ, config_path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise SettingsIOError(f"cannot read settings file {path}: {exc}") from exc
    exists = path.is_file()
    if expected_sha256 is not None:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise BaseHashMismatch("the settings file changed since it was loaded")
    if exists and not force and not _current_file_loads(path):
        raise SettingsLoadError(
            "the settings file does not load; use force to repair it"
        )
    result = validate_draft(values, environ=environ, config_path=path)
    if not result.valid:
        raise DraftInvalid(result.errors)
    clean, _ = _parse_values(values)
    document = _document_from_values(clean)
    if not exists or not raw.strip() or force:
        text = _render_canonical(document)
    else:
        try:
            existing = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RewriteError(
                "the existing settings file is not UTF-8; use force"
            ) from exc
        try:
            text = _rewrite_preserving(existing, document)
        except _RewriteFailure as exc:
            raise RewriteError(f"{exc}; use force to write canonical TOML") from exc
    try:
        _prove(text, document)
    except _RewriteFailure as exc:
        raise RewriteError(f"{exc}; use force") from exc

    mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o600
    backup: Path | None = None
    if exists:
        backup = path.with_name(path.name + ".bak")
        _atomic_write(backup, raw, mode)
    try:
        _atomic_write(path, text.encode("utf-8"), mode)
    except OSError as exc:
        raise SettingsIOError(f"cannot write settings file {path}: {exc}") from exc
    return SaveResult(
        path=path,
        backup=backup,
        wrote_keys=tuple(key for key, value in clean.items() if value),
        omitted_keys=tuple(key for key, value in clean.items() if not value),
    )
