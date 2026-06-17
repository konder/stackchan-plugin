"""Hermes platform adapter for the Windowsill StackChan robot.

Registers as a Hermes platform plugin. On connect(), starts an aiohttp
server with OTA + WS + image_proxy endpoints. Each WS connection uses
Hermes AIAgent for LLM, memory, and session management.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

from gateway.platforms.base import BasePlatformAdapter, SendResult  # type: ignore[import-not-found]
from gateway.config import Platform  # type: ignore[import-not-found]

from .ota import handle_ota
from .ws_handler import handle_ws, CHANNEL_PROMPT
from .vad import SileroVAD
from .mcp_bridge import image_proxy_handler, TOOLSET_NAME
from .audio import stt

logger = logging.getLogger(__name__)

PLATFORM_NAME = "windowsill"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8083
DEFAULT_TTS_URL = "http://127.0.0.1:8082/v1/audio/speech"
DEFAULT_SERVER_IP = "192.168.101.131"


class WindowsillAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, **kwargs: Any):
        platform = Platform(PLATFORM_NAME)
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}
        self.host: str = os.getenv("WINDOWSILL_HOST") or extra.get("host") or DEFAULT_HOST
        self.port: int = int(os.getenv("WINDOWSILL_PORT") or extra.get("port") or DEFAULT_PORT)
        self.tts_url: str = os.getenv("WINDOWSILL_TTS_URL") or extra.get("tts_url") or DEFAULT_TTS_URL
        self.server_ip: str = os.getenv("WINDOWSILL_SERVER_IP") or extra.get("server_ip") or DEFAULT_SERVER_IP
        self._model_override: Optional[str] = extra.get("model") or os.getenv("WINDOWSILL_MODEL") or None

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._vad: Optional[SileroVAD] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._session_db: Optional[Any] = None

    # ------------------------------------------------------------------
    # Agent construction
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        if self._session_db is None:
            try:
                from hermes_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.debug("SessionDB unavailable: %s", e)
        return self._session_db

    def load_history(self, session_id: str) -> List[Dict[str, str]]:
        db = self._ensure_session_db()
        if db is None:
            return []
        try:
            return db.get_messages_as_conversation(session_id)
        except Exception as e:
            logger.warning("load history failed session=%s: %s", session_id, e)
            return []

    def build_agent(self, session_id: str) -> Any:
        from run_agent import AIAgent
        from gateway.run import (
            _resolve_runtime_agent_kwargs, _resolve_gateway_model,
            _load_gateway_config, GatewayRunner,
        )
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = self._model_override or _resolve_gateway_model()
        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, PLATFORM_NAME))
        if TOOLSET_NAME not in enabled_toolsets:
            enabled_toolsets.append(TOOLSET_NAME)
            enabled_toolsets.sort()
        fallback_model = GatewayRunner._load_fallback_model()

        return AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=16,
            max_tokens=16384,
            quiet_mode=True,
            verbose_logging=False,
            enabled_toolsets=enabled_toolsets,
            ephemeral_system_prompt=CHANNEL_PROMPT,
            session_id=session_id,
            platform=PLATFORM_NAME,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
        )

    # ------------------------------------------------------------------
    # Platform lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        try:
            self._vad = SileroVAD()
        except Exception as e:
            logger.error("windowsill: VAD init failed: %s", e)
            self._set_fatal_error(code="vad_failed", message=str(e), retryable=False)
            return False

        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
        )

        vad = self._vad
        tts_url = self.tts_url
        http_session = self._http_session
        model_override = self._model_override
        server_ip = self.server_ip
        ota_port = self.port
        adapter_ref = self

        async def ws_route(request):
            return await handle_ws(
                request, vad_model=vad, tts_url=tts_url,
                http_session=http_session, model_override=model_override,
                server_ip=server_ip, ota_port=ota_port,
                adapter=adapter_ref,
            )

        app = web.Application()
        app.router.add_post("/xiaozhi/ota/", handle_ota)
        app.router.add_post("/xiaozhi/ota", handle_ota)
        app.router.add_get("/xiaozhi/v1/", ws_route)
        app.router.add_get("/xiaozhi/v1", ws_route)
        app.router.add_get("/image_proxy", image_proxy_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except OSError as e:
            logger.error("windowsill: bind %s:%s failed: %s", self.host, self.port, e)
            await runner.cleanup()
            self._set_fatal_error(code="bind_failed", message=str(e), retryable=False)
            return False

        self._app = app
        self._runner = runner
        self._site = site
        self._running = True
        self._mark_connected()

        asyncio.create_task(stt.warmup())

        logger.info(
            "windowsill: listening on http://%s:%d (OTA + WS + image_proxy), TTS=%s, agent=AIAgent",
            self.host, self.port, self.tts_url,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        if self._site:
            try:
                await self._site.stop()
            except Exception as e:
                logger.warning("windowsill: site.stop: %s", e)
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.warning("windowsill: runner.cleanup: %s", e)
        self._site = None
        self._runner = None
        self._app = None
        self._mark_disconnected()
        logger.info("windowsill: stopped")

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[dict] = None) -> SendResult:
        logger.debug("windowsill.send stub chat=%s len=%d", chat_id, len(content))
        return SendResult(success=True, message_id="stub")

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}


def register(ctx: Any) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Windowsill",
        adapter_factory=lambda cfg: WindowsillAdapter(cfg),
        check_fn=check_requirements,
        install_hint="pip install aiohttp numpy opuslib onnxruntime httpx Pillow mlx-whisper",
        allow_all_env="WINDOWSILL_ALLOW_ALL_USERS",
        emoji="🤖",
        pii_safe=False,
        platform_hint=(
            "You are speaking through a StackChan desktop robot with a small screen. "
            "Keep responses concise and natural. Use plain text only, no markdown. "
            "Responses are voiced through TTS, so write conversationally. "
            "Use the robot's expression and motion tools to be expressive."
        ),
    )
    logger.info("windowsill: registered")


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        import opuslib  # noqa: F401
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError as e:
        logger.warning("windowsill: missing dep: %s", e)
        return False
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        logger.warning("windowsill: mlx-whisper not installed — STT unavailable")
    return True
