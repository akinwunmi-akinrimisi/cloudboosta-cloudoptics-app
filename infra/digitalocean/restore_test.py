"""Restore test for the staging database (WBS OPS-8, docs/11g RB-11).

DigitalOcean's managed PostgreSQL takes a backup every day and keeps seven days, with
point-in-time restore. A restore always makes a NEW cluster, so this script:

1. restores the latest backup of ``cloudoptics-pg`` (or ``--backup`` one of the listed
   backups' timestamps) into a temporary cluster ``cloudoptics-pg-restore-test-<UTC time>``
   of the same size;
2. lets only this machine's address connect to it;
3. checks it: ``alembic current`` (against the code checked out here), row counts of every
   table, that the workspace and audit tables are not empty, and the audit-log hash chain of
   every workspace (``cloudoptics.audit.log.verify_chain``);
4. writes the result as JSON (``--output``) and deletes the temporary cluster, also when a
   step fails (unless ``--keep``).

Cost: the temporary cluster bills at the source cluster's hourly rate (db-s-1vcpu-1gb:
$0.02254/hour, Nov 2026) for the 30-60 minutes a run takes. Needs the owner's approval
before each run (docs/11g RB-11). Never run against production without a change ticket.

    uv run python infra/digitalocean/restore_test.py --plan          # read-only: what it would do
    uv run python infra/digitalocean/restore_test.py --output restore-test.json
    uv run python infra/digitalocean/restore_test.py --backup 2026-11-18T09:00:00Z --keep

A restore to an arbitrary point in time (not a backup's timestamp) is a console action
(docs/11g RB-11); this script restores whole daily backups.

Reads DIGITALOCEAN_TOKEN (needs database read/create/delete and view_credentials). Only the
temporary cluster is ever written to: the checks run in one transaction that is rolled back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provision

REPO = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO / "apps" / "api" / "alembic.ini"
RESTORE_PREFIX = f"{provision.DB_CLUSTER}-restore-test-"
ONLINE_TIMEOUT_SECONDS = 60 * 60
CONNECT_TIMEOUT_SECONDS = 5 * 60
# Tables that must hold rows in any restored staging database (``--allow-empty`` skips).
MUST_HAVE_ROWS = ("tenant", "app_user", "audit_log")

# The checks connect as the cluster owner (doadmin), which owns every table. Tables use
# FORCE ROW LEVEL SECURITY, which applies to the owner too, so the owner would see no rows.
# On the temporary copy only, and inside the check transaction (rolled back at the end), the
# FORCE flag is lifted so the owner reads every workspace's rows.
LIFT_FORCED_RLS = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relforcerowsecurity
  LOOP
    EXECUTE format('ALTER TABLE public.%I NO FORCE ROW LEVEL SECURITY', r.relname);
  END LOOP;
END $$
"""
# Exact row count of every table (partitions are counted in their parent).
TABLE_COUNTS = """
SELECT c.relname AS name,
       (xpath('/row/n/text()',
              query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname),
                           false, true, '')))[1]::text::bigint AS n
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY c.relname
"""
AUDIT_CHAINS = "SELECT DISTINCT tenant_id FROM audit_log"
NEWEST_AUDIT_ROW = "SELECT max(occurred_at) FROM audit_log"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    started_at: str
    source_cluster: str
    restore_cluster: str
    backup_created_at: str | None = None
    restore_seconds: float | None = None
    alembic_current: str | None = None
    alembic_head: str | None = None
    tables: dict[str, int] = field(default_factory=dict)
    audit_chains_verified: int = 0
    audit_chains_failed: int = 0
    newest_audit_row_at: str | None = None
    checks: list[Check] = field(default_factory=list)
    cluster_deleted: bool = False
    finished_at: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))
        print(f"  [{'pass' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def latest_backup(cluster_id: str) -> dict[str, Any]:
    backups = provision.call("GET", f"/databases/{cluster_id}/backups").get("backups") or []
    if not backups:
        sys.exit("The source cluster has no backups yet (the first runs within a day).")
    return max(backups, key=lambda b: b["created_at"])


def restore_body(
    source: dict[str, Any], name: str, project_id: str, backup_created_at: str
) -> dict[str, Any]:
    return {
        "name": name,
        "engine": source["engine"],
        "version": source["version"],
        "region": source["region"],
        "size": source["size"],
        "num_nodes": 1,
        "project_id": project_id,
        "tags": ["cloudoptics", "restore-test"],
        "backup_restore": {
            "database_name": source["name"],
            "backup_created_at": backup_created_at,
        },
    }


def public_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=30) as res:
        return str(res.read().decode().strip())


def wait_online(cluster_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + ONLINE_TIMEOUT_SECONDS
    while True:
        cluster: dict[str, Any] = provision.call("GET", f"/databases/{cluster_id}")["database"]
        if cluster["status"] == "online":
            return cluster
        if time.monotonic() > deadline:
            sys.exit(f"restore not online after {ONLINE_TIMEOUT_SECONDS // 60} minutes")
        print(f"  restore status: {cluster['status']} ...")
        time.sleep(30)


def owner_url(cluster: dict[str, Any]) -> str:
    conn = cluster["connection"]
    password = (
        conn.get("password")
        or provision.call("GET", f"/databases/{cluster['id']}/users/doadmin")["user"]["password"]
    )
    user = conn.get("user") or "doadmin"
    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{conn['host']}:{conn['port']}/{provision.DB_NAME}?sslmode=require"
    )


def alembic(command: str, url: str | None) -> str:
    env = {**os.environ, "CLOUDOPTICS_ENVIRONMENT": "local"}
    if url:
        env["CLOUDOPTICS_DATABASE_URL"] = url
    out = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), command],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return out.stdout.strip()


