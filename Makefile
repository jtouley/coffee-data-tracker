# Coffee Data. The lake is $COFFEEDATA_LAKE (default ./lake); set it to hf://datasets/<you>/<name> to publish.
SAMPLE ?=

.PHONY: install init weekly ingest probe discover enrich build site publish mcp lint test clean

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

lint:
	uv run ruff check src tests
	uv run mypy

test: lint  ## offline: unit tests + dbt builds over synthetic lakes
	uv run pytest -q

clean:
	rm -rf build dbt/target dbt/logs
