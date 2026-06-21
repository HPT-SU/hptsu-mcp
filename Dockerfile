# syntax=docker/dockerfile:1.7

# ───── Builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build wheel from source — keeps final image small (no source/.git).
COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src

RUN pip install --no-cache-dir build && \
    python -m build --wheel --outdir /wheels


# ───── Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HPTSU_TRANSPORT=streamable-http \
    HPTSU_LOG_LEVEL=INFO

# Non-root user — never run MCP server as root.
RUN groupadd --system --gid 1000 hptsu && \
    useradd --system --uid 1000 --gid 1000 --home /home/hptsu --shell /usr/sbin/nologin hptsu && \
    mkdir -p /home/hptsu && chown -R hptsu:hptsu /home/hptsu

WORKDIR /app

COPY --from=builder /wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels

USER hptsu

# FastMCP streamable-http listens on 0.0.0.0:8000 by default.
EXPOSE 8000

# Healthcheck hits the /healthz route (always 200 when process is up).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["hptsu-mcp"]
