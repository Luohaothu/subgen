from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from subtitle_pipeline.config import (
    KNOWN_COLLISION_PATHS,
    KNOWN_NO_AUDIO_PATH,
    ModelBinding,
    ModelRevisions,
    PipelineConfig,
    SchemaVersions,
    StageSettings,
)


class ConfigTests(unittest.TestCase):
    def make_config(self, root: Path, **changes: object) -> PipelineConfig:
        source = root / "source"
        work = root / "work"
        source.mkdir()
        work.mkdir()
        config = PipelineConfig.for_testing(
            source_root=source,
            work_root=work,
            expected_video_count=2,
            known_no_audio_paths=("missing.mp4",),
            known_collision_paths=("existing.zh.srt",),
        )
        return dataclasses.replace(config, **changes)

    def test_config_is_deeply_frozen_and_digest_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.assertEqual(config.digest(), config.digest())
            with self.assertRaises(dataclasses.FrozenInstanceError):
                config.work_root = Path("/tmp/elsewhere")  # type: ignore[misc]
            with self.assertRaises(dataclasses.FrozenInstanceError):
                config.asr_model.revision = "changed"  # type: ignore[misc]
            self.assertIsInstance(config.video_extensions, tuple)
            changed = dataclasses.replace(
                config,
                local_capacity_reserve_bytes=config.local_capacity_reserve_bytes + 1,
            )
            self.assertNotEqual(config.digest(), changed.digest())

            with self.assertRaises(ValueError):
                dataclasses.replace(config, video_extensions=[".mp4"])
            with self.assertRaises(ValueError):
                StageSettings(extraction_worker_candidates=[1, 2])
            with self.assertRaises(ValueError):
                ModelBinding("model", config.work_root / "models/model", "unresolved")
            with self.assertRaises(ValueError):
                ModelBinding("model", config.work_root / "models/model", "abcdef0")
            with self.assertRaises(ValueError):
                SchemaVersions(inventory=["v1"])  # type: ignore[arg-type]

    @unittest.skipUnless(
        Path("/home/leo/.local/share/abc-subtitles-recovery-6").is_dir()
        and Path("/home/leo/nas/Other/abc").is_dir(),
        "production roots are not present on this host",
    )
    def test_production_config_preserves_exact_known_paths(self):
        work_root = Path("/home/leo/.local/share/abc-subtitles-recovery-6")
        config = PipelineConfig.production(
            revisions=ModelRevisions(
                vad="1" * 40,
                asr="2" * 40,
                aligner="3" * 40,
                primary_translation="4" * 40,
                fallback_translation="5" * 40,
                reviewer_translation="6" * 40,
            ),
            source_root=Path("/home/leo/nas/Other/abc"),
            work_root=work_root,
            model_root=work_root / "models",
            asr_model_path=Path(
                "/home/leo/.cache/modelscope/FunAudioLLM/Fun-ASR-Nano-2512"
            ),
        )
        self.assertEqual(config.known_no_audio_paths, (KNOWN_NO_AUDIO_PATH,))
        self.assertEqual(config.known_collision_paths, KNOWN_COLLISION_PATHS)
        self.assertEqual(config.expected_video_count, 580)

    def test_config_rejects_unowned_or_aliased_local_paths(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.assertEqual(
                config.owned_path("state/a.json"), config.work_root / "state/a.json"
            )
            for path in (
                "src/x.py",
                ".git/config",
                "../escape",
                "/tmp/absolute",
                "state//x",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    config.owned_path(path)

    def test_config_rejects_symlinked_root_ancestors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            outside = root / "outside"
            (outside / "work").mkdir(parents=True)
            nominal = root / "nominal"
            nominal.mkdir()
            (nominal / "alias").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink ancestors"):
                PipelineConfig.for_testing(
                    source_root=source,
                    work_root=nominal / "alias/work",
                    expected_video_count=0,
                )

    def test_stage_settings_digest_covers_runtime_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            settings = config.stage_settings
            fields = {
                "extraction": settings.extraction,
                "vad": settings.vad,
                "asr": settings.asr,
                "alignment": settings.alignment,
                "translation": settings.translation,
            }
            for name, value in fields.items():
                with self.subTest(name=name):
                    changed_value = dataclasses.replace(
                        value,
                        **{
                            next(
                                iter(dataclasses.asdict(value))
                            ): self._changed_first_field(value)
                        },
                    )
                    changed_settings = dataclasses.replace(
                        settings, **{name: changed_value}
                    )
                    changed_config = dataclasses.replace(
                        config, stage_settings=changed_settings
                    )
                    self.assertNotEqual(config.digest(), changed_config.digest())

    @staticmethod
    def _changed_first_field(value: object) -> object:
        first = next(iter(dataclasses.asdict(value).values()))
        if type(first) is bool:
            return not first
        if type(first) is int:
            return first + 1
        if type(first) is float:
            return first + 0.01
        if type(first) is str:
            return first + "-changed"
        if type(first) is tuple:
            return (*first, max(first) + 1)
        raise AssertionError(f"unsupported settings field: {first!r}")


if __name__ == "__main__":
    unittest.main()
