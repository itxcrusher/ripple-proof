{{ config(materialized='table', alias='customer_identity_export') }}

select
    cast('shadow-only' as varchar) as user_id,
    'shadow' as destination
