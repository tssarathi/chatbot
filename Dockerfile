# syntax=docker/dockerfile:1

# Export deps on the builder's native arch; the lockfile is arch-independent.
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS deps
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project \
        --format requirements-txt -o /requirements.txt

# Runtime image follows TARGETPLATFORM (amd64 / arm64).
FROM python:3.12-slim-bookworm
WORKDIR /app

COPY --from=deps /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
