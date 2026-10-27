-- dim_coffee leaves out variant prices outside $0.50-$150 per 100 g (misread weights or prices).
select coffee_id, price_per_100g from {{ ref('dim_coffee') }} where price_per_100g not between 0.5 and 150
