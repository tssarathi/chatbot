# chatbot

A small chat app that reports where it is running.

## Requirements

- [uv](https://docs.astral.sh/uv/) — uv installs Python 3.12 itself if you don't have it

## Run

```
uv sync
uv run uvicorn app.main:app --reload
```

Then open <http://localhost:8000/whereami>.

## Configuration

| Env var | Default        | Purpose                                   |
| ------- | -------------- | ----------------------------------------- |
| `SITE`  | machine hostname | Name reported as the app's location      |

```
SITE=syd uv run uvicorn app.main:app
```

## Development

```
uv run ruff check app/     # lint
uv run ruff format app/    # format
uv run pytest              # tests
```
