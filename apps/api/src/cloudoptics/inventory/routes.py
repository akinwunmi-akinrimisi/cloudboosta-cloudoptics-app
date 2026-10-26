"""/v1/tenants/{t}/resources: what the inventory scan has found."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter

from cloudoptics.identity.deps import Session, ViewerAccess
from cloudoptics.inventory import scanner
from cloudoptics.platform.api import ApiModel

router = APIRouter(prefix="/v1/tenants/{tenant_id}/resources", tags=["inventory"])


class InventoryCount(ApiModel):
    resource_type: str
    count: int


class ScanProgress(ApiModel):
    """How far a running scan has got. The total is an estimate until every account's
    regions are known."""

    regions_done: int
    regions_total: int


class LastScan(ApiModel):
    """A connection's latest scan. ``status`` is running (``progress`` says how far it has
    got; null while it waits for the worker), succeeded, partial (some accounts or regions
    could not be read: see ``errors``) or failed."""

    connection_id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    resources: int
    accounts: int
    accounts_with_access: int
    regions: int
    errors: list[str]
    progress: ScanProgress | None = None


class InventorySummary(ApiModel):
    """``lastScanAt`` is null until a scan has run (it needs the resource stack)."""

    last_scan_at: datetime | None
    counts: list[InventoryCount]
    scans: list[LastScan]


@router.get("/summary", response_model=InventorySummary)
async def inventory_summary(access: ViewerAccess, session: Session) -> InventorySummary:
    counts, last = await scanner.summary(session)
    return InventorySummary(
        last_scan_at=last,
        counts=[InventoryCount.model_validate(c) for c in counts],
        scans=[LastScan.model_validate(s) for s in await scanner.last_scans(session)],
    )
