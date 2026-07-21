{{ config(materialized='table') }}

-- Aggregate orders for the revenue dashboard.
select
    customer_id as user_id,
    date(created_at) as order_date,
    sum(order_total) as total_revenue,
    'user_id' as source_key_label
from {{ source('analytics', 'orders') }}
where customer_id is not null
group by customer_id, date(created_at)
