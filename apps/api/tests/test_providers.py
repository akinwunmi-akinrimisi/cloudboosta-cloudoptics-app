"""Provider-neutral core (ADR-018): the registry, job dispatch by ``cloud_connection.provider``,
the provider and account-ID CHECKs, native currency, the rule registry and fingerprints."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from test_data_deletion import account_id, connection, workspace
from test_recommendations import LIST

from cloudoptics import providers
from cloudoptics.analysis import engine, registry, rules
from cloudoptics.analysis.pricing import PriceBook
from cloudoptics.analysis.registry import DuplicateRuleError, RuleSpec, check_unique
from cloudoptics.analysis.settings import RULE_IDS
from cloudoptics.commitments.service import R8_RULE
from cloudoptics.connectors.service import ConnectionNotFoundError
from cloudoptics.ingestion import jobs as ingestion_jobs
from cloudoptics.ingestion.quality import QualityChecks
from cloudoptics.inventory import jobs as inventory_jobs
from cloudoptics.metrics import jobs as metrics_jobs
from cloudoptics.providers.base import CloudProvider
from cloudoptics.providers.registry import register, unregister

Sql = Callable[..., list[tuple[Any, ...]]]
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


# --- registry ---------------------------------------------------------------------------


def test_aws_is_registered_with_every_capability_but_native_recs() -> None:
    aws = providers.get("aws")
    assert aws.label == "AWS" and aws.account_noun == "account"
    assert aws.billing_currencies == frozenset({"USD"})
    assert aws.valid_account_id("123456789012") and not aws.valid_account_id("12345678901")
    for name in ("connect", "credentials", "accounts", "billing_source", "reconcile",
                 "inventory", "metrics", "prices", "commitments"):  # fmt: skip
        assert aws.capability(name) is not None, name
    assert aws.native_recs is None
    assert "nat_gateway" in aws.resource_types
    assert [p.name for p in providers.registered()] == ["aws", "gcp", "digitalocean", "azure"]


def test_unknown_and_unregistered_providers() -> None:
    assert set(providers.PROVIDER_NAMES) == {"aws", "gcp", "azure", "digitalocean"}
    # Every provider the schema accepts has a plug-in now (ADR-019, -020, -021).
    assert {p.name for p in providers.registered()} == set(providers.PROVIDER_NAMES)
    with pytest.raises(providers.UnknownProviderError):
        providers.get("oracle")
    assert providers.find("oracle") is None
    with pytest.raises(ValueError, match="not a provider"):
        register(
            CloudProvider(
                name="oracle",  # type: ignore[arg-type]
                label="Oracle",
                account_noun="account",
                account_id_pattern=".+",
                billing_currencies=None,
            )
        )


@dataclass
class FakeBilling:
    calls: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)

    async def ingest(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Any:
        self.calls.append((tenant_id, connection_id))
        return type("Outcome", (), {"status": "up_to_date", "loaded_periods": []})()

    async def reload(self, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: Any) -> Any:
        raise AssertionError("not called")


@pytest.fixture
def fake_gcp() -> Iterator[FakeBilling]:
    billing = FakeBilling()
    register(
        CloudProvider(
            name="gcp",
            label="Google Cloud",
            account_noun="project",
            account_id_pattern=r"[a-z][a-z0-9-]{4,28}[a-z0-9]",
            billing_currencies=None,
            billing_source=billing,  # costs only: no inventory, metrics or rules
        )
    )
    yield billing
    unregister("gcp")


def other_connection(owner_sql: Sql, tenant: str, provider: str, account: str) -> str:
    """A connection of another provider (its secret starts 0x02, as KMS ones do: not legacy)."""
    conn = str(uuid.uuid4())
    owner_sql(
        "INSERT INTO cloud_connection (id, tenant_id, provider, provider_account_id, "
        "account_type, role_arn, external_id_enc, stack_name, status) VALUES "
        "(%s, %s, %s, %s, 'standalone', '-', '\\x02', '-', 'healthy')",
        conn,
        tenant,
        provider,
        account,
    )
    return conn


async def test_jobs_dispatch_by_the_connections_provider(
    client: AsyncClient, login, owner_sql: Sql, fake_gcp: FakeBilling, monkeypatch
) -> None:
    tenant, headers = await workspace(client, login, "Two Clouds Ltd")
    aws_conn = await connection(client, tenant, headers, account_id())
    gcp_conn = other_connection(owner_sql, tenant, "gcp", "acme-billing-01")
    azure_conn = other_connection(owner_sql, tenant, "azure", str(uuid.uuid4()))

    seen: list[str] = []

    async def aws_ingest(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Any:
        seen.append(str(connection_id))
        return type("Outcome", (), {"status": "up_to_date", "loaded_periods": []})()

    from cloudoptics.ingestion import service

    monkeypatch.setattr(service, "ingest_connection", aws_ingest)  # the AWS plug-in's target

    assert await providers.connection_provider(uuid.UUID(tenant), uuid.UUID(gcp_conn)) == "gcp"
    assert await ingestion_jobs.ingest(tenant, aws_conn) == "up_to_date"
    assert seen == [aws_conn]
    assert await ingestion_jobs.ingest(tenant, gcp_conn) == "up_to_date"
    assert fake_gcp.calls == [(uuid.UUID(tenant), uuid.UUID(gcp_conn))]
    assert seen == [aws_conn]  # not sent to AWS

    # A capability the plug-in lacks, and a provider with no plug-in: skipped, nothing runs.
    assert await inventory_jobs.scan(tenant, gcp_conn) == "unsupported"
    # Azure has a plug-in (ADR-020) that skips a connection without its settings row.
    assert await ingestion_jobs.ingest(tenant, azure_conn) == "skipped"
    assert await ingestion_jobs.snapshot(tenant, azure_conn) == "skipped"


async def test_a_gone_connection_is_reported_by_the_aws_code(client: AsyncClient, login) -> None:
    """No row (deleted after queueing): the default plug-in answers, as it always has."""
    tenant, _ = await workspace(client, login)
    gone = str(uuid.uuid4())
    assert await providers.connection_provider(uuid.UUID(tenant), uuid.UUID(gone)) == "aws"
    with pytest.raises(ConnectionNotFoundError):
        await metrics_jobs.pull(tenant, gone)


# --- schema -----------------------------------------------------------------------------


def _fails(owner_sql: Sql, sql: str, *params: object) -> str:
    with pytest.raises(psycopg.errors.CheckViolation) as err:
        owner_sql(sql, *params)
    return str(err.value)


async def test_provider_and_account_id_checks(client: AsyncClient, login, owner_sql: Sql) -> None:
    tenant, headers = await workspace(client, login)
    conn = await connection(client, tenant, headers, account_id())
    insert_account = (
        "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
        "account_type) VALUES (%s, %s, %s, %s, %s, 'standalone')"
    )
    # Only the four providers.
    assert "provider_check" in _fails(
        owner_sql, insert_account, uuid.uuid4(), tenant, conn, "oracle", "ocid1"
    )
    assert "provider_check" in _fails(
        owner_sql, "UPDATE cloud_connection SET provider = 'ibm' WHERE id = %s", conn
    )
    # The 12-digit rule binds AWS rows only; other providers' IDs are free-form (1-128).
    assert "provider_account_id_check" in _fails(
        owner_sql, insert_account, uuid.uuid4(), tenant, conn, "aws", "my-project"
    )
    owner_sql(insert_account, uuid.uuid4(), tenant, conn, "gcp", "my-project")
    owner_sql(insert_account, uuid.uuid4(), tenant, conn, "azure", str(uuid.uuid4()))
    assert "provider_account_id_check" in _fails(
        owner_sql, insert_account, uuid.uuid4(), tenant, conn, "gcp", "x" * 129
    )
    other_connection(owner_sql, tenant, "gcp", "acme-billing-01")
    assert "provider_account_id_check" in _fails(
        owner_sql,
        "INSERT INTO cloud_connection (id, tenant_id, provider_account_id, account_type, "
        "role_arn, external_id_enc, stack_name) VALUES (%s, %s, 'acme', 'standalone', '-', "
        "'\\x02', '-')",
        uuid.uuid4(),
        tenant,
    )
    # Fact tables: checked for new rows too.
    assert "resource_provider_account_id_check" in _fails(
        owner_sql,
        "INSERT INTO resource (id, tenant_id, connection_id, provider_account_id, region_id, "
        "resource_type, resource_id) VALUES (%s, %s, %s, 'abc', 'eu-west-1', 'eip', 'e-1')",
        uuid.uuid4(),
        tenant,
        conn,
    )
    # AWS resource types bind AWS rows only.
    assert "resource_type_check" in _fails(
        owner_sql,
        "INSERT INTO resource (id, tenant_id, connection_id, provider_account_id, region_id, "
        "resource_type, resource_id) VALUES (%s, %s, %s, %s, 'eu-west-1', 'gce_disk', 'd-1')",
        uuid.uuid4(),
        tenant,
        conn,
        account_id(),
    )
    owner_sql(
        "INSERT INTO resource (id, tenant_id, connection_id, provider, provider_account_id, "
        "region_id, resource_type, resource_id) VALUES (%s, %s, %s, 'gcp', 'my-project', "
        "'europe-west2', 'gce_disk', 'd-1')",
        uuid.uuid4(),
        tenant,
        conn,
    )


async def test_the_same_account_id_under_two_providers_does_not_collide(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    tenant, headers = await workspace(client, login)
    account = account_id()
    conn = await connection(client, tenant, headers, account)
    # A 12-digit ID is valid for DigitalOcean too; the keys now include the provider.
    other_connection(owner_sql, tenant, "digitalocean", account)
    owner_sql(
        "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
        "account_type) VALUES (%s, %s, %s, 'digitalocean', %s, 'standalone')",
        uuid.uuid4(),
        tenant,
        conn,
        account,
    )
    for provider in ("aws", "digitalocean"):
        owner_sql(
            "INSERT INTO resource (id, tenant_id, connection_id, provider, provider_account_id, "
            "region_id, resource_type, resource_id) VALUES (%s, %s, %s, %s, %s, 'lon1', "
            "'nat_gateway', 'same-id')",
            uuid.uuid4(),
            tenant,
            conn,
            provider,
            account,
        )
    assert owner_sql(
        "SELECT count(*) FROM resource WHERE tenant_id = %s AND resource_id = 'same-id'", tenant
    ) == [(2,)]
    # One verified workspace per account stays, per provider (ADR-017).
    rows = owner_sql(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'cloud_connection_verified_account_key'"
    )
    assert "(provider, provider_account_id)" in rows[0][0]


async def test_cost_rows_keep_the_native_currency(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    tenant, headers = await workspace(client, login)
    account = account_id()
    conn = await connection(client, tenant, headers, account)
    insert = (
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "provider_account_id, service_code, service_name, service_category, charge_category, "
        "billed_cost_usd, effective_cost_usd, net_cost_usd, list_cost_usd, data_version"
        "{extra}) VALUES (%s, %s, '2026-10-02', '2026-10-01', %s, 'AmazonEC2', 'EC2', "
        "'Compute', 'Usage', 2, 1.5, 1.25, 3, 'v1'{values})"
    )
    # An AWS row as the ingestion writes it: USD, native filled from USD by the trigger.
    owner_sql(insert.format(extra="", values=""), tenant, conn, account)
    assert owner_sql(
        "SELECT billing_currency, billed_cost, effective_cost, net_cost, list_cost "
        "FROM cost_daily WHERE connection_id = %s",
        conn,
    ) == [("USD", 2, 1.5, 1.25, 3)]
    # USD amounts changed later: native follows.
    owner_sql("UPDATE cost_daily SET effective_cost_usd = 4 WHERE connection_id = %s", conn)
    assert owner_sql("SELECT effective_cost FROM cost_daily WHERE connection_id = %s", conn) == [
        (4,)
    ]
    # Another currency must bring its own amounts.
    with pytest.raises(psycopg.errors.NotNullViolation):
        owner_sql(
            insert.format(extra=", billing_currency", values=", 'NGN'"), tenant, conn, account
        )
    owner_sql(
        insert.format(
            extra=", billing_currency, billed_cost, effective_cost, net_cost, list_cost",
            values=", 'NGN', 3000, 2250, 1875, 4500",
        ),
        tenant,
        conn,
        account,
    )
    assert owner_sql(
        "SELECT effective_cost, effective_cost_usd FROM cost_daily WHERE connection_id = %s "
        "AND billing_currency = 'NGN'",
        conn,
    ) == [(2250, 1.5)]
    with pytest.raises(psycopg.errors.CheckViolation):
        owner_sql("UPDATE cost_daily SET billing_currency = 'usd' WHERE connection_id = %s", conn)


def test_the_currency_rule_is_per_provider() -> None:
    period = date(2026, 9, 1)
    aws = QualityChecks(period)  # the default: AWS, USD only
    aws._currencies = {"USD", "ZAR"}
    assert [c.message for c in aws.finish(0) if c.id == "currency"] == [
        "Billing currency ZAR is not supported yet."
    ]
    any_currency = QualityChecks(period, accepted_currencies=None)
    any_currency._currencies = {"EUR", "NGN"}
    assert not [c for c in any_currency.finish(0) if c.id == "currency"]
    assert providers.get("aws").accepts_currency("USD")
    assert not providers.get("aws").accepts_currency("EUR")


# --- rule registry and fingerprints ------------------------------------------------------


def test_the_rule_registry_keeps_the_engines_order_and_metadata() -> None:
    specs = registry.rules_for("aws")
    # The order the engine ran its plain lists in: rules, compute, schedule, billing; then R8.
    assert [s.evaluate.__name__ if s.evaluate else s.id for s in specs] == [
        "unattached_volumes", "old_snapshots", "idle_addresses", "idle_load_balancers",
        "gp2_to_gp3", "idle_instances", "idle_databases", "rightsizing", "previous_generation",
        "schedule_instances", "schedule_databases", "extended_support", "idle_nat_gateways",
        "R8",
    ]  # fmt: skip
    assert tuple(s for s in registry.engine_rules() if s.provider == "aws") == specs[:-1]
    assert all(s.provider == "aws" and s.resource_types for s in specs)
    assert registry.spec_for("R10_idle_nat").version == 2  # type: ignore[union-attr]
    assert registry.spec_for("R6_graviton") is registry.spec_for("R6_newer_generation")
    assert registry.spec_for(R8_RULE) is not None and registry.spec_for("R9_gone") is None
    # Every rule a workspace can switch off is in the registry.
    assert set(RULE_IDS) <= {r for s in registry.all_rules() for r in s.rule_ids}
    spec = specs[0]
    with pytest.raises(DuplicateRuleError):
        check_unique((spec, RuleSpec("X", "aws", spec.rule_ids, frozenset(), 1)))


def volume(provider: str = "aws") -> rules.Resource:
    return rules.Resource(
        id=uuid.uuid4(),
        account_id="111122223333",
        region_id="us-east-2",
        resource_type="ebs_volume",
        resource_id=f"vol-{provider}",
        name=None,
        state="available",
        config={"volumeType": "gp2", "sizeGiB": 100},
        tags={},
        state_since=NOW - timedelta(days=40),
        first_seen_at=NOW - timedelta(days=60),
        provider=provider,
    )


def test_rules_see_their_own_providers_resources_and_fingerprints_carry_it() -> None:
    seen: list[str] = []

    def gcp_rule(ctx: rules.Context, resources: list[rules.Resource]) -> Iterator[Any]:
        seen.extend(r.provider for r in resources)
        return iter(())

    register(
        CloudProvider(
            name="gcp",
            label="Google Cloud",
            account_noun="project",
            account_id_pattern=".+",
            billing_currencies=None,
            rules=(RuleSpec("G1", "gcp", ("G1_test",), frozenset({"ebs_volume"}), 1, gcp_rule),),
        )
    )
    try:
        book = PriceBook(list_prices=LIST)
        scored = engine.evaluate(
            rules.Context(now=NOW, prices=book), [volume("aws"), volume("gcp")]
        )
    finally:
        unregister("gcp")
    assert seen == ["gcp"]
    flagged = {s.candidate.resource.resource_id for s in scored}
    assert flagged == {"vol-aws"}  # the AWS rules never saw the other provider's volume
    unattached = next(s for s in scored if s.candidate.rule_id == "R1_unattached_ebs")
    assert unattached.fingerprint == "aws:R1_unattached_ebs:111122223333:us-east-2:vol-aws"


async def test_api_responses_name_the_provider(client: AsyncClient, login, owner_sql: Sql) -> None:
    tenant, headers = await workspace(client, login)
    account = account_id()
    conn = await connection(client, tenant, headers, account)
    owner_sql(
        "INSERT INTO cloud_account (id, tenant_id, connection_id, provider_account_id, "
        "account_type) VALUES (%s, %s, %s, %s, 'standalone')",
        uuid.uuid4(),
        tenant,
        conn,
        account,
    )
    listed = (await client.get(f"/v1/tenants/{tenant}/connections", headers=headers)).json()
    assert [(c["awsAccountId"], c["provider"]) for c in listed] == [(account, "aws")]
    # The existing keys are untouched and keep their order; provider comes after them, then
    # how the secret was obtained (ADR-021: "oauth" for a DigitalOcean sign-in).
    assert list(listed[0])[-2:] == ["provider", "credentialType"]
    assert listed[0]["credentialType"] == "static"
    one = (await client.get(f"/v1/tenants/{tenant}/connections/{conn}", headers=headers)).json()
    assert one["provider"] == "aws"
    accounts = (await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)).json()
    assert [(a["awsAccountId"], a["provider"]) for a in accounts["accounts"]] == [(account, "aws")]
