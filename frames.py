"""Xiaozhi WS control-frame builders."""
from __future__ import annotations

import json
from typing import Any


def server_hello(session_id: str) -> dict[str, Any]:
    return {
        "type": "hello",
        "transport": "websocket",
        "session_id": session_id,
        "audio_params": {
            "format": "opus",
            "sample_rate": 24000,
            "channels": 1,
            "frame_duration": 60,
        },
    }


def tts_start(session_id: str) -> dict[str, Any]:
    return {"type": "tts", "state": "start", "session_id": session_id}


def tts_stop(session_id: str) -> dict[str, Any]:
    return {"type": "tts", "state": "stop", "session_id": session_id}


def tts_sentence_start(session_id: str, text: str) -> dict[str, Any]:
    return {"type": "tts", "state": "sentence_start", "text": text, "session_id": session_id}


def stt_result(session_id: str, text: str) -> dict[str, Any]:
    return {"type": "stt", "text": text, "session_id": session_id}


def llm_emotion(session_id: str, emotion: str = "neutral") -> dict[str, Any]:
    return {"type": "llm", "emotion": emotion, "session_id": session_id}


def encode(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def decode(text: str) -> dict[str, Any]:
    return json.loads(text)
