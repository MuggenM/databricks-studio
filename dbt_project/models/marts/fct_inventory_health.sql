{{ config(materialized='table') }}

with products as (
    select * from {{ ref('stg_products') }}
),

summary_by_category as (
    select
        category,
        count(product_id) as total_products,
        sum(stock_qty) as total_units_in_stock,
        round(sum(inventory_value), 2) as total_inventory_valuation,
        count(case when stock_status = 'CRITICAL' then 1 end) as critical_reorder_count,
        count(case when stock_status = 'LOW' then 1 end) as low_stock_count
    from products
    group by category
)

select
    category,
    total_products,
    total_units_in_stock,
    total_inventory_valuation,
    critical_reorder_count,
    low_stock_count,
    current_timestamp as refreshed_at
from summary_by_category
order by total_inventory_valuation desc
