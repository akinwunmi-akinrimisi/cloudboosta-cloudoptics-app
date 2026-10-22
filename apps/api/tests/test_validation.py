"""Unit tests for connection validation (no database)."""

from datetime import date
from typing import Any

from fake_aws import FakeClient, FakeSession, client_error, healthy_clients

from cloudoptics.connectors.validation import validate_billing_access


def run(clients: dict[str, Any] | None = None, *, assume: Exception | None = None, **kw: Any):
    def open_session() -> FakeSession:
        if assume:
            raise assume
        return FakeSession(clients or healthy_clients())

    args = {
        "aws_account_id": "123456789012",
        "account_type": "management",
        "create_cur_export": True,
    }
    return validate_billing_access(open_session, today=date(2026, 10, 22), **(args | kw))


def by_id(result) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {c.id: c for c in result.checks}


def test_healthy_connection() -> None:
    result = run()
    assert result.status == "healthy"
    assert {c.status for c in result.checks} == {"ok"}
    assert result.cur_bucket == "cloudoptics-cur-123456789012"
    assert result.cur_prefix == "cloudoptics"
    assert result.est_cost_usd == 0.01


def test_assume_role_failure_stops_early_with_guidance() -> None:
    result = run(assume=client_error("AccessDenied", "AssumeRole"))
    assert result.status == "failed"
    assert [c.id for c in result.checks] == ["assume_role"]
    assert "External ID" in (result.checks[0].hint or "")
    assert result.error == {"code": "AccessDenied", "check": "assume_role"}


def test_first_delivery_pending_is_a_warning_not_an_error() -> None:
    clients = healthy_clients(objects=0)
    result = run(clients)
    assert result.status == "healthy"
    assert by_id(result)["cur_delivery"].status == "warning"
    assert "24 hours" in by_id(result)["cur_delivery"].message


def test_missing_export_is_an_error_when_we_created_it() -> None:
    clients = healthy_clients()
    clients["bcm-data-exports"] = FakeClient(list_exports={"Exports": []})
    result = run(clients)
    assert result.status == "degraded"
    assert by_id(result)["cur_export"].status == "error"
    assert "us-east-1" in (by_id(result)["cur_export"].hint or "")
    assert by_id(result)["cur_delivery"].status == "skipped"

    lenient = run(clients, create_cur_export=False)
    assert by_id(lenient)["cur_export"].status == "warning"


def test_cost_explorer_denied_and_no_data_are_distinguished() -> None:
    clients = healthy_clients()
    clients["ce"] = FakeClient(get_cost_and_usage=client_error("AccessDeniedException"))
    assert by_id(run(clients))["cost_explorer"].status == "error"
    clients["ce"] = FakeClient(get_cost_and_usage=client_error("DataUnavailableException"))
    result = run(clients)
    assert by_id(result)["cost_explorer"].status == "warning"
    assert result.status == "healthy"


def test_member_account_chosen_as_management_gets_a_warning() -> None:
    clients = healthy_clients()
    clients["organizations"] = FakeClient(
        describe_organization={"Organization": {"MasterAccountId": "999999999999"}}
    )
    check = by_id(run(clients))["organization"]
    assert check.status == "warning" and "member account" in check.message

    clients["organizations"] = FakeClient(
        describe_organization=client_error("AWSOrganizationsNotInUseException")
    )
    assert by_id(run(clients, account_type="standalone"))["organization"].status == "ok"
    assert by_id(run(clients, account_type="management"))["organization"].status == "warning"


def test_unreadable_bucket_is_an_error() -> None:
    clients = healthy_clients()
    clients["s3"] = FakeClient(list_objects_v2=client_error("NoSuchBucket"))
    check = by_id(run(clients))["cur_delivery"]
    assert check.status == "error" and "s3://cloudoptics-cur-123456789012" in check.message
