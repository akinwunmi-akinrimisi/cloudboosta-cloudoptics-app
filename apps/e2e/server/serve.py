"""Test-only API for the E2E suite and the smoke load test: the real app, fake AWS.

    uv run python apps/e2e/server/serve.py            # from the repository root

What it does, in order:

1. Refuses to start unless ``CLOUDOPTICS_E2E=1``, the environment is ``test``, sign-in is the
   development token and jobs run inline. The API itself refuses dev sign-in in staging and
   production, and this file is not in the API image (apps/api/Dockerfile copies apps/api
   only), so it cannot run there.
2. Drops and recreates a throwaway database whose name must end in ``_e2e``, migrates it
   and gives the app role a login, as apps/api/tests/conftest.py does.
3. Writes two months of synthetic Cost and Usage Report (tools/cur-fixture) into a folder
   laid out like the export bucket: last month and this month.
4. Swaps the AWS session factory for ``fake_aws.FakeSessionFactory`` and the bill source for
   that folder, and points the Google Cloud plug-in at the fake Google Cloud
   (apps/api/tests/fake_gcp.py) with two months of synthetic FOCUS rows. Two test-only routes
   stand in for what the customer does in Google Cloud: ``POST /__e2e/gcp/billing-account``
   (a fresh billing account for the test's export) and ``POST /__e2e/gcp/label`` (the
   ``bq update --set_label`` command). The DigitalOcean API is the fake in
   ``apps/api/tests/fake_digitalocean.py``. Then it serves the app with uvicorn on 127.0.0.1.
   ``bq update --set_label`` command). Every Azure call goes to the fake Azure
   (apps/api/tests/fake_azure.py, ADR-020); ``/e2e/azure`` and ``/e2e/azure/tag`` stand in for
   the customer there. Then it serves the app with uvicorn on 127.0.0.1.

Every workspace's connection reads the same bills; the stack callback, validation, the
resource scan, metrics and analysis all run for real against the fakes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

DEFAULTS = {
    "CLOUDOPTICS_ENVIRONMENT": "test",
    "CLOUDOPTICS_AUTH_MODE": "dev",
    "CLOUDOPTICS_JOBS_MODE": "inline",
    "CLOUDOPTICS_E2E_ADMIN_URL": "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    "CLOUDOPTICS_E2E_DATABASE": "cloudoptics_e2e",
    "CLOUDOPTICS_CORS_ORIGINS": '["http://127.0.0.1:4173", "http://localhost:4173"]',
    "CLOUDOPTICS_APP_URL": "http://127.0.0.1:4173/app",
    "CLOUDOPTICS_API_PUBLIC_URL": "http://127.0.0.1:8000",
    # Staff who may set exchange rates: the suite's global setup signs in as this address.
    "CLOUDOPTICS_PLATFORM_ADMIN_EMAILS": "fx-staff@example.com",
    # Every test signs in from 127.0.0.1, so the per-address limit on the public routes is
    # raised; the per-user limit is lowered so the rate-limit test reaches it quickly.
    "CLOUDOPTICS_RATE_LIMIT_PUBLIC_PER_MINUTE": "100000",
    "CLOUDOPTICS_RATE_LIMIT_CALLBACK_PER_MINUTE": "100000",
    "CLOUDOPTICS_RATE_LIMIT_TENANT_PER_MINUTE": "100000",
    "CLOUDOPTICS_RATE_LIMIT_USER_PER_MINUTE": "240",
    "CLOUDOPTICS_LOG_LEVEL": "WARNING",
}
# Same throwaway password as the pytest suite: the role is shared by every database on a
# developer's PostgreSQL, so both must agree.
APP_PASSWORD = "app-test-password"  # noqa: S105 - local throwaway database


class RefusedError(SystemExit):
    pass


def configure() -> dict[str, str]:
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    env = os.environ
    if env.get("CLOUDOPTICS_E2E") != "1":
        raise RefusedError("refusing to start: set CLOUDOPTICS_E2E=1 (test-only server)")
    if env["CLOUDOPTICS_ENVIRONMENT"] != "test":
        raise RefusedError("refusing to start: CLOUDOPTICS_ENVIRONMENT must be 'test'")
    if env["CLOUDOPTICS_AUTH_MODE"] != "dev" or env["CLOUDOPTICS_JOBS_MODE"] != "inline":
        raise RefusedError("refusing to start: needs dev sign-in and inline jobs")
    database = env["CLOUDOPTICS_E2E_DATABASE"]
    if not database.endswith("_e2e") or not database.replace("_", "").isalnum():
        raise RefusedError("refusing to start: the E2E database name must end in '_e2e'")
    admin = env["CLOUDOPTICS_E2E_ADMIN_URL"]
    base, _ = admin.rsplit("/", 1)
    host = base.split("://", 1)[1].rsplit("@", 1)[-1]
    owner = base.replace("postgresql://", "postgresql+psycopg://", 1) + f"/{database}"
    env["CLOUDOPTICS_DATABASE_URL"] = owner
    env["CLOUDOPTICS_APP_DATABASE_URL"] = (
        f"postgresql+psycopg://cloudoptics_app:{APP_PASSWORD}@{host}/{database}"
    )
    return {"admin": admin, "database": database, "owner": owner}


def reset_database(admin_url: str, database: str, owner_url: str) -> None:
    import psycopg
    from alembic import command
    from alembic.config import Config
    from psycopg import sql

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    cfg = Config(str(REPO / "apps" / "api" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "apps" / "api" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", owner_url)
    command.upgrade(cfg, "head")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER ROLE cloudoptics_app LOGIN PASSWORD {}").format(
                sql.Literal(APP_PASSWORD)
            )
        )


def write_bills(root: Path, today: datetime) -> dict[str, dict[str, float]]:
    """Last month and this month, as AWS would deliver them to the export bucket.

    Returns the unblended cost per month and account, for the fake Cost Explorer.
    """
    from cur_fixture import BillingPeriod, generate, write_export

    this = BillingPeriod(today.year, today.month)
    last = (
        BillingPeriod(today.year - 1, 12)
        if today.month == 1
        else BillingPeriod(today.year, today.month - 1)
    )
    totals: dict[str, dict[str, float]] = {}
    for period in (last, this):
        fixture = generate(period)
        write_export(fixture, root / "cloudoptics")
        month = totals.setdefault(f"{period.year:04d}-{period.month:02d}", {})
        rows = fixture.table.select(["line_item_usage_account_id", "line_item_unblended_cost"])
        for account, cost in zip(*(rows.column(i).to_pylist() for i in range(2)), strict=True):
            month[account] = month.get(account, 0.0) + (cost or 0.0)
    return totals


def install_fakes(bills: Path, totals: dict[str, dict[str, float]]) -> None:
    from fake_aws import FakeSessionFactory

    from cloudoptics.connectors import service as connectors
    from cloudoptics.ingestion import service as ingestion
    from cloudoptics.ingestion.source import LocalExportSource

    class BucketFolder(LocalExportSource):
        """Keys with forward slashes on every platform, as S3 has them."""

        def list_keys(self, prefix: str) -> list[str]:
            return sorted(
                p.relative_to(self._root).as_posix()
                for p in self._root.rglob("*")
                if p.is_file() and p.relative_to(self._root).as_posix().startswith(prefix)
            )

    connectors.set_session_factory(FakeSessionFactory(totals))
    ingestion.set_source_factory(lambda conn, tenant_id: BucketFolder(bills))
    # DigitalOcean (ADR-021): any well-formed token reads a team of its own with the same
    # synthetic invoices and resources (apps/api/tests/fake_digitalocean.py).
    sys.path.insert(0, str(REPO / "apps" / "api" / "tests"))
    from fake_digitalocean import FakeDigitalOcean

    from cloudoptics.providers.digitalocean import client as digitalocean

    digitalocean.set_transport(FakeDigitalOcean(accept_any=True).transport())


def install_gcp(app: object) -> None:
    """The fake Google Cloud, and the routes a test uses to act as the customer there."""
    sys.path.insert(0, str(REPO / "apps" / "api" / "tests"))
    from fake_gcp import DATASET, EXPORT_PROJECT, FakeGcp, install, new_billing_account
    from fastapi import Body

    fake = FakeGcp().bill().grant_all()
    install(fake)

    def billing_account() -> dict[str, str]:
        fake.billing_account = new_billing_account()
        fake.bill()
        return {"billingAccountId": fake.billing_account, "project": EXPORT_PROJECT,
                "dataset": DATASET}  # fmt: skip

    def label(key: str = Body(), value: str = Body()) -> dict[str, str]:
        fake.set_label(EXPORT_PROJECT, DATASET, key, value)
        return {"status": "labelled"}

    app.add_api_route("/__e2e/gcp/billing-account", billing_account, methods=["POST"])  # type: ignore[attr-defined]
    app.add_api_route("/__e2e/gcp/label", label, methods=["POST"])  # type: ignore[attr-defined]


def install_azure_fake(today: datetime) -> Any:
    """The fake Azure (apps/api/tests/fake_azure.py, ADR-020): one customer directory and
    subscription that has consented, given the roles and created the FOCUS export, with last
    month's and this month's runs delivered. Only the ownership tag is left: the spec sets it
    through ``/e2e/azure/tag`` with the code the page shows, as the customer's script would."""
    sys.path.insert(0, str(REPO / "apps" / "api" / "tests"))
    from datetime import timedelta

    import fake_azure

    fake, _ = fake_azure.install()
    fake.grant_all()
    fake.add_storage(None)
    fake.add_export()
    fake.add_resources(today)
    this = today.date().replace(day=1)
    last = (this - timedelta(days=1)).replace(day=1)
    fake.deliver(last, this - timedelta(days=1))
    fake.deliver(this, max(this, today.date() - timedelta(days=1)))
    return fake


