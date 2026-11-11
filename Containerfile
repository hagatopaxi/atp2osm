# Base images pinned by digest: a tag moves under the same name. Renovate or
# a hand bump updates them — `podman pull` the tag, read its digest.
FROM docker.io/library/python:3.11-slim@sha256:b701198cb708b68d9413ca67d6311bd97c9389a2c5fe4da0511fc652642366cd

RUN apt-get update && apt-get install -y --no-install-recommends \
    osm2pgsql \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:c7db1d63057f268a8b6da8af9077850a60461f98d23db23c676a82089a050c91 /uv /uvx /bin/
# Cron for containers: runs the daily refresh in the image that carries the
# pipeline, so a deployment needs no scheduler on the host.
ADD --chmod=755 --checksum=sha256:a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1 \
    https://github.com/aptible/supercronic/releases/download/v0.2.49/supercronic-linux-amd64 /bin/supercronic

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY osm2pgsql/ osm2pgsql/
COPY src/ src/
COPY website/ website/
COPY static/ static/
COPY migrations/ migrations/
COPY config.schema.json ./

# Catalogs are compiled here: only the .po files are tracked
RUN uv run --no-sync pybabel compile -d website/translations

ARG GIT_COMMIT
LABEL org.opencontainers.image.source=https://github.com/hagatopaxi/atp2osm \
      org.opencontainers.image.revision=${GIT_COMMIT} \
      org.opencontainers.image.licenses=GPL-3.0-only
ENV GIT_COMMIT=${GIT_COMMIT}

ARG PORT=8000
ENV PORT=${PORT}
EXPOSE ${PORT}

# 0.0.0.0 so a published port reaches it (compose). A host-network deploy
# behind a reverse proxy binds 127.0.0.1 instead (deploy/atp2osm.container):
# the proxy is then the only way in, and the X-Forwarded-* headers ProxyFix
# trusts are the proxy's.
ARG BIND_HOST=0.0.0.0
ENV BIND_HOST=${BIND_HOST}

# Not root: uid 1000, which the Quadlets map onto the deploying user.
RUN useradd --uid 1000 --create-home app && mkdir -p .cache data logs && chown -R app:app .cache data logs
USER 1000:1000

CMD ["sh", "-c", "exec uv run --no-sync gunicorn --bind \"${BIND_HOST}:${PORT}\" --workers 2 --timeout 120 src.app:app"]
