import json
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from subtitle_pipeline.alignment import JapaneseCue, timestamp_digest
from subtitle_pipeline.corpus_draft_migration_cli import migrate_exact_draft
from subtitle_pipeline.translation_runner import (
    ESCALATION_MODEL_ID,
    PRIMARY_MODEL_ID,
    ModelConfig,
    _text_digest,
    extract_glossary,
)
from subtitle_pipeline.translation_runtime_cli import (
    OpenAICompatibleClient,
    TranslationRuntimeError,
    _glossary_from_dict,
    _load_alignment,
    _verified_draft,
    run_primary,
    run_review,
)


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, tuple):
            content, finish_reason = response
        else:
            content = response
            finish_reason = None
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content),
                    finish_reason=finish_reason,
                )
            ]
        )


class FakeClient:
    def __init__(self, responses):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(responses))


class TranslationRuntimeCliTests(unittest.TestCase):
    def test_verified_draft_replays_cue_specific_quality_rule(self):
        video_id = "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        cue_id = f"{video_id}-seg-00000300-cue-0000"
        source = (JapaneseCue(cue_id, 100, 421, "あめんさん"),)
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        client = FakeClient(
            [
                glossary,
                json.dumps(
                    {"cues": [{"id": "s00000300c0000", "zh": "Amen-san"}]},
                    ensure_ascii=False,
                ),
            ]
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": 1,
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue_id,
                                "start_ms": 100,
                                "end_ms": 421,
                                "text": "あめんさん",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = run_primary(
                alignment,
                video_id=video_id,
                output_dir=root / "output",
                client=client,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=20,
                max_tokens=2048,
                work_root=root,
            )

            verified = _verified_draft(
                Path(summary["draft_json"]),
                source,
                video_id=video_id,
            )

        self.assertEqual(verified.texts[cue_id], "……")

    def test_exact_draft_migration_is_atomic_audited_and_verifiable(self):
        video_id = "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        cue_id = f"{video_id}-seg-00000300-cue-0000"
        source = (JapaneseCue(cue_id, 100, 421, "あめんさん"),)
        glossary = json.dumps(
            {"version": "1", "entries": [], "relationships": [], "register": "口语"},
            ensure_ascii=False,
        )
        client = FakeClient(
            [glossary, '{"cues":[{"id":"s00000300c0000","zh":"Amen-san"}]}']
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": 1,
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue_id,
                                "start_ms": 100,
                                "end_ms": 421,
                                "text": "あめんさん",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = run_primary(
                alignment,
                video_id=video_id,
                output_dir=root / "output",
                client=client,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=20,
                max_tokens=2048,
                work_root=root,
            )
            draft = Path(summary["draft_json"])
            value = json.loads(draft.read_text(encoding="utf-8"))
            artifact = value["blocks"][0]["primary_artifact"]
            artifact["texts"] = [[cue_id, "Amen-san"]]
            artifact["output_text_digest"] = _text_digest(((cue_id, "Amen-san"),))
            artifact["attempts"][-1]["message"] = ""
            artifact["attempts"][-1]["raw"] = json.dumps(
                {"cues": [{"id": cue_id, "zh": "Amen-san"}]},
                separators=(",", ":"),
            )
            value["cues"][0]["draft_zh"] = "Amen-san"
            draft.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

            dry_run = migrate_exact_draft(
                draft, alignment, root, video_id=video_id, apply=False
            )
            self.assertEqual(dry_run["status"], "migratable")
            self.assertEqual(
                json.loads(draft.read_text(encoding="utf-8"))["cues"][0]["draft_zh"],
                "Amen-san",
            )
            applied = migrate_exact_draft(
                draft, alignment, root, video_id=video_id, apply=True
            )
            verified = _verified_draft(draft, source, video_id=video_id)

        self.assertEqual(
            applied, {"status": "migrated", "message": "", "corrections": 1}
        )
        self.assertEqual(verified.texts[cue_id], "……")
        evidence = json.loads(verified.artifacts[0].attempts[-1].message)
        self.assertIn("Amen-san", evidence["original_raw"])

    def test_stored_glossary_replays_compact_model_evidence_ids(self):
        video_id = "a" * 64
        source = (
            JapaneseCue(f"{video_id}-seg-00000001-cue-0000", 0, 500, "一花です。"),
            JapaneseCue(f"{video_id}-seg-00000002-cue-0000", 500, 1000, "一花ですよ。"),
        )
        raw = json.dumps(
            {
                "version": "1",
                "entries": [
                    {
                        "category": "name",
                        "ja": "一花",
                        "preferred_zh": "一花",
                        "aliases": [],
                        "evidence_ids": ["s00000001c0000", "s00000002c0000"],
                        "explicit": False,
                    }
                ],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        config = ModelConfig.primary(revision="primary-rev", max_tokens=2048)
        artifact = extract_glossary(FakeClient([raw]), source, config)
        stored = json.loads(json.dumps(asdict(artifact), ensure_ascii=False))
        restored = _glossary_from_dict(stored, source)
        self.assertEqual(restored, artifact)

    def test_alignment_filters_only_small_tsu_after_original_digest_validation(self):
        source = (
            JapaneseCue("c1", 0, 500, "っ！"),
            JapaneseCue("c2", 500, 1000, "あっ！"),
            JapaneseCue("c3", 1000, 1500, "ん"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"

            def write_alignment(digest: str) -> None:
                alignment.write_text(
                    json.dumps(
                        {
                            "failure_count": 0,
                            "cue_count": len(source),
                            "timestamp_digest": digest,
                            "cues": [
                                {
                                    "cue_id": cue.cue_id,
                                    "start_ms": cue.start_ms,
                                    "end_ms": cue.end_ms,
                                    "text": cue.text,
                                }
                                for cue in source
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            write_alignment(timestamp_digest(source))
            self.assertEqual(_load_alignment(alignment, root), source[1:])

            write_alignment(timestamp_digest(source[1:]))
            with self.assertRaisesRegex(
                TranslationRuntimeError, "timestamp digest differs"
            ):
                _load_alignment(alignment, root)

    def test_two_pass_smoke_preserves_ids_and_timestamps(self):
        source = (
            JapaneseCue("c1", 100, 900, "こんにちは。"),
            JapaneseCue("c2", 1200, 2200, "続けてください。"),
        )
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        primary = FakeClient(
            [
                glossary,
                json.dumps(
                    {
                        "cues": [
                            {"id": "c1", "zh": "你好。"},
                            {"id": "c2", "zh": "请继续。"},
                        ]
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review = FakeClient(
            [
                json.dumps(
                    {
                        "cues": [
                            {"id": "c1", "zh": "你好。"},
                            {"id": "c2", "zh": "请接着说。"},
                        ]
                    },
                    ensure_ascii=False,
                )
            ]
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": len(source),
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue.cue_id,
                                "start_ms": cue.start_ms,
                                "end_ms": cue.end_ms,
                                "text": cue.text,
                            }
                            for cue in source
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            primary_summary = run_primary(
                alignment,
                video_id="smoke-1",
                output_dir=root / "output",
                client=primary,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=120,
                max_tokens=2048,
                work_root=root,
            )
            self.assertFalse((root / "output" / "smoke-1.zh.srt").exists())
            draft_value = json.loads(
                Path(primary_summary["draft_json"]).read_text(encoding="utf-8")
            )
            draft_value["glossary_artifact"]["glossary"]["register"] = "被篡改"
            tampered = root / "tampered-draft.json"
            tampered.write_text(
                json.dumps(draft_value, ensure_ascii=False), encoding="utf-8"
            )
            guard_client = FakeClient(
                ['{"cues":[{"id":"c1","zh":"你好。"},{"id":"c2","zh":"继续。"}]}']
            )
            with self.assertRaises(TranslationRuntimeError):
                run_review(
                    alignment,
                    tampered,
                    video_id="smoke-1",
                    output_dir=root / "output",
                    client=guard_client,
                    model=ESCALATION_MODEL_ID,
                    revision="review-rev",
                    max_tokens=2048,
                    video_duration_ms=3000,
                    work_root=root,
                )
            self.assertEqual(guard_client.chat.completions.calls, [])
            summary = run_review(
                alignment,
                Path(primary_summary["draft_json"]),
                video_id="smoke-1",
                output_dir=root / "output",
                client=review,
                model=ESCALATION_MODEL_ID,
                revision="review-rev",
                max_tokens=2048,
                video_duration_ms=3000,
                work_root=root,
            )

            evidence = json.loads(
                Path(summary["translation_json"]).read_text(encoding="utf-8")
            )
            srt = Path(summary["srt"]).read_text(encoding="utf-8")
            self.assertEqual(
                [
                    (row["id"], row["start_ms"], row["end_ms"], row["zh"])
                    for row in evidence["cues"]
                ],
                [
                    ("c1", 100, 900, "你好。"),
                    ("c2", 1200, 2200, "请接着说。"),
                ],
            )
            self.assertEqual(evidence["timestamp_digest"], timestamp_digest(source))
            self.assertIn("00:00:00,100 --> 00:00:00,900", srt)
            self.assertIn("00:00:01,200 --> 00:00:02,200", srt)
            self.assertEqual(len(primary.chat.completions.calls), 2)
            self.assertEqual(len(review.chat.completions.calls), 1)
            review_payload = json.loads(
                review.chat.completions.calls[0]["messages"][1]["content"]
            )
            self.assertEqual(
                review_payload["DRAFT_CUES"],
                [
                    {"id": "c1", "zh": "你好。"},
                    {"id": "c2", "zh": "请继续。"},
                ],
            )
            self.assertEqual(list((root / "output").glob("*.part")), [])
            self.assertEqual(evidence["primary"]["model"], PRIMARY_MODEL_ID)
            self.assertEqual(evidence["review"]["model"], ESCALATION_MODEL_ID)

    def test_primary_resumes_from_atomic_block_checkpoint(self):
        source = (
            JapaneseCue("c1", 0, 500, "你好。"),
            JapaneseCue("c2", 500, 1000, "继续。"),
        )
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": len(source),
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue.cue_id,
                                "start_ms": cue.start_ms,
                                "end_ms": cue.end_ms,
                                "text": cue.text,
                            }
                            for cue in source
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            failed = FakeClient(
                [
                    glossary,
                    '{"cues":[{"id":"c1","zh":"你好。"}]}',
                    "bad 1",
                    "bad 2",
                    "bad 3",
                    "bad 4",
                    "bad 5",
                ]
            )
            with self.assertRaisesRegex(TranslationRuntimeError, "primary block 2"):
                run_primary(
                    alignment,
                    video_id="resume-1",
                    output_dir=root / "output",
                    client=failed,
                    model=PRIMARY_MODEL_ID,
                    revision="primary-rev",
                    commit_size=1,
                    max_tokens=2048,
                    work_root=root,
                )
            checkpoint = root / "output" / "resume-1.primary.checkpoint.json"
            self.assertTrue(checkpoint.is_file())
            resumed = FakeClient(['{"cues":[{"id":"c2","zh":"继续。"}]}'])
            summary = run_primary(
                alignment,
                video_id="resume-1",
                output_dir=root / "output",
                client=resumed,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=1,
                max_tokens=2048,
                work_root=root,
            )
            self.assertEqual(len(resumed.chat.completions.calls), 1)
            self.assertFalse(checkpoint.exists())
            draft = json.loads(Path(summary["draft_json"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [row["draft_zh"] for row in draft["cues"]],
                ["你好。", "继续。"],
            )

    def test_primary_retries_only_a_block_as_single_cues_after_batch_failure(self):
        source = (
            JapaneseCue("c1", 0, 500, "こんにちは。"),
            JapaneseCue("c2", 500, 1000, "続けて。"),
        )
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        responses = [
            glossary,
            "bad 1",
            "bad 2",
            "bad 3",
            "bad 4",
            "bad 5",
            '{"cues":[{"id":"c1","zh":"你好。"}]}',
            '{"cues":[{"id":"c2","zh":"继续。"}]}',
        ]
        client = FakeClient(responses)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": len(source),
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue.cue_id,
                                "start_ms": cue.start_ms,
                                "end_ms": cue.end_ms,
                                "text": cue.text,
                            }
                            for cue in source
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = run_primary(
                alignment,
                video_id="single-fallback",
                output_dir=root / "output",
                client=client,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=2,
                max_tokens=2048,
                work_root=root,
            )
            self.assertEqual(len(client.chat.completions.calls), 8)
            self.assertTrue(
                all(
                    call["max_tokens"] == 1024
                    for call in client.chat.completions.calls[-2:]
                )
            )
            draft = json.loads(Path(summary["draft_json"]).read_text(encoding="utf-8"))
            self.assertEqual(draft["commit_size"], 2)
            self.assertEqual(
                [row["draft_zh"] for row in draft["cues"]],
                ["你好。", "继续。"],
            )
            attempts = draft["blocks"][0]["primary_artifact"]["attempts"]
            self.assertEqual(
                [attempt["code"] for attempt in attempts],
                [
                    "invalid_json",
                    "invalid_json",
                    "invalid_json",
                    "invalid_json",
                    "ok",
                ],
            )

    def test_review_resumes_from_atomic_block_checkpoint(self):
        source = (
            JapaneseCue("c1", 0, 500, "こんにちは。"),
            JapaneseCue("c2", 500, 1000, "続けて。"),
        )
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": len(source),
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue.cue_id,
                                "start_ms": cue.start_ms,
                                "end_ms": cue.end_ms,
                                "text": cue.text,
                            }
                            for cue in source
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            primary = FakeClient(
                [
                    glossary,
                    '{"cues":[{"id":"c1","zh":"你好。"}]}',
                    '{"cues":[{"id":"c2","zh":"继续。"}]}',
                ]
            )
            primary_summary = run_primary(
                alignment,
                video_id="review-resume-1",
                output_dir=root / "output",
                client=primary,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=1,
                max_tokens=2048,
                work_root=root,
            )
            failed = FakeClient(
                [
                    '{"cues":[{"id":"c1","zh":"你好。"}]}',
                    "bad 1",
                    "bad 2",
                    "bad 3",
                    "bad 4",
                    "bad 5",
                ]
            )
            with self.assertRaisesRegex(TranslationRuntimeError, "review block 2"):
                run_review(
                    alignment,
                    Path(primary_summary["draft_json"]),
                    video_id="review-resume-1",
                    output_dir=root / "output",
                    client=failed,
                    model=ESCALATION_MODEL_ID,
                    revision="review-rev",
                    max_tokens=2048,
                    video_duration_ms=2000,
                    work_root=root,
                )
            checkpoint = root / "output" / "review-resume-1.review.checkpoint.json"
            self.assertTrue(checkpoint.is_file())
            resumed = FakeClient(['{"cues":[{"id":"c2","zh":"请继续。"}]}'])
            summary = run_review(
                alignment,
                Path(primary_summary["draft_json"]),
                video_id="review-resume-1",
                output_dir=root / "output",
                client=resumed,
                model=ESCALATION_MODEL_ID,
                revision="review-rev",
                max_tokens=2048,
                video_duration_ms=2000,
                work_root=root,
            )
            self.assertEqual(len(resumed.chat.completions.calls), 1)
            self.assertFalse(checkpoint.exists())
            final = json.loads(
                Path(summary["translation_json"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [row["zh"] for row in final["cues"]],
                ["你好。", "请继续。"],
            )

    def test_review_parallelizes_blocks_but_commits_evidence_in_source_order(self):
        source = (
            JapaneseCue("c1", 0, 500, "こんにちは。"),
            JapaneseCue("c2", 500, 1000, "続けて。"),
            JapaneseCue("c3", 1000, 1500, "終わります。"),
        )
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )

        class ParallelCompletions:
            def __init__(self) -> None:
                self.barrier = threading.Barrier(3)
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.calls: list[dict[str, object]] = []

            def create(self, **kwargs):
                payload = json.loads(kwargs["messages"][1]["content"])
                cue_id = payload["DRAFT_CUES"][0]["id"]
                with self.lock:
                    self.calls.append(kwargs)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    self.barrier.wait(timeout=2)
                    translated = {
                        "c1": "你好。",
                        "c2": "请继续。",
                        "c3": "结束了。",
                    }[cue_id]
                    content = json.dumps(
                        {"cues": [{"id": cue_id, "zh": translated}]},
                        ensure_ascii=False,
                    )
                    return types.SimpleNamespace(
                        choices=[
                            types.SimpleNamespace(
                                message=types.SimpleNamespace(content=content),
                                finish_reason=None,
                            )
                        ]
                    )
                finally:
                    with self.lock:
                        self.active -= 1

        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": len(source),
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue.cue_id,
                                "start_ms": cue.start_ms,
                                "end_ms": cue.end_ms,
                                "text": cue.text,
                            }
                            for cue in source
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            primary = FakeClient(
                [
                    glossary,
                    '{"cues":[{"id":"c1","zh":"你好。"}]}',
                    '{"cues":[{"id":"c2","zh":"继续。"}]}',
                    '{"cues":[{"id":"c3","zh":"结束。"}]}',
                ]
            )
            primary_summary = run_primary(
                alignment,
                video_id="parallel-review",
                output_dir=root / "output",
                client=primary,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=1,
                max_tokens=2048,
                work_root=root,
            )
            completions = ParallelCompletions()
            review_client = types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=completions)
            )
            with ThreadPoolExecutor(max_workers=3) as executor:
                summary = run_review(
                    alignment,
                    Path(primary_summary["draft_json"]),
                    video_id="parallel-review",
                    output_dir=root / "output",
                    client=review_client,
                    model=ESCALATION_MODEL_ID,
                    revision="review-rev",
                    max_tokens=2048,
                    video_duration_ms=2000,
                    work_root=root,
                    block_executor=executor,
                )

            final = json.loads(
                Path(summary["translation_json"]).read_text(encoding="utf-8")
            )
            self.assertEqual(completions.max_active, 3)
            self.assertEqual(
                [row["commit_ids"] for row in final["blocks"]],
                [["c1"], ["c2"], ["c3"]],
            )
            self.assertEqual(
                [row["zh"] for row in final["cues"]],
                ["你好。", "请继续。", "结束了。"],
            )

    def test_review_retries_length_exhaustion_as_single_cues(self):
        source = (
            JapaneseCue("c1", 0, 500, "こんにちは。"),
            JapaneseCue("c2", 500, 1000, "続けて。"),
        )
        glossary = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "自然口语",
            },
            ensure_ascii=False,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "failure_count": 0,
                        "cue_count": len(source),
                        "timestamp_digest": timestamp_digest(source),
                        "cues": [
                            {
                                "cue_id": cue.cue_id,
                                "start_ms": cue.start_ms,
                                "end_ms": cue.end_ms,
                                "text": cue.text,
                            }
                            for cue in source
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            primary = FakeClient(
                [
                    glossary,
                    '{"cues":[{"id":"c1","zh":"你好。"},{"id":"c2","zh":"继续。"}]}',
                ]
            )
            primary_summary = run_primary(
                alignment,
                video_id="review-length-1",
                output_dir=root / "output",
                client=primary,
                model=PRIMARY_MODEL_ID,
                revision="primary-rev",
                commit_size=2,
                max_tokens=2048,
                work_root=root,
            )
            review = FakeClient(
                [("{", "length")] * 5
                + [
                    '{"cues":[{"id":"c1","zh":"你好。"}]}',
                    '{"cues":[{"id":"c2","zh":"请继续。"}]}',
                ]
            )
            summary = run_review(
                alignment,
                Path(primary_summary["draft_json"]),
                video_id="review-length-1",
                output_dir=root / "output",
                client=review,
                model=ESCALATION_MODEL_ID,
                revision="review-rev",
                max_tokens=2048,
                video_duration_ms=2000,
                work_root=root,
            )
            self.assertEqual(len(review.chat.completions.calls), 7)
            self.assertTrue(
                all(
                    call["max_tokens"] == 1024
                    for call in review.chat.completions.calls[-2:]
                )
            )
            final = json.loads(
                Path(summary["translation_json"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [row["zh"] for row in final["cues"]],
                ["你好。", "请继续。"],
            )
            attempts = final["blocks"][0]["review_artifact"]["attempts"]
            self.assertEqual(
                [attempt["code"] for attempt in attempts],
                ["invalid_json", "invalid_json", "invalid_json", "invalid_json", "ok"],
            )

    def test_openai_compatible_client_posts_chat_completions(self):
        observed = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        def opener(request, *, timeout):
            observed["url"] = request.full_url
            observed["headers"] = dict(request.header_items())
            observed["payload"] = json.loads(request.data)
            observed["timeout"] = timeout
            return Response()

        client = OpenAICompatibleClient(
            "http://127.0.0.1:8000/v1",
            api_key="secret",
            timeout=12.5,
            opener=opener,
        )
        response = client.chat.completions.create(
            model="model",
            messages=[],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        self.assertEqual(observed["url"], "http://127.0.0.1:8000/v1/chat/completions")
        self.assertEqual(observed["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(observed["payload"]["model"], "model")
        self.assertNotIn("extra_body", observed["payload"])
        self.assertEqual(
            observed["payload"]["chat_template_kwargs"],
            {"enable_thinking": False},
        )
        self.assertEqual(observed["timeout"], 12.5)
        self.assertEqual(response.choices[0].message.content, "ok")


if __name__ == "__main__":
    unittest.main()
