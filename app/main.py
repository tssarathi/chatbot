import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
STATIC = Path(__file__).parent / "static"
INDEX = STATIC / "index.html"

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
                LINK["rtt_ms"] = LINK["ttft_ms"] = LINK["tok_per_s"] = None
                LINK["model_ip"] = None
                LINK["error"] = exc.__class__.__name__
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_probe())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Ask(BaseModel):
    session: str
    message: str


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(INDEX, headers={"cache-control": "no-cache"})


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return {"status": "ok", "instance_id": INSTANCE_ID}


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


async def _stream(history: list[dict[str, str]], message: str):
    words: list[str] = []
    t0 = time.monotonic()
    ttft_ms: int | None = None
    tok_per_s: float | None = None

    turn = {"role": "user", "content": message}
    history.append(turn)
    completed = False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=3.0, read=45.0)
        ) as client:
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
                        tok_per_s = _rate(frame)
                        completed = True
                        break
                    if not words:
                        ttft_ms = round((time.monotonic() - t0) * 1000)
                    words.append(frame["message"]["content"])
                    yield f"data: {json.dumps({'token': words[-1]})}\n\n"
    except httpx.HTTPError as exc:
        detail = exc.__class__.__name__
        yield f"data: {json.dumps({'error': 'model unreachable', 'detail': detail})}\n\n"
        return
    finally:
        if completed:
            history.append({"role": "assistant", "content": "".join(words)})
        else:
            for i in range(len(history) - 1, -1, -1):
                if history[i] is turn:
                    del history[i]
                    break

    LINK["ttft_ms"] = ttft_ms
    LINK["tok_per_s"] = tok_per_s

    receipt = {
        "done": True,
        "turns": len(history),
        "ttft_ms": ttft_ms,
        "tok_per_s": tok_per_s,
        "rtt_ms": LINK["rtt_ms"],
        "model_ip": LINK["model_ip"],
    }
    yield f"data: {json.dumps(receipt)}\n\n"


@app.post("/chat")
async def chat(ask: Ask):
    history = SESSIONS.setdefault(ask.session, [])
    return StreamingResponse(_stream(history, ask.message), media_type="text/event-stream")
