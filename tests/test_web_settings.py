from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from subtitle_pipeline.settings import (
    DEVICES,
    SETTINGS_ENV,
    SETTINGS_SECTIONS,
)
from subtitle_pipeline.web import settings_io
from subtitle_pipeline.web.settings_io import (
    BaseHashMismatch,
    DraftInvalid,
    RewriteError,
    SettingsLoadError,
)

PROVIDER = (
    "[translation]\n"
    'endpoint = "http://127.0.0.1:8123/v1"\n'
    'model = "vendor/model"\n'
    'revision = "revision-1"\n'
)


def config_text(
    source: Path,
    work: Path,
    model: Path | None = None,
    *,
    provider: bool = True,
    device: str = "auto",
) -> str:
    lines = [
        "# operator comment",
        "[paths]",
        f'source_root = "{source}"   # media',
        f'work_root = "{work}"',
    ]
    if model is not None:
        lines.append(f'model_root = "{model}"')
    lines.extend(("", "[runtime]", f'device = "{device}"', 'asr_engine = "auto"'))
    if provider:
        lines.extend(("", "[translation]"))
        lines.append('endpoint = "http://127.0.0.1:8123/v1"')
        lines.append('model = "vendor/model"')
        lines.append('revision = "revision-1"')
    return "\n".join(lines) + "\n"


def values(
    source: Path,
    work: Path,
    model: Path | None = None,
    asr_model: Path | None = None,
    **overrides: str,
) -> dict[str, str]:
    base = {
        "paths.source_root": str(source),
        "paths.work_root": str(work),
        "paths.model_root": str(model) if model is not None else "",
        "paths.asr_model": str(asr_model) if asr_model is not None else "",
        "runtime.device": "auto",
        "runtime.asr_engine": "auto",
        "translation.endpoint": "http://127.0.0.1:8123/v1",
        "translation.model": "vendor/model",
        "translation.revision": "revision-1",
        "translation.api_key_env": "",
        "translation.review_model": "",
        "translation.review_revision": "",
    }
    base.update(overrides)
    return base


def field_values(view: dict) -> dict[str, str]:
    return {
        f"{field['section']}.{field['key']}": field["file_value"] or ""
        for field in view["fields"]
    }


class SettingsFixture(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.model = self.root / "model"
        self.model.mkdir()
        self.asr = self.model / "asr"
        self.asr.mkdir()
        self.config = self.root / "subgen.toml"
        self.config.write_text(
            config_text(self.source, self.work, self.model), encoding="utf-8"
        )

    def write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")


class FieldSpecTests(SettingsFixture):
    def test_specs_cover_the_settings_tables_exactly(self):
        expected = {
            (section, key)
            for section, keys in SETTINGS_SECTIONS.items()
            for key in keys
        }
        actual = {(spec.section, spec.key) for spec in settings_io.FIELD_SPECS}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(settings_io.FIELD_SPECS))

    def test_specs_carry_the_loader_environment_names(self):
        for spec in settings_io.FIELD_SPECS:
            self.assertEqual(spec.env, SETTINGS_ENV[spec.full_key])
        self.assertEqual(
            set(SETTINGS_ENV),
            {spec.full_key for spec in settings_io.FIELD_SPECS},
        )

    def test_kinds_and_choices(self):
        kinds = {spec.full_key: spec.kind for spec in settings_io.FIELD_SPECS}
        for key in (
            "paths.source_root",
            "paths.work_root",
            "paths.model_root",
            "paths.asr_model",
        ):
            self.assertEqual(kinds[key], "path")
        self.assertEqual(kinds["runtime.device"], "enum")
        self.assertEqual(kinds["runtime.asr_engine"], "enum")
        self.assertEqual(kinds["translation.endpoint"], "url")
        self.assertEqual(kinds["translation.model"], "text")
        device = next(spec for spec in settings_io.FIELD_SPECS if spec.key == "device")
        self.assertEqual(set(device.choices), DEVICES)
        self.assertEqual(device.choices, tuple(sorted(DEVICES)))


