#!/bin/bash
# Build one tree's dbt models (no tests) against EVAL_DIR/lake (a copy of the lake), with that tree's own coffeedata package.
set -e
E=${EVAL_DIR:?set EVAL_DIR}
T=$E/trees/$1
cd $T
export PYTHONPATH=$T/src COFFEEDATA_DB=$T/coffee.duckdb
rm -f $T/coffee.duckdb
VARS=$(python -c "
import json
from coffeedata import registry, shots, verdict
print(json.dumps({'lake_roots': ['$E/lake'], 'max_failures': registry.MAX_FAILURES, 'owner_user_id': shots.owner_user_id(),
                  'verdict_model': verdict.model() or '', 'verdict_definition': verdict.DEFINITION_VERSION}))")
dbt build --exclude resource_type:test --project-dir dbt --profiles-dir dbt --target-path $T/target --log-path $T/logs --vars "$VARS" > $T/build.log 2>&1 || true
tail -3 $T/build.log
