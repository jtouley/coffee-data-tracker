# Running the app

The shot-logging app in `app/`: how to run it locally, how sign-in works, and how it's deployed. The plan and
design are in `PLAN.md` and `DESIGN.md` (branch `app-plan`).

## Locally

```bash
make app                     # http://127.0.0.1:8000 over ./lake, signed in as the owner, no password
HOST=0.0.0.0 make app        # reachable from a phone on the same network
```

Without `SESSION_SECRET` the app uses a stub that treats every request as the owner (`COFFEEDATA_OWNER_ID`,
default `owner`). Anyone who can reach it can log shots, so keep it on your own network. The stub refuses to start on
a Hugging Face Space.

To try real sign-in locally, with accounts in `./lake-users`:

```bash
SESSION_SECRET=$(openssl rand -hex 32) AUTH_PEPPER=$(openssl rand -hex 16) OWNER_SETUP_CODE=pick-one \
  COFFEEDATA_INSECURE_COOKIES=1 make app
```

`COFFEEDATA_INSECURE_COOKIES=1` drops the cookie's `Secure` flag, which plain http on a LAN address needs. Chrome
accepts Secure cookies on `localhost` without it.

## Sign-in

- **Accounts** live in their own private dataset, `COFFEEDATA_USERS` (`users/` and `invites/`, one Parquet file
  per change, latest wins). dbt, the MCP server and the weekly run never read it.
- **Passwords** are HMAC-ed with `AUTH_PEPPER`, then hashed with Argon2id (64 MiB, 3 passes, 4 lanes). They need
  at least 10 characters, and common passwords and the username are refused.
- **The owner** is the first account. Creating it needs `OWNER_SETUP_CODE`; once any account exists, the code
  does nothing. The owner keeps the id that shots from before accounts belong to.
- **Sessions** are a signed cookie (`SESSION_SECRET`): HttpOnly, Secure, SameSite=Lax, 90 days, renewed after a
  day of use. "Sign out on all devices" and a password change sign out every other phone.
- **Lockouts:** five failed sign-ins for a username, or twenty from one address, lock it for 15 minutes.
- **Invites and reset codes** come in milestone 3. Until then the owner is the only account.

Every read and write is scoped to the user in the cookie, never to a `user_id` in the request.

## On a Hugging Face Space

`app/space/` holds the Space's `Dockerfile` and `README.md`, the latter with the Space metadata.
`.github/workflows/space.yml` assembles `app/`, `src/`, `pyproject.toml` and `uv.lock` with those two files at the
root, and uploads them to the Space (`SPACE_ID` variable, default `jtouley/coffee-data-app`) on every push to main
that touches them. The workflow uses the repo's existing `HF_TOKEN` secret.

Docker Spaces need a Hugging Face PRO account, even on the free CPU hardware.

### Secrets

Set these in the Space's settings (Variables and secrets → New secret):

| Secret | What it is |
|---|---|
| `HF_TOKEN` | Writes the lake, the users dataset and the photo dataset |
| `ANTHROPIC_API_KEY` | Reads bag labels. Without it, New bag offers "Type it instead" only |
| `AUTH_PEPPER` | 16+ random characters mixed into every password hash. Changing it breaks every password |
| `SESSION_SECRET` | 32+ random characters that sign the cookie. Changing it signs everyone out |
| `OWNER_SETUP_CODE` | Typed once to create the owner's account; after that it only resets the owner's password. Optional once the owner exists |

`python -c "import secrets; print(secrets.token_urlsafe(32))"` makes a good random string. Until `AUTH_PEPPER` and
`SESSION_SECRET` are set, every page returns 503 and names the missing secrets, never their values.

Sign-in limits key on the phone's address. On the Space it's taken from the `X-Forwarded-For` entry the Space's
proxy appends (`COFFEEDATA_PROXY_HOPS=1`, set in the Dockerfile), never from what the client sent; uvicorn runs
without `--proxy-headers`.

The dataset locations are set in the Dockerfile and can be overridden as Space variables:

- `COFFEEDATA_LAKE`: `hf://datasets/jtouley/coffee-data`
- `COFFEEDATA_USERS`: `hf://datasets/jtouley/coffee-data-users`
- `COFFEEDATA_PHOTOS`: `hf://datasets/jtouley/coffee-data-photos`

Open the app at its own address, `https://<owner>-<space>.hf.space`, not inside the Hugging Face page. The app
refuses to be framed, and its cookie isn't sent inside another site's frame.

### Starting up

The Space loads shots from the weekly `marts/fct_shot.parquet` export, then reads only the shot files written after
it: one directory listing instead of one request per shot file. Without an export it reads every shot file.
