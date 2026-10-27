# The Space's image. The build context is the bundle the `space` workflow assembles: app/, src/, pyproject.toml,
# uv.lock, and this file and README.md at the root.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

# Spaces run as user 1000.
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    COFFEEDATA_LAKE=hf://datasets/jtouley/coffee-data \
    COFFEEDATA_USERS=hf://datasets/jtouley/coffee-data-users \
    COFFEEDATA_PHOTOS=hf://datasets/jtouley/coffee-data-photos \
    COFFEEDATA_STAGING=/home/user/app/build/lake-staging \
    COFFEEDATA_PROXY_HOPS=1

COPY --chown=user pyproject.toml uv.lock README.md ./
COPY --chown=user src ./src
RUN uv sync --locked --no-dev --extra app
COPY --chown=user app ./app

EXPOSE 7860
# One process: accounts and the shot cache live in memory, and usernames are checked under its lock.
# No --proxy-headers: the app reads the phone's address for its sign-in limits from the X-Forwarded-For entry the
# Space's proxy appended (COFFEEDATA_PROXY_HOPS from the right), never from what the client sent.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--no-proxy-headers"]