class DescribeTests(SettingsFixture):
    def test_describe_reports_file_values_effective_values_and_sources(self):
        other_work = self.root / "other-work"
        other_work.mkdir()
        environ = {
            "SUBGEN_WORK_ROOT": str(other_work),
            "SUBGEN_DEVICE": "mps",
        }
        view = settings_io.describe(environ, self.config)
        self.assertTrue(view["settings_ok"])
        self.assertIsNone(view["load_error"])
        self.assertTrue(view["exists"])
        self.assertEqual(
            view["base_sha256"],
            hashlib.sha256(self.config.read_bytes()).hexdigest(),
        )
        fields = {f"{item['section']}.{item['key']}": item for item in view["fields"]}
        self.assertEqual(fields["paths.source_root"]["source"], "file")
        self.assertEqual(fields["paths.source_root"]["file_value"], str(self.source))
        self.assertEqual(fields["paths.work_root"]["source"], "env")
        self.assertEqual(fields["paths.work_root"]["effective_value"], str(other_work))
        self.assertEqual(fields["paths.work_root"]["file_value"], str(self.work))
        self.assertEqual(fields["paths.model_root"]["source"], "file")
        self.assertEqual(fields["paths.model_root"]["effective_value"], str(self.model))
        self.assertEqual(fields["paths.asr_model"]["source"], "derived")
        self.assertEqual(
            fields["paths.asr_model"]["effective_value"],
            str(self.model / "Fun-ASR-Nano-2512"),
        )
        self.assertEqual(fields["runtime.device"]["source"], "env")
        self.assertEqual(fields["runtime.device"]["effective_value"], "mps")
        self.assertEqual(fields["translation.review_model"]["source"], "default")
        self.assertEqual(
            fields["translation.review_model"]["effective_value"],
            "vendor/model",
        )
        self.assertEqual(set(fields["runtime.device"]["choices"]), DEVICES)
        self.assertEqual(len(view["fields"]), 12)

    def test_describe_derives_a_missing_model_root(self):
        self.write(config_text(self.source, self.work, None))
        view = settings_io.describe({}, self.config)
        fields = {item["key"]: item for item in view["fields"]}
        self.assertEqual(fields["model_root"]["source"], "derived")
        self.assertEqual(
            fields["model_root"]["effective_value"],
            str(self.work / "models"),
        )
        self.assertEqual(fields["asr_model"]["source"], "derived")

    def test_api_key_configured_reads_the_effective_variable(self):
        view = settings_io.describe({}, self.config)
        self.assertFalse(view["api_key_configured"])
        view = settings_io.describe({"OPENAI_API_KEY": "secret"}, self.config)
        self.assertTrue(view["api_key_configured"])

    def test_invalid_file_reports_the_loader_error_and_keeps_the_bytes(self):
        broken = "[paths]\nsource_root = 1\n"
        self.write(broken)
        view = settings_io.describe({}, self.config)
        self.assertFalse(view["settings_ok"])
        self.assertTrue(view["load_error"])
        self.assertEqual(
            view["base_sha256"],
            hashlib.sha256(broken.encode("utf-8")).hexdigest(),
        )
        self.assertTrue(all(item["file_value"] is None for item in view["fields"]))

    def test_missing_file_hashes_the_empty_document(self):
        missing = self.root / "missing.toml"
        view = settings_io.describe({}, missing)
        self.assertFalse(view["exists"])
        self.assertTrue(view["settings_ok"])
        self.assertEqual(view["base_sha256"], hashlib.sha256(b"").hexdigest())

    def test_missing_config_ignores_a_decoy_in_the_working_directory(self):
        missing = self.root / "not-there.toml"
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "subgen.toml").write_text(
            '[paths]\nsource_root = "/decoy/source"\n', encoding="utf-8"
        )
        previous = os.getcwd()
        os.chdir(decoy)
        try:
            view = settings_io.describe({}, missing)
        finally:
            os.chdir(previous)
        self.assertFalse(view["exists"])
        self.assertTrue(view["settings_ok"])
        fields = {item["key"]: item for item in view["fields"]}
        self.assertEqual(fields["source_root"]["source"], "default")
        self.assertNotEqual(fields["source_root"]["effective_value"], "/decoy/source")


