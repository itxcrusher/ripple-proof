"""Create a local DataHub sample graph for exercising BlastRadius.

The graph includes a one-hop dbt compatibility boundary, a transitive dbt
consumer in another repository, and a dashboard that needs manual review. It
is safe to create repeatedly.
"""

from datahub.metadata.schema_classes import SchemaFieldClass, SchemaFieldDataTypeClass, StringTypeClass, NumberTypeClass
from datahub.sdk.dashboard import Dashboard
from datahub.sdk.dataset import Dataset
from datahub.sdk.main_client import DataHubClient


def field(name: str, type_class, description: str) -> SchemaFieldClass:
    return SchemaFieldClass(
        fieldPath=name,
        type=SchemaFieldDataTypeClass(type=type_class()),
        nativeDataType=type_class.__name__,
        description=description,
    )


def main() -> None:
    client = DataHubClient.from_env()

    orders = Dataset(
        platform="postgres",
        name="analytics.orders",
        description="Raw orders table.",
        schema=[
            field("user_id", StringTypeClass, "The customer's user id. CANDIDATE FOR RENAME to customer_id."),
            field("order_total", NumberTypeClass, "Order total in cents."),
            field("created_at", StringTypeClass, "Order timestamp."),
        ],
    )
    client.entities.upsert(orders)

    orders_daily = Dataset(
        platform="dbt",
        name="orders_daily",
        description="dbt model: daily rollup of orders, keyed by user_id.",
        schema=[
            field("user_id", StringTypeClass, "Rolled up from analytics.orders.user_id."),
            field("total_revenue", NumberTypeClass, "Sum of order_total per user per day."),
        ],
    )
    client.entities.upsert(orders_daily)

    customer_revenue = Dataset(
        platform="dbt",
        name="customer_revenue",
        description=(
            "dbt model: customer revenue derived from the stable orders_daily contract."
        ),
        schema=[
            field("user_id", StringTypeClass, "Inherited from orders_daily.user_id."),
            field("lifetime_revenue", NumberTypeClass, "Revenue summed per user."),
        ],
    )
    client.entities.upsert(customer_revenue)

    revenue_dashboard = Dashboard(
        platform="looker",
        name="revenue_dashboard",
        display_name="Revenue Dashboard",
        description="Exec-facing revenue dashboard, built on orders_daily.",
    )
    client.entities.upsert(revenue_dashboard)

    client.lineage.add_lineage(
        upstream="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
        downstream="urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)",
        column_lineage={"user_id": ["user_id"]},
    )
    client.lineage.add_lineage(
        upstream="urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)",
        downstream=(
            "urn:li:dataset:(urn:li:dataPlatform:dbt,customer_revenue,PROD)"
        ),
        column_lineage={"user_id": ["user_id"]},
    )
    client.lineage.add_lineage(
        upstream="urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)",
        downstream=revenue_dashboard.urn,
    )

    print(
        "Demo graph created: analytics.orders -> orders_daily -> "
        "{customer_revenue, Revenue Dashboard}"
    )
    print(f"  orders urn:     {orders.urn}")
    print(f"  orders_daily urn: {orders_daily.urn}")
    print(f"  customer_revenue urn: {customer_revenue.urn}")
    print(f"  dashboard urn:  {revenue_dashboard.urn}")


if __name__ == "__main__":
    main()
