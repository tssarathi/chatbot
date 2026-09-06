import os
import socket
import time
import uuid

from fastapi import FastAPI

app = FastAPI()


def _local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]


STARTED = time.monotonic()
INSTANCE_ID = uuid.uuid4().hex[:8]
HOST = socket.gethostname()
LOCAL_IP = _local_ip()


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
