{{ config(materialized='table') }}

-- A transitive consumer of the stable orders_daily output contract.
select
    user_id,
    sum(total_revenue) as lifetime_revenue
from {{ ref('orders_daily') }}
group by user_id
