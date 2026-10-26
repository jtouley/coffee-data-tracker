select
    shot_id, cast(brewed_at as timestamptz) as brewed_at, coffee_id, coffee_freetext,
    try_cast(roast_date as date) as roast_date, grinder_setting, dose_g, yield_g, time_s, water_temp_c,
    coalesce(brew_method, 'espresso') as brew_method, rating, taste, coalesce(is_milk_drink, false) as is_milk_drink,
    notes
from read_json(
    '{{ var("data_dir") }}/shots.jsonl',
    format = 'newline_delimited',
    columns = {
        shot_id: 'VARCHAR', brewed_at: 'VARCHAR', coffee_id: 'VARCHAR', coffee_freetext: 'VARCHAR',
        roast_date: 'VARCHAR', grinder_setting: 'DOUBLE', dose_g: 'DOUBLE', yield_g: 'DOUBLE', time_s: 'DOUBLE',
        water_temp_c: 'DOUBLE', brew_method: 'VARCHAR', rating: 'INTEGER', taste: 'VARCHAR',
        is_milk_drink: 'BOOLEAN', notes: 'VARCHAR'
    }
)
