from __future__ import annotations

import hashlib
import json
import sys
import types
import unittest
from dataclasses import replace
from unittest.mock import patch

from subtitle_pipeline.alignment import JapaneseCue, timestamp_digest
from subtitle_pipeline.translation import (
    Glossary,
    GlossaryEntry,
    MissingTranslationDependency,
    TranslationBlock,
    apply_translations,
    build_translation_blocks,
    exact_required_translation,
    glossary_digest,
    load_simplifier,
    merge_glossaries,
    parse_glossary_response,
    parse_translation_response,
    source_cue_digest,
    translation_flags,
)
from subtitle_pipeline.translation_runner import (
    ESCALATION_MODEL_ID,
    PRIMARY_MODEL_ID,
    EscalationSelection,
    GlossaryArtifact,
    ModelConfig,
    ReviewedTranslationArtifact,
    TranslationArtifact,
    TranslationAttempt,
    build_escalation_selection,
    extract_glossary,
    finalize_reviewed_video,
    review_block,
    select_escalation_ids,
    translate_block,
)


def cue(cue_id: str, text: str = "はい。", start: int = 0) -> JapaneseCue:
    return JapaneseCue(cue_id, start, start + 800, text)


def identity(value: str) -> str:
    return value


def text_digest(rows: tuple[tuple[str, str], ...]) -> str:
    payload = [[cue_id, text] for cue_id, text in rows]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def strict_glossary(
    source_cues: tuple[JapaneseCue, ...] | None = None,
) -> Glossary:
    if source_cues is None:
        source_cues = (cue("c1", "一花です。"),)
    return Glossary(
        version="1",
        entries=(
            GlossaryEntry(
                category="name",
                ja="一花",
                preferred_zh="一花",
                aliases=("イチカ",),
                evidence_ids=("c1",),
                explicit=False,
            ),
        ),
        relationships=("一花称呼对方为老师",),
        register="口语",
        source_digest=source_cue_digest(source_cues),
    )


def attested_glossary(glossary: Glossary) -> GlossaryArtifact:
    payload = glossary.to_dict()
    payload.pop("source_digest")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return GlossaryArtifact(
        glossary=glossary,
        source_digest=glossary.source_digest,
        content_digest=glossary_digest(glossary),
        model_id=PRIMARY_MODEL_ID,
        revision="rev-primary",
        status="ok",
        attempts=(TranslationAttempt(1, "ok", "", raw),),
    )


