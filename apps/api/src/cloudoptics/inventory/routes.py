"""/v1/tenants/{t}/resources: what the inventory scan has found."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from cloudoptics.identity.deps import Session, ViewerAccess
from cloudoptics.inventory import scanner
from cloudoptics.platform.api import ApiModel

router = APIRouter(prefix="/v1/tenants/{tenant_id}/resources", tags=["inventory"])


class InventoryCount(ApiModel):
    resource_type: str
    count: int


class InventorySummary(ApiModel):
    """``lastScanAt`` is null until a scan has run (it needs the resource stack)."""

    last_scan_at: datetime | None
    counts: list[InventoryCount]


@router.get("/summary", response_model=InventorySummary)
async def inventory_summary(access: ViewerAccess, session: Session) -> InventorySummary:
    counts, last = await scanner.summary(session)
    return InventorySummary(
        last_scan_at=last, counts=[InventoryCount.model_validate(c) for c in counts]
    )
