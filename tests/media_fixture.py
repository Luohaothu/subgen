from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av
import numpy as np


def write_sample_media(
    path: Path,
    *,
    seconds: float = 0.5,
    audio_rate: int = 44_100,
) -> None:
    """Write a small Matroska file with one video and one mono audio stream."""
    with av.open(str(path), "w", format="matroska") as container:
        video = container.add_stream("mpeg4", rate=10)
        video.width = 160
        video.height = 120
        video.pix_fmt = "yuv420p"
        audio = container.add_stream("pcm_s16le", rate=audio_rate)
        audio.layout = "mono"

        samples = int(seconds * audio_rate)
        audio_frame = av.AudioFrame.from_ndarray(
            np.zeros((1, samples), dtype=np.int16), format="s16", layout="mono"
        )
        audio_frame.sample_rate = audio_rate
        audio_frame.pts = 0
        audio_frame.time_base = Fraction(1, audio_rate)
        for packet in audio.encode(audio_frame):
            container.mux(packet)
        for packet in audio.encode(None):
            container.mux(packet)

        for index in range(max(1, round(seconds * 10))):
            frame = av.VideoFrame.from_ndarray(
                np.zeros((120, 160, 3), dtype=np.uint8), format="rgb24"
            )
            frame.pts = index
            frame.time_base = Fraction(1, 10)
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode(None):
            container.mux(packet)
