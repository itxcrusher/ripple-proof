{{ config(materialized='table') }}

select
    user_id,
    case
        when total_revenue >= 100 then 'high-value'
        else 'standard'
    end as segment
from {{ ref('orders_daily') }}
where user_id is not null
