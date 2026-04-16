#!/usr/bin/env python3
"""
psy-guard WebSocket 服务
接收 iPhone BLE 中继的 PCM 音频 -> FunASR (WebSocket) 转写 -> LLM 分析 -> 预警推送

iPhone -> 服务器:
  - 二进制帧：PCM 16bit LE, 单声道, 16000Hz
  - 文本帧 "START"：开始录音
  - 文本帧 "STOP"：停止录音

服务器 -> iPhone:
  - JSON: {"type":"alert","level":"high|medium|low","keyword":"...","text":"..."}
  - 文本: "ACK:START" / "ACK:STOP"
"""

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from collections import deque

import aiohttp
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("psy-guard")

# ─────────────────────────────────────────────────────────────
#  配置
# ─────────────────────────────────────────────────────────────
PORT            = int(os.getenv("PORT", "8097"))
FUNASR_WS_URL   = os.getenv("FUNASR_WS_URL", "ws://localhost:10095")
LLM_BASE_URL    = os.getenv("LLM_BASE_URL", "http://localhost:8086/v1")
LLM_MODEL       = os.getenv("LLM_MODEL", "gemma-4-E4B-it-Q4_K_M.gguf")
LLM_API_KEY     = os.getenv("LLM_API_KEY", "none")

SAMPLE_RATE     = 16000
SAMPLE_WIDTH    = 2
CHANNELS        = 1

WINDOW_SEC      = float(os.getenv("WINDOW_SEC", "5"))
WINDOW_BYTES    = int(WINDOW_SEC * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

# 最短有效文本长度（汉字），过短则跳过 LLM
MIN_TEXT_LEN    = int(os.getenv("MIN_TEXT_LEN", "4"))

# 滚动上下文：最多保留多少字符的历史转写文本
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "300"))

# LLM 并发限制（同一连接串行，防止积压）
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "1"))

# ─────────────────────────────────────────────────────────────
#  LLM 系统提示
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一名心理危机干预辅助系统。分析心理咨询对话片段，识别潜在危机信号。

判断标准：
- high（紧急）：明确的自杀/自伤意图、暴力威胁、急性崩溃
- medium（警示）：绝望感、无价值感、孤立、隐晦求救信号
- low（关注）：情绪低落、轻微负面词汇、需持续观察