class ValidateTests(SettingsFixture):
    def test_missing_file_preview_is_canonical(self):
        missing = self.root / "new.toml"
        result = settings_io.validate_draft(
            values(self.source, self.work, self.model, self.asr),
            environ={},
            config_path=missing,
        )
        self.assertTrue(result.valid, result.errors)
        self.assertTrue(result.preview.startswith(settings_io.CANONICAL_HEADER))
        self.assertIn("[paths]", result.preview)
        self.assertIn("[translation]", result.preview)
        self.assertEqual(result.warnings, ())

    def test_missing_root_is_rejected(self):
        result = settings_io.validate_draft(
            values(self.source / "missing", self.work, self.model),
            environ={},
            config_path=self.config,
        )
        self.assertFalse(result.valid)
        self.assertIn("paths.source_root", result.errors)

    def test_symlinked_root_is_not_canonical(self):
        link = self.root / "source-link"
        link.symlink_to(self.source, target_is_directory=True)
        result = settings_io.validate_draft(
            values(link, self.work, self.model),
            environ={},
            config_path=self.config,
        )
        self.assertFalse(result.valid)
        self.assertIn("canonical", result.errors["paths.source_root"])

    def test_relative_root_gets_a_field_error(self):
        result = settings_io.validate_draft(
            values(self.source, self.work, self.model)
            | {"paths.source_root": "relative/videos"},
            environ={},
            config_path=self.config,
        )
        self.assertFalse(result.valid)
        self.assertIn("paths.source_root", result.errors)

    def test_partial_provider_is_attributed_to_three_fields(self):
        result = settings_io.validate_draft(
            values(self.source, self.work, self.model)
            | {"translation.model": "", "translation.revision": ""},
            environ={},
            config_path=self.config,
        )
        self.assertFalse(result.valid)
        self.assertEqual(
            set(result.errors),
            {
                "translation.endpoint",
                "translation.model",
                "translation.revision",
            },
        )

    def test_unknown_field_is_rejected(self):
        result = settings_io.validate_draft(
            {"runtime.nope": "x"}, environ={}, config_path=self.config
        )
        self.assertFalse(result.valid)
        self.assertIn("runtime.nope", result.errors)

    def test_missing_model_directories_are_warnings(self):
        absent = self.root / "absent-model"
        result = settings_io.validate_draft(
            values(self.source, self.work, None, absent),
            environ={},
            config_path=self.config,
        )
        self.assertTrue(result.valid, result.errors)
        self.assertTrue(any("model root" in warning for warning in result.warnings))
        self.assertTrue(any("ASR model root" in warning for warning in result.warnings))

    def test_preview_matches_the_saved_text(self):
        view = settings_io.describe({}, self.config)
        draft = field_values(view)
        draft["runtime.device"] = "cpu"
        result = settings_io.validate_draft(draft, environ={}, config_path=self.config)
        self.assertTrue(result.valid, result.errors)
        settings_io.save(
            draft,
            environ={},
            config_path=self.config,
            expected_sha256=view["base_sha256"],
        )
        self.assertEqual(self.config.read_text(encoding="utf-8"), result.preview)


