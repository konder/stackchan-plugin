"""Opus encode/decode — xiaozhi fixed config.

Uplink (device→server): 16 kHz mono, 60 ms frames (960 samples)
Downlink (server→device): 24 kHz mono, 60 ms frames (1440 samples)
"""
from __future__ import annotations

import ctypes.util
import logging
import os

logger = logging.getLogger(__name__)


def _ensure_libopus_findable() -> None:
    if ctypes.util.find_library("opus") is not None:
        return
    candidates = [
        "/opt/homebrew/lib/libopus.0.dylib",
        "/opt/homebrew/lib/libopus.dylib",
        "/usr/local/lib/libopus.0.dylib",
        "/usr/local/lib/libopus.dylib",
    ]
    hit = next((p for p in candidates if os.path.exists(p)), None)
    if hit is None:
        return
    original = ctypes.util.find_library

    def _patched(name: str):
        if name == "opus":
            return hit
        return original(name)

    ctypes.util.find_library = _patched


_ensure_libopus_findable()

import opuslib  # noqa: E402

UPLINK_SAMPLE_RATE = 16_000
DOWNLINK_SAMPLE_RATE = 24_000
FRAME_DURATION_MS = 60
CHANNELS = 1


def _frame_samples(sample_rate: int) -> int:
    return (sample_rate * FRAME_DURATION_MS) // 1000


class OpusCodec:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.frame_samples = _frame_samples(sample_rate)
        self._enc = opuslib.Encoder(sample_rate, CHANNELS, opuslib.APPLICATION_VOIP)
        self._dec = opuslib.Decoder(sample_rate, CHANNELS)

    def encode(self, pcm_bytes: bytes) -> bytes:
        expected = self.frame_samples * 2
        if len(pcm_bytes) != expected:
            raise ValueError(f"opus encode: expected {expected} bytes, got {len(pcm_bytes)}")
        return self._enc.encode(pcm_bytes, self.frame_samples)

    def decode(self, opus_bytes: bytes) -> bytes:
        return self._dec.decode(opus_bytes, self.frame_samples)
