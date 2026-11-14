from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_pipeline.publish import (
    ExistingTargetError,
    PublicationIntegrityError,
    publish,
)
from subtitle_pipeline.srt import (
    Cue,
    SrtParseError,
    parse_srt,
    render_srt,
    validate_candidate,
    validate_srt,
)


def identity(value: str) -> str:
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish_checked(source: Path, target: Path) -> None:
    data = source.read_bytes()
    publish(
        source,
        target,
        expected_bytes=len(data),
        expected_sha256=digest(data),
    )


class SrtTests(unittest.TestCase):
    def test_render_parse_round_trip_is_canonical_and_multiline(self):
        cues = (
            Cue("c1", 1250, 3500, "你好。"),
            Cue("c2", 3600, 61000, "第一行\n第二行"),
        )
        text = render_srt(cues)
        self.assertEqual(
            text,
            "1\n00:00:01,250 --> 00:00:03,500\n你好。\n\n"
            "2\n00:00:03,600 --> 00:01:01,000\n第一行\n第二行\n\n",
        )
        parsed = parse_srt(text.encode("utf-8"))
        self.assertEqual(
            tuple((cue.start_ms, cue.end_ms, cue.text) for cue in parsed),
            tuple((cue.start_ms, cue.end_ms, cue.text) for cue in cues),
        )
        self.assertEqual(validate_srt(text, 62000, simplifier=identity), [])

    def test_direct_cue_values_are_strict_and_with_text_preserves_time(self):
        source = Cue("c1", 0, 800, "はい。")
        translated = source.with_text("好。")
        self.assertEqual(
            (translated.cue_id, translated.start_ms, translated.end_ms),
            ("c1", 0, 800),
        )
        for args in (
            ("", 0, 1, "好。"),
            ("c", True, 1, "好。"),
            ("c", -1, 1, "好。"),
            ("c", 0, 1, ""),
            ("c", 0, 1, "   "),
            ("c", 0, 1, "坏\x00文本"),
            ("c", 0, 1, "坏�文本"),
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    Cue(*args)

    def test_parser_rejects_invalid_utf8_bom_cr_and_noncanonical_structure(self):
        cases = (
            (b"\xff", "bad_sequence"),
            ("\ufeff1\n00:00:00,000 --> 00:00:01,000\n好。\n", "bad_sequence"),
            ("1\r00:00:00,000 --> 00:00:01,000\r好。\r", "bad_sequence"),
            ("1\n00:00:00,000 -> 00:00:01,000\n好。\n", "bad_timestamp"),
            ("1\n00:60:00,000 --> 00:00:01,000\n好。\n", "bad_timestamp"),
            ("0\n00:00:00,000 --> 00:00:01,000\n好。\n", "bad_sequence"),
            ("1١\n00:00:00,000 --> 00:00:01,000\n好。\n", "bad_sequence"),
            (
                "1\n00:00:00,000 --> 00:00:01,000\n好。\n\n"
                "3\n00:00:01,000 --> 00:00:02,000\n好。\n",
                "bad_sequence",
            ),
            ("1\n00:00:00,000 --> 00:00:01,000\n\n", "empty_text"),
            ("1\n00:00:00,000 --> 00:00:01,000\n坏\x01文本\n", "empty_text"),
            (
                "1\n00:00:00,000 --> 00:00:01,000\n好。\n\ntrailing prose",
                "bad_sequence",
            ),
            (
                "1\n00:00:00,000 --> 00:00:01,000\n好。\n"
                "2\n00:00:01,000 --> 00:00:02,000\n再见。\n",
                "bad_sequence",
            ),
            (
                "1\n00:00:00,000 --> 00:00:01,000\n"
                "2\n00:00:01,000 --> 00:00:02,000\n再见。\n",
                "bad_sequence",
            ),
            ("1\n00:00:00,000 --> 00:00:01,000\n好。\n\n\n", "bad_sequence"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(SrtParseError) as raised:
                    parse_srt(raw)
                self.assertEqual(raised.exception.code, code)

        huge_index = "1" + "0" * 5000
        with self.assertRaises(SrtParseError) as huge_error:
            parse_srt(f"{huge_index}\n00:00:00,000 --> 00:00:01,000\n好。\n")
        self.assertEqual(huge_error.exception.code, "bad_sequence")

        crlf = "1\r\n00:00:00,000 --> 00:00:01,000\r\n好。\r\n"
        self.assertEqual(parse_srt(crlf)[0].text, "好。")

    def test_semantic_and_language_flags_are_deterministic(self):
        text = (
            "1\n00:00:01,000 --> 00:00:03,000\nOkay\n\n"
            "2\n00:00:02,500 --> 00:00:05,000\nこれは繁體\n"
        )
        flags = validate_srt(
            text,
            4500,
            simplifier=lambda value: value.replace("體", "体"),
        )
        self.assertEqual(
            flags,
            [
                "overlap",
                "past_video_end",
                "kana_remaining",
                "traditional_character",
                "not_simplified_chinese",
            ],
        )

        non_positive = "1\n00:00:01,000 --> 00:00:01,000\n好。\n"
        self.assertEqual(
            validate_srt(non_positive, 2000, simplifier=identity),
            ["non_positive_duration"],
        )
        for meta in (
            "字幕",
            "字幕123",
            "待翻译",
            "待翻译2026",
            "翻译如下",
            "译文如下",
            "以下是翻译",
            "以下为译文",
            "这是翻译结果",
            "暂无中文字幕",
            "本视频暂无字幕",
            "本片无字幕",
            "此处没有字幕",
            "没有中文字幕",
            "字幕缺失",
            "无可用字幕",
            "本字幕由AI生成",
            "中文字幕 by AI",
            "12345",
        ):
            with self.subTest(meta=meta):
                candidate = f"1\n00:00:00,000 --> 00:00:01,000\n{meta}\n"
                self.assertIn(
                    "not_simplified_chinese",
                    validate_srt(candidate, 2000, simplifier=identity),
                )

        for meaningful in (
            "是。",
            "无。",
            "我来翻译这句话。",
            "自动翻译很差。",
            "这个视频的字幕很好。",
            "我没有看字幕。",
        ):
            with self.subTest(meaningful=meaningful):
                candidate = f"1\n00:00:00,000 --> 00:00:01,000\n{meaningful}\n"
                self.assertEqual(
                    validate_srt(candidate, 2000, simplifier=identity),
                    [],
                )

        outside = "1\n00:00:03,000 --> 00:00:02,000\n好。\n"
        self.assertEqual(
            validate_srt(outside, 2500, simplifier=identity),
            ["non_positive_duration", "past_video_end"],
        )

    def test_candidate_count_times_and_digest_must_equal_source(self):
        source = (
            Cue("c1", 1250, 3500, "こんにちは。"),
            Cue("c2", 3600, 5000, "さようなら。"),
        )
        good = render_srt(
            (source[0].with_text("你好。"), source[1].with_text("再见。"))
        )
        self.assertEqual(
            validate_candidate(good, source, 6000, simplifier=identity),
            [],
        )

        moved = render_srt(
            (Cue("c1", 1251, 3500, "你好。"), source[1].with_text("再见。"))
        )
        moved_flags = validate_candidate(
            moved,
            source,
            6000,
            simplifier=identity,
        )
        self.assertIn("timestamp_mismatch", moved_flags)
        self.assertIn("timestamp_digest_mismatch", moved_flags)

        short = render_srt((source[0].with_text("你好。"),))
        self.assertIn(
            "cue_cardinality_mismatch",
            validate_candidate(short, source, 6000, simplifier=identity),
        )

    def test_candidate_allows_only_source_grounded_non_han_cues(self):
        source = (
            Cue("c1", 0, 500, "ません。"),
            Cue("c2", 500, 900, "。"),
        )
        grounded = render_srt((source[0].with_text("……"), source[1].with_text("……")))

        self.assertIn(
            "not_simplified_chinese",
            validate_srt(grounded, 1000, simplifier=identity),
        )
        self.assertEqual(
            validate_candidate(grounded, source, 1000, simplifier=identity),
            [],
        )

        untranslated_source = (Cue("c3", 0, 500, "未知名。"),)
        untranslated = render_srt((untranslated_source[0].with_text("Unknown。"),))
        self.assertIn(
            "not_simplified_chinese",
            validate_candidate(
                untranslated,
                untranslated_source,
                1000,
                simplifier=identity,
            ),
        )

    def test_candidate_replays_exact_cue_translation_gate(self):
        cue_id = (
            "805fb20e2129e8527d2ba5650db0b4fcef78c38e0c1d5d0df478cadd9901e205"
            "-seg-00000577-cue-0000"
        )
        source = (Cue(cue_id, 0, 500, "かずしん"),)
        mismatched = render_srt((source[0].with_text("和信。"),))
        corrected = render_srt((source[0].with_text("Kazushin。"),))

        self.assertIn(
            "exact_cue_translation_mismatch",
            validate_candidate(mismatched, source, 1000, simplifier=identity),
        )
        self.assertEqual(
            validate_candidate(corrected, source, 1000, simplifier=identity),
            [],
        )

    def test_candidate_allows_only_source_grounded_japanese_name_glyphs(self):
        def convert_name_glyph(value):
            return value.replace("紬", "䌷").replace("學", "学")

        source = (Cue("c1", 0, 500, "紬さん。"),)
        grounded = render_srt((source[0].with_text("紬小姐。"),))
        self.assertIn(
            "traditional_character",
            validate_srt(grounded, 1000, simplifier=convert_name_glyph),
        )
        self.assertEqual(
            validate_candidate(
                grounded,
                source,
                1000,
                simplifier=convert_name_glyph,
            ),
            [],
        )

        genuine = render_srt((source[0].with_text("學校。"),))
        self.assertIn(
            "traditional_character",
            validate_candidate(
                genuine,
                source,
                1000,
                simplifier=convert_name_glyph,
            ),
        )

    def test_candidate_allows_only_source_grounded_quoted_kana(self):
        source = (
            Cue(
                "c1",
                0,
                500,
                "現代の日本語でぬというと、打ち消しの意味です。",
            ),
        )
        grounded = render_srt((source[0].with_text("在现代日语中，「ぬ」表示否定。"),))
        self.assertIn(
            "kana_remaining",
            validate_srt(grounded, 1000, simplifier=identity),
        )
        self.assertEqual(
            validate_candidate(grounded, source, 1000, simplifier=identity),
            [],
        )

        untranslated = render_srt((source[0].with_text("こんにちは。"),))
        untranslated_flags = validate_candidate(
            untranslated,
            source,
            1000,
            simplifier=identity,
        )
        self.assertIn("kana_remaining", untranslated_flags)
        self.assertIn("japanese_remaining", untranslated_flags)


class PublicationTests(unittest.TestCase):
    def candidate(self, root: Path, data: bytes):
        path = root / "candidate.srt"
        path.write_bytes(data)
        return path, len(data), hashlib.sha256(data).hexdigest()

    def test_existing_target_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate, size, digest = self.candidate(root, b"new")
            target = root / "movie.zh.srt"
            target.write_bytes(b"old")
            with self.assertRaises(ExistingTargetError):
                publish(candidate, target, expected_bytes=size, expected_sha256=digest)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse((root / "movie.zh.srt.part").exists())

    def test_publish_installs_candidate_and_removes_part(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = b"1\n00:00:00,000 --> 00:00:01,000\n\xe4\xbd\xa0\xe5\xa5\xbd\xe3\x80\x82\n"
            candidate, size, digest = self.candidate(root, data)
            target = root / "movie.zh.srt"
            publish(candidate, target, expected_bytes=size, expected_sha256=digest)
            self.assertEqual(target.read_bytes(), data)
            self.assertFalse((root / "movie.zh.srt.part").exists())

    def test_integrity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate, size, digest = self.candidate(root, b"new")
            target = root / "movie.zh.srt"
            with self.assertRaises(PublicationIntegrityError):
                publish(
                    candidate,
                    target,
                    expected_bytes=size + 1,
                    expected_sha256=digest,
                )
            self.assertFalse(target.exists())

    def test_target_created_during_copy_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate, size, digest = self.candidate(root, b"new")
            target = root / "movie.zh.srt"
            calls = 0

            def lexists(path: object) -> bool:
                nonlocal calls
                calls += 1
                if calls == 2:
                    Path(path).write_bytes(b"late")
                    return True
                return False

            with patch(
                "subtitle_pipeline.publish.os.path.lexists", side_effect=lexists
            ):
                with self.assertRaises(ExistingTargetError):
                    publish(
                        candidate,
                        target,
                        expected_bytes=size,
                        expected_sha256=digest,
                    )
            self.assertEqual(target.read_bytes(), b"late")
            self.assertFalse((root / "movie.zh.srt.part").exists())

    def test_stale_part_is_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate, size, digest = self.candidate(root, b"new")
            target = root / "movie.zh.srt"
            part = root / "movie.zh.srt.part"
            part.write_bytes(b"stale")
            publish(candidate, target, expected_bytes=size, expected_sha256=digest)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()