def add_azure_routes(app: Any, fake: Any) -> None:
    """Test-only routes for the Azure spec (this server never runs outside the E2E stack)."""
    import fake_azure
    from fastapi import Body

    @app.get("/e2e/azure", include_in_schema=False)
    async def azure_customer() -> dict[str, str]:
        return {
            "directoryId": fake_azure.DIRECTORY,
            "subscriptionId": fake.subscription,
            "storageAccount": fake_azure.STORAGE,
            "container": fake_azure.CONTAINER,
            "exportPath": fake_azure.FOLDER,
        }

    @app.post("/e2e/azure/tag", include_in_schema=False)
    async def azure_tag(code: str = Body(embed=True)) -> dict[str, str]:
        fake.storage[fake_azure.STORAGE].tags["cloudoptics-verify"] = code
        return {"status": "tagged"}


def main() -> None:
    urls = configure()
    sys.path.insert(0, str(HERE))
    reset_database(urls["admin"], urls["database"], urls["owner"])
    bills = Path(os.environ.get("CLOUDOPTICS_E2E_BILLS", str(HERE.parent / ".bills")))
    if bills.exists():
        import shutil

        shutil.rmtree(bills)
    install_fakes(bills, write_bills(bills, datetime.now(UTC)))
    azure = install_azure_fake(datetime.now(UTC))

    import uvicorn

    from cloudoptics.main import create_app

    port = int(os.environ.get("CLOUDOPTICS_E2E_PORT", "8000"))
    app = create_app()
    install_gcp(app)
    add_azure_routes(app, azure)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    # psycopg's async driver needs a selector event loop (Windows defaults to proactor).
    asyncio.run(uvicorn.Server(config).serve(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
