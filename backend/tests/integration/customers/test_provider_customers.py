"""Provider customer mappings: uniqueness both ways, project scoping, no provider calls."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from financial_hub.modules.customers.application.api import (
    find_identity_by_provider_customer,
    get_provider_customer,
    link_provider_customer,
)
from financial_hub.shared.errors import Conflict, NotFound
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.customers.helpers import Tenant, tenant
from tests.integration.identity.support import Api
from tests.integration.projects.helpers import create_account


async def account_of(shop: Tenant, name: str = "main") -> str:
    created = await create_account(shop.client, shop.csrf_token, shop.project_id, name=name)
    return created["id"]


async def link(
    api: Api,
    shop: Tenant,
    customer: str,
    account: str,
    provider_customer_id: str,
    *,
    commit: bool = True,
) -> None:
    async with api.factory() as session:
        await link_provider_customer(
            session,
            api.clock,
            project_id=parse_id(shop.project_id, IdPrefix.PROJECT),
            identity_id=parse_id(customer, IdPrefix.CUSTOMER),
            provider_account_id=parse_id(account, IdPrefix.PROVIDER_ACCOUNT),
            provider_customer_id=provider_customer_id,
        )
        if commit:
            await session.commit()


async def test_mapping_is_stored_looked_up_and_idempotent(api: Api) -> None:
    async with tenant(api) as shop:
        customer = (await shop.put("c1")).json()["id"]
        account = await account_of(shop)
        await link(api, shop, customer, account, "cus_provider_001")
        await link(api, shop, customer, account, "cus_provider_001")  # same mapping: no-op
        async with api.factory() as session:
            project = parse_id(shop.project_id, IdPrefix.PROJECT)
            identity = parse_id(customer, IdPrefix.CUSTOMER)
            account_id = parse_id(account, IdPrefix.PROVIDER_ACCOUNT)
            found = await get_provider_customer(
                session, project_id=project, identity_id=identity, provider_account_id=account_id
            )
            reverse = await find_identity_by_provider_customer(
                session,
                project_id=project,
                provider_account_id=account_id,
                provider_customer_id="cus_provider_001",
            )
            unknown = await find_identity_by_provider_customer(
                session,
                project_id=project,
                provider_account_id=account_id,
                provider_customer_id="cus_provider_404",
            )

    assert found == "cus_provider_001"
    assert reverse == identity
    assert unknown is None


async def test_mapping_is_unique_both_ways_per_account(api: Api) -> None:
    async with tenant(api) as shop:
        first = (await shop.put("c1")).json()["id"]
        second = (await shop.put("c2")).json()["id"]
        account = await account_of(shop)
        other_account = await account_of(shop, "second")
        await link(api, shop, first, account, "prov-1")

        with pytest.raises(Conflict):  # the identity already maps to another id on this account
            await link(api, shop, first, account, "prov-2")
        with pytest.raises(Conflict):  # the provider id already belongs to another identity
            await link(api, shop, second, account, "prov-1")
        await link(api, shop, first, other_account, "prov-1")  # a different account: free again
        await link(api, shop, second, account, "prov-2")


async def test_mapping_cannot_cross_projects(api: Api) -> None:
    async with tenant(api, "A") as shop_a, tenant(api, "B") as shop_b:
        mine = (await shop_a.put("c1")).json()["id"]
        their_account = await account_of(shop_b)
        my_account = await account_of(shop_a)

        with pytest.raises(NotFound):  # the account belongs to another project (db trigger)
            await link(api, shop_a, mine, their_account, "prov-x")
        with pytest.raises(NotFound):  # the identity is not in the project the caller names
            await link(api, shop_b, mine, their_account, "prov-x")
        async with api.factory() as session:
            hidden = await get_provider_customer(
                session,
                project_id=parse_id(shop_b.project_id, IdPrefix.PROJECT),
                identity_id=parse_id(mine, IdPrefix.CUSTOMER),
                provider_account_id=parse_id(my_account, IdPrefix.PROVIDER_ACCOUNT),
            )
        assert hidden is None


async def test_runtime_role_cannot_rewrite_or_delete_mappings(api: Api) -> None:
    async with tenant(api) as shop:
        customer = (await shop.put("c1")).json()["id"]
        account = await account_of(shop)
        await link(api, shop, customer, account, "prov-1")
        async with api.factory() as session:
            for statement in (
                "UPDATE provider_customers SET provider_customer_id = 'other'",
                "DELETE FROM provider_customers",
                "DELETE FROM customer_identities",
                "DELETE FROM financial_customers",
                "UPDATE customer_identities SET project_id = project_id",
                "UPDATE financial_customers SET id = id",
            ):
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
