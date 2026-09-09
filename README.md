# chatbot

A small chat app that reports where it is running, and keeps reporting it while it
moves. The chat itself is deliberately plain; the panel in the top right is the
point — it shows the site, node, pod, address and model link, and updates live.

## Requirements

- [uv](https://docs.astral.sh/uv/) — uv installs Python 3.12 itself if you don't have it
- [Ollama](https://ollama.com) running locally, with the model pulled:

  ```
  ollama pull granite3.1-moe:1b
  ```

## Run

```
uv sync
uv run uvicorn app.main:app --reload
```

Then open <http://localhost:8000/>.

## Configuration

Every location fact is injected, never inferred. All are optional — each falls back
to something true about the machine, so the app runs bare with no configuration.

| Env var       | Default                    | Purpose                                   |
| ------------- | -------------------------- | ----------------------------------------- |
| `SITE`        | machine hostname           | Name reported as the app's location       |
| `PLATFORM`    | `unknown`                  | `on-prem` / `cloud` — drives the colour    |
| `REGION`      | `unknown`                  | Region label                              |
| `NODE_NAME`   | machine hostname           | Node the app is running on                |
| `POD_NAME`    | machine hostname           | Pod/container name                        |
| `POD_IP`      | resolved local address     | Leave unset to report the real address    |
| `OLLAMA_URL`  | `http://localhost:11434`   | Where the model is served from            |
| `MODEL_NAME`  | `granite3.1-moe:1b`        | Model to use                              |

In Kubernetes, `NODE_NAME`, `POD_NAME` and `POD_IP` come from the Downward API.

```
SITE=cloud-syd PLATFORM=cloud uv run uvicorn app.main:app
```

## Demo runbook

Two sites, one published port, one model. Only one site runs at a time — swapping
which one **is** the move. The browser URL never changes; the container's address
and subnet do, and the gap between the two commands is the outage the panel measures.

Ollama stays on the host because it is already installed and running there, not for
speed — a containerised one gets no GPU on macOS, but a model this small barely
notices. The `modellink` relay exists so the model link is a real network hop that
can be cut.

```
docker compose --profile onprem up -d --build      # start on-prem
open http://localhost:8200/                        # leave this open throughout

docker compose stop onprem                         # move to cloud
docker compose --profile cloud up -d               #   panel flips, transcript clears

docker compose pause modellink                     # cut the model link
docker compose unpause modellink                   # restore it

docker compose stop cloud                          # move back
docker compose --profile onprem up -d
```

`pause` is deliberate: it drops packets rather than refusing them, which is what a
pulled cable does. Killing the relay instead gives an instant connection-refused
and exercises a different path.

## Development

```
uv run ruff check app/     # lint
uv run ruff format app/    # format
uv run pytest              # tests
```
