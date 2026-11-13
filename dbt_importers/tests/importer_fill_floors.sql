-- Each importer's fill-rate floors on its latest stored week: the share of lots with a value, per field, may not fall
-- below the floor. Set a little below what the rules reach on the week of 2026-11-09 (eval/importers/README.md has
-- the numbers), so a page or listing that changes shape, or a rule that stops matching, fails the build instead of
-- quietly nulling a field. Floors are only ever raised. A week with fewer than 100 of an importer's lots isn't a real
-- page (the tests' hand-made weeks hold a few), so its floors don't apply. Fields whose fill follows the week's mix of
-- lots rather than the rules (Royal's SPOT status, arrival dates and certifications) only warn
-- (importer_fill_floors_by_mix). Returns a row, and fails, for each field below its floor.
{{ fill_floor_failures([
        ['cafe_imports', 'lot_id', 0.95],
        ['cafe_imports', 'origin', 0.95],
        ['cafe_imports', 'region', 0.60],
        ['cafe_imports', 'process', 0.95],
        ['cafe_imports', 'process_type', 0.85],
        ['cafe_imports', 'status', 0.95],
        ['cafe_imports', 'is_available', 0.95],
        ['cafe_imports', 'bags', 0.95],
        ['cafe_imports', 'bag_kg', 0.95],
        ['interamerican', 'origin', 0.90],
        ['interamerican', 'region', 0.25],
        ['interamerican', 'process', 0.25],
        ['interamerican', 'process_type', 0.15],
        ['interamerican', 'status', 0.95],
        ['interamerican', 'is_available', 0.95],
        ['interamerican', 'warehouse', 0.95],
        ['interamerican', 'bags', 0.95],
        ['interamerican', 'bag_kg', 0.95],
        ['royal_coffee', 'lot_id', 0.97],
        ['royal_coffee', 'origin', 0.97],
        ['royal_coffee', 'process', 0.95],
        ['royal_coffee', 'process_type', 0.85],
        ['royal_coffee', 'is_available', 0.97],
        ['royal_coffee', 'warehouse', 0.97],
        ['royal_coffee', 'bag_size', 0.50],
        ['royal_coffee', 'bag_kg', 0.97]
]) }}
