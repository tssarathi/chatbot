# chatbot

A chat application that reports where it is running, and keeps reporting it while it moves.

The chat itself is deliberately plain. The environment rail is the subject: it shows the
site, platform, node, pod, address and model link, and refreshes once a second, including
while the application is relocated underneath it.

The application is a demonstration of workload portability. It runs identically on a
laptop under Docker Compose and on a Kubernetes cluster, and in both cases it determines
its own location rather than being told.

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Running with Docker Compose](#running-with-docker-compose)
- [Where the conversation lives](#where-the-conversation-lives)
- [Deploying to Kubernetes](#deploying-to-kubernetes)
- [Building images](#building-images)
- [HTTP API](#http-api)
- [Development](#development)
- [Design decisions](#design-decisions)
- [Third-party code](#third-party-code)

## Quick start

### Prerequisites

| Requirement | Used for |
| --- | --- |
| [uv](https://docs.astral.sh/uv/) | Python 3.12 and dependencies; installs Python itself if absent |
| [Docker](https://docs.docker.com/get-docker/) | The containerised sites and the model server |
| [Ollama](https://ollama.com) | Only for bare, non-Compose runs |

### Docker Compose

```sh
docker compose up -d --build onprem
open http://localhost:8200/
```

Naming the site activates its profile. All sites publish port `8200`, so each is gated
behind a profile and only the site you name starts.

The first build pulls the model into the image and is therefore slow. Compose builds for
the host architecture only (arm64 on Apple Silicon). Stop any host Ollama already bound
to `11434` before starting.

### Bare

```sh
ollama pull granite4.1:8b
uv sync
uv run uvicorn app.main:app --reload
```

Open <http://localhost:8000/>. The application expects Ollama on `localhost:11434`.

## How it works

Every location fact the rail displays is either injected through an environment variable,
read live from the Kubernetes API, or derived from the machine itself (hostname, resolved
local address). Nothing is guessed or hard-coded, so the application runs with no
configuration at all and still reports accurately.

Four mechanisms drive the rail.

| Mechanism | Behaviour |
| --- | --- |
| **Instance fingerprint** | A random identifier generated at process start. The browser polls `/whereami` every second; a changed identifier can only mean a different process is answering, which is how a move is detected without reloading the page. |
| **Platform detection** | Inside a cluster, the application queries the Kubernetes API with its own service account and classifies the node as `onprem`, `rosa` or `eks`. Outside a cluster it falls back to environment variables. |
| **Link probe** | Every two seconds the application measures round-trip time to the model and smooths it (EWMA, 0.3 new / 0.7 previous). If the model stops responding the rail turns red and any answer being streamed is terminated with a stated reason rather than left hanging. |
| **Session store** | Conversation state lives either in the process (default) or in a shared store. This determines whether a move loses the conversation or preserves it. |

### Platform detection

`PLATFORM` takes precedence when set. Otherwise the node's labels and `providerID` are
evaluated in this order:

| Evidence | Result |
| --- | --- |
| Label prefixed `eks.amazonaws.com/` | `eks` |
| OpenShift labels with an `aws://` provider | `rosa` |
| OpenShift labels without a cloud provider | `onprem` |
| `aws://` provider alone | `eks` |
| Anything else | `onprem` |

Region and zone are read from the standard `topology.kubernetes.io/` labels. Node, pod and
pod IP come from the Downward API. Results are cached for 15 seconds so that a one-second
poll does not translate into a one-second API call.

Detection requires read access to nodes cluster-wide and to pods within the namespace.
Both are granted by `deploy/k8s/rbac.yaml`.

## Architecture

```
HOST
│
├── Browser ─ http://localhost:8200
│
└── DOCKER
    │
    ├── network "onprem" ── 10.20.0.0/24 ──┐
    │     └── onprem site   10.20.0.x      │   SITE=on-prem-mel
    │                                      │
    ├── network "cloud"  ── 10.30.0.0/24 ──┤
    │     ├── rosa site     10.30.0.x      │   SITE=rosa-syd
    │     └── eks site      10.30.0.x      │   SITE=eks-syd
    │                                      │
    ├── model (Ollama) ── :11434 ──────────┤   model baked into the image,
    │                                      │   also published on localhost:11434
    └── sessions (Redis) ──────────────────┘   optional shared conversation store
```

Exactly one site container runs at a time, because all three publish the same port.
Swapping which one is up constitutes the move. The `model` and `sessions` containers are
attached to both networks and remain in place throughout.

A message travels browser → site container → `model` → Ollama API, and the generated
tokens stream back along the same path. Pausing `model` severs that hop mid-answer.

## Configuration

All variables are optional. Each falls back to something true about the environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SITE` | Title of the detected platform | Name reported as the application's location |
| `PLATFORM` | Detected from the cluster | `onprem`, `rosa` or `eks`; forced when set |
| `REGION` | Node label, otherwise `unknown` | Region label |
| `NODE_NAME` | Machine hostname | Node the application is running on |
| `POD_NAME` | Machine hostname | Pod or container name |
| `POD_IP` | Resolved local address | Leave unset to report the real address |
| `POD_NAMESPACE` | `default` | Namespace used when querying the pod; reported only in-cluster |
| `OLLAMA_URL` | `http://localhost:11434` | Where the model is served from |
| `MODEL_NAME` | `granite4.1:8b` | Model to use |
| `SESSION_STORE` | `memory` | `memory` or `redis`; where conversation state lives |
| `REDIS_URL` | `redis://localhost:6379` | Shared store, used when `SESSION_STORE=redis`. Compose sets `redis://sessions:6379`. |

In Kubernetes, `NODE_NAME`, `POD_NAME`, `POD_IP` and `POD_NAMESPACE` are supplied by the
Downward API. `PLATFORM` also accepts the legacy value `cloud`, which maps to `eks`.

```sh
SITE=rosa-syd PLATFORM=rosa uv run uvicorn app.main:app
```

## Running with Docker Compose

### Moving between sites

```sh
docker compose stop onprem
docker compose up -d eks                        # or: rosa

docker compose pause model                      # sever the model link
docker compose unpause model

docker compose stop eks
docker compose up -d onprem                     # return to on-prem

COMPOSE_PROFILES='*' docker compose down        # tear everything down
```

The browser URL never changes. The container's address and subnet do, and the interval
between the two commands is the outage the rail measures.

Because every site is gated behind a profile, a plain `docker compose down` removes only
services whose profile is active and leaves the running site behind. `COMPOSE_PROFILES='*'`
selects all of them.

`pause` drops packets rather than refusing them, reproducing a severed cable. Stopping the
container instead produces an immediate connection refusal, which exercises a different
path.

## Where the conversation lives

By default each site keeps conversations in its own memory, so a move discards them and
the rail reports `conversation lost`. Pointing the sites at the shared store causes the
same move to report `conversation preserved`, because the new process reads the
conversation the previous one wrote.

`SESSION_STORE` is read from the shell on each `up`, so export it once. Setting it on only
the first command leaves the other site in `memory` mode and silently invalidates the
comparison.

```sh
export SESSION_STORE=redis
docker compose up -d --build onprem
# then the same move commands as above
```

Same move, same outage, same new address. Only the outcome differs. Being stateless to
deploy is not the same as holding no state.

## Deploying to Kubernetes

Manifests live under `deploy/k8s/` and are applied with Kustomize: Namespace, RBAC,
Deployment, LoadBalancer Service, and a second Deployment plus Service for the model.

```sh
export KUBECONFIG=/path/to/kubeconfig
kubectl apply -k deploy/k8s
kubectl -n chatbot rollout status deploy/chatbot
kubectl -n chatbot get svc chatbot
```

The Service requests a fixed address through Cilium IPAM (`lbipam.cilium.io/ips`). Adjust
that annotation for your own load balancer.

Both images are referenced through the `images:` block of `kustomization.yaml`, so the
registry and tag are set in one place:

```sh
kustomize edit set image chatbot-site=ghcr.io/<you>/chatbot-site:v1
```

`imagePullPolicy` is `IfNotPresent`, so a re-pushed `latest` tag is not picked up. Push a
new tag and update `newTag` instead.

See `deploy/k8s/README.md` for image loading onto nodes and further detail on platform
detection.

## Building images

Both Dockerfiles are multi-architecture ready (`linux/amd64` and `linux/arm64`). Model
weights are pulled once on the builder's native architecture and copied into each target,
since the weights themselves are architecture-independent.

```sh
docker buildx create --name multi --use   # once
IMAGE_PREFIX=ghcr.io/<you>/ docker buildx bake --push
```

Without `IMAGE_PREFIX`, bake tags `chatbot-site` and `chatbot-model` locally. A registry
(`--push`) or a containerd image store is still required to retain a multi-platform
manifest.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET`, `HEAD` `/` | The page |
| `GET`, `HEAD` `/healthz` | Liveness and the instance identifier. Reports no topology. |
| `GET /whereami` | Everything the rail displays |
| `POST /chat` | Streams an answer as server-sent events |
| `POST /history` | The stored conversation for a session |
| `GET /static/…` | Vendored browser libraries |

FastAPI additionally serves `/docs`, `/redoc` and `/openapi.json`.

`/history` takes the session identifier in a POST body rather than a URL path
deliberately: there is no authentication, so the identifier is the only thing protecting a
conversation, and a path segment appears in every access log.

`/chat` frames are `data: {...}` lines carrying one of `token`, `error`, or a terminal
`done` receipt. Every failure ends the stream with a frame rather than dropping the
connection: an unreachable model, a model-reported error, a malformed reply, or an
unreachable session store.

## Development

```sh
uv run ruff check app/ tests/    # lint
uv run ruff format app/ tests/   # format
uv run pytest                    # tests
```

The suite covers the HTTP contract, the streaming failure paths, the session store and
platform detection, alongside a set of invariants asserted directly against the page
source: that every element identifier the script looks up exists, that every CSS custom
property used is also defined, that all three theme states remain switchable, that buttons
do not fall back to native browser chrome, and that no code path collapses the rail or
cancels an answer silently.

Two invariants are asserted against configuration rather than code: that no two Compose
sites can claim the published port without a profile, and that the Kubernetes API client
verifies the cluster certificate with a real SSL context.

Most of these tests exist because the corresponding defect occurred.

## Design decisions

**The model is packaged into `chatbot-model`.** `model/Dockerfile` pulls the model during
the image build and copies the cache into each runtime architecture, so the image is
self-contained and requires no host Ollama. Compose serves it on `11434`. On macOS the
container has no GPU access; for a model of this size the difference is negligible.

**The model container exists to be severed.** Pausing it freezes the hop mid-answer in the
same way a pulled cable would, which is the failure the link probe is built to surface.

**Published ports bind to loopback.** The application has no authentication by design and
fronts an unauthenticated model API, so `8200` and `11434` listen only on the local
machine. Change `ports` in `compose.yaml` if either needs to be reachable externally.

**Detection is cached, not live.** The Kubernetes API is queried at most once every 15
seconds regardless of poll rate, and every failure degrades to environment variables
rather than raising.

**The page has no build step.** A single static HTML file with inline CSS and vanilla
JavaScript, no framework and no bundler, so the served artefact is the source.

## Third-party code

Browser libraries under `app/static/vendor/` are vendored rather than loaded from a CDN,
so the page works offline and versions are pinned. Their licences are kept alongside them,
and `VERSIONS.txt` records the versions and how the bundles were built.

| Component | Role |
| --- | --- |
| [FastAPI](https://fastapi.tiangolo.com/), [uvicorn](https://www.uvicorn.org/) | Backend and ASGI server |
| [httpx](https://www.python-httpx.org/) | Streaming client for the model and the Kubernetes API |
| [Ollama](https://ollama.com) | Serves the model |
| [Redis](https://redis.io/) | Optional shared conversation store |
| [marked](https://marked.js.org/), [DOMPurify](https://github.com/cure53/DOMPurify), [highlight.js](https://highlightjs.org/) | Rendering model output safely in the browser |
| [Docker Compose](https://docs.docker.com/compose/), [Kustomize](https://kustomize.io/) | Local sites and cluster manifests |
