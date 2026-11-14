from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .settings import resolve_device


def _unsupported(raw: object) -> ValueError:
    serialized = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=repr)
    return ValueError(f"unsupported VAD payload: {serialized}")


def normalize_vad_payload(raw: object) -> list[list[int]]:
    payload = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
    if not isinstance(payload, dict):
        raise _unsupported(raw)

    if "value" in payload:
        intervals = payload["value"]
    elif "segments" in payload:
        intervals = payload["segments"]
    else:
        raise _unsupported(raw)

    if not isinstance(intervals, list):
        raise _unsupported(raw)
    if any(
        not isinstance(interval, list)
        or len(interval) != 2
        or any(type(value) is not int for value in interval)
        for interval in intervals
    ):
        raise _unsupported(raw)
    return [list(interval) for interval in intervals]


def run_vad(flac: Path, model_path: Path, device: str = "cpu") -> list[list[int]]:
    local_model_path = model_path.expanduser().resolve(strict=True)
    if not local_model_path.is_dir():
        raise ValueError(f"VAD model path is not a directory: {local_model_path}")

    from funasr import AutoModel

    resolved = resolve_device(device)
    funasr_device = "cpu" if resolved == "mps" else resolved
    model: Any = AutoModel(model=str(local_model_path), device=funasr_device)
    return normalize_vad_payload(model.generate(input=str(flac)))
