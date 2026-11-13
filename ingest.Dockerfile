# The ingestion jobs' image (src/terraform/ingestion.tf): the coffeedata CLI, the dbt project and the registry seed,
# without the app. Built from the repository root by Cloud Build into the app's Artifact Registry repository
# (docs/ingestion/RUNBOOK.md §1: `gcloud builds submit`, with BuildKit so ingest.Dockerfile.dockerignore applies).
# Each Cloud Run job sets its own command (coffeedata crawl / crawl-retry / merge / enrich-week / build).
# uv, as a named stage: Dependabot updates a pinned FROM, not a COPY --from=<image>.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY --from=uv /uv /usr/local/bin/uv

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
# The listing copy whose coverage claims every build checks (quality.LISTING); without it the check is skipped.
COPY --chown=user marketplace/LISTING.md ./marketplace/LISTING.md
# Fail the image build, not every weekly build, if the claims file can't be found (e.g. a non-editable install).
RUN .venv/bin/python -c "from coffeedata import quality; assert quality.LISTING.is_file(), quality.LISTING"

ENTRYPOINT ["coffeedata"]
CMD ["--help"]
