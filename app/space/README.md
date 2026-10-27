---
title: registry.coffee
emoji: ☕
colorFrom: yellow
colorTo: red
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Log espresso shots and see how each bag dials in.
---

# registry.coffee

The shot-logging app from [jtouley/coffee-data-tracker](https://github.com/jtouley/coffee-data-tracker). This Space
is built from that repo's `app/` by its `space` workflow; nothing here is edited by hand.

Open it at its own address (`https://<owner>-<space>.hf.space`), not inside the Hugging Face page: the app refuses
to be framed, and its sign-in cookie isn't sent inside another site's frame.

## Settings

Secrets (Space settings → Variables and secrets → New secret):

| Secret | What it is |
|---|---|
| `HF_TOKEN` | A token that can write the lake, the users dataset and the photo dataset |
| `ANTHROPIC_API_KEY` | For reading bag labels. Without it, New bag offers "Type it instead" only |
| `AUTH_PEPPER` | A random string, 16+ characters, mixed into every password hash. Never change it: every password stops working |
| `SESSION_SECRET` | A random string, 32+ characters, that signs the sign-in cookie. Changing it signs everyone out |
| `OWNER_SETUP_CODE` | Typed once to create the owner's account. Does nothing after that |

`python -c "import secrets; print(secrets.token_urlsafe(32))"` makes a good random string.

Variables (not secret), set in the Dockerfile and overridable in settings: `COFFEEDATA_LAKE`, `COFFEEDATA_USERS`,
`COFFEEDATA_PHOTOS`, `COFFEEDATA_OWNER_ID`.
