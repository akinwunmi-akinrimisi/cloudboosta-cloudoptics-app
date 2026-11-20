# ruff: noqa: E501  (one case per line in the tables reads best)
"""Provider names in emails, digests, reports and messages: each provider and its account
(AWS account, Google Cloud billing account, Azure subscription or billing account,
DigitalOcean team), and "cloud" wording for a workspace with several providers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cloudoptics import providers
from cloudoptics.notifications.alerts import connection_email
from cloudoptics.privacy.service import connection_deleted_email
from cloudoptics.providers import naming
from cloudoptics.recommendations.notify import assignment_email
from cloudoptics.recommendations.verification import _note
from cloudoptics.reporting import digest, monthly
from cloudoptics.support.service import granted_email

APP = "https://app.example"
VERIFICATION = {"rowsDeleted": 12, "tables": {}}


def test_labels_match_the_registry() -> None:
    assert {p.name: p.label for p in providers.registered()} == naming.LABELS


@pytest.mark.parametrize(
    ("provider", "account_type", "account", "label"),
    [
        (None, "standalone", "123456789012", "AWS account 123456789012"),
        ("aws", "management", "123456789012", "AWS account 123456789012"),
        ("gcp", "management", "012345-6789AB-CDEF01", "Google Cloud billing account 012345-6789AB-CDEF01"),
        ("azure", "standalone", "0b5e0f1c-0000-4000-8000-000000000001", "Azure subscription 0b5e0f1c-0000-4000-8000-000000000001"),
        ("azure", "management", "1234:5678_2019-05-31", "Azure billing account 1234:5678_2019-05-31"),
        ("digitalocean", "standalone", "team-uuid-1", "DigitalOcean team team-uuid-1"),
    ],
)  # fmt: skip
def test_connection_labels(
    provider: str | None, account_type: str, account: str, label: str
) -> None:
    assert naming.connection_label(provider, f"{account}  ", account_type) == label


def test_workspace_wording() -> None:
    assert naming.ordered([]) == ["aws"]
    assert naming.ordered(["digitalocean", None, "gcp"]) == ["aws", "gcp", "digitalocean"]
    assert naming.clouds(["aws"]) == "AWS"
    assert naming.clouds(["gcp", "gcp"]) == "Google Cloud"
    assert naming.clouds(["aws", "azure"]) == "cloud"
    assert naming.where(["aws", "azure"]) == "your cloud accounts"
    assert naming.credits_title(["digitalocean"]) == "DigitalOcean credits"
    assert naming.credits_title(["aws", "gcp"]) == "Credits"
    assert naming.bills_source(["aws"]) == "the AWS bills (CUR 2.0)"
    assert naming.bills_source(["azure"]) == "the Azure bills"
    assert naming.bills_source(["aws", "gcp", "azure"]) == (
        "the bills of AWS, Google Cloud and Azure"
    )
    assert naming.provider_total(["aws"]) == "Cost Explorer"
    assert naming.provider_total(["digitalocean"]) == "DigitalOcean's own total"
    assert naming.provider_total(["aws", "gcp"]) == "each provider's own total"


def _conn(provider: str, account: str, account_type: str = "standalone") -> Any:
    return SimpleNamespace(
        aws_account_id=account, provider=provider, account_type=account_type, health="failed"
    )


CHECKS = [{"label": "Billing export", "status": "error", "message": "Not found"}]


@pytest.mark.parametrize(
    ("conn", "subject", "reads"),
    [
        (_conn("aws", "123456789012"), "your AWS connection 123456789012", "AWS account 123456789012"),
        (_conn("gcp", "012345-6789AB-CDEF01", "management"), "your Google Cloud connection 012345-6789AB-CDEF01", "Google Cloud billing account 012345-6789AB-CDEF01"),
        (_conn("azure", "0b5e0f1c-0000-4000-8000-000000000001"), "your Azure connection 0b5e0f1c-0000-4000-8000-000000000001", "Azure subscription 0b5e0f1c-0000-4000-8000-000000000001"),
        (_conn("azure", "1234:5678", "management"), "your Azure connection 1234:5678", "Azure billing account 1234:5678"),
        (_conn("digitalocean", "team-uuid-1"), "your DigitalOcean connection team-uuid-1", "DigitalOcean team team-uuid-1"),
    ],
)  # fmt: skip
def test_connection_alert_names_the_provider_and_its_account(
    conn: Any, subject: str, reads: str
) -> None:
    subj, text_, html_ = connection_email(conn, CHECKS, APP)
    assert subj == f"Action needed: {subject} has stopped working"
    assert f"Cloudoptics can no longer fully read {reads}." in text_
    account = conn.aws_account_id
    assert f"read {reads.removesuffix(account)}<strong>{account}</strong>" in html_
    if conn.provider != "aws":
        assert "AWS" not in subj + text_ + html_


def test_an_older_row_without_a_provider_reads_as_aws() -> None:
    conn = SimpleNamespace(aws_account_id="123456789012", health="degraded")
    subj, text_, _ = connection_email(conn, [], APP)
    assert subj == "Action needed: your AWS connection 123456789012 needs attention"
    assert "read AWS account 123456789012." in text_


def _digest(providers_: tuple[str, ...]) -> digest.Digest:
    recs = SimpleNamespace(
        open_count=1,
        open_savings_usd_month=10.0,
        new_count=0,
        top=[],
        done_count=0,
        done_savings_usd_month=0.0,
        verified_count=2,
        realised_usd_month=5.0,
    )
    base = digest.Digest(
        workspace="Acme",
        week_start=date(2026, 11, 9),
        week_end=date(2026, 11, 15),
        data_through=date(2026, 11, 15),
        currency="USD",
        rate=None,
        spend=100.0,
        previous=100.0,
        changes=[],
        recommendations=recs,  # type: ignore[arg-type]
        budgets=[],
        anomalies=SimpleNamespace(count=0, open_count=0, impact_usd=0.0),  # type: ignore[arg-type]
        credits=SimpleNamespace(
            status="no_balance", applied_month_usd=3.0, spend_before_credits_usd_month=9.0
        ),  # type: ignore[arg-type]
    )
    return replace(base, providers=providers_)


@pytest.mark.parametrize(
    ("providers_", "week", "verified", "credits"),
    [
        (("aws",), "Your AWS week", "Verified in AWS this week", "AWS credits"),
        (("gcp",), "Your Google Cloud week", "Verified in Google Cloud this week", "Google Cloud credits"),
        (("aws", "digitalocean"), "Your cloud week", "Verified in your cloud accounts this week", "Credits"),
    ],
)  # fmt: skip
def test_digest_wording_follows_the_workspace_providers(
    providers_: tuple[str, ...], week: str, verified: str, credits: str
) -> None:
    subject, text_, html_ = digest.render(_digest(providers_), APP)
    assert subject.startswith(f"{week}: USD 100.00 spent")
    assert verified in text_
    assert f"\n{credits}\n" in text_ and f"<h3>{credits}</h3>" in html_
    if "aws" not in providers_:
        assert "AWS" not in subject + text_ + html_


def test_a_digest_defaults_to_aws() -> None:
    assert (
        digest.render(_digest(("aws",)), APP)[0]
        == digest.render(replace(_digest(("aws",)), providers=("aws",)), APP)[0]
    )


def _report(providers_: list[str] | None) -> monthly.MonthlyReport:
    extra = {} if providers_ is None else {"providers": providers_}
    return monthly.MonthlyReport(
        month="2026-10",
        workspace="Acme",
        currency="USD",
        generated_at=datetime(2026, 11, 4, tzinfo=UTC),
        spend=monthly.Spend(
            usd=200.0,
            local=None,
            rate=None,
            previous_usd=100.0,
            previous_local=None,
            previous_rate=None,
            change_usd=100.0,
            change_percent=100.0,
            usage_effect=None,
            fx_effect=None,
            credits_usd=-20.0,
        ),
        drivers=[],
        budgets=[],
        savings=monthly.Savings(
            open_count=0,
            open_usd_month=0,
            new_count=0,
            done_count=0,
            done_usd_month=0,
            verified_count=1,
            realised_usd_month=4.0,
        ),
        top_recommendations=[],
        anomalies=monthly.AnomalySummary(count=0, impact_usd=0),
        credits="USD 50.00 of credits left.",
        accuracy=monthly.Accuracy(
            data_through=date(2026, 10, 31),
            reconciliation=None,
            difference_percent=None,
            checked_at=None,
            note="From the bills.",
        ),
        **extra,
    )


def test_monthly_report_wording_follows_the_workspace_providers() -> None:
    # A report stored before the field existed was AWS.
    old = monthly.MonthlyReport.model_validate(
        {k: v for k, v in _report(None).model_dump(by_alias=True).items() if k != "providers"}
    )
    assert old.providers == ["aws"]
    subject, text_, _ = monthly.render(old, APP)
    assert subject.startswith("Acme: AWS spend for October 2026 was USD 200.00")
    assert "AWS credits applied: USD 20.00." in text_
    assert "Verified in AWS this month: 1" in text_

    subject, text_, html_ = monthly.render(_report(["azure"]), APP)
    assert subject.startswith("Acme: Azure spend for October 2026")
    assert "Azure credits applied: USD 20.00." in text_
    assert "Verified in Azure this month: 1" in text_
    assert "AWS" not in subject + text_ + html_

    subject, text_, _ = monthly.render(_report(["aws", "gcp"]), APP)
    assert subject.startswith("Acme: cloud spend for October 2026")
    assert "Verified in your cloud accounts this month: 1" in text_
    assert "\nCredits\n" in text_


@pytest.mark.parametrize(
    ("provider", "account_type", "subject", "again"),
    [
        (None, "standalone", "Data for AWS account 123456789012 deleted", "bills in your S3 bucket"),
        ("gcp", "management", "Data for Google Cloud billing account 123456789012 deleted", "loads its bills again from Google Cloud"),
        ("azure", "standalone", "Data for Azure subscription 123456789012 deleted", "loads its bills again from Azure"),
        ("digitalocean", "standalone", "Data for DigitalOcean team 123456789012 deleted", "loads its bills again from DigitalOcean"),
    ],
)  # fmt: skip
def test_deletion_email_names_the_provider(
    provider: str | None, account_type: str, subject: str, again: str
) -> None:
    email = connection_deleted_email(
        ["a@example.com"], "123456789012", VERIFICATION, provider, account_type
    )
    assert email.subject == subject
    assert again in email.text
    if provider is not None:
        assert "S3" not in email.text and "AWS" not in email.text


def test_assignment_email_says_where_the_change_is_checked() -> None:
    def rec(provider: str | None) -> Any:
        return SimpleNamespace(
            id="r1", title="Delete the disk", savings_usd_month=4, provider=provider
        )

    _, text_, html_ = assignment_email("Ada", [rec("azure")], APP)
    assert text_.endswith("Cloudoptics then checks it in Azure.")
    assert "checks it in Azure.</p>" in html_
    _, text_, _ = assignment_email("Ada", [rec("aws"), rec("gcp")], APP)
    assert text_.endswith("checks it in your cloud accounts.")
    _, text_, _ = assignment_email(
        "Ada", [SimpleNamespace(id="r", title="t", savings_usd_month=1)], APP
    )
    assert text_.endswith("checks it in AWS.")


def test_support_access_email_is_not_aws_only() -> None:
    email = granted_email(
        ["a@example.com"],
        "Acme",
        "Sam",
        "Billing question",
        "T-1",
        datetime(2026, 11, 20, tzinfo=UTC),
    )
    assert "in your cloud accounts" in email.text and "AWS" not in email.text


def test_verified_note_names_the_recommendation_cloud() -> None:
    result = SimpleNamespace(code="gone")
    for provider, label in [
        ("aws", "AWS"),
        ("gcp", "Google Cloud"),
        ("digitalocean", "DigitalOcean"),
    ]:
        item = SimpleNamespace(status="done", provider=provider)
        assert _note(item, result, "verified").startswith(f"Checked in {label}.")  # type: ignore[arg-type]


def test_a_budget_can_cover_accounts_of_any_provider() -> None:
    from pydantic import ValidationError

    from cloudoptics.budgets.routes import Scope

    ids = ["123456789012", "my-project-1", "0b5e0f1c-0000-4000-8000-000000000001", "team-1"]
    assert Scope(type="accounts", accounts=ids).accounts == sorted(ids)
    with pytest.raises(ValidationError):
        Scope(type="accounts", accounts=["not an id"])


def test_exports_filter_by_an_account_of_any_provider() -> None:
    from pydantic import ValidationError

    from cloudoptics.exports.service import RecommendationsExport

    for account in ("123456789012", "my-project-1", "0b5e0f1c-0000-4000-8000-000000000001"):
        assert (
            RecommendationsExport(kind="recommendations", account_id=account).account_id == account
        )
    with pytest.raises(ValidationError):
        RecommendationsExport(kind="recommendations", account_id="1; DROP")


def test_deletion_email_lists_each_provider_it_had() -> None:
    from cloudoptics.privacy.removal import digitalocean_removal, gcp_removal
    from cloudoptics.privacy.service import StackSteps, scheduled_email

    removals = [
        gcp_removal(
            "012345-6789AB-CDEF01",
            export_project_id="billing-proj",
            export_dataset_id="billing_export",
            export_kind="focus_view",
            query_project_id="billing-proj",
            dataset_location="EU",
            projects=["app-1"],
            service_account="reader@cloudoptics.iam.gserviceaccount.com",
        ),
        digitalocean_removal("team-1"),
    ]
    when = datetime(2026, 11, 27, tzinfo=UTC)
    aws_only = scheduled_email(["a@example.com"], "Acme", when, "Ada", [])
    assert "Google Cloud" not in aws_only.text and "DigitalOcean" not in aws_only.text
    steps = [StackSteps("123456789012", "standalone", ["cloudoptics-access"])]
    email = scheduled_email(["a@example.com"], "Acme", when, "Ada", steps, removals)
    text_ = email.text
    assert "AWS account 123456789012: cloudoptics-access" in text_
    assert "Google Cloud billing account 012345-6789AB-CDEF01:" in text_
    assert "--location=EU" in text_ and "app-1" in text_
    assert "each dataset the view reads" in text_
    assert "DigitalOcean team team-1:" in text_
    assert "https://cloud.digitalocean.com/account/api/tokens" in text_
    assert "Azure" not in text_
    assert "<li>bq update --clear_label cloudoptics billing-proj:billing_export</li>" in email.html
