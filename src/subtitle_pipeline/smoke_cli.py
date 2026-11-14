from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import soundfile as sf

from .align_runner import AlignmentTask, align_with_retries, create_aligner
from .alignment import JapaneseCue, timestamp_digest
from .asr_runner import create_model, generate_with_retries
from .segments import SpeechSegment, materialize_wav_batch, normalize_vad
from .vad_runner import run_vad

_REVIEWED_NONLEXICAL_ALIGNMENT_OMISSIONS = {
    "0e4286d77e8d18ace7c039a7ad82a93fb7d10dfdf52449d5c62debb709c1227e-seg-00000230": (
        "んんっ、んんっ、んんっ"
    ),
    "180d4967cad871a2ab8be969300eca8e162e3e14b619504b1acf861f54c96b8c-seg-00002016": "あ",
    "55a3fa823fb111025dbc3b75d0d11a770ffa4f6ad89dfeea335998d4391b3849-seg-00000824": (
        "ああああああああっ、あああああああああっ、"
        "あああああああああっ、あああああああああっ、"
        "あああああああっ、ああああああっ、あ"
    ),
    "1048b01319e9f51fe4298d12c5316a1b59b855e72a3693993f59867d5f1d6226-seg-00001499": (
        "んんっ、んんっ、んんっ、んんっ、んんんっ、んんんんっ、"
        "んんんんんっ、んんんんんっ、んんんんんっ、"
        "んんんんんんっ、んんんんんんっ、んんんんんんっ、"
        "んんんんんんっ、んんんんんんんっ、"
        "んんんんんんんっ、んんんんんんんっ"
    ),
    "29dc848f1ab4388365848a19f5c3191d5a5fd8dfcbd68ee17d0f6bc9dbdbaded-seg-00001859": (
        "あっ、あっ、ああっ、んんっ、んんっ、あっ"
    ),
    "3b81b79381b6109916940c7d82902f1ad3701486a9bbb30a4125eaedadefbf10-seg-00000620": "あ",
    "c64f25c2c53af8c26475a90587b306f2f72c6083fb6879fe80a7d6e9feb6961e-seg-00000633": (
        "ん、あん、んちゅっ、ちゅっ、んちゅっ、ちゅっ、ちゅっ、ん"
    ),
    "29c8f9c5111cd5896ba7eb0396f0d6ad8eff55c7b5916a03748de01acc8f3248-seg-00000357": (
        "んふぅんっ、んふぅんっ、んふぅんっ、んふぅんっ、んふぅんっ、んふぅんっ、"
        "んふぅんっ、んふぅんっ、んふぅんっ、んふぅんっ、んふぅんっ、んふ"
    ),
    "42ec12053bceca0d71b0f0448593d79ad6937b801d9d94b6ca3b57c46fa90e03-seg-00000498": "あん",
    "4a9e9f5e17af9ae4eed70c50c68b67a43f51ae79f261e29877ddf1d132d35e95-seg-00002029": (
        "んっ、んっ、んっ、んんっ、んんっ"
    ),
    "a5c6e4418a245405a4d7cc97ef28acf1d6d94827864afc173afe44c5915f944b-seg-00000305": (
        "んっ、んっ、んっ"
    ),
    "b63836c08fc72ac0e96421096dc75aea6cb82010b9a12b1a604da3f4cd39a78e-seg-00001024": (
        "んっ、んっ、んんっ、んんっ、んんっ、んんんっ"
    ),
    "5a8c2e6076ee16379632b23051ea7c5229b3bf82a5277454c0b9af48ff3dab11-seg-00000157": (
        "んちゅっ、ちゅっ、んちゅっ、んちゅっ、ん"
    ),
    "e57db2fa9ccecf1a38efbd63706489183e5e71e9100d59625d233f665ff88d47-seg-00000909": (
        "んちゅっ、んちゅっ、ん"
    ),
    "cc9ec2599e487178316192e779d87ffafe0f95f40f8fa30e8a05a6be626e22eb-seg-00001151": "あ",
    # Reviewed with 23 overlapping 4 s FunASR-Nano windows: none reproduced
    # the isolated lexical-looking "そう"; every valid window was vocalization.
    "40774ee6aa89911262e62c7b998b78830cacbc1223defc148f5c27a554e58819-seg-00002022": (
        "ふふっ、ああ、そう。ああ、ああ、うんうんうん。あ、あ、あ"
    ),
    "b2e38d5dc0e50d05833318e903eb46fe15691c71478cd80d108c8e24899d709f-seg-00000717": (
        "んっ、んっ、んんっ、んんっ、んんっ、んんっ"
    ),
    "ff74ebda37e2c690c41d9c4f5830a3e52f82df606ed79cf058a9bc6d87ec51bf-seg-00000744": (
        "あああああああああっ、ああああ"
    ),
    "6d97b3624030401db532f7efb103611a2b71acb93696a9a5e93d8afd852975bf-seg-00000352": (
        "んんっ、ああああああっ、ああああああっ、ああああああっ、"
        "ああああああっ、ああああああっ、ああああああっ、"
        "ああああああっ、ああああああっ、あ"
    ),
    "153afabf2909830065a94f5ac05f24521ba8201f03c24e8578adcc022c14bbb5-seg-00000827": (
        "ああああああああああ"
    ),
    "1dc08c5da791b244ef15ee4cf275c7b8499c4be123fd207a02f047778bd1839e-seg-00000596": (
        "あああああああああああ"
    ),
    "1dc08c5da791b244ef15ee4cf275c7b8499c4be123fd207a02f047778bd1839e-seg-00000841": (
        "んっ、んんっ、んんっ、んんっ、んんっ、んんんっ、"
        "んんんんんんっ、んんんんんんんっ、ああああっ、"
        "んんんんんんんっ、ああああっ、ああああっ、ああああっ、"
        "んんんんんんっ、ああああっ、ああああっ、んんんんんんっ、"
        "ああああっ、んんんんんんっ、ああああっ、ん"
    ),
    "5b50354c2806e6d6e4c76c6aaca1aa067d2d5e668d327a1796a4dd79ba9c3df1-seg-00000905": (
        "んっ、んっ、んんっ、んんっ、んんっ、んんんっ"
    ),
    "8a3a1a2fb313d1191ee2c8bfeed698a7cb42e79121c027394dba007bf851bf0d-seg-00000918": (
        "あああああああああああ"
    ),
    "daac877a34294e531b22c726015a23ea56498300d94c17d7cceb445fc7be4a82-seg-00000662": (
        "あはははははははははは。"
    ),
    "fc92ac3cf2593527460c6d0f93392527b3c08759cbd63a399f4d6244c1751d0c-seg-00000706": (
        "ああああああああああ"
    ),
    "e64495fe23fc8052a3ef3bfd58295ec10e9c26caaa5bcc61df6f0454214312f1-seg-00001529": (
        "んっ、んんっ、んんっ、んんっ、んんっ、んんっ"
    ),
    "6a172e42c0249bdc5f8e19f8aef05abd4d5dd5d1d92a2aedec50794d063dcfab-seg-00001404": (
        "ちゅっ、ちゅっ、ちゅっ、ちゅ"
    ),
    "1e0c6bc2f3d7073ff49618ff68aaea1cb88807059110470c9d3111be98b4bb8b-seg-00000554": (
        "あ"
    ),
    "ffb916fccf8122a99c7108f63f157aed7adc3c2480ccd09f7c97c95457ad3903-seg-00000703": (
        "ちゅっ、ちゅっ、ちゅ"
    ),
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + f".{os.getpid()}.part")
    try:
        with part.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=repr,
            )
            handle.write("\n")
        os.replace(part, path)
    finally:
        try:
            part.unlink()
        except FileNotFoundError:
            pass


