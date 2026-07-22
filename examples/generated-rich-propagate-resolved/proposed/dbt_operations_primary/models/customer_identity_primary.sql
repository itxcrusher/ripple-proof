{{ config(materialized='table', alias='customer_identity_export') }}

select
    customer_id,
    'primary' as destination
from {{ ref('orders_daily') }}
where customer_id is not null
