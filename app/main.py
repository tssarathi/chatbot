import asyncio
import contextlib
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))
            return s.getsockname()[0]
    except OSError:
        return "unknown"


STARTED = time.monotonic()
INSTANCE_ID = uuid.uuid4().hex[:8]
HOST = socket.gethostname()
LOCAL_IP = _local_ip()

OLLAMA_URL = os.getenv("OLLAMA_URL") or "http://localhost:11434"
MODEL_NAME = os.getenv("MODEL_NAME") or "granite3.1-moe:1b"
STATIC = Path(__file__).parent / "static"
INDEX = STATIC / "index.html"

SESSION_STORE = (os.getenv("SESSION_STORE") or "memory").lower()
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379"
SESSION_TTL_S = 3600
REDIS: Any = (
    aioredis.from_url(REDIS_URL, decode_responses=True) if SESSION_STORE == "redis" else None
)

SESSIONS: dict[str, list[dict[str, str]]] = {}
LINK: dict[str, float | str | None] = {
    "rtt_ms": None,
    "model_ip": None,
    "ttft_ms": None,
    "tok_per_s": None,
    "error": None,
}


async def _probe_once(client: httpx.AsyncClient, loop: Any, url: httpx.URL) -> None:
    t0 = time.monotonic()
    try:
        r = await client.get(f"{OLLAMA_URL}/api/version")
        r.raise_for_status()
        rtt = (time.monotonic() - t0) * 1000
        prev = LINK["rtt_ms"]
        LINK["rtt_ms"] = round(rtt if not isinstance(prev, float) else 0.3 * rtt + 0.7 * prev, 2)
        LINK["error"] = None
    except Exception as exc:
        LINK["rtt_ms"] = LINK["ttft_ms"] = LINK["tok_per_s"] = None
        LINK["model_ip"] = None
        LINK["error"] = exc.__class__.__name__
        return
    try:
        info = await loop.getaddrinfo(url.host, url.port or 11434, family=socket.AF_INET)
        LINK["model_ip"] = info[0][4][0]
    except Exception:
        LINK["model_ip"] = None


async def _probe() -> None:
    url = httpx.URL(OLLAMA_URL)
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(timeout=2) as client:
        while True:
            await _probe_once(client, loop, url)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_probe())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if REDIS is not None:
        await REDIS.aclose()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Session(BaseModel):
    session: str = Field(max_length=200)


class Ask(Session):
    message: str = Field(max_length=8000)


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
        "session_store": SESSION_STORE,
        "uptime_s": round(time.monotonic() - STARTED, 1),
    }


@app.post("/history")
async def history(ask: Session):
    try:
        return {"messages": await _load(ask.session)}
    except (StoreError, ValueError):
        return {"messages": []}


class StoreError(Exception):
    pass


def _sane(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    return [
        m
        for m in history
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ]


async def _load(session: str) -> list[dict[str, str]]:
    if SESSION_STORE != "redis":
        return list(SESSIONS.get(session, []))
    try:
        raw = await REDIS.get(f"chat:{session}")
    except Exception as exc:
        raise StoreError(exc.__class__.__name__) from exc
    return _sane(json.loads(raw)) if raw else []


async def _save(session: str, history: list[dict[str, str]]) -> None:
    if SESSION_STORE != "redis":
        SESSIONS[session] = history
        return
    try:
        await REDIS.set(f"chat:{session}", json.dumps(history), ex=SESSION_TTL_S)
    except Exception as exc:
        raise StoreError(exc.__class__.__name__) from exc


def _rate(frame: dict[str, Any]) -> float | None:
    n, ns = frame.get("eval_count"), frame.get("eval_duration")
    return round(n / (ns / 1e9), 1) if n and ns else None


async def _stream(session: str, message: str):
    words: list[str] = []
    t0 = time.monotonic()
    ttft_ms: int | None = None
    tok_per_s: float | None = None

    completed = False
    try:
        history = await _load(session)
        history.append({"role": "user", "content": message})
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
                    if frame.get("error"):
                        problem = json.dumps(
                            {"error": "model failed", "detail": str(frame["error"])}
                        )
                        yield f"data: {problem}\n\n"
                        return
                    if frame.get("done"):
                        tok_per_s = _rate(frame)
                        completed = True
                        break
                    if not words:
                        ttft_ms = round((time.monotonic() - t0) * 1000)
                    words.append(frame["message"]["content"])
                    yield f"data: {json.dumps({'token': words[-1]})}\n\n"
        if not completed:
            cut = json.dumps({"error": "the model stopped early", "detail": "no done frame"})
            yield f"data: {cut}\n\n"
            return
        history.append({"role": "assistant", "content": "".join(words)})
        LINK["ttft_ms"] = ttft_ms
        LINK["tok_per_s"] = tok_per_s
        await _save(session, history)
    except StoreError as exc:
        down = json.dumps({"error": "session store unreachable", "detail": str(exc)})
        yield f"data: {down}\n\n"
        return
    except httpx.HTTPError as exc:
        detail = exc.__class__.__name__
        yield f"data: {json.dumps({'error': 'model unreachable', 'detail': detail})}\n\n"
        return
    except Exception as exc:
        detail = exc.__class__.__name__
        yield f"data: {json.dumps({'error': 'bad reply from model', 'detail': detail})}\n\n"
        return

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
    return StreamingResponse(_stream(ask.session, ask.message), media_type="text/event-stream")
