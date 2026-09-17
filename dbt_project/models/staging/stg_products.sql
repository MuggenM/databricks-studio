{{ config(materialized='view') }}

with source_data as (
    select
        product_id,
        product_name,
        category,
        price,
        stock_qty
    from {{ source('lakehouse', 'dim_products') }}
)

select
    product_id,
    product_name,
    category,
    price,
    stock_qty,
    round(price * stock_qty, 2) as inventory_value,
    case
        when stock_qty <= 25 then 'CRITICAL'
        when stock_qty <= 50 then 'LOW'
        else 'HEALTHY'
    end as stock_status
from source_data
