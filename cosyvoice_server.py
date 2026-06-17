"""CosyVoice2-MLX TTS server with OpenAI-compatible /v1/audio/speech endpoint.

Streams 16kHz int16 LE PCM — drop-in replacement for Qwen3-TTS.

Usage:
    python cosyvoice_server.py [--host 0.0.0.0] [--port 8082] [--ref-audio path.wav]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time

import numpy as np
from aiohttp import web

logger = logging.getLogger("cosyvoice_server")

_model = None
_ref_audio = None
_gen_lock = threading.Lock()


def _load_model(ref_audio_path: str):
    global _model, _ref_audio
    import mlx.core as mx
    import librosa
    from mlx_audio.tts.utils import load_model

    logger.info("Loading CosyVoice2 model...")
    t0 = time.time()
    _model = load_model("mlx-community/CosyVoice2-0.5B-4bit")
    t1 = time.time()
    logger.info("Model loaded in %.1fs", t1 - t0)

    logger.info("Loading reference audio: %s", ref_audio_path)
    ref_np, _ = librosa.load(ref_audio_path, sr=24000)
    _ref_audio = mx.array(ref_np)
    logger.info("Reference audio: %.1fs", len(ref_np) / 24000)


def _resample_24k_to_16k(samples_f32: np.ndarray) -> bytes:
    n_in = len(samples_f32)
    if n_in == 0:
        return b""
    n_out = (n_in * 2) // 3
    x_in = np.arange(n_in, dtype=np.float64)
    x_out = np.arange(n_out, dtype=np.float64) * (3.0 / 2.0)
    y = np.interp(x_out, x_in, samples_f32.astype(np.float64))
    pcm16 = np.clip(np.round(y * 32767), -32768, 32767).astype(np.int16)
    return pcm16.tobytes()


async def handle_speech(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    text = body.get("input", "").strip()
    if not text:
        return web.json_response({"error": "no input"}, status=400)

    target_rate = int(body.get("sample_rate", 16000))

    t0 = time.time()
    logger.info("TTS request: target=%dHz text=%r", target_rate, text[:80])

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/pcm",
            "Transfer-Encoding": "chunked",
        },
    )
    await resp.prepare(request)

    chunk_q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _generate():
        try:
            with _gen_lock:
                for r in _model.generate(text=text, ref_audio=_ref_audio, verbose=False):
                    loop.call_soon_threadsafe(chunk_q.put_nowait, np.array(r.audio))
        finally:
            loop.call_soon_threadsafe(chunk_q.put_nowait, None)

    gen_task = loop.run_in_executor(None, _generate)

    first = True
    total_bytes = 0
    while True:
        chunk_f32 = await chunk_q.get()
        if chunk_f32 is None:
            break
        if target_rate == 16000:
            pcm_bytes = _resample_24k_to_16k(chunk_f32)
        else:
            pcm16 = np.clip(np.round(chunk_f32 * 32767), -32768, 32767).astype(np.int16)
            pcm_bytes = pcm16.tobytes()
        if first:
            logger.info("TTS first chunk +%.3fs (%d bytes)", time.time() - t0, len(pcm_bytes))
            first = False
        await resp.write(pcm_bytes)
        total_bytes += len(pcm_bytes)

    await gen_task
    await resp.write_eof()
    audio_s = total_bytes / (target_rate * 2)
    logger.info("TTS done: %.1fs audio in %.2fs (RTF=%.2f)",
                audio_s, time.time() - t0, (time.time() - t0) / max(audio_s, 0.01))
    return resp


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "engine": "cosyvoice2-mlx"})


def main():
    parser = argparse.ArgumentParser(description="CosyVoice2-MLX TTS Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--ref-audio", default="/Users/nanzhang/gateway/ref_voice_vivian.wav",
                        help="Reference audio for voice cloning")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    _load_model(args.ref_audio)

    app = web.Application()
    app.router.add_post("/v1/audio/speech", handle_speech)
    app.router.add_get("/health", handle_health)

    logger.info("Starting CosyVoice2 TTS server on %s:%d", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
