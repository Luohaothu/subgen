from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_pipeline.align_runner import AlignmentResult, AlignmentTask
from subtitle_pipeline.alignment import AlignedToken, JapaneseCue
from subtitle_pipeline.corpus_alignment_cli import (
    AlignerPool,
    _process_record,
    _reviewed_alignment_transcript,
    _reviewed_window_alignment_override,
    build_parser,
    run_corpus,
)
from subtitle_pipeline.domain import VideoRecord
from subtitle_pipeline.segments import SpeechSegment


class CorpusAlignmentCliTests(unittest.TestCase):
    def test_reviewed_window_consensus_recovers_lexical_overlong_alignment(self):
        video_id = "d1d431be669328ac93db8f7d1297d8d31a2f2dcfd1feb46cedbdd37077be72f3"
        segment_id = f"{video_id}-seg-00000436"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                436,
                2_534_830,
                2_551_270,
                2_534_530,
                2_551_570,
            ),
            Path("reviewed-window.wav"),
            "やばい。",
            0,
        )
        primary = AlignmentResult(
            segment_id,
            "やばい。",
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.tokens, (AlignedToken("やばい。", 2_000, 2_320),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(2_536_530, 2_536_850, "やばい。")],
        )

    def test_reviewed_asr_window_timestamps_recover_repeated_lexical_speech(self):
        video_id = "134e1b3ed61f136be211594e4bd67ad557d8efa1a01c62e046ecdb39712fc3ee"
        segment_id = f"{video_id}-seg-00001847"
        transcript = "はいはいはいはいはい…"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                1847,
                9_388_850,
                9_402_660,
                9_388_550,
                9_402_960,
            ),
            Path("reviewed-asr-window.wav"),
            transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.tokens, (AlignedToken(transcript, 0, 1_080),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(9_388_850, 9_389_630, transcript)],
        )
        self.assertEqual(
            result.raw["evidence"]["owner_clipped_cue_absolute_ms"],
            (9_388_850, 9_389_630),
        )

    def test_reviewed_multiscale_windows_correct_transcript_and_timing(self):
        video_id = "d4cfd630e718e3ba40e36039aa22dbff56332b3757060759facc8302b20b2280"
        segment_id = f"{video_id}-seg-00001304"
        source_transcript = "ああ、ああ、だめ、あい"
        corrected = "ああ、舐め、あい、舐め、あ、あい、くっ…"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                1304,
                6_587_040,
                6_599_970,
                6_586_740,
                6_600_270,
            ),
            Path("reviewed-multiscale-window.wav"),
            source_transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            source_transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.transcript, corrected)
        self.assertEqual(result.tokens, (AlignedToken(corrected, 180, 3_540),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(6_587_040, 6_590_280, corrected)],
        )
        self.assertEqual(
            result.raw["evidence"]["method"],
            "funasr_nano_multiscale_overlapping_window_timestamps",
        )
        self.assertEqual(
            _reviewed_alignment_transcript(segment_id, source_transcript),
            corrected,
        )
        self.assertEqual(
            _reviewed_alignment_transcript(segment_id, source_transcript + "。"),
            source_transcript + "。",
        )

    def test_reviewed_windows_correct_left_clipped_lexical_decode(self):
        video_id = "5c639610e56801ad02cd06395f1cf4c9ff22273bb4d375b248f2641fe3b3403a"
        segment_id = f"{video_id}-seg-00000288"
        source_transcript = (
            "あ、うんうんうん、あ、あ、すご、咥える、うん、うん、あ、あ、あ、あ、あ"
        )
        corrected = "ああ、うんうんうんうんうん。ああ、すごく入る……んんっ……"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                288,
                2_002_300,
                2_011_580,
                2_002_000,
                2_011_880,
            ),
            Path("reviewed-left-clipped-window.wav"),
            source_transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            source_transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.transcript, corrected)
        self.assertEqual(result.tokens, (AlignedToken(corrected, 60, 6_200),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(2_002_300, 2_008_200, corrected)],
        )
        self.assertEqual(
            result.raw["evidence"]["lexical_consensus_absolute_ms"],
            (2_005_420, 2_006_860),
        )

    def test_reviewed_windows_locate_same_222_lexical_phrase(self):
        video_id = "b1263d4bb9c3e4b65e40091710454ef6d06ea1e6927bb76fb5d0cdd0b4a6a2ce"
        segment_id = f"{video_id}-seg-00000253"
        transcript = "もう一回、ああ、じゃあ、尻文字してみます。"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                253,
                1_706_020,
                1_729_840,
                1_705_720,
                1_730_140,
            ),
            Path("reviewed-same-222-window.wav"),
            transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.transcript, transcript)
        self.assertEqual(result.tokens, (AlignedToken(transcript, 10_440, 13_020),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(1_716_160, 1_718_740, transcript)],
        )
        self.assertEqual(
            result.raw["evidence"]["neighboring_exact_window_span_ms"],
            (1_716_180, 1_718_700),
        )

    def test_reviewed_multiscale_windows_correct_start_214_lexical_speech(self):
        video_id = "1075c66605e6933e5929870e4c34f998a6315f19358b2f25f6801253afc14006"
        segment_id = f"{video_id}-seg-00000731"
        source_transcript = "あ、そう。あ、そうね、あ、そうね"
        corrected = "ああ、ごめん、ごめん。ああ、そうね。"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                731,
                2_943_620,
                2_956_080,
                2_943_320,
                2_956_380,
            ),
            Path("reviewed-start-214-window.wav"),
            source_transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            source_transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.transcript, corrected)
        self.assertEqual(result.tokens, (AlignedToken(corrected, 3_000, 6_750),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(2_946_320, 2_950_070, corrected)],
        )
        self.assertEqual(
            result.raw["evidence"]["gomen_consensus_absolute_ms"],
            (2_946_320, 2_947_320),
        )
        self.assertEqual(
            _reviewed_alignment_transcript(segment_id, source_transcript),
            corrected,
        )

    def test_reviewed_multiscale_windows_correct_juq_688_repetition_hallucination(self):
        video_id = "51584edadd2729c96b726cebd784f757b9c0c5911e784674274019d459e8b53d"
        segment_id = f"{video_id}-seg-00001366"
        source_transcript = (
            "はい、はい、はい、はい、はい、はい、はい、はい、はい、はい。"
        )
        corrected = "ああ、そう。"
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                1366,
                6_158_050,
                6_158_610,
                6_157_750,
                6_158_910,
            ),
            Path("reviewed-juq-688-window.wav"),
            source_transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            source_transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "aligned token 5 is outside waveform bounds",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.transcript, corrected)
        self.assertEqual(result.tokens, (AlignedToken(corrected, 200, 900),))
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [(6_158_050, 6_158_610, corrected)],
        )
        self.assertEqual(
            result.raw["evidence"]["cross_scale_exact_window_intersection_absolute_ms"],
            (6_157_950, 6_158_650),
        )
        self.assertEqual(
            _reviewed_alignment_transcript(segment_id, source_transcript),
            corrected,
        )

    def test_reviewed_multiscale_windows_split_ssni_439_recovered_speech(self):
        video_id = "a6882b2fca91f6d2d5b7f6d6a65d4ffb1cbe7a226ad80e8fabbf36e44b823fe7"
        segment_id = f"{video_id}-seg-00001506"
        source_transcript = "あ、あ、あ、あ、はいはいはいはいはい、あ、あ、あ、あ"
        corrected = (
            "ああ、なるほど。ありがとうございます。ありがとうございます。"
            "すごい。よし。うん、うん、うん、うん。はい、はい、はい、はい。"
        )
        task = AlignmentTask(
            SpeechSegment(
                segment_id,
                1506,
                6_319_140,
                6_333_230,
                6_318_840,
                6_333_530,
            ),
            Path("reviewed-ssni-439-window.wav"),
            source_transcript,
            0,
        )
        primary = AlignmentResult(
            segment_id,
            source_transcript,
            (),
            (),
            {"phase": "normalize"},
            {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        )

        result = _reviewed_window_alignment_override(task, primary)

        self.assertIsNotNone(result)
        self.assertEqual(result.error, None)
        self.assertEqual(result.transcript, corrected)
        self.assertEqual(
            result.tokens,
            (
                AlignedToken("ああ、なるほど。", 0, 1_750),
                AlignedToken(
                    "ありがとうございます。ありがとうございます。", 1_750, 4_500
                ),
                AlignedToken("すごい。よし。", 4_500, 5_500),
                AlignedToken("うん、うん、うん、うん。", 5_500, 7_500),
                AlignedToken("はい、はい、はい、はい。", 7_500, 9_500),
            ),
        )
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues],
            [
                (6_319_140, 6_320_590, "ああ、なるほど。"),
                (
                    6_320_590,
                    6_323_340,
                    "ありがとうございます。ありがとうございます。",
                ),
                (6_323_340, 6_324_340, "すごい。よし。"),
                (6_324_340, 6_326_340, "うん、うん、うん、うん。"),
                (6_326_340, 6_328_340, "はい、はい、はい、はい。"),
            ],
        )
        self.assertEqual(
            result.raw["evidence"]["trailing_region_without_stable_lexical_content_ms"],
            (9_500, 14_690),
        )
        self.assertEqual(
            _reviewed_alignment_transcript(segment_id, source_transcript),
            corrected,
        )

    def _record(
        self,
        video_id: str,
        *,
        target_exists: bool = False,
        audio_stream_index: int | None = 0,
    ) -> VideoRecord:
        return VideoRecord(
            video_id=video_id,
            relative_path=f"source/{video_id}.mp4",
            source_bytes=123,
            duration_ms=1_000,
            audio_stream_index=audio_stream_index,
            audio_offset_ms=0 if audio_stream_index is not None else None,
            target_relative_path=f"source/{video_id}.zh.srt",
            target_exists=target_exists,
            source_mtime_ns=456,
            probe_sha256="f" * 64,
        )

    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            work_root=root,
            aligner_model=SimpleNamespace(
                model_id="Qwen/Qwen3-ForcedAligner-0.6B-hf",
                revision="e" * 40,
                local_path=root / "models/aligner",
            ),
            stage_settings=SimpleNamespace(
                extraction=SimpleNamespace(duration_tolerance_ms=25),
                alignment=SimpleNamespace(batch_size=2),
            ),
        )

    def _write_inputs(self, root: Path, record: VideoRecord, texts: list[str]) -> None:
        output_dir = root / "asr/corpus" / record.video_id
        output_dir.mkdir(parents=True, exist_ok=True)
        segments = []
        results = []
        width = record.duration_ms // len(texts)
        for index, text in enumerate(texts):
            start = index * width
            end = (index + 1) * width
            segment_id = f"{record.video_id}-seg-{index:08d}"
            segments.append(
                {
                    "segment_id": segment_id,
                    "index": index,
                    "owner_start_ms": start,
                    "owner_end_ms": end,
                    "decode_start_ms": start,
                    "decode_end_ms": end,
                }
            )
            results.append(
                {
                    "segment_id": segment_id,
                    "text": text,
                    "raw": {"text": text},
                    "error": (
                        None if text else {"type": "quality_gate", "flags": ["empty"]}
                    ),
                }
            )
        payload = {
            "video_id": record.video_id,
            "relative_path": record.relative_path,
            "duration_ms": record.duration_ms,
            "audio_offset_ms": record.audio_offset_ms,
            "segment_count": len(segments),
            "failure_count": 0,
            "empty_omission_count": sum(not text for text in texts),
            "quality_omission_count": 0,
            "segments": segments,
            "results": results,
        }
        (output_dir / "asr.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        audio_dir = root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / f"{record.video_id}.flac").write_bytes(b"fake-flac")

    @staticmethod
    def _materialize(_flac: Path, segments: list, out_dir: Path) -> dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for segment in segments:
            path = out_dir / f"{segment.segment_id}.wav"
            path.touch()
            paths[segment.segment_id] = path
        return paths

    @staticmethod
    def _align(
        _processor: object, _model: object, tasks: list
    ) -> list[AlignmentResult]:
        rows = []
        for task in tasks:
            start = task.segment.owner_start_ms + task.audio_offset_ms + 10
            end = start + 100
            rows.append(
                AlignmentResult(
                    task.segment.segment_id,
                    task.transcript,
                    (AlignedToken(task.transcript, 10, 110),),
                    (
                        JapaneseCue(
                            f"{task.segment.segment_id}-cue-0000",
                            start,
                            end,
                            task.transcript,
                        ),
                    ),
                    {"ok": True},
                    None,
                )
            )
        return rows

    def test_aligner_pool_loads_once_for_multiple_videos_and_batches(self):
        video_ids = ("a" * 64, "b" * 64)
        calls = {"load": 0, "batches": []}

        def create():
            calls["load"] += 1
            return object(), object()

        def align(
            processor: object, model: object, tasks: list
        ) -> list[AlignmentResult]:
            calls["batches"].append([task.segment.segment_id for task in tasks])
            return self._align(processor, model, tasks)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            records = [self._record(video_id) for video_id in video_ids]
            for record in records:
                self._write_inputs(root, record, ["一", "二", "三"])
            pool = AlignerPool(create)
            progress: list[dict[str, object]] = []
            with (
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._verify_audio",
                    return_value=1_000,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.materialize_wav_batch",
                    side_effect=self._materialize,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.align_with_retries",
                    side_effect=align,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._append_progress",
                    side_effect=lambda _config, row: progress.append(row),
                ),
            ):
                statuses = [
                    _process_record(
                        config,
                        record,
                        batch_size=2,
                        model_pool=pool,
                        run_id="run-1",
                    )
                    for record in records
                ]

            self.assertEqual(statuses, ["completed", "completed"])
            self.assertEqual(calls["load"], 1)
            self.assertEqual([len(batch) for batch in calls["batches"]], [2, 1, 2, 1])
            for record in records:
                artifact = root / "asr/corpus" / record.video_id / "alignment.json"
                payload = json.loads(artifact.read_text(encoding="utf-8"))
                self.assertEqual(payload["stage"], "corpus_alignment")
                self.assertEqual(payload["schema_version"], "corpus-alignment-v1")
                self.assertEqual(payload["failure_count"], 0)
                self.assertEqual(payload["aligned_segment_count"], 3)
                self.assertEqual(payload["cue_count"], 3)
                self.assertIn("asr_sha256", payload["provenance"])
                self.assertFalse(list(artifact.parent.glob("*.part")))
            self.assertEqual(
                sum(
                    row.get("event") == "alignment_batch_completed" for row in progress
                ),
                4,
            )

    def test_quality_omission_is_audited_but_not_forced_aligned(self):
        video_id = "e" * 64
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            record = self._record(video_id)
            self._write_inputs(root, record, ["はぁ" * 40, "会話です"])
            asr_path = root / "asr/corpus" / video_id / "asr.json"
            payload = json.loads(asr_path.read_text(encoding="utf-8"))
            payload["results"][0]["error"] = {
                "type": "quality_gate",
                "flags": ["abnormal_repetition"],
            }
            payload["quality_omission_count"] = 1
            asr_path.write_text(json.dumps(payload), encoding="utf-8")

            with (
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._verify_audio",
                    return_value=1_000,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.materialize_wav_batch",
                    side_effect=self._materialize,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.align_with_retries",
                    side_effect=self._align,
                ),
            ):
                status = _process_record(
                    config,
                    record,
                    batch_size=2,
                    model_pool=AlignerPool(lambda: (object(), object())),
                    run_id="run-quality-omission",
                )

            alignment = json.loads(
                (root / "asr/corpus" / video_id / "alignment.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status, "completed")
            self.assertEqual(alignment["quality_omission_count"], 1)
            self.assertEqual(alignment["aligned_segment_count"], 1)

    def test_failed_qwen_result_uses_verified_funasr_timestamp_fallback(self):
        video_id = "f" * 64
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            record = self._record(video_id)
            self._write_inputs(root, record, ["会話です。"])
            asr_path = root / "asr/corpus" / video_id / "asr.json"
            payload = json.loads(asr_path.read_text(encoding="utf-8"))
            payload["results"][0]["raw"]["timestamps"] = [
                {"token": "会話", "start_time": 0.1, "end_time": 0.5},
                {"token": "です", "start_time": 0.5, "end_time": 0.8},
                {"token": "。", "start_time": 0.8, "end_time": 0.9},
            ]
            asr_path.write_text(json.dumps(payload), encoding="utf-8")

            def fail_alignment(
                _processor: object, _model: object, tasks: list
            ) -> list[AlignmentResult]:
                return [
                    AlignmentResult(
                        task.segment.segment_id,
                        task.transcript,
                        (),
                        (),
                        {"phase": "normalize", "decoded": []},
                        {"type": "alignment_validation", "message": "collapsed"},
                    )
                    for task in tasks
                ]

            with (
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._verify_audio",
                    return_value=1_000,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.materialize_wav_batch",
                    side_effect=self._materialize,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.align_with_retries",
                    side_effect=fail_alignment,
                ),
            ):
                status = _process_record(
                    config,
                    record,
                    batch_size=2,
                    model_pool=AlignerPool(lambda: (object(), object())),
                    run_id="run-funasr-fallback",
                )

            alignment = json.loads(
                (root / "asr/corpus" / video_id / "alignment.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status, "completed")
            self.assertEqual(alignment["failure_count"], 0)
            self.assertEqual(
                alignment["results"][0]["raw"]["phase"], "funasr_timestamp_fallback"
            )
            self.assertEqual(
                "".join(token["text"] for token in alignment["results"][0]["tokens"]),
                "会話です。",
            )
            self.assertEqual(
                alignment["results"][0]["raw"]["primary_alignment"]["error"]["message"],
                "collapsed",
            )

    def test_runtime_repetition_sanitization_trims_prefix_and_omits_pure_tail(self):
        video_id = "d" * 64
        prefix = "妊娠とかしちゃうかもしんないし。"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            record = self._record(video_id)
            self._write_inputs(
                root,
                record,
                [
                    prefix + "うん、" * 30,
                    "あっ、" * 30,
                    "うん" * 15 + "違うんじゃないですか。",
                    "ちょっと何ですよ。" + "痛い" * 40 + "痛" * 7,
                    "会話です",
                ],
            )

            aligned_transcripts: list[str] = []

            def align(
                processor: object, model: object, tasks: list
            ) -> list[AlignmentResult]:
                aligned_transcripts.extend(task.transcript for task in tasks)
                return self._align(processor, model, tasks)

            with (
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._verify_audio",
                    return_value=1_000,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.materialize_wav_batch",
                    side_effect=self._materialize,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.align_with_retries",
                    side_effect=align,
                ),
            ):
                status = _process_record(
                    config,
                    record,
                    batch_size=2,
                    model_pool=AlignerPool(lambda: (object(), object())),
                    run_id="run-runtime-repetition",
                )

            alignment = json.loads(
                (root / "asr/corpus" / video_id / "alignment.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status, "completed")
            self.assertEqual(
                aligned_transcripts,
                [
                    prefix,
                    "違うんじゃないですか。",
                    "ちょっと何ですよ。",
                    "会話です",
                ],
            )
            self.assertEqual(alignment["quality_omission_count"], 1)
            self.assertEqual(alignment["aligned_segment_count"], 4)

    def test_valid_result_resumes_but_tampered_result_is_rebuilt(self):
        video_id = "c" * 64
        loads = 0

        def create():
            nonlocal loads
            loads += 1
            return object(), object()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            record = self._record(video_id)
            self._write_inputs(root, record, ["こんにちは"])
            pool = AlignerPool(create)
            patches = (
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._verify_audio",
                    return_value=1_000,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.materialize_wav_batch",
                    side_effect=self._materialize,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.align_with_retries",
                    side_effect=self._align,
                ),
                patch("subtitle_pipeline.corpus_alignment_cli._append_progress"),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                first = _process_record(
                    config, record, batch_size=8, model_pool=pool, run_id="run-1"
                )
                second = _process_record(
                    config, record, batch_size=8, model_pool=pool, run_id="run-2"
                )
                artifact = root / "asr/corpus" / video_id / "alignment.json"
                payload = json.loads(artifact.read_text(encoding="utf-8"))
                payload["timestamp_digest"] = "0" * 64
                artifact.write_text(json.dumps(payload), encoding="utf-8")
                third = _process_record(
                    config, record, batch_size=8, model_pool=pool, run_id="run-3"
                )

            self.assertEqual(
                (first, second, third), ("completed", "existing", "completed")
            )
            self.assertEqual(loads, 1)
            repaired = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertNotEqual(repaired["timestamp_digest"], "0" * 64)

    def test_run_corpus_skips_existing_targets_and_no_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            records = (
                self._record("d" * 64, target_exists=True),
                self._record("e" * 64, audio_stream_index=None),
            )
            snapshot = SimpleNamespace(records=records, snapshot_digest="1" * 64)
            progress: list[dict[str, object]] = []
            with (
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._runtime_config",
                    return_value=config,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._read_manifest",
                    return_value=snapshot,
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli.create_aligner",
                    side_effect=AssertionError("model must stay lazy"),
                ),
                patch(
                    "subtitle_pipeline.corpus_alignment_cli._append_progress",
                    side_effect=lambda _config, row: progress.append(row),
                ),
            ):
                summary = run_corpus(
                    batch_size=2,
                    model_dir=Path("models/aligner"),
                    limit=None,
                    video_ids=(),
                )

            self.assertEqual(summary["target_exists"], 1)
            self.assertEqual(summary["no_audio"], 1)
            self.assertEqual(summary["missing_asr"], 0)
            self.assertEqual(summary["completed"], 0)
            self.assertEqual(progress[-1]["event"], "batch_completed")

    def test_parser_defaults_to_alignment_runtime_settings(self):
        args = build_parser().parse_args([])

        self.assertEqual(args.batch_size, 16)
        self.assertEqual(args.model_dir, Path("models/Qwen3-ForcedAligner-0.6B-hf"))
        self.assertIsNone(args.limit)


if __name__ == "__main__":
    unittest.main()
