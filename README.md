# stackchan-plugin

Hermes AIAgent platform plugin for the StackChan K151 desktop robot.

Bridges the StackChan device (running xiaozhi-protocol firmware) to [Hermes](https://github.com/NousResearch/hermes-agent) as a message channel. The plugin handles audio I/O (STT/TTS/Opus/VAD) and device tool bridging, while Hermes AIAgent handles LLM, memory, session management, and tool execution.

## Architecture

```
┌──────────────┐      WebSocket       ┌─────────────────────────────┐
│  StackChan   │◄────(xiaozhi)───────►│  stackchan-plugin           │
│  K151 Device │  opus audio + JSON   │  (Hermes platform plugin)   │
│              │                      │                             │
│  - ESP32-S3  │                      │  ┌──────────┐ ┌──────────┐ │
│  - CoreS3    │                      │  │ Silero   │ │ mlx-     │ │
│  - MCP server│                      │  │ VAD      │ │ whisper  │ │
│              │                      │  └────┬─────┘ └────┬─────┘ │
└──────────────┘                      │       │            │       │
                                      │  ┌────▼────────────▼────┐  │
                                      │  │   ws_handler          │  │
                                      │  │   drain loop +        │  │
                                      │  │   sentence split      │  │
                                      │  └───┬──────────────┬───┘  │
                                      │      │              │      │
                                      │  ┌───▼───┐    ┌────▼────┐ │
                                      │  │Hermes │    │ TTS     │ │
                                      │  │AIAgent│    │(Qwen3/  │ │
                                      │  │       │    │ other)  │ │
                                      │  └───┬───┘    └─────────┘ │
                                      │      │                    │
                                      │  ┌───▼──────────────────┐ │
                                      │  │ MCP Bridge           │ │
                                      │  │ ToolRegistry ◄──►    │ │
                                      │  │ Device MCP tools     │ │
                                      │  └──────────────────────┘ │
                                      └─────────────────────────────┘
```

### Data Flow

1. **Voice Input**: Device → opus audio → VAD (Silero) → mlx-whisper STT
2. **LLM Processing**: User text → Hermes AIAgent `run_conversation()` on thread executor
3. **Streaming Bridge**: Agent `stream_delta_callback` → `queue.Queue` → async drain loop → sentence split
4. **TTS Pipeline**: Sentences → TTS API (streaming PCM) → resample 16k→24k → opus encode → device
5. **Device Tools**: Agent tool calls → MCP Bridge → device MCP server (via WS JSON-RPC)

### Key Design Decisions

- **AIAgent per turn**: Each voice turn constructs a fresh AIAgent (Cardputer pattern). Session continuity is maintained through `session_db` + `conversation_history`.
- **Thread-safe bridge**: AIAgent's `run_conversation()` runs on a thread executor. A `queue.Queue` bridges streaming deltas to the async event loop. Device tool handlers use `asyncio.run_coroutine_threadsafe()`.
- **Fire-and-forget tools**: `set_emotion`, `motion`, `led_sequence`, `dance` return `"true"` immediately without waiting for device response. This prevents tool execution from blocking the agent's LLM loop.
- **3-stage pipeline**: LLM generates sentences → `sentence_fifo` → `_tts_worker` (serial TTS) → `playback_fifo` → `_playback_worker` (real-time opus pacing). TTS generation of sentence N+1 overlaps with playback of sentence N, eliminating inter-sentence gaps.
- **Performance tags**: LLM embeds inline tags like `{开心}`, `{LED:blue,white,blue,white}` in text. Tags are parsed, executed as fire-and-forget MCP calls, and stripped before TTS — no tool-call round-trip needed.
- **Dynamic ToolRegistry**: Device MCP tools are registered into Hermes `ToolRegistry` on WS connect and deregistered on disconnect, via a custom toolset `windowsill-device`.

## Prerequisites

- **macOS** with Apple Silicon (M1/M2/M4) — mlx-whisper requires Metal GPU
- **Hermes Agent** installed and configured at `~/.hermes/`
- **Python 3.11+** (Hermes's bundled Python)
- **Opus library**: `brew install opus`
- **Silero VAD model**: ONNX file (see setup below)
- **TTS server**: OpenAI-compatible TTS endpoint (Qwen3-TTS, Bailian CosyVoice, etc.)

## Setup

### 1. Install Python dependencies

```bash
# Use Hermes's Python environment
pip install aiohttp numpy opuslib onnxruntime httpx Pillow mlx-whisper
```

### 2. Download Silero VAD model

```bash
mkdir -p /tmp/xiaozhi-esp32-server/main/xiaozhi-server/models/snakers4_silero-vad/src/silero_vad/data/
# Download silero_vad.onnx from https://github.com/snakers4/silero-vad
# Place at the path above
```

Or change `SILERO_MODEL_PATH` in `vad.py`.

### 3. Install as Hermes plugin

Create a symlink from Hermes plugins directory to this repo:

```bash
ln -s /path/to/stackchan-plugin ~/.hermes/plugins/windowsill
```

### 4. Configure Hermes

Add to `~/.hermes/config.yaml`:

```yaml
# Under platform_toolsets:
platform_toolsets:
  windowsill:
  - windowsill-device

# Under platforms:
platforms:
  windowsill:
    enabled: true
    extra:
      host: 0.0.0.0
      port: 8083
      model: deepseek-v4-flash    # LLM model override (optional)
      tts_url: http://127.0.0.1:8082/v1/audio/speech
      server_ip: 192.168.x.x      # This Mac's LAN IP (for device to reach image proxy)

# Under plugins.enabled:
plugins:
  enabled:
  - windowsill
```

### 5. Configure the device

Set the device's OTA URL to point to this server:

```
http://<mac-ip>:8083/xiaozhi/ota/
```

The device will receive the WebSocket URL and auth token from the OTA response.

### 6. Restart Hermes

```bash
# If running as LaunchAgent:
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway

# Or manually:
hermes gateway
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WINDOWSILL_HOST` | `0.0.0.0` | Bind address |
| `WINDOWSILL_PORT` | `8083` | Listen port |
| `WINDOWSILL_AUTH_TOKEN` | `test-token` | Bearer token for WS auth |
| `WINDOWSILL_TTS_URL` | `http://127.0.0.1:8082/v1/audio/speech` | TTS endpoint |
| `WINDOWSILL_SERVER_IP` | `192.168.101.131` | Public IP for image proxy |
| `WINDOWSILL_PUBLIC_HOST` | (auto-detect) | Advertised host in OTA response |
| `WINDOWSILL_MODEL` | (global default) | LLM model override |
| `WINDOWSILL_TTS_FIRST_CHUNK_S` | `1.0` | Expected TTS first-chunk latency (for pacing hints) |
| `WINDOWSILL_TTS_RTF` | `0.15` | Expected TTS real-time factor (for pacing hints) |

Config in `config.yaml` under `platforms.windowsill.extra` takes precedence over defaults but is overridden by env vars.

## File Structure

```
├── __init__.py          # Plugin entry point (register)
├── adapter.py           # Hermes BasePlatformAdapter, AIAgent construction
├── ws_handler.py        # WebSocket handler, 3-stage pipeline, performance tags
├── mcp_bridge.py        # Device MCP tool bridge + Hermes ToolRegistry integration
├── frames.py            # Xiaozhi protocol frame builders
├── ota.py               # OTA endpoint (device boot handshake)
├── vad.py               # Silero VAD wrapper
├── cosyvoice_server.py  # CosyVoice2-MLX TTS server (OpenAI-compatible)
├── audio/
│   ├── opus_codec.py    # Opus encode/decode (16kHz uplink, 24kHz downlink)
│   ├── stt.py           # mlx-whisper STT
│   └── tts.py           # Streaming TTS → opus frames
└── plugin.yaml          # Hermes plugin manifest
```

## Device MCP Tools

Registered into Hermes ToolRegistry on each WS connection:

| Tool | Type | Description |
|------|------|-------------|
| `self.robot.motion` | fire-and-forget | Head gestures: nod, shake, look_up, look_down, tilt_left/right, go_home |
| `self.robot.set_emotion` | fire-and-forget | Face expressions: happy, sad, angry, neutral, laughing, surprised |
| `self.robot.dance` | fire-and-forget | Dance sequences: happy, robot, panic, look_around |
| `self.robot.led_sequence` | fire-and-forget | LED color patterns with timing |
| `self.robot.create_reminder` | sync (10s timeout) | Set a timed reminder |
| `self.robot.stop_reminder` | sync (10s timeout) | Cancel a reminder |
| `self.screen.preview_image` | async (download+proxy) | Display image on device screen |
| `self.screen.set_brightness` | sync (10s timeout) | Adjust screen brightness |
| `self.audio_speaker.set_volume` | sync (10s timeout) | Adjust speaker volume |
| `end_conversation` | immediate | Signal end of conversation, close WS |

## TTS Server Requirements

The plugin expects an OpenAI-compatible TTS endpoint that:
- Accepts POST with `{"model": "tts-1", "voice": "alloy", "input": "text", "response_format": "pcm", "sample_rate": 16000}`
- Returns streaming `audio/pcm` (16-bit signed LE, 16kHz mono)
- Supports chunked transfer encoding for streaming

Tested with:
- **CosyVoice2-MLX** (recommended) — zero-shot voice cloning, RTF ~0.6 on Apple Silicon with 5s reference audio
- **Qwen3-TTS** (local, via OpenAI-compatible wrapper)
- Any OpenAI TTS API compatible service

### CosyVoice2-MLX TTS Server

This repo includes `cosyvoice_server.py`, an OpenAI-compatible TTS server powered by [CosyVoice2](https://github.com/FunAudioLLM/CosyVoice) via MLX. It supports zero-shot voice cloning from a short reference audio clip.

```bash
# Install dependencies
pip install mlx-audio librosa aiohttp numpy

# Run with custom voice (5s WAV recommended for speed/quality balance)
python cosyvoice_server.py --port 8082 --ref-audio /path/to/your_voice.wav
```

The model (`mlx-community/CosyVoice2-0.5B-4bit`) is downloaded automatically on first run. A `threading.Lock` ensures thread-safe access for concurrent requests.

Reference audio tips:
- **5 seconds** is the sweet spot (RTF ~0.6). Longer clips increase latency significantly (15s → RTF ~2.8).
- Record in a quiet environment. 24kHz mono WAV works best.
- Content should be natural speech with varied intonation.

## Troubleshooting

**Device connects but ASR returns garbage (e.g. "字幕by索兰娅")**
- Wake word audio bleeding into ASR. The plugin drops 1.5s of audio after wake word detection and non-auto `listen.start`. In `mode=auto` (conversation continuation), no audio is dropped.

**Motion/dance tools fail with "Missing valid argument"**
- Performance tags now handle motion/dance/emotion/LED inline. If using direct tool calls, ensure parameter names match: `motion(action=...)`, `dance(name=...)`, `set_emotion(emotion=...)`.

**Device stuck on green light after speaking**
- LLM may be hanging with no output. A 30s first-delta timeout (`FIRST_DELTA_TIMEOUT`) catches this. After the first delta, `AGENT_TIMEOUT` (300s) applies between subsequent deltas.

**"[System: ..." text being spoken**
- The `_schedule_sentence` filter in `ws_handler.py` strips `[System:` prefixes. If new patterns appear, add them to the filter.

**Sessions not visible in Hermes TUI**
- Sessions need both a title and `ended_at`. The plugin calls `maybe_auto_title()` after each turn and `end_session()` on WS disconnect.
