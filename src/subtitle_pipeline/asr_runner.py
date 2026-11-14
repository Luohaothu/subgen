from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from subtitle_pipeline.asr import AsrResult, normalize_asr_batch, quality_flags
from subtitle_pipeline.settings import load_settings, resolve_device

GENERATE_KWARGS = {
    "language": "日文",
    "temperature": 0.0,
    "repetition_penalty": 1.0,
    "max_new_tokens": 256,
}

WavPath = str | os.PathLike[str]
AsrTask = tuple[str, WavPath]


def create_model(
    *,
    model_path: Path | None = None,
    device: str = "auto",
    engine: str = "auto",
) -> Any:
    resolved = resolve_device(device)
    if engine == "auto":
        engine = "vllm" if resolved == "cuda" else "funasr"
    if model_path is None:
        model_path = load_settings().asr_model_path
    if engine == "vllm":
        from funasr.auto.auto_model_vllm import AutoModelVLLM

        return AutoModelVLLM(
            model=str(model_path),
            device="cuda",
            dtype="bf16",
            tensor_parallel_size=1,
            gpu_memory_utilization=0.90,
            max_model_len=4096,
        )
    from funasr import AutoModel

    funasr_device = "cpu" if resolved == "mps" else resolved
    return AutoModel(model=str(model_path), device=funasr_device)


def _generate(model: Any, tasks: list[AsrTask]) -> list[AsrResult]:
    segment_ids = [segment_id for segment_id, _ in tasks]
    wav_paths = [os.fspath(wav_path) for _, wav_path in tasks]
    raw = model.generate(wav_paths, **GENERATE_KWARGS)
    return normalize_asr_batch(segment_ids, raw)


def _retry_one(model: Any, segment_id: str, wav_path: WavPath) -> AsrResult:
    try:
        row = _generate(model, [(segment_id, wav_path)])[0]
    except Exception as exc:
        return AsrResult(
            segment_id,
            "",
            {},
            {"type": "generation_exception", "message": str(exc)},
        )

    flags = quality_flags(row.text)
    if flags:
        return AsrResult(
            row.segment_id,
            row.text,
            row.raw,
            {"type": "quality_gate", "flags": flags},
        )
    return row


def generate_with_retries(
    model: Any,
    tasks: list[AsrTask],
) -> list[AsrResult]:
    try:
        rows = _generate(model, tasks)
    except Exception:
        rows = []
    else:
        if not any(quality_flags(row.text) for row in rows):
            return rows

    return [_retry_one(model, segment_id, wav_path) for segment_id, wav_path in tasks]
