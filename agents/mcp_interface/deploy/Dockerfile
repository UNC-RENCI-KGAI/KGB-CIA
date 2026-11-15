# Build from agents/mcp_interface:
# docker build -f deploy/Dockerfile -t kgbcia-mcp:dev .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 kgbcia
USER kgbcia

EXPOSE 8000
ENTRYPOINT ["kgbcia-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
