-- Fill-rate floors on fields whose fill follows the week's mix of lots more than whether the rules still match:
-- Royal's SPOT status (spot against forward lots), arrival dates (forward lots with an ETA) and certifications. They
-- warn instead of failing the build (importer_fill_floors has the stable ones, which fail). Set as those are, a
-- little below the week of 2026-11-09, and only raised.
{{ config(severity='warn') }}
{{ fill_floor_failures([
        ['royal_coffee', 'status', 0.88],
        ['royal_coffee', 'est_arrival', 0.01],
        ['royal_coffee', 'certifications', 0.35]
]) }}
