import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


def _local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]


STARTED = time.monotonic()
INSTANCE_ID = uuid.uuid4().hex[:8]
HOST = socket.gethostname()
LOCAL_IP = _local_ip()

OLLAMA_URL = os.getenv("OLLAMA_URL") or "http://localhost:11434"
MODEL_NAME = os.getenv("MODEL_NAME") or "granite3.1-moe:1b"

SESSIONS: dict[str, list[dict[str, str]]] = {}
LINK: dict[str, float | str | None] = {
    "rtt_ms": None,
    "model_ip": None,
    "ttft_ms": None,
    "tok_per_s": None,
    "error": None,
}


async def _probe() -> None:
    url = httpx.URL(OLLAMA_URL)
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(timeout=2) as client:
        while True:
            t0 = time.monotonic()
            try:
                r = await client.get(f"{OLLAMA_URL}/api/version")
                r.raise_for_status()
                rtt = (time.monotonic() - t0) * 1000
                prev = LINK["rtt_ms"]
                LINK["rtt_ms"] = round(
                    rtt if not isinstance(prev, float) else 0.3 * rtt + 0.7 * prev, 2
                )
                info = await loop.getaddrinfo(url.host, url.port or 11434, family=socket.AF_INET)
                LINK["model_ip"] = info[0][4][0]
                LINK["error"] = None
            except (httpx.HTTPError, OSError) as exc:
                LINK["rtt_ms"] = None
                LINK["error"] = exc.__class__.__name__
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_probe())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


class Ask(BaseModel):
    session: str
    message: str


@app.get("/whereami")
def whereami():
    return {
        "site": os.getenv("SITE") or HOST,
        "platform": os.getenv("PLATFORM") or "unknown",
        "region": os.getenv("REGION") or "unknown",
        "node": os.getenv("NODE_NAME") or HOST,
        "pod": os.getenv("POD_NAME") or HOST,
        "pod_ip": os.getenv("POD_IP") or LOCAL_IP,
        "instance_id": INSTANCE_ID,
        "model": MODEL_NAME,
        "model_url": OLLAMA_URL,
        "link": LINK,
        "uptime_s": round(time.monotonic() - STARTED, 1),
    }


def _rate(frame: dict) -> float | None:
    n, ns = frame.get("eval_count"), frame.get("eval_duration")
    return round(n / (ns / 1e9), 1) if n and ns else None


async def _stream(history: list[dict[str, str]]):
    words: list[str] = []
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/chat",
                json={"model": MODEL_NAME, "messages": history, "stream": True},
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    frame = json.loads(line)
                    if frame.get("done"):
                        LINK["tok_per_s"] = _rate(frame)
                        break
                    if not words:
                        LINK["ttft_ms"] = round((time.monotonic() - t0) * 1000)
                    words.append(frame["message"]["content"])
                    yield f"data: {json.dumps({'token': words[-1]})}\n\n"
    except httpx.HTTPError as exc:
        detail = exc.__class__.__name__
        yield f"data: {json.dumps({'error': 'model unreachable', 'detail': detail})}\n\n"
        return

    history.append({"role": "assistant", "content": "".join(words)})
    yield f"data: {json.dumps({'done': True, 'turns': len(history), **LINK})}\n\n"


@app.post("/chat")
async def chat(ask: Ask):
    history = SESSIONS.setdefault(ask.session, [])
    history.append({"role": "user", "content": ask.message})
    return StreamingResponse(_stream(history), media_type="text/event-stream")
