-- Current equipment: grinders, machines and baskets, with retired ones kept for history.
select
    equipment_id, user_id, kind, name, model, scale_min, scale_max, scale_step, finer_is_lower, basket_dose_g,
    coalesce(is_default, false) as is_default, coalesce(retired, false) as retired, notes, updated_at
from {{ ref('stg_equipment') }}
