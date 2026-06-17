"""Opus frames → mlx-whisper transcript.

Runs mlx-whisper on a thread executor (~150-400ms per utterance on M-series).
"""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from .opus_codec import OpusCodec, UPLINK_SAMPLE_RATE

logger = logging.getLogger(__name__)

try:
    import mlx_whisper
    MLX_AVAILABLE = True
except ImportError:
    mlx_whisper = None
    MLX_AVAILABLE = False

STT_MODEL = "mlx-community/whisper-small-mlx"
MIN_RMS = 0.005
MIN_DURATION_S = 0.2


def decode_opus_stream(frames: list[bytes], codec: OpusCodec) -> bytes:
    out = bytearray()
    for f in frames:
        try:
            out.extend(codec.decode(f))
        except Exception as e:
            logger.warning("opus decode skip: %s", e)
    return bytes(out)


async def transcribe(frames: list[bytes], codec: OpusCodec) -> str:
    if not MLX_AVAILABLE:
        logger.warning("mlx-whisper unavailable")
        return ""

    t0 = time.time()
    pcm_bytes = decode_opus_stream(frames, codec)
    if len(pcm_bytes) < 2:
        return ""

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    duration = samples.shape[0] / UPLINK_SAMPLE_RATE
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    logger.info("STT: %d samples (%.2fs) rms=%.4f", samples.size, duration, rms)

    if duration < MIN_DURATION_S:
        logger.info("STT too short, skipping")
        return ""
    if rms < MIN_RMS:
        logger.info("STT too quiet, skipping")
        return ""

    def _run():
        result = mlx_whisper.transcribe(
            samples,
            path_or_hf_repo=STT_MODEL,
            language="zh",
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
        )
        return result["text"].strip()

    text = await asyncio.get_running_loop().run_in_executor(None, _run)
    logger.info("STT result: %r (%.2fs)", text, time.time() - t0)
    return text


async def warmup() -> bool:
    if not MLX_AVAILABLE:
        return False
    logger.info("STT warmup skipped (lazy load on first transcribe to avoid Metal GPU timeout)")
    return True
