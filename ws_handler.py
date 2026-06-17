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

AGENT_TIMEOUT = 300.0

PLAYBACK_CHARS_PER_SEC = 4.0
LLM_TURN_GAP_S = 5.0
TTS_FIRST_CHUNK_S = float(os.getenv("WINDOWSILL_TTS_FIRST_CHUNK_S", "1.0"))
TTS_RTF = float(os.getenv("WINDOWSILL_TTS_RTF", "0.15"))


def _compute_pacing_hint() -> str:
    min_chars = int((LLM_TURN_GAP_S + TTS_FIRST_CHUNK_S) * PLAYBACK_CHARS_PER_SEC)
    if TTS_RTF < 0.5:
        max_chars = min_chars * 4
    else:
        max_chars = int(min_chars * (1.0 / TTS_RTF))
    min_chars = max(min_chars, 20)
    max_chars = max(max_chars, min_chars + 20)
    return (
        f"\n\n## 语音播放节奏"
        f"\n你的文字通过TTS语音播放，设备播放速度约{PLAYBACK_CHARS_PER_SEC:.0f}个汉字/秒。"
        f"\n每轮回复后系统需要约{LLM_TURN_GAP_S + TTS_FIRST_CHUNK_S:.0f}秒准备下一轮。"
        f"\n讲故事等长内容时，每轮生成{min_chars}~{max_chars}个汉字，不要一次输出整个故事，也不要每轮只说一两句。"
        f"\n日常简短对话不受此限制。"
    )


CHANNEL_PROMPT_BASE = (
    "你是一个桌面机器人助手，名叫小智。你有一个可爱的身体，能做表情、转头、跳舞、控制LED灯。"
    "默认简短回答（一两句话），但如果用户明确要求详细解释、讲故事、或说'多说点'，可以给出较长回答。"

    "\n\n## 回复格式"
    "\n- 使用纯文本，不要用markdown格式（**、##、---等）。"
    "\n- 绝对不要用括号描述动作，如'（抬头）''（点头行礼）'。用下面的表演标记代替。"

    "\n\n## 表演标记（重要）"
    "\n你的文字会被语音朗读。你可以在文字中插入表演标记，系统会自动执行对应的身体动作，标记本身不会被朗读。"
    "\n格式：{标记名} ，直接嵌入句子中你希望触发动作的位置。"
    "\n\n可用标记："
    "\n表情: {开心} {难过} {生气} {平静} {大笑} {惊讶}"
    "\n动作: {点头} {摇头} {抬头} {低头} {歪头} {回正}"
    "\n舞蹈: {开心舞} {机器舞} {慌张舞} {四处看}"
    "\n\n示例（日常）：'太好了！{开心}今天天气真不错呢。{点头}'"
    "\n示例（故事）：'{开心}从前有一只小猫，它特别喜欢追蝴蝶。{四处看}有一天它发现了一只金色的蝴蝶，"
    "就一路追到了山顶。{惊讶}哇，山顶的风景太美了！{抬头}整个小镇都在脚下闪闪发光。'"
    "\n\n每2~3句话插入一个标记，让身体跟着情节动起来。"
    "\n注意表情多样化，根据情节选择合适的表情，不要总是用同一两个。"
    "\n灯光可以烘托氛围，适当穿插使用："
    "温馨{LED:yellow,yellow,orange,yellow}、神秘{LED:blue,purple,blue,purple}、危险{LED:red,red,red,red}、"
    "开心{LED:green,yellow,green,yellow}、夜晚{LED:blue,white,blue,white}、魔法{LED:purple,pink,purple,pink}"

    "\n\n## 对话结束"
    "\n当用户说再见、晚安、结束对话时，使用 end_conversation 工具进入待机。"
)

CHANNEL_PROMPT = CHANNEL_PROMPT_BASE + _compute_pacing_hint()

PERFORMANCE_TAGS = {
    "开心": ("self.robot.set_emotion", {"emotion": "happy"}),
    "难过": ("self.robot.set_emotion", {"emotion": "sad"}),
    "生气": ("self.robot.set_emotion", {"emotion": "angry"}),
    "平静": ("self.robot.set_emotion", {"emotion": "neutral"}),
    "大笑": ("self.robot.set_emotion", {"emotion": "laughing"}),
    "惊讶": ("self.robot.set_emotion", {"emotion": "surprised"}),
    "点头": ("self.robot.motion", {"action": "nod"}),
    "摇头": ("self.robot.motion", {"action": "shake"}),
    "抬头": ("self.robot.motion", {"action": "look_up"}),
    "低头": ("self.robot.motion", {"action": "look_down"}),
    "歪头": ("self.robot.motion", {"action": "tilt_left"}),
    "回正": ("self.robot.motion", {"action": "go_home"}),
    "开心舞": ("self.robot.dance", {"name": "happy"}),
    "机器舞": ("self.robot.dance", {"name": "robot"}),
    "慌张舞": ("self.robot.dance", {"name": "panic"}),
    "四处看": ("self.robot.dance", {"name": "look_around"}),
}

