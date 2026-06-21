FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src

RUN pip install --no-cache-dir .

# stdio is the default; override with HPTSU_TRANSPORT=streamable-http for HTTP mode.
ENV HPTSU_TRANSPORT=stdio

ENTRYPOINT ["hptsu-mcp"]
