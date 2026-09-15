FROM docker.io/library/python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    osm2pgsql \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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

CMD uv run --no-sync gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers 2 \
    --timeout 120 \
    "src.app:app"
