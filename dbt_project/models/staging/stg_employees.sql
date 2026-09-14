{{ config(materialized='view') }}

with source_data as (
    select
        name as employee_name,
        department,
        salary,
        hire_date,
        bonus_estimate
    from {{ source('lakehouse', 'silver_employees') }}
)

select * from source_data
