# syntax=docker/dockerfile:1

# Pull the model on the builder's native arch (fast on Apple Silicon).
# Model weights under /root/.ollama are arch-independent and copy into each target.
FROM --platform=$BUILDPLATFORM ollama/ollama:latest AS pull

ARG MODEL_NAME=granite4.1:8b

RUN ollama serve & \
    pid=$! && \
    i=0 && \
    until ollama list >/dev/null 2>&1; do \
      i=$((i + 1)); \
      if [ "$i" -gt 60 ]; then echo "ollama did not start" >&2; exit 1; fi; \
      sleep 1; \
    done && \
    ollama pull "$MODEL_NAME" && \
    kill "$pid" && \
    wait "$pid" || true

# Runtime follows TARGETPLATFORM so the image is multi-arch capable.
FROM ollama/ollama:latest

COPY --from=pull /root/.ollama /root/.ollama

ENV OLLAMA_HOST=0.0.0.0:11434

EXPOSE 11434

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
  CMD ollama list || exit 1

CMD ["serve"]
