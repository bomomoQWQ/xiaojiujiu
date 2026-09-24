# syntax=docker/dockerfile:1
#
# 小九九 Runtime sidecar.
#
# This image contains the persistent cognition layer and nothing else: no chat
# platform, no model weights, no upstream AstrBot checkout. The AstrBot host runs
# in its own container (see docker-compose.yml) and installs the thin plugin from
# `astrbot_plugin_companion_runtime/`.
#
# The image runs unprivileged, keeps all state in the /data volume, and listens on
# 8787 inside the container network. Publish that port to loopback only.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Storage defaults to the mounted volume, so a container without extra flags
# already keeps its database and raw-event mirror somewhere durable.
ENV CR_STORAGE__DATABASE_PATH=/data/companion.sqlite3 \
    CR_STORAGE__RAW_LOG_PATH=/data/raw_events.jsonl \
    CR_SERVER__HOST=0.0.0.0 \
    CR_SERVER__PORT=8787

WORKDIR /app/runtime

# Only the package is needed: tests, docs and the plugin stay out of the image.
COPY runtime/pyproject.toml runtime/README.md /app/runtime/
COPY runtime/src /app/runtime/src

RUN pip install /app/runtime

RUN useradd --create-home --uid 10001 companion \
    && mkdir -p /data \
    && chown -R companion:companion /data

USER companion

VOLUME ["/data"]
EXPOSE 8787

# `--base-dir` resolves any relative storage path against the volume; the absolute
# defaults above are what a plain `docker run` actually uses.
ENTRYPOINT ["companion-runtime", "--base-dir", "/data"]
CMD ["serve"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status == 200 else 1)"]