def _read_trace(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid ASR trace line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict) or not isinstance(row.get("segment_id"), str):
                raise ValueError(f"invalid ASR trace row {line_number}")
            rows[row["segment_id"]] = row
    return rows


def _append_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=repr,
                )
                + "\n"
            )


def _nonlexical_alignment_omission(row: dict[str, Any]) -> bool:
    error = row.get("error")
    text = row.get("transcript")
    if (
        not isinstance(error, dict)
        or error.get("type") != "alignment_validation"
        or not isinstance(text, str)
    ):
        return False
    if _REVIEWED_NONLEXICAL_ALIGNMENT_OMISSIONS.get(row.get("segment_id")) == text:
        return True
    return False


def _run_alignment(
    flac: Path,
    segments: list[SpeechSegment],
    asr_rows: list[dict[str, Any]],
    *,
    model_dir: Path,
    output_dir: Path,
    batch_size: int,
    audio_offset_ms: int,
    device: str = "auto",
) -> Path:
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("alignment batch size must be positive")
    if type(audio_offset_ms) is not int:
        raise ValueError("audio offset must be integer milliseconds")
    transcripts = {
        row["segment_id"]: row["text"]
        for row in asr_rows
        if isinstance(row.get("text"), str) and row["text"]
    }
    selected = [item for item in segments if item.segment_id in transcripts]
    trace_path = output_dir / "alignment-trace.jsonl"
    completed = _read_trace(trace_path)
    pending = [item for item in selected if item.segment_id not in completed]
    if pending:
        scratch = output_dir / "scratch-align-wav"
        try:
            wavs = materialize_wav_batch(flac, pending, scratch)
            processor, model = create_aligner(
                model_dir.resolve(strict=True), device=device
            )
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset : offset + batch_size]
                results = align_with_retries(
                    processor,
                    model,
                    [
                        AlignmentTask(
                            item,
                            wavs[item.segment_id],
                            transcripts[item.segment_id],
                            audio_offset_ms,
                        )
                        for item in batch
                    ],
                )
                encoded = [asdict(result) for result in results]
                _append_trace(trace_path, encoded)
                for row in encoded:
                    completed[row["segment_id"]] = row
                print(
                    f"ALIGN {min(offset + len(batch), len(pending))}/{len(pending)}",
                    flush=True,
                )
        finally:
            shutil.rmtree(output_dir / "scratch-align-wav", ignore_errors=True)

    missing = [item.segment_id for item in selected if item.segment_id not in completed]
    if missing:
        raise RuntimeError(f"alignment trace is incomplete: {len(missing)} missing")
    ordered_rows = [completed[item.segment_id] for item in selected]
    nonlexical_omissions = [
        row for row in ordered_rows if _nonlexical_alignment_omission(row)
    ]
    failures = [
        row
        for row in ordered_rows
        if row.get("error") is not None and row not in nonlexical_omissions
    ]
    cues = [
        JapaneseCue(
            cue["cue_id"],
            cue["start_ms"],
            cue["end_ms"],
            cue["text"],
        )
        for row in ordered_rows
        for cue in row["cues"]
    ]
    cues.sort(key=lambda cue: (cue.start_ms, cue.end_ms, cue.cue_id))
    for previous, current in zip(cues, cues[1:]):
        if current.start_ms < previous.end_ms:
            raise RuntimeError("aligned cues overlap across segments")
    result_path = output_dir / "alignment.json"
    _atomic_json(
        result_path,
        {
            "aligned_segment_count": len(ordered_rows),
            "failure_count": len(failures),
            "nonlexical_omission_count": len(nonlexical_omissions),
            "cue_count": len(cues),
            "timestamp_digest": timestamp_digest(cues),
            "cues": [asdict(cue) for cue in cues],
            "results": ordered_rows,
        },
    )
    if failures:
        raise RuntimeError(f"alignment failures: {len(failures)}")
    return result_path


