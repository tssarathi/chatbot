# chatbot

A chat app that reports where it is running, and keeps reporting it while it moves.

The chat itself is deliberately plain. The environment rail on the right is the point: it
shows the site, node, pod, address and model link, and updates once a second, including
while the app is being moved from one place to another underneath it.

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Built with](#built-with)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Running on localhost (Mac)](#running-on-localhost-mac)
- [Where the conversation lives](#where-the-conversation-lives)
- [HTTP API](#http-api)
- [Development](#development)
- [Design notes](#design-notes)
- [Third-party code](#third-party-code)

## How it works

Every location fact the rail shows is either **injected** through an environment variable
or falls back to something true about the machine it is on: the hostname, the resolved
local address. Nothing is guessed or hard-coded, so the app runs bare with no configuration
and still reports honestly.

Three mechanisms drive the rail:

| Mechanism | What it does |
| --- | --- |
| **Instance fingerprint** | A random id generated when the process starts. The browser polls `/whereami` every second; a changed id can only mean a different process is answering, which is how a move is detected without the page reloading. |
| **Link probe** | Every two seconds the app measures round-trip time to the model and smooths it (EWMA, 0.3 new / 0.7 old). If the model stops answering the rail goes red, and an answer being streamed is stopped with a reason rather than hanging. |
| **Session store** | Conversation state lives either in the process (default) or in a shared store. This decides whether a move loses the conversation or keeps it. |

## Architecture

```
YOUR MACHINE
│
├── Browser ─ http://localhost:8200
│
└── DOCKER
    │
    ├── network "onprem" ── 10.20.0.0/24 ──┐
    │     └── app container  10.20.0.4     │   SITE=on-prem-mel
    │                                      │
    ├── network "cloud"  ── 10.30.0.0/24 ──┤
    │     └── app container  10.30.0.4     │   SITE=eks-syd
    │                                      │
    ├── model (Ollama) ── :11434 ──────────┤   granite baked into the image
    │                                      │   also published on localhost:11434
    └── sessions (redis) ──────────────────┘   optional shared conversation store
```

Only one of the two app containers runs at a time, because both publish the same port, so
swapping which one is up **is** the move. `model` and `sessions` sit on both networks and
stay put.

A message travels browser → app container → `model` → Ollama API, and the generated words
stream back the same way. Pausing `model` cuts that hop mid-answer.

## Built with

- [FastAPI](https://fastapi.tiangolo.com/) and [uvicorn](https://www.uvicorn.org/): the backend, just over 200 lines
- [httpx](https://www.python-httpx.org/): streaming client for the model
- [Ollama](https://ollama.com): runs the model (host for bare runs; container image for Compose)
- A single static HTML file: no build step, no framework, no bundler
- [marked](https://marked.js.org/), [DOMPurify](https://github.com/cure53/DOMPurify) and [highlight.js](https://highlightjs.org/): vendored, for rendering model output safely
- [Docker Compose](https://docs.docker.com/compose/) and Redis: the two sites, the model server and the shared store

## Getting started

### Prerequisites

- [uv](https://docs.astral.sh/uv/): installs Python 3.12 itself if you don't have it
- [Docker](https://docs.docker.com/get-docker/): for the containerised sites and model server
- For bare (non-Compose) runs only: [Ollama](https://ollama.com) with the model pulled:

  ```sh
  ollama pull granite4.1:8b
  ```

### Run it directly

```sh
uv sync
uv run uvicorn app.main:app --reload
```

Then open <http://localhost:8000/>. The app expects Ollama on `localhost:11434`.

## Configuration

All optional. Each falls back to something true about the machine.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SITE` | the detected flavour's title | Name reported as the app's location |
| `PLATFORM` | detected from the cluster | `onprem` / `rosa` / `eks`, forced when set |
| `REGION` | node label, else `unknown` | Region label |
| `NODE_NAME` | machine hostname | Node the app is running on |
| `POD_NAME` | machine hostname | Pod or container name |
| `POD_IP` | resolved local address | Leave unset to report the real address |
| `OLLAMA_URL` | `http://localhost:11434` | Where the model is served from |
| `MODEL_NAME` | `granite4.1:8b` | Model to use |
| `SESSION_STORE` | `memory` | `memory` or `redis`, where conversation state lives |
| `REDIS_URL` | `redis://localhost:6379` | Shared store, used when `SESSION_STORE=redis`. Compose sets `redis://sessions:6379`. |

In Kubernetes, `NODE_NAME`, `POD_NAME` and `POD_IP` come from the Downward API.

```sh
SITE=rosa-syd PLATFORM=rosa uv run uvicorn app.main:app
```

`PLATFORM` is one of `onprem` | `rosa` | `eks` (legacy `cloud` maps to `eks`).

## Running on localhost (Mac)

```sh
docker compose up -d --build onprem
open http://localhost:8200/
```

Naming the site enables its profile. Every site publishes the same `8200`, so each one is
behind a profile and only the site you name comes up.

Compose builds **native arch only** (arm64 on Apple Silicon). App on `8200`, model on `11434`.
Stop host Ollama first if it already owns `11434`.

### Move between sites

```sh
docker compose stop onprem
docker compose up -d eks                        # or: rosa

docker compose pause model                      # cut the model link
docker compose unpause model

docker compose stop eks
docker compose up -d onprem                     # back to on-prem
```

`pause` drops packets rather than refusing them. Killing the model container instead
gives connection-refused.

## Kubernetes (Cilium LB)

Manifests live under `deploy/k8s/` (Namespace, RBAC, Deployment, LoadBalancer Service).
On the nuberu mgmt cluster the Service uses Cilium IPAM VIP **10.0.0.240**:

```sh
export KUBECONFIG=/path/to/mgmt.kubeconfig
docker build -t chatbot-site:latest .
kubectl apply -k deploy/k8s
kubectl -n chatbot get svc chatbot
open http://10.0.0.240/
```

See `deploy/k8s/README.md` for image loading, model URL, and auto-detection of
ONPREM / ROSA / EKS from node labels.

### Multi-arch images

Both Dockerfiles are multi-arch ready (`linux/amd64` + `linux/arm64`). Model weights are
pulled once on the builder’s native arch and copied into each target.

```sh
docker buildx create --name multi --use   # once
IMAGE_PREFIX=ghcr.io/<you>/ docker buildx bake --push
```

Without `IMAGE_PREFIX`, bake tags `chatbot-site` / `chatbot-model` locally — you still need
a registry (`--push`) or a containerd image store to keep a multi-platform manifest.

## Where the conversation lives

By default each site keeps conversations in its own memory, so a move loses them and the
rail reports `conversation lost`. Point the sites at the shared store and the same move
reports `conversation preserved` instead, because the new process reads the conversation
the old one wrote:

`SESSION_STORE` is read from your shell on each `up`, so **export it once**. Setting it on
only the first command leaves the other site in `memory` mode, which silently breaks the
comparison:

```sh
export SESSION_STORE=redis
docker compose up -d --build onprem
# ...then the same move commands as above
```

Same move, same outage, same new address. Only the outcome differs. Stateless to deploy
is not the same as holding no state.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET`/`HEAD` `/` | The page |
| `GET`/`HEAD` `/healthz` | Liveness, plus the instance id. Reports no topology. |
| `GET /whereami` | Everything the rail displays |
| `POST /chat` | Streams an answer as server-sent events |
| `POST /history` | The stored conversation for a session |
| `GET /static/…` | Vendored browser libraries |

FastAPI also serves `/docs`, `/redoc` and `/openapi.json` by default.

`/history` takes the session id in a POST body rather than a URL path deliberately: there
is no authentication, so the id is the only thing protecting a conversation, and a path
segment ends up in every access log.

`/chat` frames are `data: {...}` lines carrying one of `token`, `error`, or a terminal
`done` receipt. Any failure ends the stream with a frame rather than dropping the
connection: an unreachable model, a model-reported error, a malformed reply, or an
unreachable session store.

## Development

```sh
uv run ruff check app/ tests/    # lint
uv run ruff format app/ tests/   # format
uv run pytest                    # tests
```

The suite covers the HTTP contract, the streaming failure paths and the session store, plus
a set of invariants asserted directly against the page source: that every element id the
script looks up exists, that no theme token is declared twice, that buttons do not fall
back to native browser chrome, and that no code path collapses the rail or cancels an
answer silently. Most of those exist because the corresponding bug happened.

## Design notes

**The model is packaged into `chatbot-model`.** `model/Dockerfile` pulls `granite4.1:8b`
on the builder’s native arch during the image build and copies the cache into each
runtime arch. Compose serves it on `11434` (loopback). On macOS the container has no GPU;
for this small model the gap is negligible.

**Images are multi-arch.** `docker compose up --build onprem` on a Mac builds arm64 only.
`docker buildx bake` produces `linux/amd64` + `linux/arm64` manifests for a registry.

**The model container exists to be cut.** Pausing `model` freezes the hop mid-answer the
same way a pulled cable would. Stopping it instead gives connection-refused.

**Published ports are bound to loopback.** The app has no authentication by design and it
fronts an unauthenticated model API, so both `8200` and `11434` listen only on the local
machine. Change `ports` in `compose.yaml` if you need either reachable from elsewhere.

## Third-party code

Browser libraries under `app/static/vendor/` are vendored rather than loaded from a CDN, so
the page works offline and the versions are pinned. Their licences are kept alongside them,
and `VERSIONS.txt` records the versions and how the bundles were built.
