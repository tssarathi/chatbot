# Two stages so the runtime image carries the app and its dependencies, but not
# uv and not a build toolchain. Image size is part of the pitch here: the app is
# what moves between sites, so it has to reschedule in seconds.
#
# The lock file governs versions, but the dependencies are installed against the
# runtime image's OWN interpreter rather than by copying a virtualenv across
# stages. A uv venv's bin/python is an absolute symlink to whichever interpreter
# built it, so a copied venv silently points at a path the runtime image does not
# have. Exporting to a hash-pinned requirements file avoids that entirely.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS deps
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project \
        --format requirements-txt -o /requirements.txt

FROM python:3.12-slim-bookworm
WORKDIR /app

# dependency layer first: editing app/ must not reinstall the world
COPY --from=deps /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run as a non-root user; nothing here needs to write to disk.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# POD_IP is deliberately NOT set: app/main.py resolves the container's own
# address, so the panel shows an IP that is genuinely routable inside the demo
# rather than one typed into a compose file.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
