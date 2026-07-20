{{ config(materialized='table') }}

-- Aggregate orders for the revenue dashboard.
select
    user_id,
    date(created_at) as order_date,
    sum(order_total) as total_revenue,
    'user_id' as source_key_label
from {{ source('analytics', 'orders') }}
where user_id is not null
group by user_id, date(created_at)