无危机信号返回 null。检测到信号必须返回如下 JSON（不含其他内容）：
{"level":"high|medium|low","keyword":"触发词","suggestion":"给咨询师的一句话建议"}"""

# ─────────────────────────────────────────────────────────────
#  FunASR 转写（WebSocket 协议）
# ─────────────────────────────────────────────────────────────
async def transcribe(pcm: bytes) -> str:
    log.info(f"[ASR] transcribing {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/SAMPLE_WIDTH:.1f}s)")
    try:
        async with websockets.connect(FUNASR_WS_URL, max_size=10 * 1024 * 1024,
                                      open_timeout=10) as ws:
            config = {
                "mode": "2pass",
                "wav_name": "audio",
                "wav_format": "pcm",
                "is_speaking": True,
                "itn": True,
                "audio_fs": SAMPLE_RATE,
                "chunk_size": [5, 10, 5],
                "chunk_interval": 10,
            }
            await ws.send(json.dumps(config))

            chunk_size = 960 * SAMPLE_WIDTH  # 1920 bytes ≈ 30ms
            for i in range(0, len(pcm), chunk_size):
                await ws.send(pcm[i:i + chunk_size])

            await ws.send(json.dumps({"is_speaking": False}))

            text = ""
            async with asyncio.timeout(30):
                async for message in ws:
                    data = json.loads(message)
                    log.info(f"[ASR] raw: {data}")
                    mode = data.get("mode", "")
                    is_final = data.get("is_final", False)
                    t = data.get("text", "").strip()
                    if is_final and mode == "2pass-offline" and t:
                        text = t
                        break
                    if is_final and not mode:
                        break

            log.info(f"[ASR] result: {text!r}")
            return text
    except Exception as e:
        log.error(f"[ASR] Error: {e}\n{traceback.format_exc()}")
        return ""

# ─────────────────────────────────────────────────────────────
#  LLM 分析
# ─────────────────────────────────────────────────────────────
async def analyze(session: aiohttp.ClientSession, context: str, new_text: str) -> dict | None:
    """
    context: 近期历史转写（给 LLM 更多背景）
    new_text: 本窗口新增文本
    """
    user_content = f"对话片段：\n{new_text}"
    if context:
        user_content = f"历史上文（供参考）：\n{context}\n\n{user_content}"

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                log.info(f"[LLM] raw: {content!r} finish_reason={data['choices'][0].get('finish_reason')}")
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
                return None
            else:
                body = await resp.text()
                log.warning(f"[LLM] HTTP {resp.status}: {body[:200]}")
                return None
    except Exception as e:
        log.error(f"[LLM] Error: {e}")
        return None

# ─────────────────────────────────────────────────────────────
#  处理一个音频窗口
# ─────────────────────────────────────────────────────────────
async def process_window(
    session: aiohttp.ClientSession,
    websocket,
    pcm: bytes,
    context_buf: deque,
    llm_sem: asyncio.Semaphore,
):
    text = await transcribe(pcm)
    if not text or len(text) < MIN_TEXT_LEN:
        if text:
            log.info(f"[SKIP] text too short: {text!r}")
        return

    # 把转写文本实时推回手机（无论是否有预警）
    try:
        await websocket.send(json.dumps({"type": "transcript", "text": text}, ensure_ascii=False))
    except Exception:
        pass

    # 构建上下文字符串（最近积累的历史）
    context = "".join(context_buf)
    if len(context) > CONTEXT_MAX_CHARS:
        context = context[-CONTEXT_MAX_CHARS:]

    # 串行化 LLM 调用，防止并发积压
    async with llm_sem:
        alert_data = await analyze(session, context, text)

    # 无论是否有预警，都把本段文字加入滚动上下文
    context_buf.append(text)
    # 修剪上下文，保持总长度在限制内
    total = sum(len(s) for s in context_buf)
    while total > CONTEXT_MAX_CHARS and context_buf:
        removed = context_buf.popleft()
        total -= len(removed)

    if not alert_data:
        return

    alert = {
        "type": "alert",
        "id": str(uuid.uuid4()),
        "level": alert_data.get("level", "low"),
        "keyword": alert_data.get("keyword", ""),
        "text": text,
        "suggestion": alert_data.get("suggestion", ""),
        "timestamp": time.time(),
    }
    log.warning(
        f"[ALERT] level={alert['level']} kw={alert['keyword']!r} text={text!r}"
    )
    try:
        await websocket.send(json.dumps(alert, ensure_ascii=False))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
#  每个连接的处理逻辑
# ─────────────────────────────────────────────────────────────
async def handle(websocket):
    client = websocket.remote_address
    log.info(f"[WS] Connected: {client}")

    audio_buf = bytearray()
    recording = False
    context_buf: deque[str] = deque()           # 滚动转写上下文
    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)  # 每连接 LLM 串行

    async with aiohttp.ClientSession() as session:
        try:
            async for message in websocket:
                if isinstance(message, str):
                    cmd = message.strip().upper()
                    if cmd == "START":
                        recording = True
                        audio_buf.clear()
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START")
                    elif cmd == "STOP":
                        recording = False
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP")
                        if len(audio_buf) > SAMPLE_RATE * SAMPLE_WIDTH // 4:
                            asyncio.create_task(
                                process_window(session, websocket, bytes(audio_buf),
                                               context_buf, llm_sem)
                            )
                        audio_buf.clear()
                    continue

                if not recording or not isinstance(message, bytes):
                    continue

                audio_buf.extend(message)

                if len(audio_buf) >= WINDOW_BYTES:
                    chunk = bytes(audio_buf[:WINDOW_BYTES])
                    audio_buf = bytearray(audio_buf[WINDOW_BYTES:])
                    asyncio.create_task(
                        process_window(session, websocket, chunk, context_buf, llm_sem)
                    )

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")

# ─────────────────────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"FunASR WS: {FUNASR_WS_URL}")
    log.info(f"LLM:       {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Window:    {WINDOW_SEC}s ({WINDOW_BYTES} bytes)  min_text={MIN_TEXT_LEN}  ctx={CONTEXT_MAX_CHARS}chars")

    async with websockets.serve(handle, "0.0.0.0", PORT, max_size=2**20):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
