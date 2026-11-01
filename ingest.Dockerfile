# The ingestion jobs' image (src/terraform/ingestion.tf): the coffeedata CLI, the dbt project and the registry seed,
# without the app. Build from the repository root and push to the app's Artifact Registry repository:
#   docker build -f ingest.Dockerfile -t "$IMAGE" . && docker push "$IMAGE"
# Each Cloud Run job sets its own command (coffeedata crawl / crawl-retry / merge / enrich-week / build).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/ingest
ENV HOME=/home/user \
    PATH=/home/user/ingest/.venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    COFFEEDATA_LAKE=hf://datasets/jtouley/coffee-data

COPY --chown=user pyproject.toml uv.lock README.md ./
COPY --chown=user src ./src
RUN uv sync --locked --no-dev
RUN python -c "import duckdb; duckdb.sql('INSTALL httpfs')"
COPY --chown=user config ./config
COPY --chown=user dbt ./dbt
COPY --chown=user contracts ./contracts

ENTRYPOINT ["coffeedata"]
CMD ["--help"]
