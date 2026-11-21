# The ingestion jobs' image (src/terraform/ingestion.tf): the coffeedata CLI, the dbt project and the registry seed,
# without the app. Built from the repository root by Cloud Build into the app's Artifact Registry repository
# (docs/ingestion/RUNBOOK.md §1: `gcloud builds submit`, with BuildKit so ingest.Dockerfile.dockerignore applies).
# Each Cloud Run job sets its own command (coffeedata crawl / crawl-retry / merge / enrich-week / build).
# Since #153 it is built, pushed and signed by deploy.yml's ingest-image job; Cloud Build is the emergency route.
# uv, as a named stage: Dependabot updates a pinned FROM, not a COPY --from=<image>.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY --from=uv /uv /usr/local/bin/uv

RUN useradd -m -u 1000 user
WORKDIR /opt/ingest
ENV HOME=/home/user \
    HF_HOME=/home/user/.cache/huggingface \
    PATH=/opt/ingest/.venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    COFFEEDATA_LAKE=hf://datasets/jtouley/coffee-data

# The code, its environment and the dbt project live in /opt/ingest, copied and installed as root, so the job's user
# can read but not change, rename or replace them (review 2026-11-21, F9). The user owns only what the jobs write:
# build/ (the databases, the site, the exports), dbt/target and dbt/logs (dbt's artifacts) and .tmp (DuckDB's spill
# folder for in-memory queries), and its own home, where Hugging Face keeps its cache (HF_HOME). The staging bucket
# is mounted at /mnt/staging. uv runs without a cache, so the build leaves nothing of root's in that home except
# DuckDB's extension folder (~/.duckdb).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-cache
# DuckDB's extensions, root's and installed now: httpfs for hf://, and aws for an export to s3://
# (public._configure_object_store), which can't install one at runtime into root's ~/.duckdb.
RUN python -c "import duckdb; duckdb.sql('INSTALL httpfs'); duckdb.sql('INSTALL aws')"
COPY config ./config
COPY dbt ./dbt
COPY contracts ./contracts
# The listing copy whose coverage claims every build checks (quality.LISTING); without it the check is skipped.
COPY marketplace/LISTING.md ./marketplace/LISTING.md
# Fail the image build, not every weekly build, if the claims file can't be found (e.g. a non-editable install).
RUN .venv/bin/python -c "from coffeedata import quality; assert quality.LISTING.is_file(), quality.LISTING"
RUN python -m compileall -q src && mkdir -p build dbt/target dbt/logs .tmp \
    && chown user:user build dbt/target dbt/logs .tmp
USER user

ENTRYPOINT ["coffeedata"]
CMD ["--help"]
