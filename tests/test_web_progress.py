from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from subtitle_pipeline.web import progress


def write_rows(path: Path, rows: list[dict]) -> int:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    path.write_text(payload, encoding="utf-8")
    return len(payload.encode("utf-8"))


class LogFixture(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.path = self.root / "runtime_progress.jsonl"


class VocabularyTests(unittest.TestCase):
    def test_terminal_events_match_the_stage_contract(self):
        self.assertEqual(set(progress.STAGE_TERMINAL_EVENTS), set(progress.STAGES))
        self.assertEqual(
            progress.STAGE_TERMINAL_EVENTS["inventory"],
            frozenset({"completed", "failed"}),
        )
        self.assertEqual(
            progress.STAGE_TERMINAL_EVENTS["asr"],
            frozenset({"batch_completed", "batch_deferred", "batch_failed"}),
        )
        self.assertEqual(
            progress.STAGE_TERMINAL_EVENTS["alignment"],
            frozenset({"batch_completed", "batch_incomplete", "batch_failed"}),
        )

    def test_aliases_cover_the_translation_quirk(self):
        self.assertEqual(
            progress.STAGE_ALIASES["translation_primary"],
            frozenset({"corpus_translation_primary", "primary"}),
        )
        self.assertEqual(
            progress.STAGE_ALIASES["translation_review"],
            frozenset({"corpus_translation_review", "review"}),
        )
        self.assertEqual(progress.row_stage_names("asr"), frozenset({"corpus_asr"}))


class WindowTests(LogFixture):
    def test_read_rows_reads_only_the_window(self):
        first = {"timestamp": 1.0, "stage": "extract", "event": "started"}
        second = {"timestamp": 2.0, "stage": "extract", "event": "completed"}
        write_rows(self.path, [first, second])
        boundary = len((json.dumps(first, sort_keys=True) + "\n").encode("utf-8"))
        size = self.path.stat().st_size
        self.assertEqual(progress.read_rows(self.path, 0, size), [first, second])
        self.assertEqual(progress.read_rows(self.path, boundary, size), [second])

    def test_torn_trailing_line_is_ignored(self):
        complete = {
            "timestamp": 1.0,
            "stage": "inventory",
            "event": "completed",
        }
        self.path.write_bytes(
            (json.dumps(complete) + "\n").encode("utf-8")
            + b'{"timestamp": 2.0, "stage": "inventory"'
        )
        rows = progress.read_rows(self.path, 0, self.path.stat().st_size)
        self.assertEqual(rows, [complete])
        window, next_offset = progress.read_window(self.path, 0)
        self.assertEqual(window, [complete])
        self.assertEqual(
            next_offset,
            len((json.dumps(complete) + "\n").encode("utf-8")),
        )

    def test_torn_line_is_reread_when_completed(self):
        self.path.write_text(
            '{"timestamp": 1.0, "stage": "extract", "event": "started"}\n',
            encoding="utf-8",
        )
        size = self.path.stat().st_size
        with self.path.open("ab") as stream:
            stream.write(b'{"timestamp": 2.0,')
        rows, offset = progress.read_window(self.path, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(offset, size)
        with self.path.open("ab") as stream:
            stream.write(b' "stage": "extract", "event": "completed"}\n')
        rows, offset = progress.read_window(self.path, offset)
        self.assertEqual(rows[0]["event"], "completed")
        self.assertEqual(offset, self.path.stat().st_size)

    def test_non_json_and_non_object_lines_are_skipped(self):
        payload = (
            b'{"timestamp": 1.0, "stage": "extract", "event": "started"}\n'
            b"not json\n"
            b"[1, 2, 3]\n"
            b'{"timestamp": 2.0, "stage": "extract", "event": "completed"}\n'
        )
        self.path.write_bytes(payload)
        rows = progress.read_rows(self.path, 0, len(payload))
        self.assertEqual([row["event"] for row in rows], ["started", "completed"])

    def test_missing_file_reads_empty(self):
        missing = self.root / "missing.jsonl"
        self.assertEqual(progress.read_rows(missing, 0, 10), [])
        self.assertEqual(progress.read_window(missing, 0), ([], 0))
        self.assertIsNone(progress.read_last_row(missing))

    def test_max_bytes_advances_to_a_line_boundary(self):
        rows = [
            {"timestamp": float(index), "stage": "extract", "event": "started"}
            for index in range(3)
        ]
        write_rows(self.path, rows)
        first_line = len((json.dumps(rows[0], sort_keys=True) + "\n").encode("utf-8"))
        parsed, offset = progress.read_window(self.path, 0, max_bytes=first_line + 2)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(offset, first_line)


class AttributionTests(LogFixture):
    def test_extract_without_run_id_uses_the_window(self):
        rows = [
            {
                "timestamp": 1.0,
                "stage": "extract",
                "event": "batch_started",
                "selected": 3,
            },
            {
                "timestamp": 2.0,
                "stage": "extract",
                "event": "completed",
                "video_id": "a" * 64,
            },
            {
                "timestamp": 3.0,
                "stage": "extract",
                "event": "skipped_existing",
                "video_id": "b" * 64,
            },
            {
                "timestamp": 4.0,
                "stage": "extract",
                "event": "skipped_no_audio",
                "video_id": "c" * 64,
            },
            {
                "timestamp": 5.0,
                "stage": "extract",
                "event": "batch_completed",
                "completed": 1,
                "existing": 1,
                "no_audio": 1,
                "failed": 0,
            },
        ]
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        self.assertIsNone(progress.stage_run_id(parsed, "extract"))
        self.assertEqual(
            progress.terminal_counters(parsed, "extract"),
            {"completed": 1, "existing": 1, "no_audio": 1, "failed": 0},
        )
        live = progress.live_counts(parsed, "extract")
        self.assertEqual(live["done"], 3)
        self.assertEqual(live["total"], 3)
        self.assertEqual(live["failed"], 0)
        self.assertFalse(live["exact"])

    def test_inventory_without_run_id(self):
        rows = [
            {"timestamp": 1.0, "stage": "inventory", "event": "started"},
            {
                "timestamp": 2.0,
                "stage": "inventory",
                "event": "discovered",
                "count": 4,
            },
            {
                "timestamp": 3.0,
                "stage": "inventory",
                "event": "observed",
                "video_id": "a" * 64,
            },
            {
                "timestamp": 4.0,
                "stage": "inventory",
                "event": "observed",
                "video_id": "b" * 64,
            },
            {
                "timestamp": 5.0,
                "stage": "inventory",
                "event": "completed",
                "count": 4,
                "manifest": "state/runtime_inventory.json",
                "source_digest": "abc",
                "snapshot_digest": "def",
            },
        ]
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        live = progress.live_counts(parsed, "inventory")
        self.assertEqual((live["done"], live["total"]), (2, 4))
        self.assertEqual(progress.terminal_counters(parsed, "inventory"), {"count": 4})

    def test_translation_aliases_are_attributed(self):
        rows = [
            {
                "timestamp": 1.0,
                "stage": "primary",
                "event": "video_completed",
                "run_id": "run-1",
                "video_id": "a" * 64,
            },
            {
                "timestamp": 2.0,
                "stage": "primary",
                "event": "video_existing",
                "run_id": "run-1",
                "video_id": "b" * 64,
            },
            {
                "timestamp": 3.0,
                "stage": "corpus_translation_primary",
                "event": "batch_started",
                "run_id": "run-1",
                "selected": 3,
                "processable": 2,
            },
            {
                "timestamp": 4.0,
                "stage": "corpus_translation_primary",
                "event": "batch_completed",
                "run_id": "run-1",
                "selected": 3,
                "completed": 1,
                "existing": 1,
            },
        ]
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        self.assertEqual(len(progress.stage_rows(parsed, "translation_primary")), 4)
        self.assertEqual(progress.stage_run_id(parsed, "translation_primary"), "run-1")
        live = progress.live_counts(parsed, "translation_primary")
        self.assertEqual((live["done"], live["total"]), (2, 2))
        self.assertEqual(
            progress.terminal_counters(parsed, "translation_primary"),
            {"selected": 3, "completed": 1, "existing": 1},
        )

    def test_last_terminal_row_wins(self):
        rows = [
            {
                "timestamp": 1.0,
                "stage": "corpus_asr",
                "event": "batch_completed",
                "completed": 1,
            },
            {
                "timestamp": 2.0,
                "stage": "corpus_asr",
                "event": "batch_failed",
                "completed": 1,
                "failed": 1,
            },
        ]
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        self.assertEqual(
            progress.terminal_counters(parsed, "asr"),
            {"completed": 1, "failed": 1},
        )

    def test_no_terminal_row_means_no_counters(self):
        rows = [
            {
                "timestamp": 1.0,
                "stage": "corpus_alignment",
                "event": "video_started",
            }
        ]
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        self.assertIsNone(progress.terminal_counters(parsed, "alignment"))

    def test_terminal_counters_drop_metadata_and_non_integers(self):
        rows = [
            {
                "timestamp": 1.0,
                "stage": "corpus_publish",
                "event": "batch_incomplete",
                "run_id": "run",
                "selected": 10,
                "completed": 7,
                "target_collision": 1,
                "failed": 2,
                "source_root": "/videos",
                "progress": "state/runtime_progress.jsonl",
                "note": "text",
                "ok": True,
            }
        ]
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        self.assertEqual(
            progress.terminal_counters(parsed, "publish"),
            {
                "selected": 10,
                "completed": 7,
                "target_collision": 1,
                "failed": 2,
            },
        )

    def test_live_counts_for_asr_alignment_and_publish(self):
        asr_rows = [
            {
                "timestamp": 1.0,
                "stage": "corpus_asr",
                "event": "batch_started",
                "selected": 10,
                "processable": 8,
            },
            {
                "timestamp": 2.0,
                "stage": "corpus_asr",
                "event": "video_completed",
                "video_id": "a" * 64,
            },
            {
                "timestamp": 3.0,
                "stage": "corpus_asr",
                "event": "skipped_verified",
                "video_id": "b" * 64,
            },
            {
                "timestamp": 4.0,
                "stage": "corpus_asr",
                "event": "video_failed_quality",
                "video_id": "c" * 64,
            },
            {
                "timestamp": 5.0,
                "stage": "corpus_asr",
                "event": "deferred_missing_audio",
                "video_id": "d" * 64,
            },
        ]
        write_rows(self.path, asr_rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        live = progress.live_counts(parsed, "asr")
        self.assertEqual((live["done"], live["total"], live["failed"]), (2, 8, 2))

        publish_rows = [
            {
                "timestamp": 1.0,
                "stage": "corpus_publish",
                "event": "batch_started",
                "selected": 5,
            },
            {
                "timestamp": 2.0,
                "stage": "corpus_publish",
                "event": "video_completed",
                "video_id": "a" * 64,
            },
            {
                "timestamp": 3.0,
                "stage": "corpus_publish",
                "event": "video_target_collision",
                "video_id": "b" * 64,
            },
            {
                "timestamp": 4.0,
                "stage": "corpus_publish",
                "event": "video_missing_candidate",
                "video_id": "c" * 64,
            },
        ]
        write_rows(self.path, publish_rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        live = progress.live_counts(parsed, "publish")
        self.assertEqual((live["done"], live["total"], live["failed"]), (1, 5, 2))

        alignment_rows = [
            {
                "timestamp": 1.0,
                "stage": "corpus_alignment",
                "event": "batch_started",
                "selected": 9,
                "processable": 6,
            },
            {
                "timestamp": 2.0,
                "stage": "corpus_alignment",
                "event": "video_completed",
                "video_id": "a" * 64,
            },
        ]
        write_rows(self.path, alignment_rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        live = progress.live_counts(parsed, "alignment")
        self.assertEqual((live["done"], live["total"]), (1, 6))

    def test_video_events_are_limited_and_only_per_video(self):
        rows = [
            {
                "timestamp": float(index),
                "stage": "extract",
                "event": "completed",
                "video_id": f"{index:064x}",
            }
            for index in range(45)
        ]
        rows.append(
            {
                "timestamp": 99.0,
                "stage": "extract",
                "event": "batch_completed",
                "completed": 45,
            }
        )
        write_rows(self.path, rows)
        parsed = progress.read_rows(self.path, 0, self.path.stat().st_size)
        events = progress.video_events(parsed, "extract", 40)
        self.assertEqual(len(events), 40)
        self.assertEqual(events[-1]["video_id"], f"{44:064x}")
        self.assertNotIn("batch_completed", [item["event"] for item in events])

    def test_read_last_row_returns_the_newest_object(self):
        rows = [
            {"timestamp": 1.0, "stage": "extract", "event": "started"},
            {"timestamp": 2.0, "stage": "extract", "event": "completed"},
        ]
        write_rows(self.path, rows)
        last = progress.read_last_row(self.path)
        self.assertEqual(last["event"], "completed")
        with self.path.open("ab") as stream:
            stream.write(b'{"timestamp": 3.0')
        last = progress.read_last_row(self.path)
        self.assertEqual(last["event"], "completed")
        self.assertEqual(progress.file_size(self.path), self.path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
