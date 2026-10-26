# Coffee Data — `make all` runs the whole daily pipeline locally.
DBT := uv run dbt
DBT_ARGS := --project-dir dbt --profiles-dir dbt
ROASTER ?=
SAMPLE ?=

.PHONY: install ingest collect build site all test lint probe discover mcp clean

install:
	uv sync

ingest:  ## make ingest [ROASTER=slug] [SAMPLE=100]
	uv run coffeedata ingest $(if $(ROASTER),--only $(ROASTER)) $(if $(SAMPLE),--sample $(SAMPLE))

collect:  ## merge finished Claude enrichment batches (needs ANTHROPIC_API_KEY)
	uv run coffeedata collect-batches

build:
	mkdir -p build
	$(DBT) build $(DBT_ARGS)

site: build
	uv run coffeedata site

all: collect ingest site

probe:  ## re-detect storefront platforms for roasters not probed in 7 days
	uv run coffeedata probe --stale-days 7

discover:  ## pull new roasters from Overture Maps and OpenStreetMap
	uv run coffeedata discover overture
	uv run coffeedata discover osm

mcp:
	uv run coffeedata mcp

lint:
	uv run ruff check src tests

test: lint  ## offline: unit tests + dbt build on recorded fixtures
	uv run pytest -q

clean:
	rm -rf build dbt/target dbt/logs
