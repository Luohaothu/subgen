import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from subtitle_pipeline import platform_io
from subtitle_pipeline.config import PipelineConfig
from subtitle_pipeline.domain import stable_video_id
from subtitle_pipeline.media import parse_probe
from subtitle_pipeline.state import (
    InventorySnapshot,
    InventoryValidationError,
    SourceChangedError,
    discover_video_paths,
    observe_video,
    validate_initial_inventory,
)
from tests.media_fixture import write_sample_media


def probe_payload(*, audio: bool = True, duration: str = "2.0") -> dict:
    streams = [{"index": 0, "codec_type": "video", "start_time": "0"}]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "sample_rate": "48000",
                "start_time": "0",
                "duration": duration,
                "tags": {"language": "jpn"},
            }
        )
    return {"format": {"duration": duration, "start_time": "0"}, "streams": streams}


def completed_probe(payload: dict):
    def probe(_source, **_kwargs):
        return payload

    return probe


class InventoryStateTests(unittest.TestCase):
    def make_config(
        self,
        root: Path,
        *,
        expected: int,
        no_audio: tuple[str, ...] = (),
        collisions: tuple[str, ...] = (),
    ) -> PipelineConfig:
        source = root / "source"
        work = root / "work"
        source.mkdir()
        work.mkdir()
        return PipelineConfig.for_testing(
            source_root=source,
            work_root=work,
            expected_video_count=expected,
            known_no_audio_paths=no_audio,
            known_collision_paths=collisions,
        )

    def test_observation_uses_the_pyav_probe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            write_sample_media(config.source_root / "sample.mkv", seconds=0.5)
            record = observe_video(config, "sample.mkv")
            self.assertEqual(record.audio_stream_index, 1)
            self.assertEqual(record.duration_ms, 500)
            self.assertIsNotNone(record.probe_sha256)

    def test_legacy_video_record_and_parse_probe_remain_compatible(self):
        record = parse_probe("a.mp4", 10, probe_payload(), False)
        self.assertEqual(
            (
                record.relative_path,
                record.source_bytes,
                record.audio_stream_index,
                record.target_relative_path,
                record.target_exists,
            ),
            ("a.mp4", 10, 1, "a.zh.srt", False),
        )
        old = type(record)("id", "a.mp4", 10, 2000, 1, 0, "a.zh.srt", False)
        self.assertIsNone(old.source_mtime_ns)

    def test_scan_is_sorted_filtered_and_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=2)
            (config.source_root / "B.MP4").write_bytes(b"b")
            (config.source_root / "skip.txt").write_bytes(b"x")
            sub = config.source_root / "sub"
            sub.mkdir()
            (sub / "a.mkv").write_bytes(b"a")
            outside = root / "outside"
            outside.mkdir()
            (outside / "hidden.mp4").write_bytes(b"h")
            (config.source_root / "linked-dir").symlink_to(
                outside, target_is_directory=True
            )
            (config.source_root / "linked.mp4").symlink_to(outside / "hidden.mp4")
            self.assertEqual(discover_video_paths(config), ("B.MP4", "sub/a.mkv"))

    def test_scan_close_failures_preserve_primary_and_are_typed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            sub = config.source_root / "sub"
            sub.mkdir()
            (sub / "inside.mp4").write_bytes(b"inside")
            real_close = os.close
            real_scandir = os.scandir

            def close_then_fail(fd):
                real_close(fd)
                raise OSError("injected close failure")

            with mock.patch(
                "subtitle_pipeline.state.os.close", side_effect=close_then_fail
            ):
                with self.assertRaisesRegex(InventoryValidationError, "close"):
                    discover_video_paths(config)

            scan_calls = 0

            def child_scan_failure(fd):
                nonlocal scan_calls
                scan_calls += 1
                if scan_calls == 2:
                    raise OSError("injected child scan failure")
                return real_scandir(fd)

            with (
                mock.patch(
                    "subtitle_pipeline.state.os.scandir",
                    side_effect=child_scan_failure,
                ),
                mock.patch(
                    "subtitle_pipeline.state.os.close",
                    side_effect=close_then_fail,
                ),
            ):
                with self.assertRaisesRegex(
                    InventoryValidationError, "cannot scan source directory sub"
                ) as captured:
                    discover_video_paths(config)
            self.assertTrue(
                any("close failure" in note for note in captured.exception.__notes__)
            )

    def test_scan_does_not_follow_directory_swapped_to_symlink_during_descent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            parent = config.source_root / "sub"
            parent.mkdir()
            (parent / "inside.mp4").write_bytes(b"inside")
            outside = root / "outside"
            outside.mkdir()
            (outside / "escaped.mp4").write_bytes(b"outside")
            detached = config.source_root / "detached"
            real_scandir = os.scandir
            swapped = False

            def racing_scandir(path):
                nonlocal swapped
                if not swapped and not isinstance(path, int) and Path(path) == parent:
                    parent.rename(detached)
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return real_scandir(path)

            with mock.patch(
                "subtitle_pipeline.state.os.scandir", side_effect=racing_scandir
            ):
                self.assertEqual(
                    discover_video_paths(config),
                    ("sub/inside.mp4",),
                )

    def test_observation_binds_probe_to_one_stat_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            source = config.source_root / "movie.mp4"
            source.write_bytes(b"video")

            def mutating_probe(_source, **_kwargs):
                with source.open("ab") as stream:
                    stream.write(b"changed")
                return probe_payload()

            with self.assertRaises(SourceChangedError):
                observe_video(config, "movie.mp4", probe=mutating_probe)

    def test_observation_probes_open_descriptor_across_pathname_aba(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            source = config.source_root / "movie.mp4"
            original_bytes = b"original"
            replacement_bytes = b"replacement"
            source.write_bytes(original_bytes)
            held = config.source_root / "held.mp4"
            observed_bytes = []
            probe_paths = []

            def aba_probe(source_path, **_kwargs):
                source.rename(held)
                source.write_bytes(replacement_bytes)
                try:
                    observed_bytes.append(Path(source_path).read_bytes())
                    probe_paths.append(str(source_path))
                finally:
                    source.unlink()
                    held.rename(source)
                duration = "2.0" if observed_bytes[-1] == original_bytes else "9.0"
                return probe_payload(duration=duration)

            record = observe_video(config, "movie.mp4", probe=aba_probe)
            self.assertEqual(observed_bytes, [original_bytes])
            self.assertEqual(record.source_bytes, len(original_bytes))
            self.assertEqual(record.duration_ms, 2_000)
            self.assertTrue(probe_paths[0].startswith(str(platform_io.fd_directory())))

    def test_observation_rejects_each_stat_identity_race(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            source = config.source_root / "movie.mp4"
            source.write_bytes(b"video")
            fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
            observed = os.stat(source, follow_symlinks=False)
            baseline = {
                "st_mode": observed.st_mode,
                "st_size": observed.st_size,
                "st_mtime_ns": observed.st_mtime_ns,
                "st_dev": observed.st_dev,
                "st_ino": observed.st_ino,
            }
            for field in fields:
                with self.subTest(field=field):
                    changed = {**baseline, field: baseline[field] + 1}
                    observations = iter(
                        (SimpleNamespace(**baseline), SimpleNamespace(**changed))
                    )

                    def stat_fn(_path, *, follow_symlinks):
                        self.assertFalse(follow_symlinks)
                        return next(observations)

                    with self.assertRaises(SourceChangedError):
                        observe_video(
                            config,
                            "movie.mp4",
                            probe=completed_probe(probe_payload()),
                            stat_fn=stat_fn,
                        )

    def test_observation_rejects_parent_symlink_swap_with_same_source_inode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            parent = config.source_root / "sub"
            parent.mkdir()
            source = parent / "movie.mp4"
            source.write_bytes(b"video")
            outside = root / "outside"
            outside.mkdir()
            os.link(source, outside / "movie.mp4")

            def swapping_probe(_source, **_kwargs):
                parent.rename(config.source_root / "detached")
                parent.symlink_to(outside, target_is_directory=True)
                return probe_payload()

            with self.assertRaises(SourceChangedError):
                observe_video(config, "sub/movie.mp4", probe=swapping_probe)

    def test_target_observation_is_bound_to_the_same_source_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            parent = config.source_root / "sub"
            parent.mkdir()
            source = parent / "movie.mp4"
            source.write_bytes(b"video")
            outside = root / "outside"
            outside.mkdir()
            os.link(source, outside / "movie.mp4")

            def swapping_lexists(_target):
                parent.rename(config.source_root / "detached")
                parent.symlink_to(outside, target_is_directory=True)
                return False

            with self.assertRaises(SourceChangedError):
                observe_video(
                    config,
                    "sub/movie.mp4",
                    probe=completed_probe(probe_payload()),
                    lexists_fn=swapping_lexists,
                )

    def test_broken_target_symlink_is_a_live_collision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=1)
            (config.source_root / "movie.mp4").write_bytes(b"video")
            (config.source_root / "movie.zh.srt").symlink_to(
                config.source_root / "missing.srt"
            )
            record = observe_video(
                config,
                "movie.mp4",
                probe=completed_probe(probe_payload()),
            )
            self.assertTrue(record.target_exists)
            self.assertIsNotNone(record.source_mtime_ns)
            self.assertIsNotNone(record.source_device)
            self.assertIsNotNone(record.source_inode)

    def test_source_and_snapshot_digests_have_separate_invalidation_domains(self):
        record = parse_probe(
            "a.mp4",
            10,
            probe_payload(),
            False,
            source_mtime_ns=20,
            source_device=30,
            source_inode=40,
            inventory_schema_version="inventory-v1",
        )
        first = InventorySnapshot.build((record,), schema_version="inventory-v1")
        collision = InventorySnapshot.build(
            (replace(record, target_exists=True),), schema_version="inventory-v1"
        )
        self.assertEqual(first.source_digest, collision.source_digest)
        self.assertNotEqual(first.snapshot_digest, collision.snapshot_digest)

        changed = (
            replace(record, source_bytes=11),
            replace(record, source_mtime_ns=21),
            replace(record, source_device=31),
            replace(record, source_inode=41),
            replace(record, probe_sha256="f" * 64),
            replace(record, duration_ms=2_001),
            replace(record, audio_stream_index=2),
            replace(record, audio_codec="opus"),
        )
        for candidate in changed:
            with self.subTest(candidate=candidate):
                snapshot = InventorySnapshot.build(
                    (candidate,), schema_version="inventory-v1"
                )
                self.assertNotEqual(first.source_digest, snapshot.source_digest)

    def test_snapshot_codec_rejects_unsorted_duplicate_and_forged_evidence(self):
        one = parse_probe(
            "a.mp4",
            10,
            probe_payload(),
            False,
            source_mtime_ns=20,
            source_device=30,
            source_inode=40,
            inventory_schema_version="inventory-v1",
        )
        two = replace(
            one,
            video_id=stable_video_id("b.mp4"),
            relative_path="b.mp4",
            target_relative_path="b.zh.srt",
            source_inode=41,
        )
        snapshot = InventorySnapshot.build((one, two), schema_version="inventory-v1")
        self.assertEqual(InventorySnapshot.from_dict(snapshot.to_dict()), snapshot)

        unsorted = snapshot.to_dict()
        unsorted["records"] = list(reversed(unsorted["records"]))
        with self.assertRaisesRegex(ValueError, "sorted"):
            InventorySnapshot.from_dict(unsorted)
        forged = snapshot.to_dict()
        forged["source_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source digest"):
            InventorySnapshot.from_dict(forged)
        unknown = snapshot.to_dict()
        unknown["unknown"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            InventorySnapshot.from_dict(unknown)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            InventorySnapshot.build((one, one), schema_version="inventory-v1")

    def test_small_corpus_gate_requires_exact_count_no_audio_and_collisions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(
                root,
                expected=2,
                no_audio=("no-audio.mp4",),
                collisions=("audio.zh.srt",),
            )
            no_audio = parse_probe(
                "no-audio.mp4",
                10,
                probe_payload(audio=False),
                False,
                source_mtime_ns=20,
                source_device=30,
                source_inode=40,
                inventory_schema_version=config.schemas.inventory,
            )
            audio = parse_probe(
                "audio.mp4",
                11,
                probe_payload(),
                True,
                source_mtime_ns=21,
                source_device=30,
                source_inode=41,
                inventory_schema_version=config.schemas.inventory,
            )
            snapshot = InventorySnapshot.build(
                (audio, no_audio), schema_version=config.schemas.inventory
            )
            validate_initial_inventory(snapshot, config)
            wrong = InventorySnapshot.build(
                (replace(audio, target_exists=False), no_audio),
                schema_version=config.schemas.inventory,
            )
            with self.assertRaises(InventoryValidationError):
                validate_initial_inventory(wrong, config)

    def test_audio_selection_policy_participates_in_config_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root, expected=0)
            policy = config.stage_settings.extraction.audio_selection
            variants = (
                replace(policy, language_preferences=("ja",)),
                replace(policy, prefer_default_disposition=False),
                replace(policy, prefer_untagged=False),
            )
            for variant in variants:
                with self.subTest(variant=variant):
                    extraction = replace(
                        config.stage_settings.extraction,
                        audio_selection=variant,
                    )
                    changed = replace(
                        config,
                        stage_settings=replace(
                            config.stage_settings,
                            extraction=extraction,
                        ),
                    )
                    self.assertNotEqual(config.digest(), changed.digest())


if __name__ == "__main__":
    unittest.main()