def revision_of(alembic_output: str) -> str:
    """First revision id in ``alembic current``/``heads`` output, e.g. '0034 (head)'."""
    for line in alembic_output.splitlines():
        if line.strip() and not line.startswith(("INFO", "WARN")):
            return line.split()[0]
    return ""


async def database_checks(url: str, result: Result, allow_empty: bool) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from cloudoptics.audit.log import verify_chain

    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        while True:  # a just-restored cluster can refuse connections for a minute or two
            try:
                async with engine.connect() as probe:
                    await probe.execute(text("SELECT 1"))
                break
            except Exception as exc:
                if time.monotonic() > deadline:
                    raise
                print(f"  waiting for the restored database to accept connections ({exc!r})")
                await asyncio.sleep(15)
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(text(LIFT_FORCED_RLS))
            rows = await session.execute(text(TABLE_COUNTS))
            result.tables = {r.name: int(r.n) for r in rows}
            result.check("tables restored", bool(result.tables), f"{len(result.tables)} tables")
            if not allow_empty:
                empty = [t for t in MUST_HAVE_ROWS if result.tables.get(t, 0) == 0]
                result.check("core tables hold rows", not empty, ", ".join(empty) or "")
            newest = (await session.execute(text(NEWEST_AUDIT_ROW))).scalar()
            result.newest_audit_row_at = newest.isoformat() if newest else None
            chains = [r[0] for r in await session.execute(text(AUDIT_CHAINS))]
            for tenant_id in chains:
                if await verify_chain(session, tenant_id):
                    result.audit_chains_verified += 1
                else:
                    result.audit_chains_failed += 1
            # Counts only: workspace IDs stay out of the record and the logs.
            result.check(
                "audit chains verify",
                result.audit_chains_failed == 0,
                f"{result.audit_chains_verified} verified, {result.audit_chains_failed} failed",
            )
            await session.rollback()  # nothing is kept, not even the lifted FORCE flags
    finally:
        await engine.dispose()


def delete_cluster(cluster_id: str, name: str) -> bool:
    if not name.startswith(RESTORE_PREFIX):  # never the source or anything else
        print(f"refusing to delete {name}: not a restore-test cluster")
        return False
    provision.call("DELETE", f"/databases/{cluster_id}")
    print(f"deleted temporary cluster {name}")
    return True


def run(args: argparse.Namespace) -> Result:
    source = provision.find_cluster()
    if source is None:
        sys.exit(f"source cluster {provision.DB_CLUSTER} not found")
    name = RESTORE_PREFIX + datetime.now(UTC).strftime("%Y%m%d%H%M")
    result = Result(started_at=now(), source_cluster=source["name"], restore_cluster=name)
    leftovers = [
        c["name"]
        for c in provision.call("GET", "/databases").get("databases") or []
        if c["name"].startswith(RESTORE_PREFIX)
    ]
    if leftovers:
        print(f"warning: earlier restore-test clusters still exist (billing): {leftovers}")
    backup = args.backup or latest_backup(source["id"])["created_at"]
    result.backup_created_at = backup
    if args.plan:
        shown = restore_body(source, name, "<Cloudoptics project>", backup)
        print("would create:", json.dumps(shown, indent=2))
        print(f"from the backup of {backup}; then check it and delete it")
        return result

    body = restore_body(source, name, provision.ensure_project(), backup)
    allow_ip = args.allow_ip or public_ip()
    started = time.monotonic()
    created = provision.call("POST", "/databases", body)["database"]
    print(f"restoring into {name} ({created['id']}); this takes several minutes")
    try:
        cluster = wait_online(created["id"])
        result.restore_seconds = round(time.monotonic() - started)
        provision.call(
            "PUT",
            f"/databases/{created['id']}/firewall",
            {"rules": [{"type": "ip_addr", "value": allow_ip}]},
        )
        print(f"restore online after {result.restore_seconds} s; only {allow_ip} may connect")
        url = owner_url(cluster)

        result.alembic_head = revision_of(alembic("heads", None))
        result.alembic_current = revision_of(alembic("current", url))
        result.check(
            "alembic current",
            bool(result.alembic_current),
            f"{result.alembic_current or 'none'} (code head {result.alembic_head})",
        )
        # psycopg's async mode needs a selector loop (the Windows default is proactor).
        asyncio.run(
            database_checks(url, result, args.allow_empty), loop_factory=asyncio.SelectorEventLoop
        )
    except (Exception, SystemExit) as exc:  # record the failure; the cluster is still deleted
        result.check("restore test ran to the end", False, repr(exc))
    finally:
        if args.keep:
            print(f"--keep: {name} is still running and billing. Delete it with:")
            print(f"  doctl databases delete {created['id']} --force")
        else:
            try:
                result.cluster_deleted = delete_cluster(created["id"], name)
            except BaseException:
                print(
                    f"DELETE FAILED: remove {name} by hand: doctl databases delete "
                    f"{created['id']} --force"
                )
                raise
        result.finished_at = now()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", action="store_true", help="read-only: show what would run")
    parser.add_argument("--backup", help="created_at of the backup to restore (default: latest)")
    parser.add_argument("--allow-ip", help="address allowed to connect (default: this machine)")
    parser.add_argument("--allow-empty", action="store_true", help="skip the non-empty check")
    parser.add_argument("--keep", action="store_true", help="do not delete the restored cluster")
    parser.add_argument("--output", type=Path, help="write the result as JSON here")
    args = parser.parse_args()
    result = run(args)
    if args.plan:
        return 0
    record = {**asdict(result), "ok": result.ok}
    if args.output:
        args.output.write_text(json.dumps(record, indent=2) + "\n")
        print(f"result written to {args.output}")
    print(f"restore test {'PASSED' if result.ok else 'FAILED'}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
