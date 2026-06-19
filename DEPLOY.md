# StackChan 新机器部署手册

在一台已安装 Hermes 的 Mac Mini（Apple Silicon）上部署 stackchan-plugin 和 CosyVoice2 TTS，并将设备切换到新服务器。

## Part A — Mac Mini 部署 plugin + TTS

### 1. 克隆代码

```bash
mkdir -p ~/StackChan && cd ~/StackChan
git clone git@github.com:konder/stackchan-plugin.git plugin
```

### 2. 创建 Hermes 插件软链接

```bash
ln -s ~/StackChan/plugin ~/.hermes/plugins/windowsill
```

### 3. 安装 Python 依赖

使用 Hermes 自带的 Python 环境：

```bash
~/.hermes/hermes-agent/venv/bin/pip install \
  aiohttp numpy opuslib onnxruntime httpx Pillow \
  mlx-whisper mlx-audio librosa
```

还需要 opus 系统库：

```bash
brew install opus
```

### 4. 下载 Silero VAD 模型

```bash
mkdir -p /tmp/xiaozhi-esp32-server/main/xiaozhi-server/models/snakers4_silero-vad/src/silero_vad/data/
# 从 https://github.com/snakers4/silero-vad 下载 silero_vad.onnx
# 放到上面的路径
```

> 或直接从已有机器 scp 过来。

### 5. 准备语音克隆参考音频

```bash
mkdir -p ~/gateway
# 从已有机器拷贝参考音频：
scp nanzhang@<已有机器IP>:~/gateway/ref_voice_custom.wav ~/gateway/
```

> 5 秒 24kHz mono WAV，用于 CosyVoice2 声音克隆。

### 6. 配置 Hermes config.yaml

编辑 `~/.hermes/config.yaml`，添加以下内容：

```yaml
# platforms: 下添加
platforms:
  windowsill:
    enabled: true
    extra:
      host: 0.0.0.0
      port: 8083
      model: deepseek-v4-flash        # 或你用的 LLM 模型
      tts_url: http://127.0.0.1:8082/v1/audio/speech
      server_ip: <本机局域网IP>         # 如 192.168.1.100，设备通过它访问图片代理

# platform_toolsets: 下添加
platform_toolsets:
  windowsill:
  - windowsill-device

# plugins.enabled: 下添加
plugins:
  enabled:
  - windowsill
```

> **注意**：`server_ip` 必须填本机局域网 IP，设备需要通过它访问图片代理。

### 7. 配置 LLM provider

确认 Hermes 的 `config.yaml` 中已配置可用的 LLM provider：

```yaml
# 示例：使用 DeepSeek 官方 API
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key: sk-xxx

# 或使用本地/内网 LLM 服务
providers:
  local:
    base_url: http://<LLM服务IP>:4000/v1
    api_key: xxx
```

> **注意**：确认本机网络能访问 LLM provider。

### 8. 创建 LaunchAgent（开机自启）

创建 `~/Library/LaunchAgents/ai.hermes.gateway.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.hermes.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/nanzhang/.hermes/hermes-agent/venv/bin/python</string>
    <string>-m</string>
    <string>hermes_cli.main</string>
    <string>gateway</string>
    <string>run</string>
    <string>--replace</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/nanzhang/.hermes</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/nanzhang/.hermes/hermes-agent/venv/bin:/Users/nanzhang/.hermes/hermes-agent/node_modules/.bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>VIRTUAL_ENV</key>
    <string>/Users/nanzhang/.hermes/hermes-agent/venv</string>
    <key>HERMES_HOME</key>
    <string>/Users/nanzhang/.hermes</string>
    <key>WINDOWSILL_TTS_FIRST_CHUNK_S</key>
    <string>2.0</string>
    <key>WINDOWSILL_TTS_RTF</key>
    <string>0.7</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/nanzhang/.hermes/logs/gateway.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/nanzhang/.hermes/logs/gateway.error.log</string>
</dict>
</plist>
```

> **注意**：plist 中的路径必须是完整绝对路径，不能用 `~`。根据实际用户名修改 `/Users/nanzhang`。

### 9. 启动 CosyVoice2 TTS 服务

```bash
# 直接运行（测试用）：
cd ~/StackChan/plugin
~/.hermes/hermes-agent/venv/bin/python cosyvoice_server.py \
  --port 8082 --ref-audio ~/gateway/ref_voice_custom.wav

# 首次运行会自动下载模型 mlx-community/CosyVoice2-0.5B-4bit
```

> TTS 必须在 Hermes gateway 之前启动，否则 TTS 请求会失败。

### 10. 启动 Hermes gateway

```bash
# 加载 LaunchAgent：
launchctl load ~/Library/LaunchAgents/ai.hermes.gateway.plist

# 或重启已有的：
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway

# 查看日志：
tail -f ~/.hermes/logs/gateway.log
```

确认日志中出现 `windowsill: listening on http://0.0.0.0:8083`。

---

## Part B — 设备切换 OTA 地址

### 方式一：设备触屏设置（推荐，不用重新刷固件）

固件从 NVS 读取 `ota_url`，优先级高于编译时默认值。

1. 设备开机后，触摸屏幕进入设置界面
2. 找到 WiFi / 网络设置
3. 修改 OTA URL 为：`http://<Mac Mini 局域网IP>:8083/xiaozhi/ota/`
4. 保存，设备会自动重连

> 设备需要连接本地 WiFi，确保和 Mac Mini 在同一局域网。

### 方式二：重新编译固件（写死新地址）

需要搭建 ESP-IDF v5.5.x 编译环境。

```bash
# 修改 sdkconfig：
# CONFIG_OTA_URL="http://<Mac Mini IP>:8083/xiaozhi/ota/"

# 编译 & 刷入（需要 USB 连接设备）：
cd ~/StackChan/firmware
source ~/esp/esp-idf/export.sh
idf.py build
python3 -m esptool --chip esp32s3 -b 460800 \
  -p /dev/cu.usbmodem101 \
  write_flash 0x20000 build/stack-chan.bin
```

---

## Part C — 验证清单

```bash
# 1. 确认 TTS 服务正常
curl -s http://127.0.0.1:8082/health
# 应返回 {"status": "ok", "engine": "cosyvoice2-mlx"}

# 2. 确认 Hermes gateway 正常
curl -s -X POST http://127.0.0.1:8083/xiaozhi/ota/ \
  -H "Content-Type: application/json" -d '{}'
# 应返回含 websocket.url 的 JSON

# 3. 设备开机，确认连接
tail -f ~/.hermes/logs/gateway.log | grep "windowsill"
# 应看到 "windowsill.ota device=..." 和 WS 连接日志

# 4. 对设备说话，测试完整链路
```

---

## 关键配置速查

| 服务 | 端口 | 说明 |
|------|------|------|
| Hermes Gateway | 8083 | OTA + WebSocket + 图片代理 |
| CosyVoice2 TTS | 8082 | OpenAI 兼容 TTS 接口 |

| 配置项 | 值 |
|--------|-----|
| 设备 OTA URL | `http://<IP>:8083/xiaozhi/ota/` |
| 参考音频 | `~/gateway/ref_voice_custom.wav` |
| VAD 模型 | `/tmp/xiaozhi-esp32-server/main/xiaozhi-server/models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx` |
| 日志 | `~/.hermes/logs/gateway.log` |