class TranslationValueTests(unittest.TestCase):
    def test_translation_response_requires_exact_strict_schema_and_order(self):
        raw = '{"cues":[{"id":"c1","zh":"没关系。"},{"id":"c2","zh":"继续吧。"}]}'
        self.assertEqual(
            parse_translation_response(raw, ["c1", "c2"]),
            {"c1": "没关系。", "c2": "继续吧。"},
        )

        invalid = (
            ('{"cues":[{"id":"c1","zh":"x"}]}', ["c1", "c2"]),
            ('{"cues":[{"id":"c2","zh":"x"},{"id":"c1","zh":"y"}]}', ["c1", "c2"]),
            ('{"cues":[{"id":"c1","zh":"x","start":0}]}', ["c1"]),
            ('{"cues":[{"id":1,"zh":"x"}]}', ["c1"]),
            ('{"cues":[{"id":"c1","zh":1}]}', ["c1"]),
            ('{"cues":[{"id":"c1","zh":""}]}', ["c1"]),
            ('{"cues":[],"note":"x"}', []),
            ('{"cues":[{"id":"c1","id":"c1","zh":"x"}]}', ["c1"]),
            ('{"cues":[{"id":"c1","zh":NaN}]}', ["c1"]),
            ('```json\n{"cues":[]}\n```', []),
        )
        for response, expected in invalid:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_translation_response(response, expected)

        for expected in ([""], ["c1", "c1"], [1]):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError):
                    parse_translation_response('{"cues":[]}', expected)

    def test_blocks_commit_every_id_exactly_once_and_context_is_read_only(self):
        cues = tuple(cue(f"c{i}", str(i), i * 1000) for i in range(250))
        blocks = build_translation_blocks(cues, commit_size=120, context_size=10)

        self.assertEqual(
            [cue_id for block in blocks for cue_id in block.commit_ids],
            [item.cue_id for item in cues],
        )
        self.assertEqual([len(block.commit_cues) for block in blocks], [120, 120, 10])
        self.assertEqual(len(blocks[1].before_context), 10)
        self.assertEqual(len(blocks[1].after_context), 10)
        self.assertTrue(
            all(
                set(block.commit_ids).isdisjoint(
                    item.cue_id for item in block.before_context + block.after_context
                )
                for block in blocks
            )
        )

    def test_block_arguments_and_duplicate_cue_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            build_translation_blocks((cue("c1"), cue("c1")))
        for commit_size, context_size in (
            (0, 10),
            (121, 10),
            (120, -1),
            (120, 1),
            (True, 10),
        ):
            with self.subTest(commit_size=commit_size, context_size=context_size):
                with self.assertRaises(ValueError):
                    build_translation_blocks(
                        (cue("c1"),),
                        commit_size=commit_size,
                        context_size=context_size,
                    )

    def test_direct_block_construction_cannot_reuse_context_or_fake_window_shape(self):
        source = (cue("c0", "前。"), cue("c1", "中。", 1000), cue("c2", "後。", 2000))
        digest = source_cue_digest(source)
        with self.assertRaises(ValueError):
            TranslationBlock(
                before_context=(source[1],),
                commit_cues=(source[1],),
                after_context=(source[2],),
                source_digest=digest,
                source_count=3,
                commit_start=1,
            )
        with self.assertRaises(ValueError):
            TranslationBlock(
                before_context=(),
                commit_cues=(source[1],),
                after_context=(source[2],),
                source_digest=digest,
                source_count=3,
                commit_start=1,
            )

    def test_apply_translations_preserves_digest_and_requires_exact_mapping(self):
        source = (cue("c1", "はい。", 100), cue("c2", "続けて。", 1000))
        before = timestamp_digest(source)
        translated = apply_translations(
            source,
            {"c1": "好的。", "c2": "继续。"},
            before,
        )
        self.assertEqual(timestamp_digest(translated), before)
        self.assertEqual([item.text for item in translated], ["好的。", "继续。"])
        with self.assertRaises(ValueError):
            apply_translations(source, {"c1": "好的。"}, before)
        with self.assertRaises(ValueError):
            apply_translations(source, {"c2": "继续。", "c1": "好的。"}, before)
        with self.assertRaises(ValueError):
            apply_translations(source, {"c1": "好的。", "c2": "继续。"}, "bad")

    def test_glossary_requires_real_evidence_and_merge_surfaces_conflicts(self):
        source = (cue("c1", "一花です。"), cue("c2", "先生。", 1000))
        raw = json.dumps(
            {
                "version": "1",
                "entries": [
                    {
                        "category": "name",
                        "ja": "一花",
                        "preferred_zh": "一花",
                        "aliases": ["イチカ"],
                        "evidence_ids": ["c1"],
                        "explicit": False,
                    }
                ],
                "relationships": ["一花称呼对方为老师"],
                "register": "口语",
            },
            ensure_ascii=False,
        )
        glossary = parse_glossary_response(raw, source)
        self.assertEqual(glossary.entries[0].preferred_zh, "一花")

        hallucinated = raw.replace('"c1"', '"missing"')
        with self.assertRaises(ValueError):
            parse_glossary_response(hallucinated, source)
        absent = raw.replace("一花", "不存在", 1)
        with self.assertRaises(ValueError):
            parse_glossary_response(absent, source)

        conflict = Glossary(
            "1",
            (GlossaryEntry("name", "一花", "伊知花", (), ("c1",), False),),
            (),
            "口语",
            glossary.source_digest,
        )
        with self.assertRaisesRegex(ValueError, "conflict"):
            merge_glossaries(glossary, conflict)

    def test_glossary_rejects_invented_han_spelling_for_observed_kana_name(self):
        source = (cue("c1", "まゆきちゃんです。"),)

        def payload(preferred):
            return json.dumps(
                {
                    "version": "1",
                    "entries": [
                        {
                            "category": "character",
                            "ja": "まゆきちゃん",
                            "preferred_zh": preferred,
                            "aliases": [],
                            "evidence_ids": ["c1"],
                            "explicit": False,
                        }
                    ],
                    "relationships": [],
                    "register": "口语",
                },
                ensure_ascii=False,
            )

        with self.assertRaisesRegex(ValueError, "Latin romanization"):
            parse_glossary_response(payload("真由纪"), source)
        with self.assertRaisesRegex(ValueError, "exact Latin stem Mayuki"):
            parse_glossary_response(payload("Maria小姐"), source)
        glossary = parse_glossary_response(payload("Mayuki小姐"), source)
        self.assertEqual(glossary.entries[0].preferred_zh, "Mayuki小姐")

    def test_glossary_rejects_overbroad_generic_phrase_inventory(self):
        terms = ("ありがとう", "そうですね", "どう", "もっと", "思います", "私たち")
        source = tuple(cue(f"c{i}", term, i * 1000) for i, term in enumerate(terms))
        payload = json.dumps(
            {
                "version": "1",
                "entries": [
                    {
                        "category": "phrase",
                        "ja": term,
                        "preferred_zh": "普通短语",
                        "aliases": [],
                        "evidence_ids": [f"c{i}"],
                        "explicit": False,
                    }
                    for i, term in enumerate(terms)
                ],
                "relationships": [],
                "register": "口语",
            },
            ensure_ascii=False,
        )

        with self.assertRaisesRegex(ValueError, "too many non-explicit generic"):
            parse_glossary_response(payload, source)

    def test_glossary_rejects_context_inconsistent_asr_artifact_as_name(self):
        source = (
            cue("c1", "オナニー、いかゆさん、我慢したということでよろしいですか？"),
            cue("c2", "私は一ヶ月、オナニーを我慢しました。", 1000),
        )
        payload = json.dumps(
            {
                "version": "1",
                "entries": [
                    {
                        "category": "name",
                        "ja": "いかゆさん",
                        "preferred_zh": "饭由小姐",
                        "aliases": [],
                        "evidence_ids": ["c1"],
                        "explicit": False,
                    }
                ],
                "relationships": [],
                "register": "口语",
            },
            ensure_ascii=False,
        )

        with self.assertRaisesRegex(ValueError, "context-inconsistent ASR artifact"):
            parse_glossary_response(payload, source)

    def test_glossary_rejects_more_observed_invented_kana_name_spellings(self):
        for name in (
            "えいちゃん",
            "かずき",
            "なおん",
            "ひな",
            "ひなこ",
            "マリちゃん",
            "メイ",
            "ルカ",
            "さやか",
            "さやかちゃん",
            "サイバちゃん",
            "けすけくん",
            "いちか",
            "えりこ",
            "たくろう",
            "こだま",
            "こだまさん",
            "なな",
            "ななちゃん",
            "エサーリア",
        ):
            source = (cue("c1", f"{name}です。"),)
            payload = json.dumps(
                {
                    "version": "1",
                    "entries": [
                        {
                            "category": "character",
                            "ja": name,
                            "preferred_zh": "虚构汉字名",
                            "aliases": [],
                            "evidence_ids": ["c1"],
                            "explicit": False,
                        }
                    ],
                    "relationships": [],
                    "register": "口语",
                },
                ensure_ascii=False,
            )
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "Latin romanization"),
            ):
                parse_glossary_response(payload, source)

    def test_duplicate_grounded_glossary_rows_merge_only_when_meaning_agrees(self):
        source = (
            cue("c1", "エッチしよう。"),
            cue("c2", "エッチはだめ。", 1000),
        )

        def payload(second_overrides=None):
            second = {
                "category": "explicit",
                "ja": "エッチ",
                "preferred_zh": "做爱",
                "aliases": ["色色"],
                "evidence_ids": ["c2"],
                "explicit": True,
            }
            if second_overrides:
                second.update(second_overrides)
            return json.dumps(
                {
                    "version": "1",
                    "entries": [
                        {
                            "category": "explicit",
                            "ja": "エッチ",
                            "preferred_zh": "做爱",
                            "aliases": ["性爱"],
                            "evidence_ids": ["c1"],
                            "explicit": False,
                        },
                        second,
                    ],
                    "relationships": [],
                    "register": "露骨口语",
                },
                ensure_ascii=False,
            )

        glossary = parse_glossary_response(payload(), source)
        self.assertEqual(len(glossary.entries), 1)
        self.assertEqual(glossary.entries[0].aliases, ("性爱", "色色"))
        self.assertEqual(glossary.entries[0].evidence_ids, ("c1", "c2"))
        self.assertTrue(glossary.entries[0].explicit)

        for conflict in (
            {"category": "term"},
            {"preferred_zh": "调情"},
        ):
            with self.subTest(conflict=conflict):
                with self.assertRaisesRegex(ValueError, "conflict"):
                    parse_glossary_response(payload(conflict), source)

        with self.assertRaisesRegex(ValueError, "lacks source evidence"):
            parse_glossary_response(
                payload({"evidence_ids": ["c1"], "ja": "だめ"}),
                source,
            )

    def test_glossary_digest_binds_every_canonical_field(self):
        glossary = strict_glossary()
        digest = glossary_digest(glossary)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

        variants = (
            replace(glossary, version="2"),
            replace(glossary, register="正式"),
            replace(glossary, relationships=("关系已改变",)),
            replace(
                glossary,
                entries=(replace(glossary.entries[0], explicit=True),),
            ),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(glossary_digest(variant), digest)

    def test_contextual_glossary_variants_accept_natural_inflection(self):
        glossary = Glossary(
            "1",
            (
                GlossaryEntry("noun", "臭い", "臭味", (), ("c1",), False),
                GlossaryEntry("noun", "いじめ", "校园霸凌", (), ("c2",), False),
                GlossaryEntry("term", "秘密", "秘密", (), ("c3",), False),
                GlossaryEntry("relationship", "お父さん", "爸爸", (), ("c4",), False),
                GlossaryEntry("relationship", "お母さん", "妈妈", (), ("c4",), False),
                GlossaryEntry("term", "大丈夫", "没关系", (), ("c5",), False),
                GlossaryEntry(
                    "character",
                    "せーな姉ちゃん",
                    "Seina姐姐",
                    (),
                    ("c6",),
                    False,
                ),
                GlossaryEntry("term", "万引き", "偷窃", (), ("c7",), False),
                GlossaryEntry("person", "ちろ", "Chiro", (), ("c8",), False),
                GlossaryEntry("person", "加藤", "Kato 小姐", (), ("c9",), False),
                GlossaryEntry("term", "何", "什么", (), ("c10",), False),
            ),
            (),
            "口语",
            "0" * 64,
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "もう臭い。", "已经很臭了。", glossary, simplifier=identity
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "いじめについて。", "关于校园欺凌。", glossary, simplifier=identity
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "秘密にして。", "替我保密。", glossary, simplifier=identity
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "お父さんもお母さんも初めまして。",
                "叔叔阿姨，初次见面。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "難しいところだけなら大丈夫。",
                "只讲难的地方就没问题。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "せーな姉ちゃんには関係ねえだろ。",
                "这跟你没关系吧，Seina姐。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "うちの商品万引きしましたよね。",
                "你偷了我们店里的商品吧。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "してるよ、もちろん。",
                "当然见过了。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "ちろさん。",
                "Chiro小姐。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "加藤さん、えっ、あっ、ちっちゃめっちゃい。",
                "Kato小姐，呃，啊，小小的，超级小。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "何チュウされたんだけど。",
                "到底被亲了哪里啊。",
                glossary,
                simplifier=identity,
            ),
        )

    def test_longer_glossary_term_shadows_only_contained_shorter_term(self):
        glossary = Glossary(
            "1",
            (
                GlossaryEntry("person", "父", "父亲", (), ("c1",), False),
                GlossaryEntry("person", "お父さん", "爸爸", (), ("c2",), False),
            ),
            (),
            "口语",
            "0" * 64,
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "お父さんから。ちょっと出るね。",
                "是爸爸打来的，我出去接一下。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "父に相談して、お父さんにも電話した。",
                "我和父亲商量过，也给爸爸打了电话。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "glossary_term_missing",
            translation_flags(
                "父に相談して、お父さんにも電話した。",
                "我商量过，也给爸爸打了电话。",
                glossary,
                simplifier=identity,
            ),
        )

    def test_source_grounded_japanese_name_glyph_is_not_opencc_mangled(self):
        glossary = strict_glossary()

        def convert_name_glyph(value):
            return value.replace("紬", "䌷")

        self.assertNotIn(
            "traditional_character",
            translation_flags(
                "紬さん。", "紬小姐。", glossary, simplifier=convert_name_glyph
            ),
        )

        def convert_bridge_name(value):
            return value.replace("橋", "桥")

        self.assertNotIn(
            "traditional_character",
            translation_flags(
                "大橋さん。",
                "大橋小姐。",
                glossary,
                simplifier=convert_bridge_name,
            ),
        )
        self.assertIn(
            "traditional_character",
            translation_flags(
                "橋を渡る。",
                "过橋。",
                glossary,
                simplifier=convert_bridge_name,
            ),
        )

        def convert_kanazuka_name(value):
            return value.replace("塚", "冢")

        self.assertNotIn(
            "traditional_character",
            translation_flags(
                "いやちょっと金塚さん。",
                "不，等等，金塚小姐。",
                glossary,
                simplifier=convert_kanazuka_name,
            ),
        )
        self.assertNotIn(
            "traditional_character",
            translation_flags(
                "大塚はそれ知らん状態で。",
                "大塚完全不知道这回事。",
                glossary,
                simplifier=convert_kanazuka_name,
            ),
        )

    def test_kana_name_and_contextual_asr_artifact_cannot_invent_names(self):
        glossary = strict_glossary()
        self.assertIn(
            "kana_name_romanization_missing",
            translation_flags(
                "エサーリアです。",
                "我是艾莎莉亚。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "kana_name_romanization_missing",
            translation_flags(
                "エサーリアです。",
                "我是Esaria。",
                glossary,
                simplifier=identity,
            ),
        )
        kazushin_id = (
            "805fb20e2129e8527d2ba5650db0b4fcef78c38e0c1d5d0df478cadd9901e205"
            "-seg-00000577-cue-0000"
        )
        self.assertIn(
            "exact_cue_translation_mismatch",
            translation_flags(
                "かずしん",
                "和信",
                glossary,
                simplifier=identity,
                cue_id=kazushin_id,
            ),
        )
        self.assertEqual(
            translation_flags(
                "かずしん",
                "Kazushin",
                glossary,
                simplifier=identity,
                cue_id=kazushin_id,
            ),
            [],
        )
        self.assertIn(
            "kana_name_romanization_missing",
            translation_flags(
                "エサーリアです。",
                "我是Maria。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "kana_name_romanization_missing",
            translation_flags(
                "あかりさん。",
                "明里小姐。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "kana_name_romanization_missing",
            translation_flags(
                "あかりさん。",
                "Akari小姐。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "kana_name_romanization_missing",
            translation_flags(
                "じゃあ次、ふるみさんですね。",
                "那下一个，是古见小姐。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "kana_name_romanization_missing",
            translation_flags(
                "じゃあ次、ふるみさんですね。",
                "那下一个，是Furumi小姐。",
                glossary,
                simplifier=identity,
            ),
        )
        for source_text, wrong_text, correct_text in (
            ("明かり紡ぎです。", "我是明かり纺。", "我是Akari Tsumugi。"),
            ("つらいもこちゃん", "好难受啊，小こちゃん。", "好难受啊，Moko酱。"),
            ("あれ、すみれ。", "诶，小すみれ。", "诶，Sumire。"),
            (
                "つずねちゃんが舐めてくれる？",
                "由つずね酱来舔吗？",
                "由Tsuzune酱来舔吗？",
            ),
            ("ガルちゃんもやった。", "还玩了ガルちゃん。", "还玩了Garu酱。"),
        ):
            with self.subTest(source_text=source_text):
                self.assertIn(
                    "kana_name_romanization_missing",
                    translation_flags(
                        source_text,
                        wrong_text,
                        glossary,
                        simplifier=identity,
                    ),
                )
                self.assertNotIn(
                    "kana_name_romanization_missing",
                    translation_flags(
                        source_text,
                        correct_text,
                        glossary,
                        simplifier=identity,
                    ),
                )
        for source_text, target_text in (
            ("メイクしてきました。", "我化好妆来了。"),
            ("スリムな体型です。", "身材很苗条。"),
        ):
            with self.subTest(source_text=source_text):
                self.assertNotIn(
                    "kana_name_romanization_missing",
                    translation_flags(
                        source_text,
                        target_text,
                        glossary,
                        simplifier=identity,
                    ),
                )
        source = "オナニー、いかゆさん、我慢したということでよろしいですか？"
        wrong = translation_flags(
            source,
            "自慰，伊卡尤小姐，忍耐了一个月，是这样吗？",
            glossary,
            simplifier=identity,
        )
        self.assertIn("contextual_asr_name_invented", wrong)
        self.assertNotIn("contextual_asr_correction_missing", wrong)
        corrected = translation_flags(
            source,
            "你忍了一个月没自慰，是这样吗？",
            glossary,
            simplifier=identity,
        )
        self.assertNotIn("contextual_asr_name_invented", corrected)
        self.assertNotIn("contextual_asr_correction_missing", corrected)
        for source_text, wrong_text, correct_text in (
            (
                "あんぴゃんぽんっ…ん",
                "啊……",
                "嗯……",
            ),
            (
                "喫茶店のやつだ。",
                "是那个喫茶店的家伙。",
                "是那个咖啡店的家伙。",
            ),
            (
                "じゃこちゃん。",
                "小こ酱。",
                "那就这边吧。",
            ),
            (
                "ちゅうくた",
                "中くた。",
                "……",
            ),
            (
                "of.",
                "of.",
                "……",
            ),
        ):
            with self.subTest(source_text=source_text):
                wrong_flags = translation_flags(
                    source_text,
                    wrong_text,
                    glossary,
                    simplifier=identity,
                )
                self.assertIn("contextual_asr_correction_missing", wrong_flags)
                correct_flags = translation_flags(
                    source_text,
                    correct_text,
                    glossary,
                    simplifier=identity,
                )
                self.assertNotIn("contextual_asr_correction_missing", correct_flags)
                self.assertNotIn("suspiciously_short", correct_flags)

    def test_linguistic_explanations_may_quote_short_kana_terms(self):
        glossary = strict_glossary()
        for source_text, target_text in (
            (
                "現代の日本語でぬというと、打ち消しの意味です。",
                "在现代日语中，「ぬ」表示否定。",
            ),
            ("古文でも打ち消しの意味で縫を使う。", "古文中也用「ぬ」表示否定。"),
            ("完了のルレス。", "表示完成的「る」。"),
            ("クリは省略だよね。", "“クリ”是省略吧。"),
            ("ムカつくって何?", "“ムカつく”是什么意思？"),
            (
                "それでは今日は間違いやすいぬのお話をします。",
                "那么今天我们来谈谈容易出错的「ぬ」。",
            ),
        ):
            with self.subTest(source_text=source_text):
                self.assertNotIn(
                    "japanese_remaining",
                    translation_flags(
                        source_text,
                        target_text,
                        glossary,
                        simplifier=identity,
                    ),
                )
        self.assertIn(
            "japanese_remaining",
            translation_flags(
                "今日はぬるい。",
                "今天是「ぬるい」的。",
                glossary,
                simplifier=identity,
            ),
        )

    def test_cue_specific_translation_does_not_change_same_source_elsewhere(self):
        cue_id = (
            "397e5df75c0d2768e57e93e0b86e55cc6a429201f9c24349404b945cf61e094d"
            "-seg-00003085-cue-0005"
        )
        self.assertEqual(
            translation_flags(
                "しろ。",
                "。",
                strict_glossary(),
                simplifier=identity,
                cue_id=cue_id,
            ),
            [],
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags(
                "しろ。",
                "。",
                strict_glossary(),
                simplifier=identity,
                cue_id="another-cue",
            ),
        )

    def test_second_review_failure_batch_has_exact_source_grounded_corrections(self):
        cases = {
            "66520888e2d840b33f75a33f756986aa42bdacdac858764da6eb1a5e433814a6-seg-00000297-cue-0000": (
                "リクリクに",
                "……",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000012-cue-0000": (
                "潮 喷 表 演 可 以 指 挥。",
                "潮喷表演可以指挥。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000015-cue-0000": (
                "嗯嗯，还有万部AV在线观看哦，老公快来操我。",
                "嗯嗯，还有万部AV在线观看哦，老公快来操我。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000016-cue-0000": (
                "台湾悠悠裸聊室注册成为会员免费送五十点。",
                "台湾悠悠裸聊室注册成为会员免费送五十点。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000017-cue-0000": (
                "是绵绵一段乐章多。",
                "是绵绵一段乐章多。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000018-cue-0000": (
                "台湾真人视频聊天，十年信誉任你指挥，任你调教，激情无限。",
                "台湾真人视频聊天，十年信誉任你指挥，任你调教，激情无限。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000018-cue-0001": (
                "免费看美女真人秀表演，美女裸聊表演，寂寞交友聊天室，乐趣多多。",
                "免费看美女真人秀表演，美女裸聊表演，寂寞交友聊天室，乐趣多多。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000023-cue-0000": (
                "正有。",
                "征友。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000072-cue-0000": (
                "それでは、今日は間違えやすい布のお話をします。",
                "那么，今天我们来讲容易混淆的「ぬ」。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000075-cue-0001": (
                "官僚の縫というものがあります。",
                "那就是表示完成的「ぬ」。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000089-cue-0000": (
                "丸一の図は。",
                "图一中的「ぬ」，",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000091-cue-0000": (
                "完了のルレス。",
                "是表示完成的「ぬ」。",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000092-cue-0000": (
                "丸二のでは。",
                "图二中的「ぬ」，",
            ),
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a-seg-00000094-cue-0000": (
                "打ち消しの図です。",
                "是表示否定的「ぬ」。",
            ),
            "e61b6979e9c481ce0af46bcf37e41cc32a835fe5fcec2817f256938ae719a132-seg-00000026-cue-0000": (
                "します。",
                "做。",
            ),
            "e61b6979e9c481ce0af46bcf37e41cc32a835fe5fcec2817f256938ae719a132-seg-00000046-cue-0000": (
                "不起。",
                "不行。",
            ),
            "d1d431be669328ac93db8f7d1297d8d31a2f2dcfd1feb46cedbdd37077be72f3-seg-00000053-cue-0000": (
                "狩りを。",
                "回去吧。",
            ),
            "805fb20e2129e8527d2ba5650db0b4fcef78c38e0c1d5d0df478cadd9901e205-seg-00000298-cue-0000": (
                "cause",
                "因为。",
            ),
            "8f7406b91dc12d284e51c4673bcf110ca5138d1ac813f6aa8912c038413ad4d2-seg-00000062-cue-0000": (
                "召喚 会うの？",
                "会见面吗？",
            ),
            "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939-seg-00000406-cue-0000": (
                "が聞いてみても。",
                "就算去问了……",
            ),
            "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939-seg-00000407-cue-0000": (
                "展示なんて、古はずなく分かっているけれど、帰るまんま、うんうんうん、",
                "虽然我知道，但还是就这样回去了，嗯、嗯、嗯……",
            ),
            "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939-seg-00000407-cue-0001": (
                "うんうんうん、さまよ。",
                "嗯、嗯、嗯……徘徊着。",
            ),
            "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939-seg-00000407-cue-0003": (
                "やり直して、気が付いて、うん、うん、うん、うん。",
                "重新来过，回过神来，嗯、嗯、嗯、嗯。",
            ),
            "ce37e47fdf9ee231b95f1f44dae4c1a4203ed23999895f25216cce726fa8203d-seg-00000212-cue-0000": (
                "いい天狗丸玉",
                "……",
            ),
            "a5235799cb44359b4f760b725256d831197a43741f2430b0e9cfb7b34cc713a5-seg-00000114-cue-0000": (
                "時",
                "当时。",
            ),
            "4d5d65a9642087e4587774089635db32e349db46678fbac15ad5a76fd04cf759-seg-00000223-cue-0000": (
                "ここ す ね も な ん です よ ね。",
                "这里也是这样，对吧。",
            ),
            "05aad23dda4327a3b8d9046a763a8de690dd5253d9a001cd7c41605785967285-seg-00000426-cue-0000": (
                "グックリでしょ仲です。",
                "很深吧，在里面。",
            ),
            "05aad23dda4327a3b8d9046a763a8de690dd5253d9a001cd7c41605785967285-seg-00000436-cue-0000": (
                "どんなカドンの名前？",
                "这叫什么名字？",
            ),
            "05aad23dda4327a3b8d9046a763a8de690dd5253d9a001cd7c41605785967285-seg-00000438-cue-0000": (
                "都会。",
                "都市。",
            ),
        }
        for cue_id, (source, target) in cases.items():
            with self.subTest(cue_id=cue_id):
                self.assertEqual(
                    exact_required_translation(source, cue_id=cue_id), target
                )
                self.assertEqual(
                    translation_flags(
                        source,
                        target,
                        strict_glossary(),
                        simplifier=identity,
                        cue_id=cue_id,
                    ),
                    [],
                )

    def test_cue_specific_translation_resolves_observed_quality_gate_conflicts(self):
        cases = (
            (
                "921b490fb0a5ea00c268ac1f0563117c0fa0ee2d3be14f080294545e9723dc71"
                "-seg-00000075-cue-0000",
                "っていう。",
                "那个。",
            ),
            (
                "921b490fb0a5ea00c268ac1f0563117c0fa0ee2d3be14f080294545e9723dc71"
                "-seg-00000078-cue-0000",
                "台風でした。",
                "是台风天。",
            ),
            (
                "b971130f6873aaa35684e412491e0d9425332d09a2b125f866e1b15ad5a419ad"
                "-seg-00000031-cue-0005",
                "空いてる。",
                "空着。",
            ),
            (
                "61fe480e85b868966bb22c61c2a1d8c48348b28e2895a18021fac94b85c0b15a"
                "-seg-00000086-cue-0000",
                "ぴんこ。",
                "粉色。",
            ),
            (
                "12410ad96380934673b676792de5895a6c60ca639f549d8f5f9b6b253bb424cd"
                "-seg-00000226-cue-0000",
                "関係になってかかった。",
                "开始有关系了。",
            ),
            (
                "12410ad96380934673b676792de5895a6c60ca639f549d8f5f9b6b253bb424cd"
                "-seg-00000227-cue-0000",
                "されるでしょうか？",
                "会被做吗？",
            ),
            (
                "12410ad96380934673b676792de5895a6c60ca639f549d8f5f9b6b253bb424cd"
                "-seg-00000228-cue-0000",
                "父母母。",
                "父母。",
            ),
            (
                "b62b00216cd62560c4be39ee1a1c34fd15e2f2b7eb5280ae4f90f995e7b9187e"
                "-seg-00000094-cue-0000",
                "れたね。",
                "太丢人了。",
            ),
            (
                "4161cf5278c2a9d83d65c36fc2fe1587e4457086c1cb941717546552b20caeb7"
                "-seg-00000755-cue-0000",
                "M. O. D. N. A. J. R.",
                "莫德纳",
            ),
            (
                "3ae89c41503656fc87db978ab023414c32d73693890d06d48efffc194528962d"
                "-seg-00001197-cue-0000",
                "畑さん。",
                "畑小姐。",
            ),
            (
                "ffb916fccf8122a99c7108f63f157aed7adc3c2480ccd09f7c97c95457ad3903"
                "-seg-00001381-cue-0000",
                "加油，加油。",
                "加油，加油。",
            ),
            (
                "9b3b127dd6e2d79a034c633ef8fe5dea3550d78129ec9ba8f5d9f3f2ca7e0b93"
                "-seg-00000475-cue-0000",
                "ベトゥイイオンスベトゥ。",
                "为了上床。",
            ),
            (
                "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
                "-seg-00000300-cue-0000",
                "あめんさん",
                "……",
            ),
            (
                "da882fbe03724493963851cc266b7afa4441ae2bc943a7c559f9b54a24108fbb"
                "-seg-00000015-cue-0000",
                "つむちゃんの前で、でんれしちゃって。",
                "还在Tsumu酱面前卿卿我我的。",
            ),
            (
                "6d417676f86f7836022877766536329fb871d947b3bbf091d8be89bef271b5fa"
                "-seg-00000712-cue-0002",
                "あかりさん。",
                "Akari小姐。",
            ),
            (
                "6d417676f86f7836022877766536329fb871d947b3bbf091d8be89bef271b5fa"
                "-seg-00000712-cue-0004",
                "もうあかりさんに会えないと思うと。",
                "一想到以后再也见不到Akari小姐了……",
            ),
            (
                "c78f851b8fcb484ee2172a71e4482492b5f1dd64a26acf30a74364e791ad6890"
                "-seg-00000891-cue-0000",
                "あず！",
                "啊！",
            ),
            (
                "5c94cba5678faf3dc02c3ac0aa32c86d4d6b778515dbc086a95b565a228c4d6b"
                "-seg-00000035-cue-0000",
                "お父さんからちょっと出るね。",
                "是爸爸打来的，我出去接一下。",
            ),
            (
                "6d417676f86f7836022877766536329fb871d947b3bbf091d8be89bef271b5fa"
                "-seg-00000902-cue-0000",
                "くまさん",
                "……",
            ),
            (
                "d2470ececaef99e2b920dcdadd63fd57e92e0028fcddf64c511d86b6d7593053"
                "-seg-00000691-cue-0001",
                "しっていうのはね、書いてないと思う。",
                "至于这件事，我觉得没有写出来。",
            ),
            (
                "3ae89c41503656fc87db978ab023414c32d73693890d06d48efffc194528962d"
                "-seg-00000170-cue-0000",
                "ドアハングルアジネシタリオ。",
                "你不是把门半开着吗？",
            ),
            (
                "a2066f68e73d30770cc61528a4f6b3879935bbffc2a560f859ce5fa868977fa7"
                "-seg-00000019-cue-0001",
                "愚蠢的男人。",
                "愚蠢的男人。",
            ),
            (
                "a2066f68e73d30770cc61528a4f6b3879935bbffc2a560f859ce5fa868977fa7"
                "-seg-00001034-cue-0000",
                "あすかあたってる",
                "啊，顶到那里了。",
            ),
            (
                "5578822219f3a604f7b96d62566b0863993d1e9c688e4e0e469a8f13898d12a9"
                "-seg-00001766-cue-0001",
                "ひかる。",
                "Hikaru。",
            ),
            (
                "2ce5ab9e34b05ab744c9e12dae278f942ba46d1a36602c5656f1c022b77f5a64"
                "-seg-00000059-cue-0001",
                "ちょっと、まあまあまあまあまあまあ、あの、ここを掃除すればいいのかな。",
                "等等……嗯……那个，把这里清理干净就行了吧。",
            ),
            (
                "f812cd0d871693924c297014449b9f6ffe78e036437c86a3f173b9f43da494a2"
                "-seg-00001152-cue-0006",
                "最適なコンテンツを自動で表示するようにしています。",
                "从而自动显示最合适的内容。",
            ),
            (
                "aa92fc8a4aa63b3829bdc543c3c16fee776aa0b88970a7afcdc7108591309ce1"
                "-seg-00000193-cue-0000",
                "あいくいくいくっ…",
                "啊，要去了、要去了、要去了……",
            ),
            (
                "d319a1494e5d3522d9d05780c20da28c73ea8d96f623340eb05233cf655434fb"
                "-seg-00000353-cue-0000",
                "子とてか",
                "……",
            ),
            (
                "f499abfa0cb9ea5537e54ff4a84cf1323e0092460be06e2a2880b2ec44da66d9"
                "-seg-00001356-cue-0000",
                "25周年。",
                "25周年。",
            ),
            (
                "134e1b3ed61f136be211594e4bd67ad557d8efa1a01c62e046ecdb39712fc3ee"
                "-seg-00001939-cue-0003",
                "あ、いや、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ、あ。あ、ちょっと、ちょっと。",
                "啊，不……啊、啊、啊……等等、等等。",
            ),
            (
                "d269825a1a13a2f6f89b6038cc805de7f8b0297ad0417f9b257d052cbef77ea0"
                "-seg-00000470-cue-0000",
                "けい！",
                "景！",
            ),
            (
                "12b064c77c1ff8c16ce2be675f97ce9bd8020ea0cbdc7dda76a60703ec85a2f1"
                "-seg-00000751-cue-0000",
                "不急，不急，不急。",
                "不着急，不着急，不着急。",
            ),
            (
                "fcbfb62f1fa47c405c7adc83e1430bdf49c7efbca0a16ed1526cf90c30815c77"
                "-seg-00000706-cue-0000",
                "はぁ、はぁ…んんっ、んんっ、んんっ、んんんっ、あ、んんんっ、んんんんっ、んんんんっ、あぁ、んんんんっ、んんんんっ、あぁ、んんんんっ、んんんんっ、んんんんっ、んんんんっ、んんんんんっ、んんんんんっ、んんんんんんっ、んんんんんんっ、",
                "哈啊、哈啊……嗯、嗯……啊……",
            ),
            (
                "56022dacbcb4c9ddc54e4b017cb2d57f5ab11b4ea260015d0c04bb4b6d1a5a90"
                "-seg-00001320-cue-0000",
                "敏感敏感。",
                "好敏感，好敏感。",
            ),
            (
                "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
                "-seg-00000031-cue-0000",
                "出来的他，不想擦边，一天最棒。",
                "出来的他，不想擦边，一天最棒。",
            ),
            (
                "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
                "-seg-00000031-cue-0000",
                "出来的他，不想擦边，一天最棒。",
                "出来的他，不想擦边，一天最棒。",
            ),
            (
                "b97e633255b9b11035319e052db668646bec3d0bfea4f08dcd3d5033b8ee1ec4"
                "-seg-00000867-cue-0000",
                "に失った。",
                "弄丢了。",
            ),
            (
                "85b4e3181113849deb9ef600dacb39a8a679b73d222d92d7cd46207870a19171"
                "-seg-00000257-cue-0000",
                "是围着店家。",
                "是围着店家。",
            ),
            (
                "1337b52ebedcebc0ac0a7223e42ac9f9597adf250589aea8ddd27b914c720819"
                "-seg-00000525-cue-0000",
                "ライトリンク。",
                "光之链接。",
            ),
            (
                "9f94dda31aa39e4b831c31383a046103891e23c901f125c1ae1e6401c9dc9420"
                "-seg-00000497-cue-0000",
                "とととららとととち。",
                "……",
            ),
            (
                "6d97b3624030401db532f7efb103611a2b71acb93696a9a5e93d8afd852975bf"
                "-seg-00000292-cue-0000",
                "俺のさあや。",
                "都是我的错。",
            ),
            (
                "40f8976ac62f9796bdce46e60864d5d2c0f8266644310cacaf625dc6a66ce998"
                "-seg-00000000-cue-0004",
                "好友相约竞技。",
                "好友相约竞技。",
            ),
            (
                "b6ab8f3924fffffb6231c679fbd97fdacc83198fee6fe5c736a9f8b4136c8f19"
                "-seg-00000506-cue-0000",
                "にそうせわちと",
                "……",
            ),
            (
                "84fc1de80b63849cd84ee25fe31664a77b8b0618f90f1314d9251a96d376bd53"
                "-seg-00000020-cue-0001",
                "日常生物。",
                "这是常有的事。",
            ),
            (
                "e4d7f32001515d46b61880ca3a0940c46a992f5521a7f1cdb7fc528de3175684"
                "-seg-00000870-cue-0000",
                "我要去广州。",
                "我要去广州。",
            ),
            (
                "1193feda53ff5e9dc94325bf06934658fa960660f9fb975b86a6e05282c1569b"
                "-seg-00000442-cue-0000",
                "放着放着放着放着。",
                "放着、放着、放着、放着。",
            ),
            (
                "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
                "-seg-00000039-cue-0000",
                "今晚游泳，圣诞宝贝",
                "今晚游泳，圣诞宝贝。",
            ),
            (
                "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
                "-seg-00000039-cue-0000",
                "今晚游泳，圣诞宝贝",
                "今晚游泳，圣诞宝贝。",
            ),
            (
                "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
                "-seg-00000041-cue-0000",
                "生存惆怅。",
                "生存的惆怅。",
            ),
            (
                "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
                "-seg-00000041-cue-0000",
                "生存惆怅。",
                "生存的惆怅。",
            ),
            (
                "b97e633255b9b11035319e052db668646bec3d0bfea4f08dcd3d5033b8ee1ec4"
                "-seg-00000868-cue-0005",
                "いつかちゃんが戻ってきたら安心ね。",
                "等她回来就放心了。",
            ),
            (
                "6d97b3624030401db532f7efb103611a2b71acb93696a9a5e93d8afd852975bf"
                "-seg-00000311-cue-0000",
                "ピンピンタ」",
                "（听不清）",
            ),
            (
                "e4d7f32001515d46b61880ca3a0940c46a992f5521a7f1cdb7fc528de3175684"
                "-seg-00000880-cue-0000",
                "青春美院。",
                "青春美术学院。",
            ),
            (
                "1193feda53ff5e9dc94325bf06934658fa960660f9fb975b86a6e05282c1569b"
                "-seg-00000443-cue-0000",
                "尝尝尝尝尝尝尝尝尝尝。",
                "尝一尝、尝一尝……",
            ),
            (
                "5b75549f4c4d5437da0b43da569cf5f43432fc5eecb6e0a93a841ee0619ae761"
                "-seg-00000060-cue-0000",
                "7秒30。",
                "7.30秒。",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000134-cue-0000",
                "ステートポイント。",
                "状态点。",
            ),
            (
                "85c4a1a8bf91850f1974918aec3b759012a4a86cfea471cb9aa4a1d74bccb7d7"
                "-seg-00000857-cue-0000",
                "惜しみや先生。",
                "星宫老师。",
            ),
            (
                "635c891fc02ad78836d12a4f121d62aa73dd848cad1de5234a3a8d67eb0f9b0b"
                "-seg-00001132-cue-0000",
                "r t",
                "（听不清）",
            ),
            (
                "a6882b2fca91f6d2d5b7f6d6a65d4ffb1cbe7a226ad80e8fabbf36e44b823fe7"
                "-seg-00001510-cue-0000",
                "つあいつあ",
                "啊、啊……",
            ),
            (
                "fa98fef035313933494b516cda37e09ce285d1ad40cfe16e299e935bc1635dd0"
                "-seg-00001544-cue-0003",
                "んんっ、んんっ、んんっ、ああっ、んんっ、ああっ、んんっ、んんっ、んんっ、んんっ、んんっ、んんっ、んんっ、いっぱい出してくれてるし、んんんっ、んん",
                "嗯、嗯……啊……给我射了好多……嗯……",
            ),
            (
                "9f94dda31aa39e4b831c31383a046103891e23c901f125c1ae1e6401c9dc9420"
                "-seg-00002062-cue-0003",
                "やばいああああやばいす。",
                "不行了，啊啊啊，真的不行了。",
            ),
            (
                "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
                "-seg-00000042-cue-0000",
                "出来的他不像",
                "出来的他，不像……",
            ),
            (
                "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
                "-seg-00000042-cue-0000",
                "出来的他不像",
                "出来的他，不像……",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000162-cue-0000",
                "你这。",
                "你这家伙。",
            ),
            (
                "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
                "-seg-00000043-cue-0000",
                "天一点嘴巴。",
                "嘴巴甜一点。",
            ),
            (
                "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
                "-seg-00000043-cue-0000",
                "天一点嘴巴。",
                "嘴巴甜一点。",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000244-cue-0000",
                "阿姨",
                "阿姨。",
            ),
            *(
                (f"{video_id}-seg-{segment:08d}-cue-0000", source, target)
                for video_id in (
                    "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91",
                    "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0",
                )
                for segment, source, target in (
                    (
                        44,
                        "汉哥哥。" if video_id.startswith("52ea") else "涵哥哥。",
                        "涵哥哥。",
                    ),
                    (45, "在路上。", "在路上。"),
                    (46, "圣诞。", "圣诞节。"),
                    (47, "你想他。", "你想念他。"),
                    (48, "家。", "家。"),
                    (
                        49,
                        "今晚拥有圣诞宝贝，还保持关心。",
                        "今晚拥有圣诞宝贝，还保持关心。",
                    ),
                    (
                        50,
                        "叮叮当，叮叮当，铃声多响亮。",
                        "叮叮当，叮叮当，铃声多响亮。",
                    ),
                    (51, "气故事空间待等你追踪它。", "故事空间等着你去追踪它。"),
                    (53, "里", "夜里。"),
                    (54, "圣诞宝贝总", "圣诞宝贝总是能。"),
                    (55, "能够完成你愿望。", "总能实现你的愿望。"),
                )
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000548-cue-0000",
                "给我",
                "给我。",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000569-cue-0000",
                "姨们",
                "阿姨们。",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000634-cue-0000",
                "富贵斌哥",
                "富贵斌哥。",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000791-cue-0000",
                "啊",
                "啊。",
            ),
            (
                "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
                "-seg-00000823-cue-0000",
                "ちくら",
                "Chikura。",
            ),
        )
        for cue_id, source, expected in cases:
            with self.subTest(cue_id=cue_id):
                self.assertEqual(
                    translation_flags(
                        source,
                        expected,
                        strict_glossary(),
                        simplifier=identity,
                        cue_id=cue_id,
                    ),
                    [],
                )

    def test_source_aware_flags_are_conservative_and_glossary_aware(self):
        glossary = strict_glossary()
        self.assertIn(
            "refusal",
            translation_flags(
                "これはテスト",
                "抱歉，我无法协助翻译该内容。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "refusal",
            translation_flags("ごめん", "抱歉。", glossary, simplifier=identity),
        )
        self.assertIn(
            "japanese_remaining",
            translation_flags(
                "これはテスト", "これは测试", glossary, simplifier=identity
            ),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags(
                "これはテスト", "これはテスト", glossary, simplifier=identity
            ),
        )
        self.assertNotIn(
            "unchanged_source",
            translation_flags(
                "尽在凤凰娱乐幺幺五六点COM。",
                "尽在凤凰娱乐幺幺五六点COM。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "unchanged_source",
            translation_flags("好好好。", "好好好。", glossary, simplifier=identity),
        )
        self.assertNotIn(
            "unchanged_source",
            translation_flags("学校", "学校", glossary, simplifier=identity),
        )
        for source in (
            "症。",
            "自由。",
            "脱掉。",
            "精神分裂症。",
            "A型。",
            "碧浪彩池一拉几动，二卷不停，经典棋盘扎金花。",
            "寻找同城新伴侣、新炮友、少妇、美男子。",
            "成功率高达百分之九十。",
            "选择人生不甘于只有一种玩法。",
            "在数以万计的抉择中，我只想忠于自我。",
            "成功不是梦想，是我的选择。",
            "随点随播，随手录。",
            "下载收藏不限量",
            "第一首AV新片天天更新",
            "最新鲜的A B平台，A V大平台，最优质的中文A B平台。",
            "快快快快快。",
            "算了算了算了算了算了算了。",
            "不好意思。",
            "不是不是。",
            "英子英子。",
            "期待、期待。",
            "上上上上。",
            "一二三、全部",
            "直接、直接。",
            "全部全部。",
            "最近、最近。",
            "我要吃火锅。",
            "全网最新最齐AV视频，超便宜只要52块。",
            "Tiktok 成人版，百万原创视频博主，海量全球成人视频。",
            "好渴好渴好渴好渴。",
            "扫我立即下载 GM.95 AI 点 CC DQ 点 NET 全球最大免费直播平台。",
            "好好好，快快快。",
            "修学旅行。",
            "超多片源。",
            "AV大平台，华语最大AV平台。",
            "第一印象。",
            "下下下下下下。",
            "深呼吸、深呼吸。",
            "千秋、千秋。",
            "内马尔代言。",
            "万部影片每天更新。",
            "全国不同城市均有秘密注册服务。",
            "台湾又又提供隐私保护，不用担心隐私泄露。",
            "英皇体育千万赞助投资，获得所有比赛版权，免费直播在线观看。",
            "能做就做，不能做就滚蛋，老板。",
            "讨厌，我都快脱光了。",
            "我只想忠于自我。",
            "高崎雄二郎。",
            "川北彩花。",
            "自由体操。",
            "嘟嘟嘟嘟嘟嘟。",
            "有痛就有痛。",
            "潮 喷 表 演 可 以 指 挥。",
            "现金红包周周拿",
            "商品、免税、兑换。",
            "淑女片源、业界最多。",
            "不不不不不不。",
            "不不不不不。",
            "悄悄悄悄。",
            "自爆、自爆。",
            "寿司寿司寿司。",
        ):
            with self.subTest(source=source):
                self.assertNotIn(
                    "unchanged_source",
                    translation_flags(source, source, glossary, simplifier=identity),
                )
        self.assertEqual(
            translation_flags("321。", "321。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags(
                "一万三千。", "一万三千。", glossary, simplifier=identity
            ),
            [],
        )
        self.assertEqual(
            translation_flags(
                "五十七分。", "五十七分。", glossary, simplifier=identity
            ),
            [],
        )
        self.assertEqual(
            translation_flags("25人。", "25人。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("2022年。", "2022年。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("M。", "M。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("F。", "F。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("えふ。", "F。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("S。", "S。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("て。", "……", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("って。", "……", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("l.", "……", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("あすか。", "Asuka。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("紗文義。", "Saon。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags(
                "そうだそうだキビは何でも飲み込みが早かったなあ。",
                "对，Kibi一直什么都学得很快。",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        self.assertEqual(
            translation_flags(
                "田中さんと飯塚さん。",
                "田中先生和饭冢先生。",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        self.assertEqual(
            translation_flags(
                "明かり紡ぎです。",
                "我是Akari Tsumugi老师。",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        self.assertIn(
            "exact_cue_translation_mismatch",
            translation_flags(
                "明かり紡ぎです。",
                "我是明かり紡ぎ（Akari Tsumugi）。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertEqual(
            translation_flags(
                "シジュアルが来る日しか。",
                "只有那一天。",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        exact_e4_cues = (
            ("はいじゃあ皆さん帰りますよ。", "好了，大家回去吧。"),
            ("気持ち悪い。", "好难受。"),
            (
                "あ、あのうささん、だい、大丈夫、もう起きて、え、もう帰りますよ。",
                "那个，Usa小姐，你还好吗？快起来，我们要回去了。",
            ),
            ("ちょっとお水飲んだ方がいいかな。", "要不要喝点水？"),
        )
        for source_text, target_text in exact_e4_cues:
            with self.subTest(exact_e4_source=source_text):
                self.assertEqual(
                    translation_flags(
                        source_text, target_text, glossary, simplifier=identity
                    ),
                    [],
                )
        self.assertEqual(
            translation_flags(
                "無親者入ったらやらしめなったら。",
                "……",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        self.assertEqual(
            translation_flags("に！", "啊！", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("how am i", "你。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("天香。", "Ame。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("まいまい。", "MaiMai。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("ネージェ？", "Nage？", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("かんちゃん。", "Kan。", glossary, simplifier=identity),
            [],
        )
        new_exact_cues = (
            ("看懂水的引导。", "……"),
            ("めいそしゃのて。", "……"),
            (
                "だから、さんが出したせいで、ここぐちゃぐちゃですよ。",
                "都怪你射出来了，把这里弄得湿漉漉的。",
            ),
            (
                "せーな姉ちゃんには関係ねえだろ。",
                "这跟你没关系吧，Seina姐。",
            ),
            (
                "うちの商品万引きしましたよね。",
                "你偷了我们店里的商品吧。",
            ),
            ("スコエ", "太厉害了。"),
            ("にいう。", "哥哥……"),
            ("過ごしたつか。", "怎么了？"),
            ("止めばったイッチャーをよしに行く", "……"),
            ("口開けろよこ。", "把嘴张开啊，笨蛋。"),
            ("カナオ ジェーム。", "……"),
            ("あっあっあっあっ……！", "啊、啊、啊、啊……！"),
            ("てってれー", "锵锵——！"),
            ("牛肚桂味。", "……"),
            ("珍愛。", "珍爱。"),
            ("ガッティーユーティー。", "……"),
            ("南京交通大学。", "南京交通大学。"),
            ("ぐせんぐせ", "……"),
            ("不用不用不用。", "不用不用不用。"),
            ("うずい", "……"),
            ("にむし。", "……"),
            ("顕著で、口の中から体を大吉は大吉。", "……"),
            ("the", "……"),
            ("刚才写的。", "刚才写的。"),
            ("来者不善。", "来者不善。"),
            ("ちびな生き遅れ争うかいかんよいかん捕獲されちまう", "……"),
            ("かんちゃん。", "Kan。"),
            ("ちゅうはぁぁぁぁぁぁぁぁ", "啾——"),
            ("不要不要不要不要不要不要。", "不要不要不要不要不要不要。"),
            ("好快好快。", "好快好快。"),
            (
                "で、その次の日、赤すり行って、真っ裸で、真っ裸で、そうそう、",
                "然后第二天去了搓澡店，全身赤裸着，对，对，",
            ),
            (
                "赤すりのやってくれる女の人に、真っ裸でガーって。",
                "让搓澡的女工作人员给我全身搓了一遍。",
            ),
            ("チャンスの時間。", "《机会时间》。"),
            ("晚安，小乖。", "晚安，小乖。"),
            ("ストラッシュ", "STLASSH。"),
            ("of where.", "……"),
            ("エルエル。", "LL。"),
            ("九十十一。", "九、十和十一。"),
            ("我送", "……"),
            ("うむやのやや", "……"),
            ("解除解除。", "解除，解除。"),
            ("可以可以可以。", "可以，可以，可以。"),
            (
                "ちょっと八つ揚げさんにむしろ前義なしはなんかちょっとなんか愛がないとかっていう風にいう声もあったりとかするんです",
                "不过，也有人说，对她来说，没有前戏之类的，会让人觉得有点缺乏爱意。",
            ),
            ("V I O脱毛。", "VIO私处脱毛。"),
            ("コルティ。", "这边。"),
            ("ライブハウス。", "现场音乐演出场所。"),
            ("とても好きな時に出していい。", "特别舒服的时候就可以射出来。"),
            ("意思意思。", "好、好。"),
            ("こびしむ", "……"),
            ("操作 都知。", "……"),
            ("くいく", "要去了。"),
            ("あらかん。", "哎呀。"),
            ("あい。", "嗯。"),
            ("たらしい。", "好像是这样。"),
            ("ちょめちょめって何ですか？", "“那个那个”是什么意思？"),
            ("ファースト。", "第一（First）。"),
            ("はああああああああ", "哈啊啊啊啊啊啊啊——"),
            ("ハイボールです。", "是高球酒。"),
            ("キングコープ。", "King Corp公司。"),
            ("しもんちゃんの", "是Shimon的。"),
            ("我有记事典章。", "……"),
            ("数学物理学。", "数学、物理学。"),
            ("ません。", "……"),
            ("宝玉与q有性格。", "……"),
            ("支持燕京站。", "……"),
            ("かなさんの方がなんか。", "Kana反而看起来……"),
            ("奥。", "深处。"),
            ("すみちゃん、理由が回したくてね。", "Sumi，我还想再来一轮。"),
            (
                "じゃあつみさんからシャンパーいただきました。",
                "那么，我们收到了Tsumugi送的香槟。",
            ),
            ("てます。", "……"),
            (
                "冰冰冰冰冰冰手套不怕冰，他要求吉吉不戴套，只握手刚好。",
                "冰冰冰冰冰冰手套不怕冰，他要求吉吉不戴套，只握手刚好。",
            ),
            (
                "チュウされたんだけど、何チュウされたんだけど。",
                "我被亲了，不过到底亲的是哪里？",
            ),
            ("えみかちゃんに合わせる顔がないよ。", "我都没脸见Emika了。"),
            ("デミコ。", "Emika。"),
            (
                "今のままの成績じゃ志望校に行けないと言われましてね。",
                "老师说，照现在的成绩，考不上目标学校。",
            ),
            ("スムレちゃんの体見", "我想看看Sumire的身体……"),
            ("呸呸呸呸", "呸、呸、呸、呸。"),
            ("ひくあい", "……"),
            (
                "谁也不认识谁，诚信交友，不提兼职交友。",
                "谁也不认识谁，诚信交友，不提兼职交友。",
            ),
            ("かざいて逆線に一方でちょっとざまんことあります", "……"),
            ("あ、おまかみゆいです。", "啊，是天神Yui。"),
            ("あ、おまかみゆいさん。", "啊，是天神Yui小姐。"),
            ("えきど。", "……"),
            ("えんのえって…", "说到“en”里的“e”……"),
            ("たいとうとう。", "……"),
            ("都足以改变人生。", "都足以改变人生。"),
            ("我我看看。", "我、我看看。"),
            ("てるのってさ、何じゃないの？", "你说的“在做”到底是什么？"),
            ("タウンシノ。", "……"),
            ("たくろう。", "Takuro。"),
            ("マインドエンサー。", "来得及的。"),
            ("あらへんぬく", "……"),
            ("ざるしゃる。", "……"),
            ("なんか 前原さんも反応してんの？", "怎么，前原也有反应了？"),
            ("大王大王。", "……"),
            ("たちかさか", "……"),
            ("しもんべく。", "……"),
            ("我不是老公。", "我不是你老公。"),
            (
                "hello大家好，我叫舒曼，我今年二十五岁，身高一百六十八公分，体重五十五公斤，",
                "大家好，我叫舒曼，我今年二十五岁，身高一百六十八厘米，体重五十五公斤，",
            ),
            ("我比较喜欢", "我比较喜欢"),
            ("一年以内。", "一年以内。"),
            ("っ、んんんんんんっ…んんんんんんっ…", "嗯……嗯……"),
            ("河马奥特曼。", "河马奥特曼。"),
            ("警察、警察。", "警察，警察。"),
            ("はにせんぼんのまず指切った。", "要是说谎就吞一千根针，拉钩了。"),
            ("ねえしゅ", "喂……"),
            ("宇宙大王。", "宇宙大王。"),
            ("えずさんの奥まで…", "一直到最深处……"),
            ("えらなあんんんんんんんんんん。", "呃……啊……"),
            ("スカイ。", "Sky。"),
            ("あすか…んんっ…", "Asuka……嗯……"),
            ("がいくすと", "……"),
            ("五十九、二十九です。", "59、29。"),
            ("大和。", "Yamato。"),
            ("きこき", "……"),
            ("ちゅうん", "啾"),
            ("青万寿寺", "湿漉漉的？"),
            ("张子健。", "张子健。"),
            (
                "ひだめ、ひだめ、ひだめ、ひだめ、ひだめ。",
                "不行、不行、不行、不行、不行。",
            ),
            (
                "れろれろれろれれろれれろれれろれれろれれろれれろれれろれれろれ",
                "……",
            ),
            ("と。", "……"),
        )
        for source_text, target_text in new_exact_cues:
            with self.subTest(new_exact_source=source_text):
                self.assertEqual(
                    translation_flags(
                        source_text, target_text, glossary, simplifier=identity
                    ),
                    [],
                )
        exact_glossary_source = (cue("exact", "チャンスの時間。"),)
        exact_glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "title",
                    "チャンスの時間",
                    "Chance no Jikan",
                    (),
                    ("exact",),
                    False,
                ),
            ),
            (),
            "口语",
            source_cue_digest(exact_glossary_source),
        )
        self.assertEqual(
            translation_flags(
                "チャンスの時間。",
                "《机会时间》。",
                exact_glossary,
                simplifier=identity,
            ),
            [],
        )
        asuka_source = (cue("asuka", "あすか。"),)
        asuka_glossary = Glossary(
            "1",
            (GlossaryEntry("name", "あすか", "明日香", (), ("asuka",), False),),
            (),
            "口语",
            source_cue_digest(asuka_source),
        )
        self.assertEqual(
            translation_flags(
                "あすか。", "Asuka。", asuka_glossary, simplifier=identity
            ),
            [],
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("未知名。", "Unknown。", glossary, simplifier=identity),
        )
        self.assertEqual(
            translation_flags("ちり", "……", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("にのう。", "……", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags(
                "好きな長さは眺め固めって感じ。",
                "喜欢的长度嘛，我喜欢长一点、硬一点的。",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        self.assertEqual(
            translation_flags("。", "……", glossary, simplifier=identity),
            [],
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("。", "！？", glossary, simplifier=identity),
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("て。", "！？", glossary, simplifier=identity),
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("ながい", "……", glossary, simplifier=identity),
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("長い残片。", "……", glossary, simplifier=identity),
        )
        self.assertEqual(
            translation_flags(
                "希望、希望。", "希望、希望。", glossary, simplifier=identity
            ),
            [],
        )
        self.assertEqual(
            translation_flags(
                "五十以上、五十以下。",
                "五十以上、五十以下。",
                glossary,
                simplifier=identity,
            ),
            [],
        )
        self.assertIn(
            "unchanged_source",
            translation_flags("23年。", "23年。", glossary, simplifier=identity),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags(
                "九十十一。", "九十十一。", glossary, simplifier=identity
            ),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags(
                "全国有九万的年轻女性在台湾拥有。",
                "全国有九万的年轻女性在台湾拥有。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags(
                "谁也不认识谁，诚信交友不提前指交友。",
                "谁也不认识谁，诚信交友不提前指交友。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "traditional_character",
            translation_flags(
                "知名女優。",
                "知名女優。",
                glossary,
                simplifier=lambda value: value.replace("優", "优"),
            ),
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("b。", "b。", glossary, simplifier=identity),
        )
        self.assertIn(
            "japanese_remaining",
            translation_flags("ええ、せ", "ええ、せ", glossary, simplifier=identity),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags("大丈夫", "大丈夫", glossary, simplifier=identity),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags("本当", "本当", glossary, simplifier=identity),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags(
                "牛肚桂味。", "牛肚桂味。", glossary, simplifier=identity
            ),
        )
        self.assertIn(
            "unchanged_source",
            translation_flags(
                "我的水杯。", "我的水杯。", glossary, simplifier=identity
            ),
        )
        self.assertEqual(
            translation_flags("我的水杯。", "……", glossary, simplifier=identity),
            [],
        )
        for source_text in (
            "2000年 世界第32位。",
            "看一下口味。",
            "七七点COM",
            "AV大平台。",
            "记得不是。",
            "无任何广告，完全不卡顿。",
        ):
            with self.subTest(source_text=source_text):
                self.assertNotIn(
                    "unchanged_source",
                    translation_flags(
                        source_text,
                        source_text,
                        glossary,
                        simplifier=identity,
                    ),
                )
        source_ad = "亚洲唯一官方赞助商，实力有保证，二十分钟内秒出款。"
        self.assertNotIn(
            "unchanged_source",
            translation_flags(source_ad, source_ad, glossary, simplifier=identity),
        )
        self.assertNotIn(
            "suspiciously_short",
            translation_flags(
                "かなああああああ",
                "啊……",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "suspiciously_short",
            translation_flags(
                "ありがとうございます",
                "啊……",
                glossary,
                simplifier=identity,
            ),
        )
        for source in ("胡椒梅。", "喝了。", "不。", "萌。", "三年。"):
            with self.subTest(short_han_source=source):
                self.assertNotIn(
                    "unchanged_source",
                    translation_flags(source, source, glossary, simplifier=identity),
                )
        self.assertEqual(
            translation_flags("。", "。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("vc。", "VC。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("tiktok。", "TikTok。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("iphone。", "iPhone。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("アベマ。", "ABEMA。", glossary, simplifier=identity),
            [],
        )
        self.assertEqual(
            translation_flags("エス?エム?。", "S? M?。", glossary, simplifier=identity),
            [],
        )
        self.assertIn(
            "suspiciously_short",
            translation_flags(
                "これは十分に長い日本語の発話です",
                "好",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "suspiciously_short",
            translation_flags("うん", "嗯。", glossary, simplifier=identity),
        )
        self.assertNotIn(
            "suspiciously_short",
            translation_flags(
                "ありがとうございます。",
                "谢谢。",
                glossary,
                simplifier=identity,
            ),
        )
        explicit_source = (cue("c1", "射精して、さっき出したばっかのに"),)
        explicit_glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "action",
                    "出した",
                    "射了",
                    (),
                    ("c1",),
                    True,
                ),
            ),
            (),
            "露骨口语",
            source_cue_digest(explicit_source),
        )
        self.assertNotIn(
            "explicit_term_missing",
            translation_flags(
                explicit_source[0].text,
                "明明刚才才射出来的",
                explicit_glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "explicit_term_missing",
            translation_flags(
                explicit_source[0].text,
                "明明刚才才出来的",
                explicit_glossary,
                simplifier=identity,
            ),
        )
        insertion_source = (cue("c1", "ちんぽ、そろそろ入れない。"),)
        insertion_glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "action",
                    "入れない",
                    "插入",
                    (),
                    ("c1",),
                    True,
                ),
            ),
            (),
            "露骨口语",
            source_cue_digest(insertion_source),
        )
        self.assertNotIn(
            "explicit_term_missing",
            translation_flags(
                insertion_source[0].text,
                "差不多该插进去了。",
                insertion_glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "explicit_term_missing",
            translation_flags(
                insertion_source[0].text,
                "差不多该放进去了。",
                insertion_glossary,
                simplifier=identity,
            ),
        )
        bare_source = (cue("c1", "私と生でする方が気持ちいい。"),)
        bare_glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "action",
                    "生",
                    "不戴套",
                    (),
                    ("c1",),
                    True,
                ),
            ),
            (),
            "露骨口语",
            source_cue_digest(bare_source),
        )
        self.assertNotIn(
            "explicit_term_missing",
            translation_flags(
                bare_source[0].text,
                "和我裸着做更舒服。",
                bare_glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "explicit_term_missing",
            translation_flags(
                bare_source[0].text,
                "和我做更舒服。",
                bare_glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "explicit_term_missing",
            translation_flags(
                "さっきの先生、できちゃっ",
                "刚才的老师，已经射了……",
                bare_glossary,
                simplifier=identity,
            ),
        )
        generic_source = (cue("c1", "何回でもできるよ。"),)
        generic_glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "action",
                    "何回",
                    "多少次",
                    (),
                    ("c1",),
                    True,
                ),
            ),
            (),
            "口语",
            source_cue_digest(generic_source),
        )
        self.assertNotIn(
            "explicit_term_missing",
            translation_flags(
                generic_source[0].text,
                "随时都可以。",
                generic_glossary,
                simplifier=identity,
            ),
        )
        manner_source = (cue("c1", "激しく。"),)
        manner_glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "manner",
                    "激しく",
                    "激烈地",
                    (),
                    ("c1",),
                    False,
                ),
            ),
            (),
            "口语",
            source_cue_digest(manner_source),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                manner_source[0].text,
                "激烈。",
                manner_glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "glossary_term_missing",
            translation_flags(
                manner_source[0].text,
                "猛烈。",
                manner_glossary,
                simplifier=identity,
            ),
        )
        student_source = (cue("student", "男子生徒。"),)
        student_glossary = Glossary(
            "1",
            (GlossaryEntry("term", "生徒", "学生", (), ("student",), False),),
            (),
            "口语",
            source_cue_digest(student_source),
        )
        self.assertNotIn(
            "glossary_term_missing",
            translation_flags(
                "男子生徒。", "那些男生。", student_glossary, simplifier=identity
            ),
        )
        self.assertIn(
            "traditional_character",
            translation_flags(
                "体です",
                "繁體字",
                glossary,
                simplifier=lambda value: value.replace("體", "体"),
            ),
        )
        self.assertNotIn(
            "traditional_character",
            translation_flags("一花です", "一花来了。", glossary, simplifier=identity),
        )
        ordinary_term = Glossary(
            "1",
            (GlossaryEntry("term", "體", "体", (), ("c1",), False),),
            (),
            "口语",
            source_cue_digest((cue("c1", "體です"),)),
        )
        self.assertIn(
            "traditional_character",
            translation_flags(
                "體です",
                "繁體",
                ordinary_term,
                simplifier=lambda value: value.replace("體", "体"),
            ),
        )
        self.assertIn(
            "not_simplified_chinese",
            translation_flags("はい。", "Okay.", glossary, simplifier=identity),
        )
        self.assertIn(
            "glossary_term_missing",
            translation_flags(
                "イチカです。",
                "我是伊知卡。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "refusal",
            translation_flags(
                "これは会社の方針です。",
                "这是公司的政策。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "refusal",
            translation_flags(
                "手伝って。", "我不能帮助你。", glossary, simplifier=identity
            ),
        )
        self.assertNotIn(
            "refusal",
            translation_flags(
                "規則です。", "不能违反政策。", glossary, simplifier=identity
            ),
        )
        self.assertIn(
            "refusal",
            translation_flags(
                "普通の台詞です。",
                "抱歉，我无\u200b 法协助翻译该内容。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertNotIn(
            "refusal",
            translation_flags(
                "申し訳ないが翻訳できない。",
                "抱歉，我不能翻译。",
                glossary,
                simplifier=identity,
            ),
        )
        for source_dialogue, translated_dialogue in (
            ("翻訳を手伝えない。", "抱歉，我不能帮你翻译。"),
            ("申し訳ありませんが翻訳できません。", "抱歉，我不能翻译。"),
        ):
            with self.subTest(source_dialogue=source_dialogue):
                self.assertNotIn(
                    "refusal",
                    translation_flags(
                        source_dialogue,
                        translated_dialogue,
                        glossary,
                        simplifier=identity,
                    ),
                )
        self.assertIn(
            "refusal",
            translation_flags(
                "彼は翻訳できないと言ったが、私はできます。",
                "抱歉，我无法协助翻译该内容。",
                glossary,
                simplifier=identity,
            ),
        )
        self.assertIn(
            "refusal",
            translation_flags(
                "この内容には無理がある。",
                "抱歉，我不能翻译。",
                glossary,
                simplifier=identity,
            ),
        )
        for boilerplate in (
            "对不起，我没办法帮你。",
            "抱歉，我无法提供这类内容。",
            "抱歉，我无\ufe0f法协助翻译该内容。",
            "抱歉，我无\u0301法协助翻译该内容。",
        ):
            with self.subTest(boilerplate=boilerplate):
                self.assertIn(
                    "refusal",
                    translation_flags(
                        "普通の台詞です。",
                        boilerplate,
                        glossary,
                        simplifier=identity,
                    ),
                )

    def test_missing_opencc_is_an_actionable_blocking_dependency(self):
        with patch.dict(sys.modules, {"opencc": None}):
            with self.assertRaises(MissingTranslationDependency) as raised:
                load_simplifier()
        self.assertIn("opencc-python-reimplemented", str(raised.exception))


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(message=types.SimpleNamespace(content=response))
            ]
        )


class FakeClient:
    def __init__(self, responses):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(responses))


class TranslationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.sources = (
            cue("c0", "前。"),
            cue("c1", "一花です。", 1000),
            cue("c2", "後。", 2000),
        )
        self.block = build_translation_blocks(
            self.sources,
            commit_size=1,
            context_size=10,
        )[1]
        self.glossary_value = strict_glossary(self.sources)
        self.glossary = attested_glossary(self.glossary_value)
        self.primary = ModelConfig.primary(revision="rev-primary", max_tokens=2048)
        self.digest = timestamp_digest(self.block.commit_cues)
        self.source_digest = source_cue_digest(self.sources)
        self.glossary_digest = glossary_digest(self.glossary_value)

    def test_primary_request_is_pinned_contextual_and_deterministic(self):
        client = FakeClient(['{"cues":[{"id":"c1","zh":"我是一花。"}]}'])
        artifact = translate_block(
            client,
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        self.assertEqual(artifact.texts, (("c1", "我是一花。"),))
        self.assertEqual(
            artifact.timestamp_digest, timestamp_digest(self.block.commit_cues)
        )
        call = client.chat.completions.calls[0]
        self.assertIn("失礼します", call["messages"][0]["content"])
        self.assertIn("那我先挂了", call["messages"][0]["content"])
        self.assertIn("校园霸凌", call["messages"][0]["content"])
        self.assertEqual(call["model"], PRIMARY_MODEL_ID)
        self.assertEqual(call["temperature"], 0.2)
        self.assertEqual(call["top_p"], 0.8)
        self.assertEqual(call["seed"], 20260710)
        self.assertEqual(call["n"], 1)
        self.assertEqual(call["max_tokens"], 2048)
        self.assertEqual(call["response_format"]["type"], "json_schema")
        schema = call["response_format"]["json_schema"]["schema"]
        self.assertEqual(
            schema["properties"]["cues"]["prefixItems"][0]["properties"]["id"]["const"],
            "c1",
        )
        self.assertEqual(
            call["extra_body"],
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        user_payload = json.loads(call["messages"][1]["content"])
        self.assertEqual([row["id"] for row in user_payload["COMMIT_CUES"]], ["c1"])
        self.assertEqual(
            [row["id"] for row in user_payload["READ_ONLY_CONTEXT"]["before"]],
            ["c0"],
        )
        self.assertEqual(
            [row["id"] for row in user_payload["READ_ONLY_CONTEXT"]["after"]],
            ["c2"],
        )
        self.assertEqual(user_payload["model_revision"], "rev-primary")
        self.assertEqual(user_payload["source_digest"], self.source_digest)
        self.assertEqual(user_payload["glossary_digest"], self.glossary_digest)

    def test_invalid_and_refused_responses_retry_with_validation_feedback(self):
        client = FakeClient(
            [
                "not json",
                '{"cues":[{"id":"c1","zh":"抱歉，我无法协助翻译该内容。"}]}',
                '{"cues":[{"id":"c1","zh":"我是一花。"}]}',
            ]
        )
        artifact = translate_block(
            client,
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["invalid_json", "refusal", "ok"],
        )
        calls = client.chat.completions.calls
        payloads = [json.loads(call["messages"][1]["content"]) for call in calls]
        self.assertNotIn("VALIDATION_FEEDBACK", payloads[0])
        self.assertIn("strict JSON validation", payloads[1]["VALIDATION_FEEDBACK"])
        self.assertIn("quality gates", payloads[2]["VALIDATION_FEEDBACK"])
        self.assertIn("refusal", payloads[2]["VALIDATION_FEEDBACK"])
        self.assertEqual(payloads[1]["VALIDATION_ATTEMPT"], 2)
        self.assertEqual(payloads[2]["VALIDATION_ATTEMPT"], 3)
        self.assertNotEqual(calls[0], calls[1])
        self.assertNotEqual(calls[1], calls[2])
        self.assertEqual(artifact.attempts[0].raw, "not json")

    def test_primary_request_supplies_exact_name_and_asr_corrections_up_front(self):
        sources = (
            cue("c0", "明かり紡ぎです。"),
            cue("c1", "我的水杯。", 1000),
        )
        block = build_translation_blocks(sources, commit_size=2)[0]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        glossary = attested_glossary(glossary_value)
        client = FakeClient(
            [
                '{"cues":[{"id":"c0","zh":"我是Akari Tsumugi老师。"},'
                '{"id":"c1","zh":"……"}]}'
            ]
        )
        artifact = translate_block(
            client,
            block,
            glossary,
            self.primary,
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary_value),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        call = client.chat.completions.calls[0]
        self.assertIn("EXACT_KANA_NAME_ROMANIZATIONS", call["messages"][0]["content"])
        payload = json.loads(call["messages"][1]["content"])
        self.assertEqual(
            payload["EXACT_KANA_NAME_ROMANIZATIONS"],
            {"c0": {"明かり紡ぎ": "Akari Tsumugi"}},
        )
        self.assertEqual(
            payload["EXACT_CONTEXTUAL_ASR_CORRECTIONS"],
            {"c1": {"我的水杯": "……"}},
        )
        self.assertEqual(
            payload["EXACT_CUE_TRANSLATIONS"],
            {"c0": "我是Akari Tsumugi老师。"},
        )

    def test_cue_specific_exact_translation_is_applied_deterministically(self):
        cue_id = (
            "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
            "-seg-00000300-cue-0000"
        )
        sources = (cue(cue_id, "あめんさん"),)
        block = build_translation_blocks(sources, commit_size=1)[0]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        client = FakeClient([])

        artifact = translate_block(
            client,
            block,
            attested_glossary(glossary_value),
            self.primary,
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary_value),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        self.assertEqual(artifact.texts, ((cue_id, "……"),))
        correction_evidence = json.loads(artifact.attempts[-1].message)
        self.assertEqual(
            correction_evidence["deterministic_exact_short_circuit"],
            {cue_id: "……"},
        )
        self.assertEqual(client.chat.completions.calls, [])

    def test_isolated_particle_retry_feedback_forbids_punctuation_only_output(self):
        sources = (
            cue("c0", "前。"),
            cue("c1", "よ。", 1000),
            cue("c2", "後。", 2000),
        )
        block = build_translation_blocks(sources, commit_size=1, context_size=10)[1]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        client = FakeClient(
            [
                '{"cues":[{"id":"c1","zh":"。"}]}',
                '{"cues":[{"id":"c1","zh":"啊。"}]}',
            ]
        )
        artifact = translate_block(
            client,
            block,
            attested_glossary(glossary_value),
            self.primary,
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=source_cue_digest(sources),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        self.assertEqual(artifact.texts, (("c1", "啊。"),))
        feedback = json.loads(
            client.chat.completions.calls[1]["messages"][1]["content"]
        )["VALIDATION_FEEDBACK"]
        self.assertIn("an isolated よ should become 啊 or 哦", feedback)
        self.assertIn("a lone full stop is never acceptable", feedback)

    def test_length_limited_json_retry_forbids_repetition_beyond_source(self):
        class LengthThenValidCompletions:
            def __init__(self):
                self.calls = []

            def create(inner_self, **kwargs):
                inner_self.calls.append(kwargs)
                first = len(inner_self.calls) == 1
                content = (
                    '{"cues":[{"id":"c1","zh":"' + "痛" * 200
                    if first
                    else '{"cues":[{"id":"c1","zh":"我是一花。"}]}'
                )
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            finish_reason="length" if first else "stop",
                            message=types.SimpleNamespace(content=content),
                        )
                    ]
                )

        completions = LengthThenValidCompletions()
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )
        artifact = translate_block(
            client,
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        feedback = json.loads(completions.calls[1]["messages"][1]["content"])[
            "VALIDATION_FEEDBACK"
        ]
        self.assertIn("generation reached its length limit", feedback)
        self.assertIn("never repeat a word more times than its source cue", feedback)

    def test_context_inconsistent_asr_retry_supplies_exact_cue_correction(self):
        sources = (
            cue("c0", "ああ。"),
            cue("c1", "我的水杯。", 1000),
            cue("c2", "行く。", 2000),
        )
        block = build_translation_blocks(sources, commit_size=1, context_size=10)[1]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        client = FakeClient(
            [
                '{"cues":[{"id":"c1","zh":"我的水杯。"}]}',
                '{"cues":[{"id":"c1","zh":"……"}]}',
            ]
        )
        artifact = translate_block(
            client,
            block,
            attested_glossary(glossary_value),
            self.primary,
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=source_cue_digest(sources),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        self.assertEqual(artifact.texts, (("c1", "……"),))
        feedback = json.loads(
            client.chat.completions.calls[1]["messages"][1]["content"]
        )["VALIDATION_FEEDBACK"]
        self.assertIn('"我的水杯":"……"', feedback)

    def test_fifth_failure_is_blocked_with_all_raw_attempts_and_no_placeholder(self):
        responses = [f"bad {index}" for index in range(1, 6)]
        artifact = translate_block(
            FakeClient(responses),
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "blocked")
        self.assertEqual(artifact.texts, ())
        self.assertEqual(len(artifact.attempts), 5)
        self.assertEqual([attempt.raw for attempt in artifact.attempts], responses)

    def test_client_exception_is_typed_and_retried(self):
        client = FakeClient(
            [RuntimeError("endpoint down"), '{"cues":[{"id":"c1","zh":"我是一花。"}]}']
        )
        artifact = translate_block(
            client,
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "draft_ok")
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["client_exception", "ok"],
        )
        self.assertIn("endpoint down", artifact.attempts[0].message)

    def test_malformed_response_envelope_retains_raw_repr_evidence(self):
        artifact = translate_block(
            FakeClient([object(), object(), object(), object(), object()]),
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "blocked")
        self.assertTrue(all(attempt.raw for attempt in artifact.attempts))
        self.assertTrue(all("content=" in attempt.raw for attempt in artifact.attempts))

    def test_glossary_extraction_retries_hallucinated_evidence(self):
        block_glossary = strict_glossary(self.block.commit_cues)
        payload = block_glossary.to_dict()
        payload.pop("source_digest")
        valid = json.dumps(payload, ensure_ascii=False)
        invalid = valid.replace('"c1"', '"missing"')
        client = FakeClient([invalid, valid])
        artifact = extract_glossary(
            client,
            self.block.commit_cues,
            self.primary,
        )

        self.assertEqual(artifact.status, "ok")
        self.assertEqual(artifact.glossary, block_glossary)
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["schema_mismatch", "ok"],
        )
        first_payload = json.loads(
            client.chat.completions.calls[0]["messages"][1]["content"]
        )
        second_payload = json.loads(
            client.chat.completions.calls[1]["messages"][1]["content"]
        )
        self.assertNotIn("VALIDATION_FEEDBACK", first_payload)
        entry_schema = client.chat.completions.calls[0]["response_format"][
            "json_schema"
        ]["schema"]["properties"]["entries"]["items"]
        evidence_schema = entry_schema["properties"]["evidence_ids"]
        self.assertEqual(evidence_schema["minItems"], 2)
        self.assertEqual(evidence_schema["maxItems"], 4)
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"]["json_schema"][
                "schema"
            ]["properties"]["entries"]["maxItems"],
            20,
        )
        self.assertNotIn("uniqueItems", evidence_schema)
        feedback = second_payload["VALIDATION_FEEDBACK"]
        self.assertIn("unknown cue ID", feedback)
        self.assertIn("Regenerate the complete JSON", feedback)
        self.assertIn("copied verbatim from a cited cue", feedback)
        self.assertNotIn(invalid, feedback)

    def test_long_cue_ids_are_aliased_only_on_the_model_boundary(self):
        long_ids = (
            "a" * 64 + "-seg-00000001-cue-0000",
            "a" * 64 + "-seg-00000002-cue-0000",
        )
        sources = (
            cue(long_ids[0], "一花です。"),
            cue(long_ids[1], "一花ですよ。", 1000),
        )
        glossary = Glossary(
            "1",
            (
                GlossaryEntry(
                    "name",
                    "一花",
                    "一花",
                    (),
                    long_ids,
                    False,
                ),
            ),
            (),
            "口语",
            source_cue_digest(sources),
        )
        artifact_glossary = attested_glossary(glossary)
        block = build_translation_blocks(sources)[0]
        client = FakeClient(
            [
                json.dumps(
                    {
                        "cues": [
                            {"id": "s00000001c0000", "zh": "我是一花。"},
                            {"id": "s00000002c0000", "zh": "我是一花哦。"},
                        ]
                    },
                    ensure_ascii=False,
                )
            ]
        )

        artifact = translate_block(
            client,
            block,
            artifact_glossary,
            self.primary,
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary),
            simplifier=identity,
        )

        request = client.chat.completions.calls[0]
        payload = json.loads(request["messages"][1]["content"])
        model_ids = [row["id"] for row in payload["COMMIT_CUES"]]
        self.assertEqual(model_ids, ["s00000001c0000", "s00000002c0000"])
        self.assertNotIn(long_ids[0], request["messages"][1]["content"])
        self.assertEqual(
            artifact.texts,
            ((long_ids[0], "我是一花。"), (long_ids[1], "我是一花哦。")),
        )
        self.assertIn('"id": "s00000001c0000"', artifact.attempts[-1].raw)

    def test_glossary_aliases_model_evidence_and_restores_original_ids(self):
        long_ids = (
            "b" * 64 + "-seg-00000001-cue-0000",
            "b" * 64 + "-seg-00000002-cue-0000",
        )
        sources = (
            cue(long_ids[0], "一花です。"),
            cue(long_ids[1], "一花ですよ。", 1000),
        )
        response = {
            "version": "1",
            "entries": [
                {
                    "category": "name",
                    "ja": "一花",
                    "preferred_zh": "一花",
                    "aliases": [],
                    "evidence_ids": [
                        "s00000001c0000",
                        "s00000002c0000",
                    ],
                    "explicit": False,
                }
            ],
            "relationships": [],
            "register": "口语",
        }
        client = FakeClient([json.dumps(response, ensure_ascii=False)])

        artifact = extract_glossary(client, sources, self.primary)

        payload = client.chat.completions.calls[0]["messages"][1]["content"]
        self.assertNotIn(long_ids[0], payload)
        self.assertEqual(artifact.status, "ok")
        self.assertEqual(artifact.glossary.entries[0].evidence_ids, long_ids)
        self.assertIn("s00000001c0000", artifact.attempts[-1].raw)

    def test_glossary_third_attempt_requests_auditable_empty_fallback(self):
        block_glossary = strict_glossary(self.block.commit_cues)
        payload = block_glossary.to_dict()
        payload.pop("source_digest")
        valid = json.dumps(payload, ensure_ascii=False)
        invalid = valid.replace('"c1"', '"missing"')
        empty = json.dumps(
            {
                "version": "fallback-empty-v1",
                "entries": [],
                "relationships": [],
                "register": "source-matched",
            },
            ensure_ascii=False,
        )
        client = FakeClient([invalid, invalid, empty])

        artifact = extract_glossary(
            client,
            self.block.commit_cues,
            self.primary,
        )

        self.assertEqual(artifact.status, "ok")
        self.assertEqual(artifact.glossary.entries, ())
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["schema_mismatch", "schema_mismatch", "ok"],
        )
        third = client.chat.completions.calls[2]
        third_payload = json.loads(third["messages"][1]["content"])
        self.assertTrue(third_payload["FALLBACK_EMPTY_GLOSSARY"])
        self.assertNotIn("SOURCE_CUES", third_payload)
        self.assertIn("empty glossary", third["messages"][0]["content"].lower())

    def test_review_receives_source_draft_context_and_glossary(self):
        config = ModelConfig.review(
            model_id=PRIMARY_MODEL_ID,
            revision="rev-primary",
            max_tokens=2048,
        )
        client = FakeClient(['{"cues":[{"id":"c1","zh":"我叫一花。"}]}'])
        artifact = review_block(
            client,
            self.block,
            {"c1": "我是一花。"},
            self.glossary,
            config,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.source_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "review_ok")
        payload = json.loads(client.chat.completions.calls[0]["messages"][1]["content"])
        self.assertEqual(payload["DRAFT_CUES"], [{"id": "c1", "zh": "我是一花。"}])
        self.assertEqual(payload["COMMIT_CUES"], [{"id": "c1", "ja": "一花です。"}])
        self.assertIn("glossary", payload)
        self.assertEqual(client.chat.completions.calls[0]["temperature"], 0.1)

    def test_review_falls_back_only_failed_cues_to_validated_draft(self):
        sources = (
            cue("c1", "一花です。"),
            cue("c2", "これは大切な台詞です。", 1000),
        )
        block = build_translation_blocks(sources, commit_size=2)[0]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        response = '{"cues":[{"id":"c1","zh":"我叫一花。"},{"id":"c2","zh":"……"}]}'
        client = FakeClient([response] * 5)

        artifact = review_block(
            client,
            block,
            {"c1": "我是一花。", "c2": "这是一句重要的台词。"},
            attested_glossary(glossary_value),
            ModelConfig.review(
                model_id=PRIMARY_MODEL_ID,
                revision="rev-primary",
                max_tokens=2048,
            ),
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary_value),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "review_ok")
        self.assertEqual(
            artifact.texts,
            (("c1", "我叫一花。"), ("c2", "这是一句重要的台词。")),
        )
        self.assertEqual(len(client.chat.completions.calls), 5)
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["quality_gate"] * 4 + ["ok"],
        )
        evidence = json.loads(artifact.attempts[-1].message)
        self.assertEqual(
            evidence["validated_draft_fallback_policy"],
            "retry-before-fallback-v2",
        )
        fallback = evidence["deterministic_validated_draft_fallbacks"]["c2"]
        self.assertEqual(fallback["draft_zh"], "这是一句重要的台词。")
        self.assertEqual(fallback["rejected_zh"], "……")
        self.assertIn("not_simplified_chinese", fallback["review_flags"])
        self.assertIn("original_raw", evidence)

    def test_review_repairs_ellipsis_before_draft_fallback(self):
        sources = (cue("c1", "これは大切な台詞です。"),)
        block = build_translation_blocks(sources, commit_size=1)[0]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        client = FakeClient(
            [
                '{"cues":[{"id":"c1","zh":"……"}]}',
                '{"cues":[{"id":"c1","zh":"这是一句重要的台词。"}]}',
            ]
        )

        artifact = review_block(
            client,
            block,
            {"c1": "这句台词很重要。"},
            attested_glossary(glossary_value),
            ModelConfig.review(
                model_id=PRIMARY_MODEL_ID,
                revision="rev-primary",
                max_tokens=2048,
            ),
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary_value),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "review_ok")
        self.assertEqual(artifact.texts, (("c1", "这是一句重要的台词。"),))
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["quality_gate", "ok"],
        )
        second_call = client.chat.completions.calls[1]
        self.assertIn("（语音不清）", second_call["messages"][0]["content"])
        self.assertIn("（语音不清）", second_call["messages"][1]["content"])

    def test_review_retries_forbidden_format_character(self):
        bad = '{"cues":[{"id":"c1","zh":"我叫一\u200b花。"}]}'
        good = '{"cues":[{"id":"c1","zh":"我叫一花。"}]}'
        client = FakeClient([bad, good])

        artifact = review_block(
            client,
            self.block,
            {"c1": "我是一\u200b花。"},
            self.glossary,
            ModelConfig.review(
                model_id=PRIMARY_MODEL_ID,
                revision="rev-primary",
                max_tokens=2048,
            ),
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.glossary_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "review_ok")
        self.assertEqual(artifact.texts, (("c1", "我叫一花。"),))
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["quality_gate", "ok"],
        )
        self.assertIn("forbidden_character", artifact.attempts[0].message)
        self.assertIn(
            "zero-width spaces",
            client.chat.completions.calls[1]["messages"][1]["content"],
        )

    def test_review_accepts_explicit_inaudible_marker(self):
        sources = (cue("c1", "文脈と一致しない非常に長い認識断片です。"),)
        block = build_translation_blocks(sources, commit_size=1)[0]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        response = '{"cues":[{"id":"c1","zh":"（语音不清）"}]}'
        client = FakeClient([response] * 5)

        artifact = review_block(
            client,
            block,
            {"c1": "这是一段与上下文不符的很长识别片段。"},
            attested_glossary(glossary_value),
            ModelConfig.review(
                model_id=PRIMARY_MODEL_ID,
                revision="rev-primary",
                max_tokens=2048,
            ),
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary_value),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "review_ok")
        self.assertEqual(artifact.texts, (("c1", "（语音不清）"),))
        self.assertEqual(len(client.chat.completions.calls), 5)
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["quality_gate"] * 4 + ["ok"],
        )
        evidence = json.loads(artifact.attempts[-1].message)
        self.assertEqual(
            evidence["confirmed_inaudible_policy"],
            "review-retry-confirmed-v1",
        )
        self.assertEqual(
            evidence["confirmed_inaudible_markers"]["c1"]["source_ja"],
            sources[0].text,
        )

    def test_review_retries_unconfirmed_inaudible_marker(self):
        sources = (cue("c1", "お天気予報中。"),)
        block = build_translation_blocks(sources, commit_size=1)[0]
        glossary_value = Glossary(
            version="1",
            entries=(),
            relationships=(),
            register="口语",
            source_digest=source_cue_digest(sources),
        )
        client = FakeClient(
            [
                '{"cues":[{"id":"c1","zh":"（语音不清）"}]}',
                '{"cues":[{"id":"c1","zh":"正在播天气预报。"}]}',
            ]
        )

        artifact = review_block(
            client,
            block,
            {"c1": "天气预报中。"},
            attested_glossary(glossary_value),
            ModelConfig.review(
                model_id=PRIMARY_MODEL_ID,
                revision="rev-primary",
                max_tokens=2048,
            ),
            source_cues=sources,
            expected_digest=timestamp_digest(block.commit_cues),
            expected_source_digest=source_cue_digest(sources),
            expected_glossary_digest=glossary_digest(glossary_value),
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "review_ok")
        self.assertEqual(artifact.texts, (("c1", "正在播天气预报。"),))
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["quality_gate", "ok"],
        )
        self.assertIn("inaudible_marker_unconfirmed", artifact.attempts[0].message)
        self.assertIn(
            "translate every recoverable word",
            client.chat.completions.calls[1]["messages"][1]["content"],
        )

    def test_review_does_not_fall_back_to_an_invalid_draft(self):
        response = '{"cues":[{"id":"c1","zh":"……"}]}'
        artifact = review_block(
            FakeClient([response] * 5),
            self.block,
            {"c1": "……"},
            self.glossary,
            ModelConfig.review(
                model_id=PRIMARY_MODEL_ID,
                revision="rev-primary",
                max_tokens=2048,
            ),
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.glossary_digest,
            simplifier=identity,
        )

        self.assertEqual(artifact.status, "blocked")
        self.assertEqual(
            [attempt.code for attempt in artifact.attempts],
            ["quality_gate"] * 5,
        )

    def test_external_timestamp_digest_is_verified_before_any_request(self):
        client = FakeClient(['{"cues":[{"id":"c1","zh":"我是一花。"}]}'])
        with self.assertRaisesRegex(ValueError, "timestamp digest"):
            translate_block(
                client,
                self.block,
                self.glossary,
                self.primary,
                source_cues=self.sources,
                expected_digest="stale",
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.source_digest,
                simplifier=identity,
            )
        self.assertEqual(client.chat.completions.calls, [])

    def test_mutated_context_and_hallucinated_direct_glossary_are_rejected(self):
        mutated = TranslationBlock(
            before_context=(cue("c0", "被篡改。"),),
            commit_cues=self.block.commit_cues,
            after_context=self.block.after_context,
            source_digest=self.source_digest,
            source_count=3,
            commit_start=1,
        )
        client = FakeClient(['{"cues":[{"id":"c1","zh":"我是一花。"}]}'])
        with self.assertRaisesRegex(ValueError, "window differs"):
            translate_block(
                client,
                mutated,
                self.glossary,
                self.primary,
                source_cues=self.sources,
                expected_digest=self.digest,
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.source_digest,
                simplifier=identity,
            )

        with self.assertRaisesRegex(ValueError, "successful GlossaryArtifact"):
            translate_block(
                client,
                self.block,
                self.glossary_value,
                self.primary,
                source_cues=self.sources,
                expected_digest=self.digest,
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.glossary_digest,
                simplifier=identity,
            )

        same_source_swap = attested_glossary(
            replace(self.glossary_value, register="正式")
        )
        with self.assertRaisesRegex(ValueError, "content digest"):
            translate_block(
                client,
                self.block,
                same_source_swap,
                self.primary,
                source_cues=self.sources,
                expected_digest=self.digest,
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.glossary_digest,
                simplifier=identity,
            )

        hallucinated = Glossary(
            "1",
            (GlossaryEntry("name", "不存在", "虚构", (), ("c1",), False),),
            (),
            "口语",
            self.source_digest,
        )
        with self.assertRaisesRegex(ValueError, "lacks.*source evidence"):
            translate_block(
                client,
                self.block,
                attested_glossary(hallucinated),
                self.primary,
                source_cues=self.sources,
                expected_digest=self.digest,
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.source_digest,
                simplifier=identity,
            )
        self.assertEqual(client.chat.completions.calls, [])

    def test_finalization_requires_baseline_and_mandatory_escalation_coverage(self):
        source = (cue("x1", "裸です。"),)
        block = build_translation_blocks(source)[0]
        source_digest = source_cue_digest(source)
        digest = timestamp_digest(source)
        glossary = Glossary(
            "1",
            (GlossaryEntry("explicit", "裸", "裸体", (), ("x1",), True),),
            (),
            "露骨口语",
            source_digest,
        )
        glossary_content_digest = glossary_digest(glossary)
        glossary_artifact = attested_glossary(glossary)
        primary = translate_block(
            FakeClient(['{"cues":[{"id":"x1","zh":"裸体。"}]}']),
            block,
            glossary_artifact,
            self.primary,
            source_cues=source,
            expected_digest=digest,
            expected_source_digest=source_digest,
            expected_glossary_digest=glossary_content_digest,
            simplifier=identity,
        )
        baseline_config = ModelConfig.review(
            model_id=PRIMARY_MODEL_ID,
            revision="rev-primary",
            max_tokens=2048,
        )
        baseline = review_block(
            FakeClient(['{"cues":[{"id":"x1","zh":"裸体。"}]}']),
            block,
            dict(primary.texts),
            glossary_artifact,
            baseline_config,
            source_cues=source,
            expected_digest=digest,
            expected_source_digest=source_digest,
            expected_glossary_digest=glossary_content_digest,
            simplifier=identity,
        )
        selection = build_escalation_selection(
            source,
            glossary_artifact,
            baseline.texts,
            expected_glossary_digest=glossary_content_digest,
            flagged_ids=(),
            ambiguous_ids=(),
        )
        self.assertEqual(selection.required_ids, ("x1",))

        with self.assertRaisesRegex(ValueError, "escalation coverage"):
            finalize_reviewed_video(
                source,
                (primary,),
                (baseline,),
                (),
                glossary_artifact,
                selection,
                expected_timestamp_digest=digest,
                expected_source_digest=source_digest,
                expected_glossary_digest=glossary_content_digest,
                simplifier=identity,
            )

        changed_baseline = review_block(
            FakeClient(['{"cues":[{"id":"x1","zh":"我是裸体。"}]}']),
            block,
            dict(primary.texts),
            glossary_artifact,
            baseline_config,
            source_cues=source,
            expected_digest=digest,
            expected_source_digest=source_digest,
            expected_glossary_digest=glossary_content_digest,
            simplifier=identity,
        )
        with self.assertRaisesRegex(ValueError, "selection baseline digest"):
            finalize_reviewed_video(
                source,
                (primary,),
                (changed_baseline,),
                (),
                glossary_artifact,
                selection,
                expected_timestamp_digest=digest,
                expected_source_digest=source_digest,
                expected_glossary_digest=glossary_content_digest,
                simplifier=identity,
            )

        escalation = review_block(
            FakeClient(['{"cues":[{"id":"x1","zh":"裸体。"}]}']),
            block,
            dict(baseline.texts),
            glossary_artifact,
            ModelConfig.escalation(revision="rev-reviewer", max_tokens=2048),
            source_cues=source,
            expected_digest=digest,
            expected_source_digest=source_digest,
            expected_glossary_digest=glossary_content_digest,
            simplifier=identity,
        )
        final = finalize_reviewed_video(
            source,
            (primary,),
            (baseline,),
            (escalation,),
            glossary_artifact,
            selection,
            expected_timestamp_digest=digest,
            expected_source_digest=source_digest,
            expected_glossary_digest=glossary_content_digest,
            simplifier=identity,
        )
        self.assertIsInstance(final, ReviewedTranslationArtifact)
        self.assertEqual(final.status, "ok")
        self.assertEqual(final.texts, (("x1", "裸体。"),))
        self.assertEqual(final.required_escalation_ids, ("x1",))
        self.assertEqual(final.timestamp_digest, digest)
        self.assertEqual(
            final.primary_models,
            ((PRIMARY_MODEL_ID, "rev-primary"),),
        )
        with self.assertRaisesRegex(ValueError, "finalizer-only"):
            replace(final, texts=(("x1", "伪造。"),))

    def test_translation_artifacts_reject_forged_provenance_and_raw_evidence(self):
        artifact = translate_block(
            FakeClient(['{"cues":[{"id":"c1","zh":"我是一花。"}]}']),
            self.block,
            self.glossary,
            self.primary,
            source_cues=self.sources,
            expected_digest=self.digest,
            expected_source_digest=self.source_digest,
            expected_glossary_digest=self.glossary_digest,
            simplifier=identity,
        )
        self.assertIsInstance(artifact, TranslationArtifact)

        for changes in (
            {"revision": ""},
            {"attempts": ()},
            {"output_text_digest": "0" * 64},
            {
                "attempts": (
                    TranslationAttempt(
                        1,
                        "ok",
                        "",
                        '{"cues":[{"id":"c1","zh":"伪造文本。"}]}',
                    ),
                )
            },
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(artifact, **changes)

        with self.assertRaises(ValueError):
            TranslationAttempt(0, "ok", "", "{}")
        with self.assertRaises(ValueError):
            TranslationAttempt(1, "unknown", "", "{}")

        for changes in (
            {"attempts": ()},
            {"content_digest": "0" * 64},
            {"glossary": None},
        ):
            with self.subTest(glossary_changes=changes):
                with self.assertRaises(ValueError):
                    replace(self.glossary, **changes)

        mismatched_raw = json.dumps(
            {
                "version": "1",
                "entries": [],
                "relationships": [],
                "register": "口语",
            },
            ensure_ascii=False,
        )
        forged_glossary = replace(
            self.glossary,
            attempts=(TranslationAttempt(1, "ok", "", mismatched_raw),),
        )
        with self.assertRaisesRegex(ValueError, "raw response differs"):
            translate_block(
                FakeClient([]),
                self.block,
                forged_glossary,
                self.primary,
                source_cues=self.sources,
                expected_digest=self.digest,
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.glossary_digest,
                simplifier=identity,
            )

    def test_finalizer_replays_quality_gates_on_structurally_valid_artifacts(self):
        source = (cue("q1", "はい。"),)
        source_digest = source_cue_digest(source)
        glossary = Glossary("1", (), (), "口语", source_digest)
        glossary_artifact = attested_glossary(glossary)
        rows = (("q1", "Okay."),)
        forged_primary = TranslationArtifact(
            texts=rows,
            timestamp_digest=timestamp_digest(source),
            source_digest=source_digest,
            glossary_digest=glossary_digest(glossary),
            input_text_digest=None,
            output_text_digest=text_digest(rows),
            model_id=PRIMARY_MODEL_ID,
            revision="rev-primary",
            pass_name="primary",
            status="draft_ok",
            attempts=(
                TranslationAttempt(
                    1,
                    "ok",
                    "",
                    '{"cues":[{"id":"q1","zh":"Okay."}]}',
                ),
            ),
        )
        selection = build_escalation_selection(
            source,
            glossary_artifact,
            (("q1", "好。"),),
            expected_glossary_digest=glossary_digest(glossary),
            flagged_ids=(),
            ambiguous_ids=(),
        )

        with self.assertRaisesRegex(ValueError, "quality replay"):
            finalize_reviewed_video(
                source,
                (forged_primary,),
                (),
                (),
                glossary_artifact,
                selection,
                expected_timestamp_digest=timestamp_digest(source),
                expected_source_digest=source_digest,
                expected_glossary_digest=glossary_digest(glossary),
                simplifier=identity,
            )

    def test_finalizer_rejects_mixed_model_revisions_within_a_pass(self):
        translations = ("之前。", "我是一花。", "之后。")
        revisions = ("rev-a", "rev-b", "rev-a")
        artifacts = []
        for block, translated, revision in zip(
            build_translation_blocks(self.sources, commit_size=1),
            translations,
            revisions,
        ):
            artifacts.append(
                translate_block(
                    FakeClient(
                        [
                            json.dumps(
                                {
                                    "cues": [
                                        {"id": block.commit_ids[0], "zh": translated}
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        ]
                    ),
                    block,
                    self.glossary,
                    ModelConfig.primary(revision=revision, max_tokens=2048),
                    source_cues=self.sources,
                    expected_digest=timestamp_digest(block.commit_cues),
                    expected_source_digest=self.source_digest,
                    expected_glossary_digest=self.glossary_digest,
                    simplifier=identity,
                )
            )
        baseline_rows = tuple(
            (item.cue_id, text) for item, text in zip(self.sources, translations)
        )
        selection = build_escalation_selection(
            self.sources,
            self.glossary,
            baseline_rows,
            expected_glossary_digest=self.glossary_digest,
            flagged_ids=(),
            ambiguous_ids=(),
        )

        with self.assertRaisesRegex(ValueError, "mixes model IDs or revisions"):
            finalize_reviewed_video(
                self.sources,
                tuple(artifacts),
                (),
                (),
                self.glossary,
                selection,
                expected_timestamp_digest=timestamp_digest(self.sources),
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.glossary_digest,
                simplifier=identity,
            )

    def test_finalizer_revalidates_glossary_grounding_and_selection_binding(self):
        baseline_rows = (("c0", "前。"), ("c1", "我是一花。"), ("c2", "后。"))
        selection = build_escalation_selection(
            self.sources,
            self.glossary,
            baseline_rows,
            expected_glossary_digest=self.glossary_digest,
            flagged_ids=(),
            ambiguous_ids=(),
        )
        self.assertIsInstance(selection, EscalationSelection)
        hallucinated = Glossary(
            "1",
            (GlossaryEntry("name", "不存在", "虚构", (), ("c1",), False),),
            (),
            "口语",
            self.source_digest,
        )
        with self.assertRaisesRegex(ValueError, "lacks.*source evidence"):
            finalize_reviewed_video(
                self.sources,
                (),
                (),
                (),
                attested_glossary(hallucinated),
                selection,
                expected_timestamp_digest=timestamp_digest(self.sources),
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.glossary_digest,
                simplifier=identity,
            )

        same_source_swap = attested_glossary(
            replace(self.glossary_value, register="正式")
        )
        with self.assertRaisesRegex(ValueError, "upstream expected digest"):
            finalize_reviewed_video(
                self.sources,
                (),
                (),
                (),
                same_source_swap,
                selection,
                expected_timestamp_digest=timestamp_digest(self.sources),
                expected_source_digest=self.source_digest,
                expected_glossary_digest=self.glossary_digest,
                simplifier=identity,
            )

        explicit_glossary = replace(
            self.glossary_value,
            entries=(replace(self.glossary_value.entries[0], explicit=True),),
        )
        weakened_selection = build_escalation_selection(
            self.sources,
            self.glossary,
            baseline_rows,
            expected_glossary_digest=self.glossary_digest,
            flagged_ids=(),
            ambiguous_ids=(),
        )
        with self.assertRaisesRegex(ValueError, "upstream expected digest"):
            finalize_reviewed_video(
                self.sources,
                (),
                (),
                (),
                self.glossary,
                weakened_selection,
                expected_timestamp_digest=timestamp_digest(self.sources),
                expected_source_digest=self.source_digest,
                expected_glossary_digest=glossary_digest(explicit_glossary),
                simplifier=identity,
            )

        with self.assertRaises(TypeError):
            build_escalation_selection(self.sources, self.glossary, baseline_rows)

    def test_escalation_selector_includes_flags_ambiguity_and_stable_sample(self):
        source = tuple(cue(f"s{i}", "普通です。", i * 1000) for i in range(30))
        glossary = Glossary("1", (), (), "口语", source_cue_digest(source))
        glossary_artifact = attested_glossary(glossary)
        baseline_rows = tuple((item.cue_id, "普通。") for item in source)
        selected_a = build_escalation_selection(
            source,
            glossary_artifact,
            baseline_rows,
            expected_glossary_digest=glossary_digest(glossary),
            flagged_ids=("s1",),
            ambiguous_ids=("s2",),
        )
        selected_b = build_escalation_selection(
            source,
            glossary_artifact,
            baseline_rows,
            expected_glossary_digest=glossary_digest(glossary),
            flagged_ids=("s1",),
            ambiguous_ids=("s2",),
        )
        self.assertEqual(selected_a, selected_b)
        self.assertTrue({"s1", "s2"}.issubset(selected_a.required_ids))
        self.assertGreater(len(selected_a.required_ids), 2)
        self.assertEqual(selected_a.source_digest, source_cue_digest(source))
        self.assertEqual(selected_a.glossary_digest, glossary_digest(glossary))
        self.assertTrue(selected_a.sampled_blocks)
        self.assertEqual(
            select_escalation_ids(
                source,
                glossary_artifact,
                baseline_rows,
                expected_glossary_digest=glossary_digest(glossary),
                flagged_ids=("s1",),
                ambiguous_ids=("s2",),
            ),
            selected_a.required_ids,
        )
        with self.assertRaisesRegex(ValueError, "cannot be weakened"):
            replace(selected_a, sample_denominator=10**18)

    def test_escalation_selection_records_complete_stable_blocks(self):
        source = tuple(cue(f"b{i}", "普通です。", i * 1000) for i in range(250))
        glossary = Glossary("1", (), (), "口语", source_cue_digest(source))
        glossary_artifact = attested_glossary(glossary)
        baseline_rows = tuple((item.cue_id, "普通。") for item in source)
        selection = build_escalation_selection(
            source,
            glossary_artifact,
            baseline_rows,
            expected_glossary_digest=glossary_digest(glossary),
            flagged_ids=("b0",),
            ambiguous_ids=("b249",),
        )

        self.assertGreaterEqual(len(selection.sampled_blocks), 1)
        self.assertIn(tuple(f"b{i}" for i in range(120)), selection.required_blocks)
        self.assertIn(
            tuple(f"b{i}" for i in range(240, 250)), selection.required_blocks
        )
        self.assertEqual(
            selection.required_ids,
            tuple(cue_id for block in selection.required_blocks for cue_id in block),
        )

    def test_escalation_sampling_is_bound_to_full_source_not_generic_ids(self):
        source_a = tuple(cue(f"v{i}", "普通です。", i * 1000) for i in range(250))
        source_b = tuple(cue(f"v{i}", "別の台詞です。", i * 1000) for i in range(250))

        def selection_for(source):
            glossary = Glossary("1", (), (), "口语", source_cue_digest(source))
            return build_escalation_selection(
                source,
                attested_glossary(glossary),
                tuple((item.cue_id, "普通。") for item in source),
                expected_glossary_digest=glossary_digest(glossary),
                flagged_ids=(),
                ambiguous_ids=(),
            )

        selection_a = selection_for(source_a)
        selection_b = selection_for(source_b)
        self.assertNotEqual(selection_a.source_digest, selection_b.source_digest)
        self.assertNotEqual(selection_a.sampled_blocks, selection_b.sampled_blocks)

    def test_escalation_config_accepts_custom_provider_models(self):
        escalation = ModelConfig.escalation(
            revision="rev-reviewer",
            max_tokens=2048,
        )
        self.assertEqual(escalation.model_id, ESCALATION_MODEL_ID)
        self.assertNotEqual(escalation.model_id, PRIMARY_MODEL_ID)

        custom = ModelConfig(
            model_id="vendor/custom-reviewer",
            revision="2026-11-01",
            pass_name="escalation",
            temperature=0.1,
            top_p=0.8,
            seed=20260710,
            n=1,
            max_tokens=2048,
            enable_thinking=False,
        )
        self.assertEqual(custom.model_id, "vendor/custom-reviewer")

        custom_primary = ModelConfig(
            model_id="vendor/custom-primary",
            revision="2026-11-01",
            pass_name="primary",
            temperature=0.2,
            top_p=0.8,
            seed=20260710,
            n=1,
            max_tokens=2048,
            enable_thinking=False,
        )
        self.assertEqual(custom_primary.model_id, "vendor/custom-primary")

        with self.assertRaises(ValueError):
            ModelConfig(
                model_id=PRIMARY_MODEL_ID,
                revision="rev",
                pass_name="primary",
                temperature=0.9,
                top_p=0.8,
                seed=20260710,
                n=1,
                max_tokens=2048,
                enable_thinking=False,
            )


if __name__ == "__main__":
    unittest.main()
