{{ config(materialized='table', alias='customer_identity_export') }}

select
    user_id,
    'primary' as destination
from {{ ref('orders_daily') }}
where user_id is not null
