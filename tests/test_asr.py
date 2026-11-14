import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_pipeline.asr import (
    normalize_asr_batch,
    quality_flags,
    trim_abnormal_repetition_edges,
    trim_abnormal_repetition_tail,
)
from subtitle_pipeline.asr_runner import create_model, generate_with_retries

EXPECTED_GENERATE_KWARGS = {
    "language": "日文",
    "temperature": 0.0,
    "repetition_penalty": 1.0,
    "max_new_tokens": 256,
}


class AsrTests(unittest.TestCase):
    def test_batch_cardinality_is_exact(self):
        rows = normalize_asr_batch(
            ["a", "b"], [{"text": "こんにちは"}, {"text": "はい"}]
        )
        self.assertEqual([r.segment_id for r in rows], ["a", "b"])
        with self.assertRaisesRegex(ValueError, "returned 1 results for 2"):
            normalize_asr_batch(["a", "b"], [{"text": "x"}])

    def test_batch_normalization_preserves_real_prefix_before_repetitive_tail(self):
        prefix = "妊娠とかしちゃうかもしんないし。"
        raw = {"text": prefix + "うん、" * 30}

        row = normalize_asr_batch(["a"], [raw])[0]

        self.assertEqual(row.text, prefix)
        self.assertIs(row.raw, raw)

    def test_repetition_and_empty_speech_are_flagged(self):
        self.assertIn("empty", quality_flags(""))
        self.assertIn("abnormal_repetition", quality_flags("はい" * 30))
        self.assertIn("abnormal_repetition", quality_flags("あっ、" * 30))
        self.assertEqual(quality_flags("今日は大丈夫です。"), [])

    def test_abnormal_repetition_tail_is_trimmed_without_losing_real_prefix(self):
        prefix = "妊娠とかしちゃうかもしんないし。"

        self.assertEqual(
            trim_abnormal_repetition_tail(prefix + "うん、" * 30),
            prefix,
        )
        self.assertEqual(trim_abnormal_repetition_tail("あっ、" * 30), "")
        self.assertEqual(
            trim_abnormal_repetition_tail("使ってください、" * 4),
            "使ってください、" * 4,
        )

    def test_abnormal_repetition_prefix_is_trimmed_before_real_speech(self):
        self.assertEqual(
            trim_abnormal_repetition_edges(
                "うん" * 15 + "ああ。違うんじゃないですか。"
            ),
            "ああ。違うんじゃないですか。",
        )

    def test_nested_abnormal_repetition_tails_are_trimmed_until_stable(self):
        prefix = "あ、あ、あ"

        self.assertEqual(
            trim_abnormal_repetition_edges(prefix + "、" + "痛い" * 24 + "痛" * 100),
            prefix,
        )

    def test_truncated_repetition_tail_ending_in_replacement_is_trimmed(self):
        prefix = "お姉ちゃんのこと"

        self.assertEqual(
            trim_abnormal_repetition_edges(prefix + "、" + "勃発、" * 60 + "勃\ufffd"),
            prefix,
        )

    def test_periodic_tail_with_short_corrupted_remainder_is_trimmed(self):
        prefix = "ちょっと何ですよ。"

        self.assertEqual(
            trim_abnormal_repetition_edges(prefix + "痛い" * 40 + "痛" * 7),
            prefix,
        )

    def test_variable_nonlexical_repetition_after_long_run_is_trimmed(self):
        prefix = "ええに、やばっ"
        degenerate = "あ、" * 24 + "んんっ、あ、" * 8

        self.assertEqual(
            trim_abnormal_repetition_edges(prefix + "、" + degenerate),
            prefix,
        )

    def test_obvious_latin_output_is_flagged_as_unexpected_language(self):
        self.assertIn(
            "unexpected_language",
            quality_flags("This is clearly an English transcript"),
        )
        self.assertNotIn("unexpected_language", quality_flags("今日はOKです。"))

    def test_failed_batch_retries_each_segment_in_order(self):
        class FakeModel:
            def __init__(self):
                self.call_sizes = []

            def generate(self, paths, **kwargs):
                self.call_sizes.append(len(paths))
                if len(paths) > 1:
                    raise RuntimeError("batch failed")
                return [{"text": "こんにちは" if paths[0] == "a.wav" else "はい"}]

        model = FakeModel()
        rows = generate_with_retries(model, [("a", "a.wav"), ("b", "b.wav")])
        self.assertEqual(model.call_sizes, [2, 1, 1])
        self.assertEqual([row.segment_id for row in rows], ["a", "b"])
        self.assertTrue(all(row.error is None for row in rows))

    def test_pathlike_inputs_are_strings_for_batch_and_ordered_retries(self):
        class FakeModel:
            def __init__(self):
                self.calls = []

            def generate(self, paths, **kwargs):
                self.calls.append(list(paths))
                if len(paths) > 1:
                    raise RuntimeError("batch failed")
                return [{"text": "こんにちは" if paths[0] == "a.wav" else "はい"}]

        model = FakeModel()
        rows = generate_with_retries(
            model,
            [("a", Path("a.wav")), ("b", Path("b.wav"))],
        )
        self.assertEqual(
            model.calls,
            [["a.wav", "b.wav"], ["a.wav"], ["b.wav"]],
        )
        self.assertEqual([row.segment_id for row in rows], ["a", "b"])
        self.assertTrue(all(row.error is None for row in rows))

    def test_clean_batch_uses_exact_generation_arguments(self):
        class FakeModel:
            def __init__(self):
                self.calls = []

            def generate(self, paths, **kwargs):
                self.calls.append((paths, kwargs))
                return [{"text": "こんにちは"}, {"text": "はい"}]

        model = FakeModel()
        rows = generate_with_retries(model, [("a", "a.wav"), ("b", "b.wav")])
        self.assertEqual(model.calls, [(["a.wav", "b.wav"], EXPECTED_GENERATE_KWARGS)])
        self.assertNotIn("max_length", model.calls[0][1])
        self.assertEqual([row.text for row in rows], ["こんにちは", "はい"])

    def test_cardinality_or_quality_failure_retries_complete_batch_in_order(self):
        for batch_result in (
            [{"text": "こんにちは"}],
            [
                {"text": "This is clearly an English transcript"},
                {"text": "はい"},
            ],
        ):
            with self.subTest(batch_result=batch_result):

                class FakeModel:
                    def __init__(self):
                        self.calls = []

                    def generate(self, paths, **kwargs):
                        self.calls.append(list(paths))
                        if len(paths) > 1:
                            return batch_result
                        return [
                            {"text": ("こんにちは" if paths[0] == "a.wav" else "はい")}
                        ]

                model = FakeModel()
                rows = generate_with_retries(model, [("a", "a.wav"), ("b", "b.wav")])
                self.assertEqual(
                    model.calls, [["a.wav", "b.wav"], ["a.wav"], ["b.wav"]]
                )
                self.assertEqual([row.segment_id for row in rows], ["a", "b"])

    def test_failed_or_flagged_single_retry_returns_explicit_error(self):
        flagged_payload = {
            "text": "This is clearly an English transcript",
            "score": 0.1,
        }

        class FakeModel:
            def generate(self, paths, **kwargs):
                if len(paths) > 1 or paths[0] == "a.wav":
                    raise RuntimeError("decode failed")
                return [flagged_payload]

        rows = generate_with_retries(FakeModel(), [("a", "a.wav"), ("b", "b.wav")])
        self.assertEqual([row.segment_id for row in rows], ["a", "b"])
        self.assertEqual(rows[0].text, "")
        self.assertEqual(rows[0].raw, {})
        self.assertEqual(rows[0].error["type"], "generation_exception")
        self.assertIn("decode failed", rows[0].error["message"])
        self.assertEqual(rows[1].text, "This is clearly an English transcript")
        self.assertIs(rows[1].raw, flagged_payload)
        self.assertEqual(rows[1].error["type"], "quality_gate")
        self.assertEqual(rows[1].error["flags"], ["unexpected_language"])
        self.assertNotIn("error", flagged_payload)

    def test_model_factory_uses_exact_bf16_vllm_arguments_without_real_model(self):
        calls = []
        sentinel = object()
        fake_funasr = types.ModuleType("funasr")
        fake_funasr.__path__ = []
        fake_auto = types.ModuleType("funasr.auto")
        fake_auto.__path__ = []
        fake_vllm = types.ModuleType("funasr.auto.auto_model_vllm")

        def fake_auto_model_vllm(**kwargs):
            calls.append(kwargs)
            return sentinel

        fake_vllm.AutoModelVLLM = fake_auto_model_vllm
        with patch.dict(
            sys.modules,
            {
                "funasr": fake_funasr,
                "funasr.auto": fake_auto,
                "funasr.auto.auto_model_vllm": fake_vllm,
            },
        ):
            model = create_model(
                model_path=Path(
                    "/home/leo/.cache/modelscope/FunAudioLLM/Fun-ASR-Nano-2512"
                ),
                device="cuda",
                engine="vllm",
            )

        self.assertIs(model, sentinel)
        self.assertEqual(
            calls,
            [
                {
                    "model": "/home/leo/.cache/modelscope/FunAudioLLM/Fun-ASR-Nano-2512",
                    "device": "cuda",
                    "dtype": "bf16",
                    "tensor_parallel_size": 1,
                    "gpu_memory_utilization": 0.90,
                    "max_model_len": 4096,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
