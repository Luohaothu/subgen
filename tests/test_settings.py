from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from subtitle_pipeline.settings import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_DEVICE,
    SettingsError,
    TranslationProvider,
    load_settings,
    resolve_device,
)


class SettingsTests(unittest.TestCase):
    def test_defaults_keep_the_original_linux_layout(self):
        settings = load_settings(environ={})
        self.assertEqual(settings.source_root, Path("/home/leo/nas/Other/abc"))
        self.assertEqual(
            settings.work_root,
            Path("/home/leo/.local/share/abc-subtitles-recovery-6"),
        )
        self.assertEqual(
            settings.model_root,
            settings.work_root / "models",
        )
        self.assertEqual(
            settings.asr_model_path,
            settings.model_root / "Fun-ASR-Nano-2512",
        )
        self.assertEqual(settings.device, DEFAULT_DEVICE)
        self.assertEqual(settings.asr_engine, "auto")
        self.assertIsNone(settings.translation)

    def test_environment_overrides_paths_and_devices(self):
        settings = load_settings(
            environ={
                "SUBGEN_SOURCE_ROOT": "/media/videos",
                "SUBGEN_WORK_ROOT": "/var/tmp/subgen",
                "SUBGEN_MODEL_ROOT": "/opt/models",
                "SUBGEN_ASR_MODEL": "/opt/models/asr",
                "SUBGEN_DEVICE": "mps",
                "SUBGEN_ASR_ENGINE": "funasr",
            }
        )
        self.assertEqual(settings.source_root, Path("/media/videos"))
        self.assertEqual(settings.work_root, Path("/var/tmp/subgen"))
        self.assertEqual(settings.model_root, Path("/opt/models"))
        self.assertEqual(settings.asr_model_path, Path("/opt/models/asr"))
        self.assertEqual(settings.device, "mps")
        self.assertEqual(settings.asr_engine, "funasr")

    def test_toml_file_supplies_values_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "subgen.toml"
            path.write_text(
                "\n".join(
                    (
                        "[paths]",
                        'source_root = "/srv/videos"',
                        'work_root = "/srv/subgen"',
                        "",
                        "[runtime]",
                        'device = "cpu"',
                        "",
                        "[translation]",
                        'endpoint = "http://127.0.0.1:8080/v1"',
                        'model = "local/model"',
                        'revision = "v1"',
                        'review_model = "local/reviewer"',
                        'review_revision = "v2"',
                    )
                ),
                encoding="utf-8",
            )
            settings = load_settings(
                environ={"SUBGEN_DEVICE": "cuda"}, config_path=path
            )
        self.assertEqual(settings.source_root, Path("/srv/videos"))
        self.assertEqual(settings.work_root, Path("/srv/subgen"))
        self.assertEqual(settings.model_root, Path("/srv/subgen/models"))
        self.assertEqual(settings.device, "cuda")
        self.assertEqual(
            settings.translation,
            TranslationProvider(
                "http://127.0.0.1:8080/v1",
                "local/model",
                "v1",
                review_model="local/reviewer",
                review_revision="v2",
            ),
        )
        self.assertEqual(settings.translation.api_key_env, DEFAULT_API_KEY_ENV)

    def test_review_provider_defaults_to_the_primary_model(self):
        provider = TranslationProvider("http://127.0.0.1:8080/v1", "model", "v1")
        self.assertEqual(provider.review_model, "model")
        self.assertEqual(provider.review_revision, "v1")

    def test_partial_translation_provider_is_rejected(self):
        with self.assertRaises(SettingsError):
            load_settings(
                environ={
                    "SUBGEN_TRANSLATION_ENDPOINT": "http://127.0.0.1:8080/v1",
                }
            )

    def test_unknown_section_and_key_are_rejected(self):
        for body in ("[unknown]\nkey = 1\n", "[runtime]\nunknown = 1\n"):
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "subgen.toml"
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(SettingsError):
                    load_settings(environ={}, config_path=path)

    def test_invalid_device_and_relative_root_are_rejected(self):
        with self.assertRaises(SettingsError):
            load_settings(environ={"SUBGEN_DEVICE": "tpu"})
        with self.assertRaises(SettingsError):
            load_settings(environ={"SUBGEN_SOURCE_ROOT": "relative/videos"})

    def test_resolve_device_honors_explicit_choices_and_auto(self):
        self.assertEqual(resolve_device("cpu"), "cpu")
        self.assertEqual(resolve_device("cuda"), "cuda")
        self.assertEqual(resolve_device("mps"), "mps")
        self.assertIn(resolve_device("auto"), {"cpu", "cuda", "mps"})
