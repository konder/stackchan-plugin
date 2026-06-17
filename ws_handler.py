"""Xiaozhi WebSocket handler — Hermes AIAgent message channel.

Plugin handles: audio I/O (STT/TTS/opus/VAD), WS protocol, MCP bridge.
Hermes AIAgent handles: LLM, memory, session management, tool execution.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue as _q
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp
from aiohttp import WSMsgType, web

from . import frames
from .audio.opus_codec import OpusCodec, UPLINK_SAMPLE_RATE, DOWNLINK_SAMPLE_RATE, FRAME_DURATION_MS
from .audio import stt, tts
from .vad import SileroVAD, VADState, vad_process
from .mcp_bridge import McpBridge

logger = logging.getLogger(__name__)

HELLO_TIMEOUT = 10.0
NO_VOICE_TIMEOUT = 30
PRE_BUFFER_COUNT = 5
SENTENCE_SPLIT_RE = re.compile(r'(?<=[。！？；\n.!?;])')

AGENT_TIMEOUT = 120.0

CHANNEL_PROMPT = (
    "你是一个桌面机器人助手，名叫小智。你有一个可爱的身体，能做表情、转头、跳舞、控制LED灯。"
    "默认简短回答（一两句话），但如果用户明确要求详细解释、讲故事、或说'多说点'，可以给出较长回答。"

    "\n\n## 重要：回复格式规则"
    "\n1. 每次回复必须同时包含文字和工具调用，不要只调工具不说话。先写文字内容，同时调用需要的工具。"
    "\n2. 绝对不要用括号描述动作，如'（抬头）''（点头行礼）'。你有真实的身体，通过工具调用来表演。"
    "\n3. 讲故事、讲笑话等较长内容时，请分段回复：每次讲2~4句话，配合表情/动作/灯光工具调用，"
    "然后继续下一段。不要一次输出整个故事。这样你的身体可以跟着故事情节表演。"
    "\n4. 回复请使用纯文本，不要使用markdown格式（如**、##、---等）。"

    "\n\n## 身体工具"
    "\n- 动作 self.robot.motion(action=动作名): nod(点头), shake(摇头), look_up(抬头), "
    "look_down(低头), tilt_left/tilt_right(歪头), go_home(回正)"
    "\n- 舞蹈 self.robot.dance(action=舞蹈名): happy, robot, panic, look_around"
    "\n- 表情 self.robot.set_emotion(emotion=表情名): happy, sad, angry, neutral, laughing, surprised"
    "\n- LED灯 self.robot.led_sequence(colors=颜色逗号分隔如'red,off,blue,off'，可设interval_ms和repeats)"

    "\n\n## 对话结束"
    "\n当用户说再见、晚安、结束对话时，使用 end_conversation 工具进入待机。"
)


@dataclass
class WSSession:
    session_id: str
    device_id: str
    ws: web.WebSocketResponse
    mcp: McpBridge
    uplink_codec: OpusCodec
    downlink_codec: OpusCodec
    http_session: aiohttp.ClientSession
    tts_url: str
    adapter: Any = None
    vad_state: VADState = field(default_factory=VADState)
    listening: bool = False
    busy: bool = False
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_task: Optional[asyncio.Task] = None
    last_voice_time: float = 0.0
    end_conversation: bool = False
    mcp_init_done: asyncio.Event = field(default_factory=asyncio.Event)
    connected_at: float = field(default_factory=time.monotonic)


async def _send_text(ws: web.WebSocketResponse, payload: dict) -> None:
    data = frames.encode(payload)
    logger.info(">>> %s", data[:300])
    await ws.send_str(data)


async def _send_opus(ws: web.WebSocketResponse, opus_packet: bytes) -> None:
    header = struct.pack(">BBH", 0, 0, len(opus_packet))
    await ws.send_bytes(header + opus_packet)


# --- AIAgent streaming bridge ---

def _run_agent_sync(adapter, session_id: str, user_message: str, on_delta) -> str:
    """Build AIAgent and run_conversation on the current (executor) thread."""
    logger.info("agent: building for session=%s", session_id)
    agent = adapter.build_agent(session_id)
    agent.stream_delta_callback = on_delta
    history = adapter.load_history(session_id)
    logger.info("agent: run_conversation start (history=%d msgs)", len(history))
    result = agent.run_conversation(
        user_message=user_message,
        conversation_history=history,
        task_id="default",
    )
    logger.info("agent: run_conversation finished")
    if isinstance(result, dict):
        return result.get("response", "") or result.get("final_response", "") or ""
    return str(result) if result else ""


# --- Sentence-level streaming TTS pipeline ---

async def _sentence_producer(
    sess: WSSession, sentence: str, out_q: asyncio.Queue,
    tts_sem: asyncio.Semaphore,
) -> None:
    try:
        async with tts_sem:
            async for opus, _ in tts.tts_to_opus_frames(
                tts_url=sess.tts_url, text=sentence,
                codec=sess.downlink_codec, http_session=sess.http_session,
            ):
                if sess.abort_event.is_set():
                    break
                await out_q.put(opus)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("sentence producer error: %r", e)
    finally:
        await out_q.put(None)


async def _sentence_consumer(
    sess: WSSession, fifo: asyncio.Queue,
) -> None:
    """Drain sentences in FIFO order, pace opus frames to device."""
    frame_interval_s = (FRAME_DURATION_MS - 10) / 1000.0
    next_send_at = 0.0
    while True:
        item = await fifo.get()
        if item is None:
            return
        sentence, sent_q = item
        aborted = sess.abort_event.is_set()
        if not aborted:
            await _send_text(sess.ws, frames.tts_sentence_start(sess.session_id, sentence))
        while True:
            frame = await sent_q.get()
            if frame is None:
                break
            if aborted or sess.abort_event.is_set():
                aborted = True
                continue
            now = time.monotonic()
            if next_send_at > now:
                await asyncio.sleep(next_send_at - now)
                now = next_send_at
            await _send_opus(sess.ws, frame)
            next_send_at = max(now, time.monotonic()) + frame_interval_s


async def _chat_handler(sess: WSSession, user_text: str) -> None:
    """AIAgent streaming → sentence split → TTS → opus → device."""
    sid = sess.session_id

    await _send_text(sess.ws, frames.stt_result(sid, user_text))
    await _send_text(sess.ws, frames.tts_start(sid))
    await _send_text(sess.ws, frames.llm_emotion(sid))

    sess.mcp.end_conversation_flag = False

    stream_q: _q.Queue = _q.Queue()

    def _on_delta(delta):
        if delta:
            stream_q.put(delta)

    sentence_fifo: asyncio.Queue = asyncio.Queue()
    producers: list[asyncio.Task] = []
    tts_sem = asyncio.Semaphore(2)
    consumer_task = asyncio.create_task(_sentence_consumer(sess, sentence_fifo))

    def _schedule_sentence(sentence: str):
        s = sentence.strip()
        if not s:
            return
        if s.startswith("[System:") or s.startswith("[system:"):
            logger.debug("filtered system text: %s", s[:60])
            return
        if len(s) <= 2 and not any(u'一' <= c <= u'鿿' for c in s):
            return
        sent_q: asyncio.Queue = asyncio.Queue()
        sentence_fifo.put_nowait((s, sent_q))
        producers.append(asyncio.create_task(
            _sentence_producer(sess, s, sent_q, tts_sem)))

    loop = asyncio.get_running_loop()
    agent_task = loop.run_in_executor(
        None,
        _run_agent_sync, sess.adapter, sid, user_text, _on_delta,
    )

    accumulated = ""
    full_response = ""
    deadline = time.monotonic() + AGENT_TIMEOUT
    try:
        while not agent_task.done() or not stream_q.empty():
            if sess.abort_event.is_set():
                agent_task.cancel()
                break
            if time.monotonic() > deadline:
                logger.warning("agent timeout (%.0fs), forcing completion", AGENT_TIMEOUT)
                agent_task.cancel()
                break
            try:
                delta = stream_q.get_nowait()
            except _q.Empty:
                await asyncio.sleep(0.02)
                continue
            accumulated += delta
            full_response += delta
            deadline = time.monotonic() + AGENT_TIMEOUT
            while True:
                m = SENTENCE_SPLIT_RE.search(accumulated)
                if not m:
                    break
                sentence = accumulated[:m.end()].strip()
                accumulated = accumulated[m.end():]
                if sentence:
                    _schedule_sentence(sentence)

        if agent_task.done() and not agent_task.cancelled():
            try:
                final = agent_task.result()
                if final and not full_response:
                    full_response = final
                    accumulated = final
            except Exception as e:
                logger.error("agent error: %s", e)
                if not full_response:
                    accumulated = "抱歉，我现在无法回答。"

        if accumulated.strip():
            _schedule_sentence(accumulated.strip())

    except Exception as e:
        logger.error("chat pipeline error: %s", e, exc_info=True)
    finally:
        await sentence_fifo.put(None)
        try:
            await consumer_task
        except Exception as e:
            logger.error("consumer error: %s", e)
        for p in producers:
            if not p.done():
                p.cancel()
        for p in producers:
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass
        await _send_text(sess.ws, frames.tts_stop(sid))

        if sess.mcp.end_conversation_flag:
            sess.end_conversation = True

        if full_response and sess.adapter:
            try:
                from agent.title_generator import maybe_auto_title
                maybe_auto_title(
                    session_db=sess.adapter._ensure_session_db(),
                    session_id=sid,
                    user_message=user_text,
                    assistant_response=full_response,
                    conversation_history=[],
                )
            except Exception as e:
                logger.debug("auto-title failed: %s", e)

        logger.info("chat done: %d chars", len(full_response))


async def _process_turn(sess: WSSession, asr_frames: list[bytes]) -> None:
    """ASR → chat. Runs as a background task per voice turn."""
    try:
        if not sess.mcp_init_done.is_set():
            logger.info("Waiting for MCP init...")
            await asyncio.wait_for(sess.mcp_init_done.wait(), timeout=25.0)

        text = await stt.transcribe(asr_frames, sess.uplink_codec)
        logger.info("ASR: %r", text)

        if sess.abort_event.is_set():
            return

        if not text or text.strip().strip("/").lower() in ("sil", "silence", ""):
            await _send_text(sess.ws, frames.tts_start(sess.session_id))
            await _send_text(sess.ws, frames.tts_sentence_start(
                sess.session_id, "我没有听清楚，请再说一遍。"))
            async for opus, _ in tts.tts_to_opus_frames(
                sess.tts_url, "我没有听清楚，请再说一遍。",
                sess.downlink_codec, sess.http_session,
            ):
                if sess.abort_event.is_set():
                    break
                await _send_opus(sess.ws, opus)
            await _send_text(sess.ws, frames.tts_stop(sess.session_id))
            return

        await _chat_handler(sess, text)

    except asyncio.CancelledError:
        logger.info("turn cancelled")
        try:
            await _send_text(sess.ws, frames.tts_stop(sess.session_id))
        except Exception:
            pass
    except Exception as e:
        logger.exception("turn error: %s", e)
        try:
            await _send_text(sess.ws, frames.tts_stop(sess.session_id))
        except Exception:
            pass
    finally:
        sess.busy = False


# --- Main WS handler ---

async def handle_ws(
    request: web.Request,
    vad_model: SileroVAD,
    tts_url: str,
    http_session: aiohttp.ClientSession,
    model_override: Optional[str],
    server_ip: str,
    ota_port: int,
    adapter: Any = None,
) -> web.WebSocketResponse:
    token = os.getenv("WINDOWSILL_AUTH_TOKEN", "test-token")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:].strip() != token:
        return web.Response(status=401, text="unauthorized")

    device_id = (request.headers.get("Device-Id") or "unknown").lower()

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    # Hello handshake
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=HELLO_TIMEOUT)
    except asyncio.TimeoutError:
        await ws.close(code=1002, message=b"hello timeout")
        return ws
    if msg.type != WSMsgType.TEXT:
        await ws.close(code=1002, message=b"expected hello")
        return ws
    hello = frames.decode(msg.data)
    if hello.get("type") != "hello":
        await ws.close(code=1002, message=b"expected hello")
        return ws

    session_id = str(uuid.uuid4())
    logger.info("WS connect device=%s session=%s", device_id, session_id)
    await _send_text(ws, frames.server_hello(session_id))

    # Codecs
    uplink_codec = OpusCodec(UPLINK_SAMPLE_RATE)
    downlink_codec = OpusCodec(DOWNLINK_SAMPLE_RATE)

    # send_json helper
    async def send_json_fn(obj: dict):
        data = frames.encode(obj)
        logger.info(">>> %s", data[:300])
        await ws.send_str(data)

    mcp = McpBridge(send_json_fn, server_ip, ota_port)
    sess = WSSession(
        session_id=session_id,
        device_id=device_id,
        ws=ws,
        mcp=mcp,
        uplink_codec=uplink_codec,
        downlink_codec=downlink_codec,
        http_session=http_session,
        tts_url=tts_url,
        adapter=adapter,
    )

    # MCP init + register tools into Hermes
    async def _mcp_init():
        try:
            await mcp.initialize()
            mcp.register_hermes_tools()
        except Exception as e:
            logger.warning("MCP init error: %s", e)
        sess.mcp_init_done.set()

    asyncio.ensure_future(_mcp_init())

    # Main loop
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if not sess.listening:
                    continue
                if len(msg.data) < 4:
                    continue
                pkt_type, _, size = struct.unpack(">BBH", msg.data[:4])
                if pkt_type != 0:
                    continue
                opus_pkt = msg.data[4:4 + size]

                if sess.vad_state.just_woken_up:
                    continue

                # Decode for VAD
                try:
                    pcm_frame = uplink_codec.decode(opus_pkt)
                except Exception:
                    continue

                have_voice = vad_process(vad_model, sess.vad_state, pcm_frame)
                sess.vad_state.asr_audio.append(opus_pkt)

                if have_voice:
                    sess.last_voice_time = time.time()

                if not have_voice and not sess.vad_state.have_voice:
                    if sess.last_voice_time > 0 and (time.time() - sess.last_voice_time) > NO_VOICE_TIMEOUT:
                        logger.info("No voice for %ds, closing", NO_VOICE_TIMEOUT)
                        sess.listening = False
                        await _send_text(ws, frames.tts_start(session_id))
                        goodbye = "你好像不在了，下次再聊吧。"
                        await _send_text(ws, frames.tts_sentence_start(session_id, goodbye))
                        async for opus, _ in tts.tts_to_opus_frames(
                            tts_url, goodbye, downlink_codec, http_session,
                        ):
                            await _send_opus(ws, opus)
                        await _send_text(ws, frames.tts_stop(session_id))
                        await asyncio.sleep(0.5)
                        await ws.close()
                        return ws
                    sess.vad_state.asr_audio = sess.vad_state.asr_audio[-10:]
                    continue

                if sess.vad_state.voice_stop and not sess.busy:
                    sess.busy = True
                    sess.abort_event.clear()
                    sess.end_conversation = False
                    asr_frames = sess.vad_state.asr_audio.copy()
                    sess.vad_state.reset()
                    logger.info("VAD: voice stopped, %d frames", len(asr_frames))

                    if len(asr_frames) > 15:
                        sess.current_task = asyncio.ensure_future(
                            _process_turn(sess, asr_frames)
                        )

                        async def _check_end(task=sess.current_task, s=sess):
                            await task
                            if s.end_conversation:
                                logger.info("end_conversation, closing")
                                await asyncio.sleep(1.0)
                                try:
                                    await _send_text(s.ws, frames.tts_stop(s.session_id))
                                    await asyncio.sleep(0.3)
                                except Exception:
                                    pass
                                await s.ws.close()
                        asyncio.ensure_future(_check_end())

            elif msg.type == WSMsgType.TEXT:
                try:
                    data = frames.decode(msg.data)
                except Exception:
                    continue
                logger.info("<<< %s", msg.data[:300])
                msg_type = data.get("type")

                if msg_type == "mcp":
                    mcp.handle_response(data.get("payload", {}))

                elif msg_type == "listen":
                    state = data.get("state")
                    if state == "start":
                        sess.listening = True
                        sess.busy = False
                        sess.vad_state.reset()
                        sess.last_voice_time = time.time()
                        sess.vad_state.just_woken_up = True
                        asyncio.get_event_loop().call_later(
                            0.8, lambda: setattr(sess.vad_state, 'just_woken_up', False)
                        )
                        logger.info("listen.start mode=%s", data.get("mode"))

                    elif state == "stop":
                        sess.listening = False
                        logger.info("listen.stop frames=%d", len(sess.vad_state.asr_audio))
                        if sess.vad_state.asr_audio and not sess.busy:
                            sess.busy = True
                            sess.abort_event.clear()
                            asr_frames = sess.vad_state.asr_audio.copy()
                            sess.vad_state.reset()
                            if len(asr_frames) > 15:
                                sess.current_task = asyncio.ensure_future(
                                    _process_turn(sess, asr_frames)
                                )

                    elif state == "detect":
                        logger.info("wake word: %s (busy=%s)", data.get("text"), sess.busy)
                        sess.vad_state.just_woken_up = True
                        asyncio.get_event_loop().call_later(
                            0.5, lambda: setattr(sess.vad_state, 'just_woken_up', False)
                        )
                        if sess.busy and sess.current_task and not sess.current_task.done():
                            logger.info("Interrupting for wake word")
                            sess.abort_event.set()
                            sess.current_task.cancel()
                            sess.current_task = None
                            sess.busy = False
                            try:
                                await _send_text(ws, frames.tts_stop(session_id))
                            except Exception:
                                pass

                elif msg_type == "abort":
                    logger.info("abort reason=%s", data.get("reason"))
                    sess.abort_event.set()
                    if sess.current_task and not sess.current_task.done():
                        sess.current_task.cancel()
                        sess.current_task = None
                    sess.listening = False
                    sess.busy = False
                    sess.vad_state.reset()
                    try:
                        await _send_text(ws, frames.tts_stop(session_id))
                    except Exception:
                        pass

            elif msg.type == WSMsgType.ERROR:
                logger.warning("ws error: %s", ws.exception())
                break

    finally:
        mcp.deregister_hermes_tools()
        if adapter:
            try:
                db = adapter._ensure_session_db()
                if db:
                    db.end_session(session_id, "disconnect")
            except Exception as e:
                logger.debug("end_session failed: %s", e)
        logger.info("WS disconnect session=%s lifetime=%.1fs",
                     session_id, time.monotonic() - sess.connected_at)

    return ws
