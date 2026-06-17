"""OTA endpoint — device POSTs here on boot to get its WebSocket URL."""
from __future__ import annotations

import logging
import os
import time

from aiohttp import web

logger = logging.getLogger(__name__)


def _resolve_host(request: web.Request) -> str:
    explicit = os.getenv("WINDOWSILL_PUBLIC_HOST", "").strip()
    if explicit:
        return explicit
    host_header = request.headers.get("Host")
    if host_header:
        return host_header
    sock = request.transport.get_extra_info("sockname") if request.transport else None
    if sock:
        return f"{sock[0]}:{sock[1]}"
    return f"0.0.0.0:{os.getenv('WINDOWSILL_PORT', '8083')}"


async def handle_ota(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}

    board = body.get("board") or {}
    device_id = (board.get("mac") or "").lower()
    app_version = (body.get("application") or {}).get("version", "?")

    advertised = _resolve_host(request)
    token = os.getenv("WINDOWSILL_AUTH_TOKEN", "test-token")

    response = {
        "websocket": {
            "url": f"ws://{advertised}/xiaozhi/v1/",
            "token": token,
        },
        "server_time": {
            "timestamp": time.time(),
            "timezone_offset": 28800,
        },
    }
    logger.info("windowsill.ota device=%s app=%s -> %s", device_id or "?", app_version, response["websocket"]["url"])
    return web.json_response(response)
