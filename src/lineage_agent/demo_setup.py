"""Create a local DataHub sample graph for exercising RippleProof.

The graph includes a one-hop dbt compatibility boundary, several transitive
consumers owned by different teams, an intentionally ambiguous code mapping,
and a dashboard that needs manual review. It is safe to create repeatedly.
"""

import argparse
import os

from datahub.api.entities.corpgroup.corpgroup import CorpGroup
from datahub.metadata.schema_classes import (
    NumberTypeClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    StringTypeClass,
)
from datahub.sdk.dashboard import Dashboard
from datahub.sdk.dataset import Dataset
from datahub.sdk.main_client import DataHubClient

from .mutation_safety import (
    MutationSafetyError,
    NON_LOOPBACK_APPROVAL_TOKEN,
    require_datahub_mutation_permission,
)


def field(name: str, type_class, description: str) -> SchemaFieldClass:
    return SchemaFieldClass(
        fieldPath=name,
        type=SchemaFieldDataTypeClass(type=type_class()),
        nativeDataType=type_class.__name__,
        description=description,
    )


def team(client: DataHubClient, team_id: str, display_name: str) -> str:
    group = CorpGroup(
        id=team_id,
        display_name=display_name,
        email=f"{team_id}@example.com",
        description=f"Demo review team for {display_name} assets.",
    )
    for proposal in group.generate_mcp():
        client._graph.emit_mcp(proposal)
    return group.urn


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or refresh the controlled RippleProof demo graph. This command "
            "writes datasets, ownership, and lineage into DataHub."
        )
    )
    parser.add_argument(
        "--allow-non-loopback-datahub-mutation",
        action="store_true",
        help="allow this destructive demo setup to target a non-loopback DataHub",
    )
    parser.add_argument(
        "--non-loopback-approval-token",
        help=(
            "exact non-loopback mutation approval token (required with the override): "
            f"{NON_LOOPBACK_APPROVAL_TOKEN}"
        ),
    )
    args = parser.parse_args(argv)
    try:
        args.datahub_url = require_datahub_mutation_permission(
            allow_non_loopback=args.allow_non_loopback_datahub_mutation,
            approval_token=args.non_loopback_approval_token,
        )
    except MutationSafetyError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    client = DataHubClient(
        server=args.datahub_url,
        token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
    )
    platform_team = team(client, "platform-data", "Platform Data")
    analytics_team = team(
        client, "analytics-engineering", "Analytics Engineering"
    )
    finance_team = team(client, "finance-analytics", "Finance Analytics")
    growth_team = team(client, "growth-analytics", "Growth Analytics")
    operations_team = team(client, "operations-data", "Operations Data")
    revenue_team = team(client, "revenue-operations", "Revenue Operations")

    orders = Dataset(
        platform="postgres",
        name="analytics.orders",
        description="Raw orders table.",
        owners=[platform_team],
        schema=[
            field(
                "user_id",
                StringTypeClass,
                "The customer's user id. CANDIDATE FOR RENAME to customer_id.",
            ),
            field("order_total", NumberTypeClass, "Order total in cents."),
            field("created_at", StringTypeClass, "Order timestamp."),
        ],
    )
    client.entities.upsert(orders)

    orders_daily = Dataset(
        platform="dbt",
        name="orders_daily",
        description="dbt model: daily rollup of orders, keyed by user_id.",
        owners=[analytics_team],
        schema=[
            field(
                "user_id",
                StringTypeClass,
                "Rolled up from analytics.orders.user_id.",
            ),
            field(
                "total_revenue",
                NumberTypeClass,
                "Sum of order_total per user per day.",
            ),
        ],
    )
    client.entities.upsert(orders_daily)

    customer_revenue = Dataset(
        platform="dbt",
        name="customer_revenue",
        description=(
            "dbt model: customer revenue derived from the stable orders_daily contract."
        ),
        owners=[finance_team],
        schema=[
            field(
                "user_id",
                StringTypeClass,
                "Inherited from orders_daily.user_id.",
            ),
            field(
                "lifetime_revenue",
                NumberTypeClass,
                "Revenue summed per user.",
            ),
        ],
    )
    client.entities.upsert(customer_revenue)

    customer_segments = Dataset(
        platform="dbt",
        name="customer_segments",
        description="dbt model: growth segments derived from orders_daily.",
        owners=[growth_team],
        schema=[
            field(
                "user_id",
                StringTypeClass,
                "Inherited from orders_daily.user_id.",
            ),
            field("segment", StringTypeClass, "Revenue-based customer segment."),
        ],
    )
    client.entities.upsert(customer_segments)

    customer_identity_export = Dataset(
        platform="dbt",
        name="customer_identity_export",
        description=(
            "dbt model with two repository implementations that requires owner review."
        ),
        owners=[operations_team],
        schema=[
            field(
                "user_id",
                StringTypeClass,
                "Inherited from orders_daily.user_id.",
            ),
            field("destination", StringTypeClass, "Export destination label."),
        ],
    )
    client.entities.upsert(customer_identity_export)

    revenue_dashboard = Dashboard(
        platform="looker",
        name="revenue_dashboard",
        display_name="Revenue Dashboard",
        description="Exec-facing revenue dashboard, built on orders_daily.",
        owners=[revenue_team],
    )
    client.entities.upsert(revenue_dashboard)

    client.lineage.add_lineage(
        upstream="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
        downstream="urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)",
        column_lineage={"user_id": ["user_id"]},
    )
    client.lineage.add_lineage(
        upstream="urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)",
        downstream=customer_segments.urn,
        column_lineage={"user_id": ["user_id"]},
    )
    client.lineage.add_lineage(
        upstream="urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)",
        downstream=customer_identity_export.urn,
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
        "{customer_revenue, customer_segments, customer_identity_export, "
        "Revenue Dashboard}"
    )
    print(f"  orders urn:     {orders.urn}")
    print(f"  orders_daily urn: {orders_daily.urn}")
    print(f"  customer_revenue urn: {customer_revenue.urn}")
    print(f"  customer_segments urn: {customer_segments.urn}")
    print(f"  customer_identity_export urn: {customer_identity_export.urn}")
    print(f"  dashboard urn:  {revenue_dashboard.urn}")


if __name__ == "__main__":
    main()
