{{ config(materialized='table') }}

-- A transitive consumer of the stable orders_daily output contract.
select
    customer_id,
    sum(total_revenue) as lifetime_revenue
from {{ ref('orders_daily') }}
group by customer_id
