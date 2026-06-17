"""Qwen3-TTS streaming → 24 kHz opus frames.

TTS server outputs 16 kHz int16 LE PCM (response_format=pcm).
We resample 16k→24k and encode to 60ms opus frames for xiaozhi downlink.
"""
from __future__ import annotations

import logging
import re
import time
from typing import AsyncIterator, Tuple

import aiohttp
import numpy as np

from .opus_codec import DOWNLINK_SAMPLE_RATE, FRAME_DURATION_MS, OpusCodec

logger = logging.getLogger(__name__)

TTS_SOURCE_RATE = 16_000
DOWNLINK_FRAME_SAMPLES = (DOWNLINK_SAMPLE_RATE * FRAME_DURATION_MS) // 1000  # 1440

EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U00002300-\U000023FF\U0000200D\U0000FE0F]+", re.UNICODE)


def clean_tts_text(text: str) -> str:
    text = EMOJI_RE.sub("", text)
    text = text.replace("～", "~").replace("```", "")
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+\s*', '', text)
    text = re.sub(r'["""""]', '', text)
    text = re.sub(r'^[-•─—]+\s*', '', text)
    text = re.sub(r'^-{2,}$', '', text)
    return text.strip()


def _resample_16k_to_24k(pcm_int16: np.ndarray) -> np.ndarray:
    n_in = pcm_int16.shape[0]
    if n_in == 0:
        return pcm_int16
    n_out = (n_in * 3) // 2
    x_in = np.arange(n_in, dtype=np.float64)
    x_out = np.arange(n_out, dtype=np.float64) * (2.0 / 3.0)
    y = np.interp(x_out, x_in, pcm_int16.astype(np.float64))
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


async def tts_to_opus_frames(
    tts_url: str,
    text: str,
    codec: OpusCodec,
    http_session: aiohttp.ClientSession,
) -> AsyncIterator[Tuple[bytes, int]]:
    """Stream TTS → opus, yielding (opus_frame, timestamp_ms)."""
    text = clean_tts_text(text)
    if not text:
        return

    assert codec.sample_rate == DOWNLINK_SAMPLE_RATE
    t0 = time.time()
    logger.info("TTS request: %r", text[:60])

    src_per_frame_16k = (TTS_SOURCE_RATE * FRAME_DURATION_MS) // 1000  # 960
    src_per_frame_bytes = src_per_frame_16k * 2
    ts_ms = 0
    total_frames = 0
    src_buf = bytearray()

    async with http_session.post(
        tts_url,
        json={
            "model": "tts-1", "voice": "alloy",
            "input": text, "response_format": "pcm",
            "sample_rate": TTS_SOURCE_RATE,
        },
        headers={"Content-Type": "application/json"},
    ) as resp:
        logger.info("TTS connected status=%d (+%.2fs)", resp.status, time.time() - t0)
        if resp.status != 200:
            return

        first = True
        async for chunk in resp.content.iter_chunked(8192):
            if first:
                logger.info("TTS first audio (+%.2fs)", time.time() - t0)
                first = False
            src_buf.extend(chunk)
            while len(src_buf) >= src_per_frame_bytes:
                src_chunk = bytes(src_buf[:src_per_frame_bytes])
                del src_buf[:src_per_frame_bytes]
                pcm16k = np.frombuffer(src_chunk, dtype=np.int16)
                pcm24k = _resample_16k_to_24k(pcm16k).tobytes()
                opus = codec.encode(pcm24k)
                yield opus, ts_ms
                ts_ms += FRAME_DURATION_MS
                total_frames += 1

        if src_buf:
            pad = b"\x00" * (src_per_frame_bytes - len(src_buf))
            pcm16k = np.frombuffer(bytes(src_buf) + pad, dtype=np.int16)
            pcm24k = _resample_16k_to_24k(pcm16k).tobytes()
            opus = codec.encode(pcm24k)
            yield opus, ts_ms
            total_frames += 1

    logger.info("TTS done: %d frames (%.1fs audio) in %.2fs",
                total_frames, total_frames * FRAME_DURATION_MS / 1000.0, time.time() - t0)
