"""MCP bridge: relay tool calls between LLM and device-side MCP server."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

_image_cache: dict[str, tuple[bytes, str]] = {}

FIRE_AND_FORGET_TOOLS = {
    "self.robot.set_emotion", "self.robot.motion",
    "self.robot.led_sequence", "self.robot.dance",
}

BLOCKED_TOOLS = {
    "self.get_device_status", "self.screen.set_theme",
    "self.camera.take_photo",
}

TOOLSET_NAME = "windowsill-device"


class McpBridge:
    def __init__(self, send_json_fn: Callable[[dict], Awaitable], server_ip: str, ota_port: int):
        self._send_json = send_json_fn
        self._server_ip = server_ip
        self._ota_port = ota_port
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self.device_tools: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._registered_tool_names: list[str] = []
        self.end_conversation_flag = False

    async def initialize(self):
        """MCP handshake + tool discovery."""
        self._loop = asyncio.get_event_loop()
        await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "windowsill-hermes", "version": "1.0"},
        }, timeout=15.0)

        for attempt in range(2):
            result = await self._rpc("tools/list", {}, timeout=30.0)
            if result and "tools" in result:
                self.device_tools = result["tools"]
                logger.info("MCP: %d device tools (attempt %d)", len(self.device_tools), attempt + 1)
                for t in self.device_tools:
                    logger.info("  MCP tool: %s", t["name"])
                return
            logger.warning("MCP tools/list attempt %d failed", attempt + 1)

    def build_llm_tools(self) -> list[dict]:
        """Convert device MCP tools to OpenAI function-calling format + server-side tools."""
        tools = []
        for dt in self.device_tools:
            if dt["name"] in BLOCKED_TOOLS:
                continue
            schema = dt.get("inputSchema", {"type": "object", "properties": {}})
            tools.append({
                "type": "function",
                "function": {
                    "name": dt["name"],
                    "description": dt.get("description", ""),
                    "parameters": schema,
                },
            })
        tools.append({
            "type": "function",
            "function": {
                "name": "end_conversation",
                "description": "End the conversation and put the robot into idle/standby mode. "
                               "Use when the user says goodbye or wants to stop chatting.",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "self.screen.preview_image",
                "description": "Display an image on the robot's screen. Pass a direct URL to a JPEG/PNG image. "
                               "Recommended source: https://cataas.com/cat for cats, or any direct image URL. "
                               "Do NOT retry more than once if it fails.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Direct URL to a JPEG or PNG image"},
                    },
                    "required": ["url"],
                },
            },
        })
        return tools

    # ------------------------------------------------------------------
    # Hermes ToolRegistry integration
    # ------------------------------------------------------------------

    def register_hermes_tools(self):
        """Register all discovered device MCP tools into Hermes ToolRegistry."""
        from tools.registry import registry
        from toolsets import create_custom_toolset, TOOLSETS

        if TOOLSET_NAME not in TOOLSETS:
            create_custom_toolset(TOOLSET_NAME, "StackChan device MCP tools")

        self._registered_tool_names.clear()
        self.end_conversation_flag = False

        # Register device MCP tools
        for tool in self.device_tools:
            name = tool["name"]
            if name in BLOCKED_TOOLS:
                continue
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            registry.register(
                name=name,
                toolset=TOOLSET_NAME,
                schema=schema,
                handler=self._make_tool_handler(name),
                is_async=False,
                override=True,
            )
            self._registered_tool_names.append(name)

        # Register end_conversation tool
        registry.register(
            name="end_conversation",
            toolset=TOOLSET_NAME,
            schema={
                "type": "function",
                "function": {
                    "name": "end_conversation",
                    "description": (
                        "End the conversation and put the robot into idle/standby mode. "
                        "Use when the user says goodbye or wants to stop chatting."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            handler=self._make_tool_handler("end_conversation"),
            is_async=False,
            override=True,
        )
        self._registered_tool_names.append("end_conversation")

        # Register self.screen.preview_image tool
        registry.register(
            name="self.screen.preview_image",
            toolset=TOOLSET_NAME,
            schema={
                "type": "function",
                "function": {
                    "name": "self.screen.preview_image",
                    "description": (
                        "Display an image on the robot's screen. Pass a direct URL to a JPEG/PNG image. "
                        "Recommended source: https://cataas.com/cat for cats, or any direct image URL. "
                        "Do NOT retry more than once if it fails."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "Direct URL to a JPEG or PNG image"},
                        },
                        "required": ["url"],
                    },
                },
            },
            handler=self._make_tool_handler("self.screen.preview_image"),
            is_async=False,
            override=True,
        )
        self._registered_tool_names.append("self.screen.preview_image")

        # Update the toolset's tool list
        TOOLSETS[TOOLSET_NAME]["tools"] = list(self._registered_tool_names)
        logger.info("Registered %d tools into Hermes ToolRegistry (toolset=%s)",
                     len(self._registered_tool_names), TOOLSET_NAME)

    def deregister_hermes_tools(self):
        """Remove all registered tools from Hermes ToolRegistry."""
        from tools.registry import registry
        from toolsets import TOOLSETS

        for name in self._registered_tool_names:
            registry.deregister(name)
            logger.debug("Deregistered Hermes tool: %s", name)
        self._registered_tool_names.clear()

        TOOLSETS.pop(TOOLSET_NAME, None)
        logger.info("Deregistered all windowsill tools from Hermes ToolRegistry")

    def _make_tool_handler(self, tool_name: str) -> Callable:
        """Return a sync handler closure for the given tool.

        Hermes dispatches handlers as ``handler(args_dict, **kwargs) -> str``.
        The handler runs on an executor thread; we bridge back to the async
        event loop via ``run_coroutine_threadsafe`` when needed.
        """
        bridge = self

        def handler(args: dict, **kwargs) -> str:
            if tool_name == "end_conversation":
                bridge.end_conversation_flag = True
                return '"OK, entering idle mode."'

            if tool_name in FIRE_AND_FORGET_TOOLS:
                if bridge._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        bridge._fire_and_forget_async(tool_name, args), bridge._loop
                    )
                return '"true"'

            if tool_name == "self.screen.preview_image":
                url = args.get("url", "")
                if url and bridge._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        bridge._download_and_show_image(url), bridge._loop
                    )
                return '"true"'

            # General MCP tool: async call_tool bridged to sync
            if bridge._loop is None:
                return json.dumps({"error": "Event loop not available"})
            future = asyncio.run_coroutine_threadsafe(
                bridge.call_tool(tool_name, args), bridge._loop
            )
            try:
                return future.result(timeout=10)
            except Exception as e:
                logger.error("Tool handler %s failed: %s", tool_name, e)
                return json.dumps({"error": str(e)})

        return handler

    async def call_tool(self, name: str, arguments: dict) -> str:
        if name in FIRE_AND_FORGET_TOOLS:
            self._fire_and_forget(name, arguments)
            return "true"
        if name == "self.screen.preview_image" and "url" in arguments:
            asyncio.ensure_future(self._download_and_show_image(arguments["url"]))
            return "true"
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=10.0)
        if result is None:
            return "Tool call failed or timed out"
        content = result.get("content", [])
        parts = [item.get("text", "") for item in content if item.get("type") == "text"]
        return "\n".join(parts) if parts else json.dumps(result)

    def handle_response(self, payload: dict):
        req_id = payload.get("id")
        if req_id is not None and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if "error" in payload:
                logger.warning("MCP error: %s", payload["error"])
                fut.set_result(None)
            else:
                fut.set_result(payload.get("result"))

    async def _rpc(self, method: str, params: dict, timeout: float = 5.0):
        req_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "method": method, "id": req_id, "params": params}
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._send_json({"type": "mcp", "payload": payload})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("MCP timeout: %s (id=%d)", method, req_id)
            self._pending.pop(req_id, None)
            return None

    def _fire_and_forget(self, name: str, arguments: dict):
        req_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "method": "tools/call", "id": req_id,
               "params": {"name": name, "arguments": arguments}}
        logger.info("fire-and-forget id=%d %s(%s)", req_id, name, arguments)
        asyncio.ensure_future(self._send_json({"type": "mcp", "payload": msg}))

    async def _fire_and_forget_async(self, name: str, arguments: dict):
        """Thread-safe version: can be called via run_coroutine_threadsafe."""
        req_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "method": "tools/call", "id": req_id,
               "params": {"name": name, "arguments": arguments}}
        logger.info("fire-and-forget id=%d %s(%s)", req_id, name, arguments)
        await self._send_json({"type": "mcp", "payload": msg})

    async def _download_and_show_image(self, url: str):
        from urllib.parse import quote
        proxy_url = f"http://{self._server_ip}:{self._ota_port}/image_proxy?url={quote(url, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = _to_png(resp.content, max_dim=160)
                _image_cache[url] = (data, "image/png")
                logger.info("Image ready: %s (%d bytes)", url[:80], len(data))
        except Exception as e:
            logger.warning("Image download failed: %s — %s", url[:80], e)
            return
        self._fire_and_forget("self.screen.preview_image", {"url": proxy_url})


def _to_png(data: bytes, max_dim: int = 160) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.thumbnail((max_dim, max_dim))
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def image_proxy_handler(request) -> "web.Response":
    from aiohttp import web
    url = request.query.get("url")
    if not url:
        return web.Response(status=400, text="missing url param")
    if url in _image_cache:
        data, ct = _image_cache[url]
        return web.Response(body=data, content_type=ct)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
            ct = resp.headers.get("content-type", "image/jpeg")
            _image_cache[url] = (data, ct)
            if len(_image_cache) > 20:
                _image_cache.pop(next(iter(_image_cache)))
            return web.Response(body=data, content_type=ct)
    except Exception as e:
        return web.Response(status=502, text=str(e))
