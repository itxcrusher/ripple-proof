{{ config(materialized='table') }}

select
    customer_id,
    case
        when total_revenue >= 100 then 'high-value'
        else 'standard'
    end as segment
from {{ ref('orders_daily') }}
where customer_id is not null