class SaveTests(SettingsFixture):
    def _save(self, draft: dict[str, str], **kwargs: object):
        view = settings_io.describe({}, self.config)
        return settings_io.save(
            draft,
            environ={},
            config_path=self.config,
            expected_sha256=view["base_sha256"],
            **kwargs,
        )

    def test_save_preserves_comments_order_and_mode(self):
        os.chmod(self.config, 0o640)
        before = self.config.read_bytes()
        draft = field_values(settings_io.describe({}, self.config))
        draft["runtime.device"] = "mps"
        result = self._save(draft)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("# operator comment", text)
        self.assertIn(f'source_root = "{self.source}"   # media', text)
        self.assertIn('device = "mps"', text)
        self.assertLess(text.index("[paths]"), text.index("[runtime]"))
        self.assertLess(text.index("[runtime]"), text.index("[translation]"))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertIsNotNone(result.backup)
        self.assertEqual(result.backup.read_bytes(), before)
        self.assertIn("runtime.device", result.wrote_keys)
        self.assertIn("paths.asr_model", result.omitted_keys)

    def test_save_keeps_a_trailing_comment(self):
        self.write(
            config_text(self.source, self.work, self.model).replace(
                'device = "auto"', 'device = "cpu"  # keep this'
            )
        )
        draft = field_values(settings_io.describe({}, self.config))
        draft["runtime.device"] = "cuda"
        self._save(draft)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn('device = "cuda"  # keep this', text)

    def test_save_removes_an_unset_key(self):
        draft = field_values(settings_io.describe({}, self.config))
        draft["paths.model_root"] = ""
        self._save(draft)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("model_root", text)
        from subtitle_pipeline.settings import load_settings

        loaded = load_settings(environ={}, config_path=self.config)
        self.assertEqual(loaded.model_root, self.work / "models")

    def test_save_inserts_a_new_key_in_the_existing_table(self):
        draft = field_values(settings_io.describe({}, self.config))
        draft["translation.api_key_env"] = "MY_API_KEY"
        self._save(draft)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn('api_key_env = "MY_API_KEY"', text)
        self.assertLess(
            text.index("revision ="),
            text.index("api_key_env ="),
        )
        from subtitle_pipeline.settings import load_settings

        loaded = load_settings(environ={}, config_path=self.config)
        self.assertEqual(loaded.translation.api_key_env, "MY_API_KEY")

    def test_save_appends_a_missing_table(self):
        self.write(
            f'[paths]\nsource_root = "{self.source}"\nwork_root = "{self.work}"\n'
        )
        draft = values(self.source, self.work, self.model)
        self._save(draft)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("[runtime]", text)
        self.assertIn("[translation]", text)
        self.assertLess(text.index("[paths]"), text.index("[runtime]"))
        self.assertLess(text.index("[runtime]"), text.index("[translation]"))

    def test_save_refuses_when_the_proof_fails(self):
        before = self.config.read_bytes()
        draft = field_values(settings_io.describe({}, self.config))
        with mock.patch.object(
            settings_io,
            "_prove",
            side_effect=settings_io._RewriteFailure("proof failed"),
        ):
            with self.assertRaises(RewriteError):
                self._save(draft)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.root / "subgen.toml.bak").exists())

    def test_save_force_repairs_an_invalid_file(self):
        broken = b"this is not toml\n"
        self.write("this is not toml\n")
        view = settings_io.describe({}, self.config)
        draft = values(self.source, self.work, self.model)
        with self.assertRaises(SettingsLoadError):
            settings_io.save(
                draft,
                environ={},
                config_path=self.config,
                expected_sha256=view["base_sha256"],
            )
        result = settings_io.save(
            draft,
            environ={},
            config_path=self.config,
            expected_sha256=view["base_sha256"],
            force=True,
        )
        self.assertIsNotNone(result.backup)
        self.assertEqual(result.backup.read_bytes(), broken)
        text = self.config.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(settings_io.CANONICAL_HEADER))
        self.assertIn("[paths]", text)

    def test_save_creates_a_missing_file_with_mode_0600(self):
        missing = self.root / "new.toml"
        result = settings_io.save(
            values(self.source, self.work, self.model),
            environ={},
            config_path=missing,
            expected_sha256=hashlib.sha256(b"").hexdigest(),
        )
        self.assertIsNone(result.backup)
        self.assertEqual(stat.S_IMODE(missing.stat().st_mode), 0o600)
        self.assertTrue(
            missing.read_text(encoding="utf-8").startswith(settings_io.CANONICAL_HEADER)
        )

    def test_save_rejects_a_changed_base_hash(self):
        before = self.config.read_bytes()
        with self.assertRaises(BaseHashMismatch):
            settings_io.save(
                values(self.source, self.work, self.model),
                environ={},
                config_path=self.config,
                expected_sha256="0" * 64,
            )
        self.assertEqual(self.config.read_bytes(), before)

    def test_save_rejects_an_invalid_draft(self):
        view = settings_io.describe({}, self.config)
        with self.assertRaises(DraftInvalid):
            settings_io.save(
                values(self.source / "missing", self.work, self.model),
                environ={},
                config_path=self.config,
                expected_sha256=view["base_sha256"],
            )

    def test_save_reports_omitted_keys(self):
        result = self._save(values(self.source, self.work, self.model))
        self.assertIn("translation.review_model", result.omitted_keys)
        self.assertNotIn("translation.review_model", result.wrote_keys)


class ResolveAndSnapshotTests(SettingsFixture):
    def test_resolve_config_path_prefers_explicit_then_environment(self):
        explicit = self.root / "explicit.toml"
        self.assertEqual(
            settings_io.resolve_config_path({}, explicit),
            explicit,
        )
        self.assertEqual(
            settings_io.resolve_config_path({"SUBGEN_CONFIG": str(explicit)}, None),
            explicit,
        )
        default = settings_io.resolve_config_path({}, None)
        self.assertTrue(default.is_absolute())
        self.assertEqual(default.name, "subgen.toml")

    def test_snapshot_contains_every_field(self):
        from subtitle_pipeline.settings import load_settings

        loaded = load_settings(environ={}, config_path=self.config)
        snap = settings_io.snapshot(loaded)
        self.assertEqual(set(snap), {spec.full_key for spec in settings_io.FIELD_SPECS})
        self.assertEqual(snap["runtime.device"], "auto")
        self.assertEqual(snap["translation.review_model"], "vendor/model")


if __name__ == "__main__":
    unittest.main()
