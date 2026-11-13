# Coffee Data. The lake is $COFFEEDATA_LAKE (default ./lake); set it to hf://datasets/<you>/<name> to publish.
SAMPLE ?=
# Claude's verdicts are required for a release: `coffeedata classify`, `build` and `weekly` fail without
# COFFEEDATA_VERDICT_MODEL and an Anthropic key. A local lake is for development, so make opts out explicitly
# (COFFEEDATA_ALLOW_RULES_ONLY=1: classify skips, the build's verdict gate is off); a remote lake (hf://, s3://)
# never does. Override with `make build COFFEEDATA_ALLOW_RULES_ONLY=`.
ifeq ($(findstring ://,$(COFFEEDATA_LAKE)),)
export COFFEEDATA_ALLOW_RULES_ONLY ?= 1
endif

.PHONY: install hooks fmt init weekly ingest probe discover enrich build site publish mcp app admin contracts lint test smoke clean

install:
	uv sync

init:  ## create the lake (and publish its schema files to a remote lake); make init PUBLIC=1 for a public dataset
	uv run coffeedata init $(if $(PUBLIC),--public)

weekly:  ## the full weekly run: discover (1st week), probe, ingest, enrich, build, publish, site
	uv run coffeedata weekly $(if $(SAMPLE),--sample $(SAMPLE))

ingest:  ## make ingest [SAMPLE=50]
	uv run coffeedata ingest $(if $(SAMPLE),--sample $(SAMPLE))

probe:
	uv run coffeedata probe

discover:
	uv run coffeedata discover overture
	uv run coffeedata discover osm

enrich:  ## Claude enrichment (needs ANTHROPIC_API_KEY)
	uv run coffeedata enrich

build:  ## dbt build from the lake + export marts
	uv run coffeedata build

site: build
	uv run coffeedata site

publish:
	uv run coffeedata publish

mcp:
	uv run coffeedata mcp

app:  ## the shot-logging app on http://localhost:8000, over the local lake (signed in as the owner unless SESSION_SECRET is set)
	COFFEEDATA_LOCAL_OWNER=1 uv run uvicorn app.main:app --reload --host $${HOST:-127.0.0.1} --port $${PORT:-8000}

admin:  ## the admin on http://localhost:8001 over the local lake, read-only; set ADMIN_SESSION_SECRET and ADMIN_ENROLL_TOKEN first (docs/admin/README.md)
	ADMIN_ORIGIN=$${ADMIN_ORIGIN:-http://localhost:8001} ADMIN_STATE_DIR=$${ADMIN_STATE_DIR:-build/admin-state} \
		uv run uvicorn --factory admin.main:create_app --host 127.0.0.1 --port 8001

hooks:  ## install the git pre-commit hooks
	uv run pre-commit install

fmt:
	uv run ruff check --fix src app admin tests
	uv run ruff format src app admin tests

contracts:  ## regenerate the internal ODCS contracts (contracts/internal/) from lake.SCHEMAS and contract_docs.py
	uv run python -m coffeedata.raw_contracts --all

lint:  ## the same checks as the pre-commit hooks, on every file
	uv run pre-commit run --all-files

test: lint  ## offline: unit tests + dbt builds over synthetic lakes, one pytest-xdist worker per CPU; `make smoke` runs the rest
	uv run pytest -q -n auto -m "not smoke"

smoke:  ## the fast smoke suite CI requires on every PR: dbt, ingestion and web over the recorded fixtures, no network
	uv run pytest -q -m smoke $(if $(PART),tests/test_smoke_$(PART).py)

clean:
	rm -rf build dbt/target dbt/logs
