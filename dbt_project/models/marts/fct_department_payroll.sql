{{ config(materialized='table') }}

with employees as (
    select * from {{ ref('stg_employees') }}
),

dept_metrics as (
    select
        department,
        count(employee_name) as total_headcount,
        round(sum(salary), 2) as total_payroll,
        round(avg(salary), 2) as avg_salary,
        min(salary) as min_salary,
        max(salary) as max_salary
    from employees
    group by department
)

select
    department,
    total_headcount,
    total_payroll,
    avg_salary,
    min_salary,
    max_salary,
    current_timestamp as refreshed_at
from dept_metrics
order by total_payroll desc
