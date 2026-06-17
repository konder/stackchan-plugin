"""Silero VAD for server-side voice activity detection."""
from __future__ import annotations

import logging
import time
from collections import deque

import numpy as np
import onnxruntime

logger = logging.getLogger(__name__)

SILERO_MODEL_PATH = "/tmp/xiaozhi-esp32-server/main/xiaozhi-server/models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx"

VAD_THRESHOLD = 0.5
VAD_THRESHOLD_LOW = 0.2
VAD_SILENCE_MS = 1000
VAD_FRAME_WINDOW = 3


class SileroVAD:
    def __init__(self, model_path: str = SILERO_MODEL_PATH):
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts,
        )
        logger.info("Silero VAD loaded from %s", model_path)


class VADState:
    def __init__(self):
        self.audio_buffer = bytearray()
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)
        self.last_is_voice = False
        self.voice_window: deque = deque(maxlen=5)
        self.have_voice = False
        self.voice_stop = False
        self.vad_last_voice_time = 0.0
        self.asr_audio: list[bytes] = []
        self.just_woken_up = False

    def reset(self):
        self.audio_buffer.clear()
        self.have_voice = False
        self.voice_stop = False
        self.voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0
        self.asr_audio.clear()
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)


def vad_process(model: SileroVAD, vs: VADState, pcm_frame: bytes) -> bool:
    """Process one 60ms PCM frame (960 samples @ 16kHz, int16 LE).
    Returns True if voice is currently detected."""
    try:
        vs.audio_buffer.extend(pcm_frame)

        client_have_voice = False
        while len(vs.audio_buffer) >= 512 * 2:
            chunk = vs.audio_buffer[:512 * 2]
            vs.audio_buffer = vs.audio_buffer[512 * 2:]

            audio_int16 = np.frombuffer(chunk, dtype=np.int16)
            audio_float32 = audio_int16.astype(np.float32) / 32768.0
            audio_input = np.concatenate(
                [vs.context, audio_float32.reshape(1, -1)], axis=1,
            ).astype(np.float32)

            ort_inputs = {
                "input": audio_input,
                "state": vs.state,
                "sr": np.array(16000, dtype=np.int64),
            }
            out, state = model.session.run(None, ort_inputs)
            vs.state = state
            vs.context = audio_input[:, -64:]
            speech_prob = out.item()

            if speech_prob >= VAD_THRESHOLD:
                is_voice = True
            elif speech_prob <= VAD_THRESHOLD_LOW:
                is_voice = False
            else:
                is_voice = vs.last_is_voice

            vs.last_is_voice = is_voice
            vs.voice_window.append(is_voice)
            client_have_voice = vs.voice_window.count(True) >= VAD_FRAME_WINDOW

            if vs.have_voice and not client_have_voice:
                stop_duration = time.time() * 1000 - vs.vad_last_voice_time
                if stop_duration >= VAD_SILENCE_MS:
                    vs.voice_stop = True

            if client_have_voice:
                vs.have_voice = True
                vs.vad_last_voice_time = time.time() * 1000

        return client_have_voice
    except Exception as e:
        logger.error("VAD error: %s", e)
        return False
