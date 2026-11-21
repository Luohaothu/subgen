"""Runtime settings loaded from a TOML file and environment variables.

The settings are the boundary between the host and the pipeline. They carry the
four roots, the accelerator choice, the ASR engine, and an optional external
translation provider. Defaults keep the original Linux host working. A different
host supplies a ``subgen.toml`` or the matching ``SUBGEN_*`` variables.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_ROOT = Path("/home/leo/nas/Other/abc")
DEFAULT_WORK_ROOT = Path("/home/leo/.local/share/abc-subtitles-recovery-6")
DEFAULT_DEVICE = "auto"
DEFAULT_ASR_ENGINE = "auto"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
CONFIG_ENV = "SUBGEN_CONFIG"
CONFIG_NAME = "subgen.toml"

DEVICES = frozenset({"auto", "cpu", "cuda", "mps"})
ASR_ENGINES = frozenset({"auto", "vllm", "funasr"})

SETTINGS_SECTIONS: dict[str, frozenset[str]] = {
    "paths": frozenset({"source_root", "work_root", "model_root", "asr_model"}),
    "runtime": frozenset({"device", "asr_engine"}),
    "translation": frozenset(
        {
            "endpoint",
            "model",
            "revision",
            "api_key_env",
            "review_model",
            "review_revision",
        }
    ),
}

SETTINGS_ENV: dict[str, str] = {
    "paths.source_root": "SUBGEN_SOURCE_ROOT",
    "paths.work_root": "SUBGEN_WORK_ROOT",
    "paths.model_root": "SUBGEN_MODEL_ROOT",
    "paths.asr_model": "SUBGEN_ASR_MODEL",
    "runtime.device": "SUBGEN_DEVICE",
    "runtime.asr_engine": "SUBGEN_ASR_ENGINE",
    "translation.endpoint": "SUBGEN_TRANSLATION_ENDPOINT",
    "translation.model": "SUBGEN_TRANSLATION_MODEL",
    "translation.revision": "SUBGEN_TRANSLATION_REVISION",
    "translation.api_key_env": "SUBGEN_API_KEY_ENV",
    "translation.review_model": "SUBGEN_TRANSLATION_REVIEW_MODEL",
    "translation.review_revision": "SUBGEN_TRANSLATION_REVIEW_REVISION",
}

_SECTIONS = {section: set(keys) for section, keys in SETTINGS_SECTIONS.items()}


class SettingsError(ValueError):
    """A settings file or environment value is invalid."""


@dataclass(frozen=True)
class TranslationProvider:
    endpoint: str
    model: str
    revision: str
    api_key_env: str = DEFAULT_API_KEY_ENV
    review_model: str = ""
    review_revision: str = ""

    def __post_init__(self) -> None:
        for value, field in (
            (self.endpoint, "translation endpoint"),
            (self.model, "translation model"),
            (self.revision, "translation revision"),
            (self.api_key_env, "translation API key variable"),
        ):
            if type(value) is not str or not value.strip():
                raise SettingsError(f"{field} must be a nonblank string")
        if not self.endpoint.startswith(("http://", "https://")):
            raise SettingsError("translation endpoint must be an HTTP(S) URL")
        if not self.review_model:
            object.__setattr__(self, "review_model", self.model)
        if not self.review_revision:
            object.__setattr__(self, "review_revision", self.revision)
        for value, field in (
            (self.review_model, "review model"),
            (self.review_revision, "review revision"),
        ):
            if type(value) is not str or not value.strip():
                raise SettingsError(f"{field} must be a nonblank string")


@dataclass(frozen=True)
class RuntimeSettings:
    source_root: Path
    work_root: Path
    model_root: Path
    asr_model_path: Path
    device: str = DEFAULT_DEVICE
    asr_engine: str = DEFAULT_ASR_ENGINE
    translation: TranslationProvider | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_root, "source root"),
            (self.work_root, "work root"),
            (self.model_root, "model root"),
            (self.asr_model_path, "ASR model path"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise SettingsError(f"{field} must be an absolute Path")
        if self.device not in DEVICES:
            raise SettingsError(
                f"device must be one of {', '.join(sorted(DEVICES))}: {self.device}"
            )
        if self.asr_engine not in ASR_ENGINES:
            raise SettingsError(
                "ASR engine must be one of "
                f"{', '.join(sorted(ASR_ENGINES))}: {self.asr_engine}"
            )
        if (
            self.translation is not None
            and type(self.translation) is not TranslationProvider
        ):
            raise SettingsError("translation must be a TranslationProvider or None")


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except OSError as exc:
        raise SettingsError(f"cannot read settings file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"settings file is not valid TOML: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SettingsError(f"settings file must contain a table: {path}")
    for section, keys in value.items():
        if section not in _SECTIONS:
            raise SettingsError(f"unknown settings section: {section}")
        if not isinstance(keys, dict):
            raise SettingsError(f"settings section must be a table: {section}")
        unknown = set(keys) - _SECTIONS[section]
        if unknown:
            raise SettingsError(
                f"unknown keys in [{section}]: {', '.join(sorted(unknown))}"
            )
    return value


def resolve_config_path(
    environ: Mapping[str, str], config_path: Path | None
) -> Path | None:
    """Resolve the settings file: explicit path, environment, then the CWD."""
    if config_path is not None:
        return config_path
    configured = environ.get(CONFIG_ENV)
    if configured:
        return Path(configured).expanduser()
    candidate = Path(CONFIG_NAME)
    return candidate if candidate.is_file() else None


def _text(
    environ: Mapping[str, str],
    section: Mapping[str, object],
    key: str,
    env_name: str,
    default: str,
) -> str:
    if env_name in environ:
        return environ[env_name]
    value = section.get(key, default)
    if type(value) is not str or not value.strip():
        raise SettingsError(f"{key} must be a nonblank string")
    return value


def _optional_text(
    environ: Mapping[str, str],
    section: Mapping[str, object],
    key: str,
    env_name: str,
) -> str:
    if env_name in environ:
        return environ[env_name]
    value = section.get(key, "")
    if type(value) is not str:
        raise SettingsError(f"{key} must be a string")
    return value


def load_settings(
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    *,
    missing_ok: bool = False,
) -> RuntimeSettings:
    """Load settings with environment values taking precedence over the file.

    ``missing_ok`` treats an explicit path that does not exist as an empty
    document. The web console uses it so a first run can start with defaults and
    create the file on the first save.
    """
    values = os.environ if environ is None else environ
    path = resolve_config_path(values, config_path)
    if missing_ok and path is not None and not path.is_file():
        document: dict[str, object] = {}
    else:
        document = _read_toml(path) if path is not None else {}
    paths = document.get("paths", {})
    runtime = document.get("runtime", {})
    translation = document.get("translation", {})
    assert isinstance(paths, dict)
    assert isinstance(runtime, dict)
    assert isinstance(translation, dict)

    source_root = Path(
        _text(
            values, paths, "source_root", "SUBGEN_SOURCE_ROOT", str(DEFAULT_SOURCE_ROOT)
        )
    ).expanduser()
    work_root = Path(
        _text(values, paths, "work_root", "SUBGEN_WORK_ROOT", str(DEFAULT_WORK_ROOT))
    ).expanduser()
    model_root = Path(
        _text(
            values, paths, "model_root", "SUBGEN_MODEL_ROOT", str(work_root / "models")
        )
    ).expanduser()
    asr_model = Path(
        _text(
            values,
            paths,
            "asr_model",
            "SUBGEN_ASR_MODEL",
            str(model_root / "Fun-ASR-Nano-2512"),
        )
    ).expanduser()
    device = _text(values, runtime, "device", "SUBGEN_DEVICE", DEFAULT_DEVICE)
    asr_engine = _text(
        values, runtime, "asr_engine", "SUBGEN_ASR_ENGINE", DEFAULT_ASR_ENGINE
    )

    endpoint = _optional_text(
        values, translation, "endpoint", "SUBGEN_TRANSLATION_ENDPOINT"
    )
    model = _optional_text(values, translation, "model", "SUBGEN_TRANSLATION_MODEL")
    revision = _optional_text(
        values, translation, "revision", "SUBGEN_TRANSLATION_REVISION"
    )
    api_key_env = _text(
        values,
        translation,
        "api_key_env",
        "SUBGEN_API_KEY_ENV",
        DEFAULT_API_KEY_ENV,
    )
    review_model = _optional_text(
        values, translation, "review_model", "SUBGEN_TRANSLATION_REVIEW_MODEL"
    )
    review_revision = _optional_text(
        values,
        translation,
        "review_revision",
        "SUBGEN_TRANSLATION_REVIEW_REVISION",
    )
    provider: TranslationProvider | None = None
    if endpoint or model or revision:
        if not (endpoint and model and revision):
            raise SettingsError(
                "translation endpoint, model, and revision must be set together"
            )
        provider = TranslationProvider(
            endpoint, model, revision, api_key_env, review_model, review_revision
        )

    return RuntimeSettings(
        source_root=source_root,
        work_root=work_root,
        model_root=model_root,
        asr_model_path=asr_model,
        device=device,
        asr_engine=asr_engine,
        translation=provider,
    )


def resolve_device(requested: str) -> str:
    """Resolve ``auto`` to the best available accelerator."""
    if requested != "auto":
        if requested not in DEVICES:
            raise SettingsError(f"unknown device: {requested}")
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"
