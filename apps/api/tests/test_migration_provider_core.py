"""Migrations 0042 and 0043 (ADR-018) on a database holding AWS data: up keeps every row,
status and fingerprint (now ``aws:``-prefixed, as the engine writes them); down restores
the rows exactly, and refuses while another provider's data exists."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from conftest import ADMIN_URL, API_DIR, _with_db
from psycopg import sql

from cloudoptics.analysis import engine, rules

TENANT, CONN, RESOURCE = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
ACCOUNT, MEMBER = "111122223333", "444455556666"
OLD_FINGERPRINT = f"R1_unattached_ebs:{ACCOUNT}:us-east-2:vol-1"
EIP_FINGERPRINT = f"R2_idle_eip:{MEMBER}:eu-west-1:eipalloc-1"

# Rows as the code before 0042 wrote them (aws_account_id / account_id char(12)).
SEED: list[tuple[str, tuple[Any, ...]]] = [
    (
        "INSERT INTO tenant (id, name, country_code, display_currency) "
        "VALUES (%s, 'Acme', 'NG', 'NGN')",
        (TENANT,),
    ),
    (
        "INSERT INTO cloud_connection (id, tenant_id, aws_account_id, account_type, role_arn, "
        "external_id_enc, stack_name, status, account_verified_at) "
        "VALUES (%s, %s, %s, 'management', 'arn', '\\x00', 's', 'healthy', now())",
        (CONN, TENANT, ACCOUNT),
    ),
    (
        "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, account_type) "
        "VALUES (gen_random_uuid(), %s, %s, %s, 'management'), "
        "(gen_random_uuid(), %s, %s, %s, 'member')",
        (TENANT, CONN, ACCOUNT, TENANT, CONN, MEMBER),
    ),
    (
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, service_name, service_category, charge_category, "
        "billed_cost_usd, effective_cost_usd, net_cost_usd, list_cost_usd, data_version) "
        "VALUES (%s, %s, '2026-10-02', '2026-10-01', %s, 'AmazonEC2', 'EC2', 'Compute', "
        "'Usage', 2.5, 2.25, 2, 3, 'v1')",
        (TENANT, CONN, MEMBER),
    ),
    (
        "INSERT INTO cost_resource_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, usage_type, resource_id, effective_cost_usd, usage_quantity) "
        "VALUES (%s, %s, '2026-10-02', '2026-10-01', %s, 'AmazonEC2', 'BoxUsage', 'i-1', "
        "1.75, 24)",
        (TENANT, CONN, MEMBER),
    ),
    (
        "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, period_start, through_date, "
        "effective_cost_usd, billed_cost_usd, credits_usd) "
        "VALUES (%s, %s, '2026-10-01', '2026-10-02', 9.5, 10, -0.5)",
        (TENANT, CONN),
    ),
    (
        "INSERT INTO resource (id, tenant_id, connection_id, account_id, region_id, "
        "resource_type, resource_id) VALUES (%s, %s, %s, %s, 'us-east-2', 'ebs_volume', 'vol-1')",
        (RESOURCE, TENANT, CONN, ACCOUNT),
    ),
    (
        "INSERT INTO recommendation (id, tenant_id, fingerprint, type, rule_id, rule_version, "
        "rule_config, generation_id, resource_ref, account_id, region_id, resource_id, title, "
        "cost_now_usd_month, cost_after_usd_month, savings_basis, effort, reversibility, "
        "risk_score, risk_level, confidence, priority_score, engine_version, status, "
        "realised_savings_usd_month) VALUES "
        "(gen_random_uuid(), %s, %s, 'delete', 'R1_unattached_ebs', 1, '{}', 1, %s, %s, "
        "'us-east-2', 'vol-1', 'Delete volume', 10, 0, 'list_price', 'S', 'with_snapshot', 10, "
        "'low', 'high', 1, 't', 'verified', 9.5), "
        "(gen_random_uuid(), %s, %s, 'delete', 'R2_idle_eip', 1, '{}', 1, NULL, %s, "
        "'eu-west-1', 'eipalloc-1', 'Release address', 3.6, 0, 'list_price', 'S', "
        "'irreversible', 10, 'low', 'high', 1, 't', 'dismissed', NULL)",
        (TENANT, OLD_FINGERPRINT, RESOURCE, ACCOUNT, TENANT, EIP_FINGERPRINT, MEMBER),
    ),
    (
        "INSERT INTO anomaly (id, tenant_id, usage_date, account_id, service_code, service_name, "
        "actual_usd, expected_usd, mad_usd, impact_usd, engine_version, status) VALUES "
        "(gen_random_uuid(), %s, '2026-10-02', %s, 'AmazonEC2', 'EC2', 10, 1, 1, 9, 't', "
        "'expected')",
        (TENANT, MEMBER),
    ),
    (
        "INSERT INTO commitment (id, tenant_id, connection_id, kind, commitment_id, account_id, "
        "description) VALUES (gen_random_uuid(), %s, %s, 'savings_plan', 'sp-1', %s, "
        "'Compute SP'), (gen_random_uuid(), %s, %s, 'ec2_reserved', 'ri-1', NULL, 'RI')",
        (TENANT, CONN, ACCOUNT, TENANT, CONN),
    ),
]
TABLES = [
    "cloud_connection",
    "cloud_account",
    "cost_daily",
    "cost_resource_daily",
    "cost_snapshot_ce",
    "resource",
    "recommendation",
    "anomaly",
    "commitment",
]


@pytest.fixture(scope="module")
def migrated() -> Iterator[tuple[Config, psycopg.Connection]]:
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    name = f"cloudoptics_mig_{secrets.token_hex(4)}"
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    cfg = Config(str(API_DIR / "alembic.ini"))
    url = _with_db(ADMIN_URL, name).replace("postgresql://", "postgresql+psycopg://")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0041")
    conn = psycopg.connect(_with_db(ADMIN_URL, name), autocommit=True)
    try:
        yield cfg, conn
    finally:
        conn.close()
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.close()


def snapshot(conn: psycopg.Connection) -> dict[str, list[str]]:
    out = {}
    for table in TABLES:
        query = sql.SQL("SELECT to_jsonb(t) FROM {} t").format(sql.Identifier(table))
        out[table] = sorted(json.dumps(r[0], sort_keys=True) for r in conn.execute(query))
    return out


def values(conn: psycopg.Connection, query: str, *params: Any) -> list[Any]:
    return [r[0] if len(r) == 1 else r for r in conn.execute(query, params or None)]


def test_up_and_down_keep_aws_data(migrated: tuple[Config, psycopg.Connection]) -> None:
    cfg, conn = migrated
    for statement, params in SEED:
        conn.execute(statement, params)
    before = snapshot(conn)

    command.upgrade(cfg, "head")
    for table in TABLES:
        query = sql.SQL("SELECT DISTINCT provider FROM {}").format(sql.Identifier(table))
        assert [r[0] for r in conn.execute(query)] == ["aws"], table
    assert sorted(values(conn, "SELECT provider_account_id FROM cloud_account")) == [
        ACCOUNT,
        MEMBER,
    ]
    assert values(conn, "SELECT pg_typeof(provider_account_id)::text FROM cost_daily") == ["text"]
    # Fingerprints carry the provider; statuses and realised savings stay where they were.
    assert sorted(
        values(conn, "SELECT fingerprint, status, realised_savings_usd_month FROM recommendation")
    ) == [
        (f"aws:{OLD_FINGERPRINT}", "verified", 9.5),
        (f"aws:{EIP_FINGERPRINT}", "dismissed", None),
    ]
    # ...in the form the engine now writes, so its next run updates the same rows.
    volume = rules.Resource(
        id=uuid.UUID(RESOURCE),
        account_id=ACCOUNT,
        region_id="us-east-2",
        resource_type="ebs_volume",
        resource_id="vol-1",
        name=None,
        state="available",
        config={},
        tags={},
        state_since=None,  # type: ignore[arg-type]
        first_seen_at=None,  # type: ignore[arg-type]
    )
    candidate = SimpleNamespace(rule_id="R1_unattached_ebs", resource=volume)
    scored = engine.Scored(candidate, 0, "low", 1.0)  # type: ignore[arg-type]
    assert values(
        conn, "SELECT fingerprint FROM recommendation WHERE resource_ref IS NOT NULL"
    ) == [scored.fingerprint]
    # Native amounts equal USD, billed in USD.
    assert values(
        conn,
        "SELECT billing_currency, billed_cost, effective_cost, net_cost, list_cost FROM cost_daily",
    ) == [("USD", 2.5, 2.25, 2, 3)]
    assert values(conn, "SELECT billing_currency, effective_cost FROM cost_resource_daily") == [
        ("USD", 1.75)
    ]
    assert values(conn, "SELECT effective_cost, billed_cost, credits FROM cost_snapshot_ce") == [
        (9.5, 10, -0.5)
    ]
    assert values(conn, "SELECT status FROM anomaly") == ["expected"]
    assert sorted(values(conn, "SELECT coalesce(provider_account_id, '-') FROM commitment")) == [
        "-",
        ACCOUNT,
    ]
    # Every one of these tenant tables still has FORCE RLS.
    unforced = values(
        conn,
        "SELECT relname FROM pg_class WHERE relname = ANY(%s) "
        "AND NOT (relrowsecurity AND relforcerowsecurity)",
        TABLES,
    )
    assert unforced == []

    # Another provider's row blocks the way down (char(12) cannot hold it)...
    conn.execute(
        "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
        "account_type) VALUES (gen_random_uuid(), %s, %s, 'gcp', 'acme-prod', 'standalone')",
        (TENANT, CONN),
    )
    with pytest.raises(Exception, match="non-AWS rows"):
        command.downgrade(cfg, "0041")
    conn.execute("DELETE FROM cloud_account WHERE provider = 'gcp'")

    # ...and without it, down restores every row exactly.
    command.downgrade(cfg, "0041")
    assert snapshot(conn) == before
    assert values(conn, "SELECT pg_typeof(account_id)::text FROM cost_daily") == ["character"]

    # Up again (the deploy path after a rollback) gives the same result as the first time.
    command.upgrade(cfg, "head")
    assert sorted(values(conn, "SELECT fingerprint FROM recommendation")) == [
        f"aws:{OLD_FINGERPRINT}",
        f"aws:{EIP_FINGERPRINT}",
    ]
