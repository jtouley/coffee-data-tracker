# Coffee Data. The lake is $COFFEEDATA_LAKE (default ./lake); set it to hf://datasets/<you>/<name> to publish.
SAMPLE ?=

.PHONY: install hooks fmt init weekly ingest probe discover enrich build site publish mcp lint test clean

install:
	uv sync

init:  ## create the lake (and publish its schema files to a remote lake)
	uv run coffeedata init

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

hooks:  ## install the git pre-commit hooks
	uv run pre-commit install

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

lint:  ## the same checks as the pre-commit hooks, on every file
	uv run pre-commit run --all-files

test: lint  ## offline: unit tests + dbt builds over synthetic lakes
	uv run pytest -q

clean:
	rm -rf build dbt/target dbt/logs
