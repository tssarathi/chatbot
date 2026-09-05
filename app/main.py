import os
import socket
import uuid

from fastapi import FastAPI

app = FastAPI()

INSTANCE_ID = uuid.uuid4().hex[:8]


@app.get("/whereami")
def whereami():
    return {
        "site": os.getenv("SITE", socket.gethostname()),
        "instance_id": INSTANCE_ID,
    }
