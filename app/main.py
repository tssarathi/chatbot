import json
import os
import socket
import time
import uuid

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()


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
LINK: dict[str, object] = {"ttft_ms": None, "tok_per_s": None, "error": None}


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
        LINK["error"] = exc.__class__.__name__
        yield f"data: {json.dumps({'error': 'model unreachable', 'detail': LINK['error']})}\n\n"
        return

    LINK["error"] = None
    history.append({"role": "assistant", "content": "".join(words)})
    yield f"data: {json.dumps({'done': True, 'turns': len(history), **LINK})}\n\n"


@app.post("/chat")
async def chat(ask: Ask):
    history = SESSIONS.setdefault(ask.session, [])
    history.append({"role": "user", "content": ask.message})
    return StreamingResponse(_stream(history), media_type="text/event-stream")
