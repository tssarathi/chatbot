import os
import socket
import time
import uuid

import httpx
from fastapi import FastAPI
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
        "uptime_s": round(time.monotonic() - STARTED, 1),
    }


@app.post("/chat")
async def chat(ask: Ask):
    history = SESSIONS.setdefault(ask.session, [])
    history.append({"role": "user", "content": ask.message})

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": MODEL_NAME, "messages": history, "stream": False},
        )
    reply = r.json()["message"]
    history.append(reply)
    return {"reply": reply["content"], "turns": len(history)}
