# The app's image, for any container host (Cloud Run today). Build from the repository root:
#   docker build -f app/Dockerfile .
# Nothing in it is specific to one host: the port comes from $PORT, secrets from the environment.
# uv, as a named stage: Dependabot updates a pinned FROM, not a COPY --from=<image>.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY --from=uv /uv /usr/local/bin/uv

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    COFFEEDATA_LAKE=hf://datasets/jtouley/coffee-data \
    COFFEEDATA_USERS=hf://datasets/jtouley/coffee-data-users \
    COFFEEDATA_PHOTOS=hf://datasets/jtouley/coffee-data-photos \
    COFFEEDATA_STAGING=/home/user/app/build/lake-staging \
    COFFEEDATA_PROXY_HOPS=1

COPY --chown=user pyproject.toml uv.lock README.md ./
COPY --chown=user src ./src
RUN uv sync --locked --no-dev --extra app
# DuckDB's httpfs extension (reading the lake over hf://), installed now rather than downloaded on every cold start.
RUN python -c "import duckdb; duckdb.sql('INSTALL httpfs')"
COPY --chown=user app ./app

EXPOSE 8080
# One process: accounts, the shot cache and the sign-in limits live in memory (so the host runs one instance).
# No --proxy-headers: the app takes the phone's address for its sign-in limits from the X-Forwarded-For entry the
# host's front end appended (COFFEEDATA_PROXY_HOPS from the right), never from what the client sent.
# COFFEEDATA_APP picks the module: the app (app.main) unless the service sets it, as the usage collector does
# (app.usage, src/terraform/usage.tf).
CMD ["sh", "-c", "exec uvicorn \"${COFFEEDATA_APP:-app.main}:app\" --host 0.0.0.0 --port \"$PORT\" --no-proxy-headers"]