def run_vad_asr(
    flac: Path,
    *,
    video_id: str,
    vad_model: Path,
    output_dir: Path,
    batch_size: int,
    aligner_model: Path | None = None,
    alignment_batch_size: int = 16,
    audio_offset_ms: int = 0,
    device: str = "auto",
) -> Path:
    flac = flac.resolve(strict=True)
    vad_model = vad_model.resolve(strict=True)
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("batch size must be positive")
    with sf.SoundFile(flac) as audio:
        if audio.samplerate != 16_000 or audio.channels != 1:
            raise ValueError("smoke input must be 16 kHz mono audio")
        duration_ms = round(audio.frames * 1000 / audio.samplerate)

    output_dir.mkdir(parents=True, exist_ok=True)
    vad_path = output_dir / "vad.json"
    if vad_path.exists():
        raw_vad = json.loads(vad_path.read_text(encoding="utf-8"))["raw"]
    else:
        raw_vad = run_vad(flac, vad_model, device)
        _atomic_json(vad_path, {"duration_ms": duration_ms, "raw": raw_vad})
    segments = normalize_vad(video_id, raw_vad, duration_ms)
    segments_path = output_dir / "segments.json"
    _atomic_json(
        segments_path,
        {
            "duration_ms": duration_ms,
            "segment_count": len(segments),
            "segments": [asdict(segment) for segment in segments],
        },
    )
    if not segments:
        raise RuntimeError("VAD returned no speech segments")

    trace_path = output_dir / "asr-trace.jsonl"
    completed = _read_trace(trace_path)
    pending = [item for item in segments if item.segment_id not in completed]
    if pending:
        scratch = output_dir / "scratch-wav"
        try:
            wavs = materialize_wav_batch(flac, pending, scratch)
            ordered = sorted(
                pending,
                key=lambda item: (
                    item.decode_end_ms - item.decode_start_ms,
                    item.index,
                ),
            )
            print(
                f"loading FunASR-Nano for {len(ordered)} segments, "
                f"batch_size={batch_size}",
                flush=True,
            )
            model = create_model(device=device)
            for offset in range(0, len(ordered), batch_size):
                batch = ordered[offset : offset + batch_size]
                results = generate_with_retries(
                    model,
                    [(item.segment_id, wavs[item.segment_id]) for item in batch],
                )
                encoded = [asdict(result) for result in results]
                _append_trace(trace_path, encoded)
                for row in encoded:
                    completed[row["segment_id"]] = row
                print(
                    f"ASR {min(offset + len(batch), len(ordered))}/{len(ordered)}",
                    flush=True,
                )
        finally:
            shutil.rmtree(output_dir / "scratch-wav", ignore_errors=True)

    missing = [item.segment_id for item in segments if item.segment_id not in completed]
    if missing:
        raise RuntimeError(f"ASR trace is incomplete: {len(missing)} missing")
    ordered_rows = [completed[item.segment_id] for item in segments]
    empty_omissions = [
        row
        for row in ordered_rows
        if row.get("error") == {"type": "quality_gate", "flags": ["empty"]}
    ]
    failures = [
        row
        for row in ordered_rows
        if row.get("error") is not None and row not in empty_omissions
    ]
    result_path = output_dir / "asr.json"
    _atomic_json(
        result_path,
        {
            "video_id": video_id,
            "duration_ms": duration_ms,
            "segment_count": len(segments),
            "failure_count": len(failures),
            "empty_omission_count": len(empty_omissions),
            "segments": [asdict(item) for item in segments],
            "results": ordered_rows,
        },
    )
    if failures:
        raise RuntimeError(f"ASR quality/generation failures: {len(failures)}")
    if aligner_model is not None:
        return _run_alignment(
            flac,
            segments,
            ordered_rows,
            model_dir=aligner_model,
            output_dir=output_dir,
            batch_size=alignment_batch_size,
            audio_offset_ms=audio_offset_ms,
            device=device,
        )
    return result_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("flac", type=Path)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--vad-model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--aligner-model", type=Path)
    parser.add_argument("--alignment-batch-size", type=int, default=16)
    parser.add_argument("--audio-offset-ms", type=int, default=0)
    parser.add_argument("--device", default="auto")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_vad_asr(
        args.flac,
        video_id=args.video_id,
        vad_model=args.vad_model,
        output_dir=args.output_dir,
        batch_size=args.batch_size,
        aligner_model=args.aligner_model,
        alignment_batch_size=args.alignment_batch_size,
        audio_offset_ms=args.audio_offset_ms,
        device=args.device,
    )
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
