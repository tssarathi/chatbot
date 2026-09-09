# chatbot

A chat app that reports where it is running, and keeps reporting it while it moves.

The chat itself is deliberately plain. The panel in the top right is the point: it shows
the site, node, pod, address and model link, and updates once a second, including while
the app is being moved from one place to another underneath it.

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Built with](#built-with)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Running the two sites](#running-the-two-sites)
- [Where the conversation lives](#where-the-conversation-lives)
- [HTTP API](#http-api)
- [Development](#development)
- [Design notes](#design-notes)
- [Third-party code](#third-party-code)

## How it works

Every location fact the panel shows is either **injected** through an environment variable
or falls back to something true about the machine it is on: the hostname, the resolved
local address. Nothing is guessed or hard-coded, so the app runs bare with no configuration
and still reports honestly.

Three mechanisms drive the panel:

| Mechanism | What it does |
| --- | --- |
| **Instance fingerprint** | A random id generated when the process starts. The browser polls `/whereami` every second; a changed id can only mean a different process is answering, which is how a move is detected without the page reloading. |
| **Link probe** | Every two seconds the app measures round-trip time to the model and smooths it (EWMA, 0.3 new / 0.7 old). If the model stops answering the panel goes red, and an answer being streamed is stopped with a reason rather than hanging. |
| **Session store** | Conversation state lives either in the process (default) or in a shared store. This decides whether a move loses the conversation or keeps it. |

## Architecture

```
YOUR MACHINE
│
├── Ollama ─ localhost:11434 ─ the model, running natively
│
├── Browser ─ http://localhost:8200
│
└── DOCKER
    │
    ├── network "onprem" ── 10.20.0.0/24 ──┐
    │     └── app container  10.20.0.4     │   SITE=on-prem-mel
    │                                      │
    ├── network "cloud"  ── 10.30.0.0/24 ──┤
    │     └── app container  10.30.0.4     │   SITE=cloud-syd
    │                                      │
    ├── modellink (nginx) ─────────────────┤   relays to the host's Ollama
    │                                      │
    └── sessions (redis) ──────────────────┘   optional shared conversation store
```

Only one of the two app containers runs at a time, because both publish the same port, so
swapping which one is up **is** the move. `modellink` and `sessions` sit on both networks
and stay put.

A message travels browser → app container → `modellink` → Ollama on the host, and the
generated words stream back the same way. The relay exists so that path contains a real
container that can be cut.

## Built with

- [FastAPI](https://fastapi.tiangolo.com/) and [uvicorn](https://www.uvicorn.org/): the backend, just over 200 lines
- [httpx](https://www.python-httpx.org/): streaming client for the model
- [Ollama](https://ollama.com): runs the model
- A single static HTML file: no build step, no framework, no bundler
- [marked](https://marked.js.org/), [DOMPurify](https://github.com/cure53/DOMPurify) and [highlight.js](https://highlightjs.org/): vendored, for rendering model output safely
- [Docker Compose](https://docs.docker.com/compose/), nginx and Redis: the two sites, the relay and the shared store

## Getting started

### Prerequisites

- [uv](https://docs.astral.sh/uv/): installs Python 3.12 itself if you don't have it
- [Ollama](https://ollama.com), with the model pulled:

  ```sh
  ollama pull granite3.1-moe:1b
  ```

- [Docker](https://docs.docker.com/get-docker/): only for the containerised sites

### Run it directly

```sh
uv sync
uv run uvicorn app.main:app --reload
```

Then open <http://localhost:8000/>.

## Configuration

All optional. Each falls back to something true about the machine.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SITE` | machine hostname | Name reported as the app's location |
| `PLATFORM` | `unknown` | `on-prem` / `cloud`, drives the panel's accent colour |
| `REGION` | `unknown` | Region label |
| `NODE_NAME` | machine hostname | Node the app is running on |
| `POD_NAME` | machine hostname | Pod or container name |
| `POD_IP` | resolved local address | Leave unset to report the real address |
| `OLLAMA_URL` | `http://localhost:11434` | Where the model is served from |
| `MODEL_NAME` | `granite3.1-moe:1b` | Model to use |
| `SESSION_STORE` | `memory` | `memory` or `redis`, where conversation state lives |
| `REDIS_URL` | `redis://sessions:6379` | Shared store, used when `SESSION_STORE=redis` |

In Kubernetes, `NODE_NAME`, `POD_NAME` and `POD_IP` come from the Downward API.

```sh
SITE=cloud-syd PLATFORM=cloud uv run uvicorn app.main:app
```

## Running the two sites

Two sites, one published port, one model. Only one site runs at a time, and swapping which
one is up **is** the move. The browser URL never changes; the container's address and subnet do,
and the gap between the two commands is the outage the panel measures.

```sh
docker compose --profile onprem up -d --build   # start on-prem
open http://localhost:8200/                     # leave this open throughout

docker compose stop onprem                      # move to cloud
docker compose --profile cloud up -d            #   panel flips, transcript clears

docker compose pause modellink                  # cut the model link
docker compose unpause modellink                # restore it

docker compose stop cloud                       # move back
docker compose --profile onprem up -d
```

`pause` is deliberate: it drops packets rather than refusing them, which is what a pulled
cable does. Killing the relay instead gives an instant connection-refused and exercises a
different path.

Both sites build from one image (`chatbot-site`), so rebuilding either updates both.

## Where the conversation lives

By default each site keeps conversations in its own memory, so a move loses them and the
panel reports `conversation lost`. Point the sites at the shared store and the same move
reports `conversation preserved` instead, because the new process reads the conversation
the old one wrote:

`SESSION_STORE` is read from your shell on each `up`, so **export it once**. Setting it on
only the first command leaves the other site in `memory` mode, which silently breaks the
comparison:

```sh
export SESSION_STORE=redis
docker compose --profile onprem up -d --build
# ...then the same move commands as above
```

Same move, same outage, same new address. Only the outcome differs. Stateless to deploy
is not the same as holding no state.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET`/`HEAD` `/` | The page |
| `GET`/`HEAD` `/healthz` | Liveness, plus the instance id. Reports no topology. |
| `GET /whereami` | Everything the panel displays |
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
back to native browser chrome, and that no code path collapses the panel or cancels an
answer silently. Most of those exist because the corresponding bug happened.

## Design notes

**Ollama runs on the host, not in a container.** It is already installed and running there.
A containerised Ollama gets no GPU on macOS, but measured on this model the difference is
single-digit milliseconds to first token at identical throughput, so this is a setup
convenience, not a performance decision.

**The relay exists to be cut.** With the model on the host, nothing between the app and the
model would otherwise be a container. `modellink` puts a real hop in that path, one that
can be frozen mid-answer.

**The published port is bound to loopback.** The app has no authentication by design and it
fronts an unauthenticated relay to the model, so it listens only on the local machine.
Change `ports` in `compose.yaml` if you need it reachable from elsewhere.

## Third-party code

Browser libraries under `app/static/vendor/` are vendored rather than loaded from a CDN, so
the page works offline and the versions are pinned. Their licences are kept alongside them,
and `VERSIONS.txt` records the versions and how the bundles were built.
