"""Connection validation: assume the billing role and probe each permission we rely on.

Messages are specific and actionable (PRD FR-CON-14). Each check reports ``ok``,
``warning`` (works, but something needs attention) or ``error`` (a capability is missing).
The connection is ``failed`` if the role cannot be assumed, ``degraded`` if any check
errors, otherwise ``healthy``. Costs one Cost Explorer call (USD 0.01) per run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError

CheckStatus = Literal["ok", "warning", "error", "skipped"]
ConnectionStatus = Literal["healthy", "degraded", "failed"]
CE_CALL_COST_USD = 0.01


@dataclass
class Check:
    id: str
    label: str
    status: CheckStatus
    message: str
    hint: str | None = None


@dataclass
class ValidationResult:
    status: ConnectionStatus
    checks: list[Check]
    cur_bucket: str | None = None
    cur_prefix: str | None = None
    cur_region: str | None = None
    cur_export_arn: str | None = None
    est_cost_usd: float = 0.0
    error: dict[str, str] | None = field(default=None)

    def checks_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.checks]


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


def _denied(code: str) -> bool:
    return code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


def validate_billing_access(
    open_session: Callable[[], Any],
    *,
    aws_account_id: str,
    account_type: str,
    create_cur_export: bool,
    today: date | None = None,
) -> ValidationResult:
    checks: list[Check] = []
    today = today or date.today()

    try:
        session = open_session()
    except (ClientError, BotoCoreError) as exc:
        code = _code(exc)
        checks.append(
            Check(
                "assume_role",
                "Assume the Cloudoptics role",
                "error",
                "We could not assume the role.",
                "Check that the CloudFormation stack finished in us-east-1 and that the "
                "External ID matches the one shown in Cloudoptics.",
            )
        )
        return ValidationResult("failed", checks, error={"code": code, "check": "assume_role"})
    checks.append(Check("assume_role", "Assume the Cloudoptics role", "ok", "Role assumed."))

    result = ValidationResult("healthy", checks)

    # Cost Explorer -------------------------------------------------------------------
    try:
        session.client("ce").get_cost_and_usage(
            TimePeriod={
                "Start": (today - timedelta(days=2)).isoformat(),
                "End": today.isoformat(),
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        checks.append(Check("cost_explorer", "Read Cost Explorer", "ok", "Cost data readable."))
    except ClientError as exc:
        code = _code(exc)
        if code == "DataUnavailableException":
            checks.append(
                Check(
                    "cost_explorer",
                    "Read Cost Explorer",
                    "warning",
                    "Cost Explorer has no data yet.",
                    "Data appears up to 24 hours after Cost Explorer is first enabled.",
                )
            )
        else:
            checks.append(
                Check(
                    "cost_explorer",
                    "Read Cost Explorer",
                    "error",
                    "Cost Explorer access was denied." if _denied(code) else f"Error: {code}.",
                    "Re-deploy the latest Cloudoptics stack; the role needs ce:Get*.",
                )
            )
    finally:
        result.est_cost_usd += CE_CALL_COST_USD

    # CUR 2.0 export --------------------------------------------------------------------
    export = None
    try:
        exports = session.client("bcm-data-exports").list_exports().get("Exports", [])
        ours = [e for e in exports if str(e.get("ExportName", "")).startswith("cloudoptics")]
        candidates = ours or exports
        if candidates:
            export = (
                session.client("bcm-data-exports")
                .get_export(ExportArn=candidates[0]["ExportArn"])
                .get("Export", {})
            )
            dest = export.get("DestinationConfigurations", {}).get("S3Destination", {})
            result.cur_export_arn = candidates[0]["ExportArn"]
            result.cur_bucket = dest.get("S3Bucket")
            result.cur_prefix = dest.get("S3Prefix")
            result.cur_region = dest.get("S3Region")
            checks.append(
                Check(
                    "cur_export",
                    "Find the CUR 2.0 export",
                    "ok",
                    f"Found export {export.get('Name', candidates[0].get('ExportName'))}.",
                )
            )
        else:
            checks.append(
                Check(
                    "cur_export",
                    "Find the CUR 2.0 export",
                    "error" if create_cur_export else "warning",
                    "No CUR 2.0 export was found in this account.",
                    "The stack must be deployed in us-east-1 with 'Create CUR export' on, "
                    "or create a CUR 2.0 export yourself and reconnect.",
                )
            )
    except ClientError as exc:
        code = _code(exc)
        checks.append(
            Check(
                "cur_export",
                "Find the CUR 2.0 export",
                "error",
                "Listing data exports was denied." if _denied(code) else f"Error: {code}.",
                "Re-deploy the latest Cloudoptics stack; the role needs bcm-data-exports:List*.",
            )
        )

    # First delivery in S3 --------------------------------------------------------------
    if result.cur_bucket:
        try:
            listing = session.client("s3", result.cur_region or "us-east-1").list_objects_v2(
                Bucket=result.cur_bucket, Prefix=result.cur_prefix or "", MaxKeys=1
            )
            if listing.get("KeyCount", 0) == 0:
                checks.append(
                    Check(
                        "cur_delivery",
                        "Read the export bucket",
                        "warning",
                        "The export exists; its first delivery usually arrives within 24 hours.",
                        "We will check again automatically.",
                    )
                )
            else:
                checks.append(
                    Check("cur_delivery", "Read the export bucket", "ok", "Export files found.")
                )
        except ClientError as exc:
            code = _code(exc)
            checks.append(
                Check(
                    "cur_delivery",
                    "Read the export bucket",
                    "error",
                    f"No CUR 2.0 manifest at s3://{result.cur_bucket}/{result.cur_prefix or ''}."
                    if code == "NoSuchBucket"
                    else "The role cannot read the export bucket.",
                    "The role may only read the bucket named in the stack. Re-deploy with the "
                    "bucket that the export writes to.",
                )
            )
    else:
        checks.append(
            Check("cur_delivery", "Read the export bucket", "skipped", "No export bucket yet.")
        )

    # Organization ----------------------------------------------------------------------
    try:
        org = session.client("organizations").describe_organization()["Organization"]
        is_payer = org.get("MasterAccountId") == aws_account_id
        if account_type == "management" and not is_payer:
            checks.append(
                Check(
                    "organization",
                    "Check the account type",
                    "warning",
                    "This is a member account, not the management (payer) account.",
                    "Connect the management account to see costs for every account, or "
                    "continue as a standalone account.",
                )
            )
        else:
            checks.append(
                Check("organization", "Check the account type", "ok", "Account type confirmed.")
            )
    except ClientError as exc:
        code = _code(exc)
        if code == "AWSOrganizationsNotInUseException":
            status: CheckStatus = "warning" if account_type == "management" else "ok"
            checks.append(
                Check(
                    "organization",
                    "Check the account type",
                    status,
                    "This account is not part of an AWS Organization.",
                    "Choose 'standalone' for this account." if status == "warning" else None,
                )
            )
        else:
            checks.append(
                Check(
                    "organization",
                    "Check the account type",
                    "warning" if account_type == "management" else "ok",
                    "This looks like a member account (organization details are hidden).",
                    "Connect the management (payer) account to see every account."
                    if account_type == "management"
                    else None,
                )
            )

    # Savings Plans ---------------------------------------------------------------------
    try:
        session.client("savingsplans").describe_savings_plans(maxResults=1)
        checks.append(Check("savings_plans", "Read Savings Plans", "ok", "Savings Plans readable."))
    except ClientError as exc:
        checks.append(
            Check(
                "savings_plans",
                "Read Savings Plans",
                "error",
                "Reading Savings Plans was denied."
                if _denied(_code(exc))
                else f"Error: {_code(exc)}.",
                "Re-deploy the latest Cloudoptics stack; the role needs savingsplans:Describe*.",
            )
        )

    errors = [c for c in checks if c.status == "error"]
    if errors:
        result.status = "degraded"
        result.error = {"code": "check_failed", "check": errors[0].id}
    return result
