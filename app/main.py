import os
import socket

from fastapi import FastAPI

app = FastAPI()


@app.get("/whereami")
def whereami():
    return {"site": os.getenv("SITE", socket.gethostname())}
