"""Ingest a connection's CUR 2.0 export into ``cost_daily`` (docs/11c §1.1 F1-F4).

Unit of work: (connection, billing period, export version). A version is loaded once; a new
AWS delivery for the same period (a restatement) replaces that period's aggregates in one
transaction, so readers see either the old or the new numbers (docs/11b §2.5). A version
that fails a blocking check is recorded as failed and the previous good one stays live.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.aws import STACK_REGION, session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.ingestion.pipeline import LoadedExport, load_export
from cloudoptics.ingestion.source import (
    ExportSource,
    Manifest,
    ManifestError,
    S3ExportSource,
    manifest_keys,
    parse_manifest,
)
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

INGESTABLE = ("healthy", "degraded")
LOAD_ERRORS = (ClientError, BotoCoreError, ManifestError, OSError, pa.ArrowException)

SourceFactory = Callable[[Any, uuid.UUID], ExportSource]


def _s3_source(conn: Any, tenant_id: uuid.UUID) -> ExportSource:
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    session = get_session_factory().assume(
        conn.role_arn, external_id, session_name(tenant_id, "ingest")
    )
    return S3ExportSource(session.client("s3", conn.cur_region or STACK_REGION), conn.cur_bucket)


_source_factory: SourceFactory = _s3_source


def set_source_factory(factory: SourceFactory | None) -> None:
    """Swap where exports are read from (tests and local runs use a folder)."""
    global _source_factory
    _source_factory = factory or _s3_source


@dataclass
class IngestResult:
    status: Literal["skipped", "up_to_date", "succeeded", "partial", "failed"]
    loaded: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    records: int = 0


def _discover(source: ExportSource, conn: Any) -> tuple[list[Manifest], list[str]]:
    prefix = (conn.cur_prefix or "").strip("/")
    keys = manifest_keys(source.list_keys(f"{prefix}/" if prefix else ""))
    manifests, errors = [], []
    for _, key in sorted(keys.items()):
        try:
            manifests.append(
                parse_manifest(
                    key,
                    source.read_text(key),
                    bucket=getattr(source, "bucket", None),
                    prefix=prefix,
                )
            )
        except LOAD_ERRORS as exc:
            errors.append(f"{key}: {exc}")
    return manifests, errors


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


async def ingest_connection(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> IngestResult:
    """Load every export version of one connection that is not loaded yet."""
    maker = get_sessionmaker()
    started = datetime.now(UTC)
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        done = {
            (r.billing_period, r.export_version)
            for r in await session.execute(
                text(
                    "SELECT billing_period, export_version FROM ingest_batch "
                    "WHERE connection_id = :c"
                ),
                {"c": connection_id},
            )
        }
    if conn.status not in INGESTABLE or not conn.cur_bucket:
        return IngestResult("skipped")

    result = IngestResult("up_to_date")
    try:
        source = await run_in_threadpool(_source_factory, conn, tenant_id)
        manifests, result.errors = await run_in_threadpool(_discover, source, conn)
    except LOAD_ERRORS as exc:
        result.status = "failed"
        result.errors.append(_error_code(exc))
        await _record_run(maker, tenant_id, connection_id, started, result)
        return result

    settings = get_settings()
    norm_base = (
        Path(settings.normalised_dir) / f"tenant={tenant_id}" if settings.normalised_dir else None
    )
    for manifest in manifests:
        if (manifest.billing_period, manifest.export_version) in done:
            continue
        label = f"{manifest.billing_period:%Y-%m}@{manifest.export_version}"
        try:
            loaded = await run_in_threadpool(load_export, source, manifest, norm_base)
        except LOAD_ERRORS as exc:
            result.errors.append(f"{label}: {_error_code(exc)}")
            continue
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            await _store(session, tenant_id, conn, loaded)
        if loaded.ok:
            result.loaded.append(label)
            result.records += loaded.row_count
        else:
            result.rejected.append(label)

    if result.errors or result.rejected:
        result.status = "partial" if result.loaded else "failed"
    elif result.loaded:
        result.status = "succeeded"
    await _record_run(maker, tenant_id, connection_id, started, result)
    return result


async def _store(
    session: AsyncSession, tenant_id: uuid.UUID, conn: Any, loaded: LoadedExport
) -> None:
    m = loaded.manifest
    batch_id = uuid7()
    period: date = m.billing_period
    await session.execute(
        text(
            "INSERT INTO ingest_batch (id, tenant_id, connection_id, billing_period, "
            "export_version, manifest_key, mapping_version, status, row_count, checks, "
            "error_code) VALUES (:id, :t, :c, :p, :v, :key, :mv, :status, :rows, "
            "CAST(:checks AS jsonb), :code)"
        ),
        {
            "id": batch_id,
            "t": tenant_id,
            "c": conn.id,
            "p": period,
            "v": m.export_version,
            "key": m.key,
            "mv": loaded.mapping_version,
            "status": "succeeded" if loaded.ok else "failed",
            "rows": loaded.row_count,
            "checks": json.dumps([c.as_json() for c in loaded.checks]),
            "code": None if loaded.ok else "quality_check_failed",
        },
    )
    if loaded.ok:
        scope_params = {"c": conn.id, "p": period, "id": batch_id}
        await session.execute(
            text(
                "UPDATE ingest_batch SET status = 'superseded' WHERE connection_id = :c "
                "AND billing_period = :p AND id <> :id AND status = 'succeeded'"
            ),
            scope_params,
        )
        await session.execute(
            text(
                "UPDATE billing_file SET status = 'superseded' WHERE connection_id = :c "
                "AND billing_period = :p AND batch_id <> :id"
            ),
            scope_params,
        )
        await session.execute(
            text("DELETE FROM cost_daily WHERE connection_id = :c AND billing_period = :p"),
            {"c": conn.id, "p": period},
        )
        if loaded.tag_sets:
            await session.execute(
                text(
                    "INSERT INTO tag_set (tenant_id, hash, tags) VALUES (:t, :h, "
                    "CAST(:tags AS jsonb)) ON CONFLICT DO NOTHING"
                ),
                [
                    {"t": tenant_id, "h": h, "tags": json.dumps(tags)}
                    for h, tags in loaded.tag_sets.items()
                ],
            )
        if loaded.rows:
            await session.execute(
                text(
                    "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, "
                    "billing_period, account_id, service_code, service_name, service_category, "
                    "region_id, usage_type, charge_category, tag_set_hash, billed_cost_usd, "
                    "effective_cost_usd, net_cost_usd, list_cost_usd, usage_quantity, "
                    "data_version) VALUES (:t, :c, :d, :p, :acct, :svc, :svc_name, :cat, "
                    ":region, :usage_type, :charge, :tags, :billed, :effective, :net, :list, "
                    ":qty, :v)"
                ),
                [
                    {
                        "t": tenant_id,
                        "c": conn.id,
                        "d": r.usage_date,
                        "p": period,
                        "acct": r.account_id,
                        "svc": r.service_code,
                        "svc_name": r.service_name,
                        "cat": r.service_category,
                        "region": r.region_id,
                        "usage_type": r.usage_type,
                        "charge": r.charge_category,
                        "tags": r.tag_set_hash,
                        "billed": r.billed_cost,
                        "effective": r.effective_cost,
                        "net": r.net_cost,
                        "list": r.list_cost,
                        "qty": r.usage_quantity,
                        "v": m.export_version,
                    }
                    for r in loaded.rows
                ],
            )
        if loaded.accounts:
            await session.execute(
                text(
                    "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, "
                    "name, account_type) VALUES (:id, :t, :c, :acct, :name, :type) "
                    "ON CONFLICT (tenant_id, aws_account_id) "
                    "DO UPDATE SET name = COALESCE(EXCLUDED.name, cloud_account.name)"
                ),
                [
                    {
                        "id": uuid7(),
                        "t": tenant_id,
                        "c": conn.id,
                        "acct": acct,
                        "name": name,
                        "type": conn.account_type if acct == conn.aws_account_id else "member",
                    }
                    for acct, name in sorted(loaded.accounts.items())
                ],
            )
    if loaded.files:
        await session.execute(
            text(
                "INSERT INTO billing_file (id, tenant_id, connection_id, batch_id, "
                "billing_period, export_version, object_key, sha256, size_bytes, row_count, "
                "status) VALUES (:id, :t, :c, :b, :p, :v, :key, :sha, :size, :rows, :status) "
                "ON CONFLICT DO NOTHING"
            ),
            [
                {
                    "id": uuid7(),
                    "t": tenant_id,
                    "c": conn.id,
                    "b": batch_id,
                    "p": period,
                    "v": m.export_version,
                    "key": f.key,
                    "sha": f.sha256,
                    "size": f.size_bytes,
                    "rows": f.row_count,
                    "status": "aggregated" if loaded.ok else "failed",
                }
                for f in loaded.files
            ],
        )


async def _record_run(
    maker: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    started: datetime,
    result: IngestResult,
) -> None:
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at, finished_at, records, error_code, error_detail) VALUES (:id, :t, "
                ":c, 'cur.ingest', CAST(:scope AS jsonb), :status, :started, now(), :records, "
                ":code, :detail)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps({"loaded": result.loaded, "rejected": result.rejected}),
                "status": "succeeded" if result.status == "up_to_date" else result.status,
                "started": started,
                "records": result.records,
                "code": "quality_check_failed"
                if result.rejected and not result.errors
                else ("load_failed" if result.errors else None),
                "detail": "; ".join(result.errors)[:2000] or None,
            },
        )