import re as _re
_TAG_RE = _re.compile(r"\{(" + "|".join(_re.escape(k) for k in PERFORMANCE_TAGS) + r")\}")
_LED_RE = _re.compile(r"\{LED:([a-zA-Z,_]+)\}")


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

async def _tts_worker(
    sess: WSSession, sentence_fifo: asyncio.Queue, playback_fifo: asyncio.Queue,
) -> None:
    """Take sentences one at a time, generate TTS, push opus frames to playback."""
    while True:
        item = await sentence_fifo.get()
        if item is None:
            await playback_fifo.put(None)
            return
        sentence = item
        if sess.abort_event.is_set():
            continue
        await playback_fifo.put(("start", sentence))
        try:
            async for opus, _ in tts.tts_to_opus_frames(
                tts_url=sess.tts_url, text=sentence,
                codec=sess.downlink_codec, http_session=sess.http_session,
            ):
                if sess.abort_event.is_set():
                    break
                await playback_fifo.put(("frame", opus))
        except Exception as e:
            logger.error("TTS error for %r: %r", sentence[:30], e)


async def _playback_worker(
    sess: WSSession, playback_fifo: asyncio.Queue,
) -> None:
    """Pace opus frames to device at real-time rate."""
    frame_interval_s = (FRAME_DURATION_MS - 10) / 1000.0
    next_send_at = 0.0
    while True:
        item = await playback_fifo.get()
        if item is None:
            return
        kind, data = item
        if kind == "start":
            await _send_text(sess.ws, frames.tts_sentence_start(sess.session_id, data))
            continue
        if sess.abort_event.is_set():
            continue
        now = time.monotonic()
        if next_send_at > now:
            await asyncio.sleep(next_send_at - now)
            now = next_send_at
        await _send_opus(sess.ws, data)
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
    playback_fifo: asyncio.Queue = asyncio.Queue()
    tts_task = asyncio.create_task(_tts_worker(sess, sentence_fifo, playback_fifo))
    playback_task = asyncio.create_task(_playback_worker(sess, playback_fifo))

    def _schedule_sentence(sentence: str):
        s = sentence.strip()
        if not s:
            return
        if s.startswith("[System:") or s.startswith("[system:"):
            logger.debug("filtered system text: %s", s[:60])
            return

        for tag_match in _TAG_RE.finditer(s):
            tag_name = tag_match.group(1)
            tool_name, tool_args = PERFORMANCE_TAGS[tag_name]
            logger.info("performance tag {%s} -> %s(%s)", tag_name, tool_name, tool_args)
            sess.mcp._fire_and_forget(tool_name, tool_args)
        s = _TAG_RE.sub("", s)

        for led_match in _LED_RE.finditer(s):
            colors = led_match.group(1)
            logger.info("performance tag {LED:%s}", colors)
            sess.mcp._fire_and_forget("self.robot.led_sequence", {"colors": colors})
        s = _LED_RE.sub("", s).strip()

        if not s:
            return
        if len(s) <= 2 and not any(u'一' <= c <= u'鿿' for c in s):
            return
        sentence_fifo.put_nowait(s)

    loop = asyncio.get_running_loop()
    agent_task = loop.run_in_executor(
        None,
        _run_agent_sync, sess.adapter, sid, user_text, _on_delta,
    )

    accumulated = ""
    full_response = ""
    FIRST_DELTA_TIMEOUT = 30.0
    deadline = time.monotonic() + FIRST_DELTA_TIMEOUT
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
            await tts_task
        except Exception as e:
            logger.error("tts worker error: %s", e)
        try:
            await playback_task
        except Exception as e:
            logger.error("playback worker error: %s", e)
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
                        mode = data.get("mode")
                        if mode != "auto":
                            sess.vad_state.just_woken_up = True
                            asyncio.get_event_loop().call_later(
                                1.5, lambda: setattr(sess.vad_state, 'just_woken_up', False)
                            )
                        logger.info("listen.start mode=%s", mode)

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
                            1.5, lambda: setattr(sess.vad_state, 'just_woken_up', False)
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
