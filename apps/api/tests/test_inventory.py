"""Inventory scans: resources per account and region, access, deletions and jobs."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fake_aws import client_error
from httpx import AsyncClient
from test_ingestion import connected

from cloudoptics.connectors import service as connectors
from cloudoptics.inventory import scanner

LAUNCH = datetime(2026, 1, 5, tzinfo=UTC)


class FakeEc2:
    def __init__(self, region: str, world: "World") -> None:
        self.region = region
        self.world = world

    def describe_regions(self, **_: Any) -> dict[str, Any]:
        return {"Regions": [{"RegionName": r} for r in self.world.regions]}

    def describe_instances(self, **kw: Any) -> dict[str, Any]:
        # Two pages, to exercise NextToken.
        if "NextToken" not in kw:
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": f"i-{self.region}-1",
                                "InstanceType": "t3.micro",
                                "State": {"Name": "stopped"},
                                "LaunchTime": LAUNCH,
                                "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-attached"}}],
                                "Tags": [{"Key": "Name", "Value": "web"}],
                            }
                        ]
                    }
                ],
                "NextToken": "2",
            }
        return {"Reservations": []}

    def describe_volumes(self, **_: Any) -> dict[str, Any]:
        if self.region in self.world.broken_regions:
            raise client_error("RequestLimitExceeded")
        return {
            "Volumes": [
                {
                    "VolumeId": "vol-attached",
                    "VolumeType": "gp3",
                    "Size": 8,
                    "State": "in-use",
                    "Attachments": [{"InstanceId": f"i-{self.region}-1", "State": "attached"}],
                },
                {
                    "VolumeId": "vol-loose",
                    "VolumeType": "gp2",
                    "Size": 100,
                    "State": "available",
                    "CreateTime": LAUNCH,
                    "Tags": [{"Key": "team", "Value": "data"}],
                },
            ][: self.world.volumes]
        }

    def describe_snapshots(self, **kw: Any) -> dict[str, Any]:
        assert kw["OwnerIds"] == ["self"]
        return {
            "Snapshots": [
                {
                    "SnapshotId": "snap-1",
                    "VolumeSize": 100,
                    "State": "completed",
                    "StartTime": LAUNCH,
                    "Description": "x" * 500,
                }
            ]
        }

    def describe_images(self, **kw: Any) -> dict[str, Any]:
        assert kw["Owners"] == ["self"]
        return {"Images": [{"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami"}}, {}]}]}

    def describe_addresses(self, **_: Any) -> dict[str, Any]:
        return {
            "Addresses": [
                {"AllocationId": "eipalloc-free", "PublicIp": "203.0.113.5", "Domain": "vpc"},
                {"AllocationId": "eipalloc-used", "PublicIp": "203.0.113.6", "AssociationId": "a"},
            ]
        }


class FakeElbv2:
    def describe_load_balancers(self, **_: Any) -> dict[str, Any]:
        return {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": "arn:lb/app/idle",
                    "LoadBalancerName": "idle",
                    "Type": "application",
                    "State": {"Code": "active"},
                }
            ]
        }

    def describe_target_groups(self, **_: Any) -> dict[str, Any]:
        return {"TargetGroups": [{"TargetGroupArn": "arn:tg/1"}]}

    def describe_target_health(self, **_: Any) -> dict[str, Any]:
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "unhealthy"}}]}


class FakeElb:
    def describe_load_balancers(self, **_: Any) -> dict[str, Any]:
        return {
            "LoadBalancerDescriptions": [
                {"LoadBalancerName": "classic", "Instances": [{"InstanceId": "i-1"}]}
            ]
        }

    def describe_instance_health(self, **_: Any) -> dict[str, Any]:
        return {"InstanceStates": [{"State": "InService"}]}


class FakeRds:
    def describe_db_instances(self, **_: Any) -> dict[str, Any]:
        return {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "orders-db",
                    "DBInstanceArn": "arn:aws:rds:eu-west-1:1:db:orders-db",
                    "DBInstanceClass": "db.m5.large",
                    "DBInstanceStatus": "available",
                    "Engine": "postgres",
                    "MultiAZ": True,
                    "AllocatedStorage": 100,
                    "TagList": [{"Key": "env", "Value": "prod"}],
                }
            ]
        }


class FakeCloudWatch:
    def __init__(self, region: str, world: "World") -> None:
        self.region = region
        self.world = world

    def get_metric_data(self, **kw: Any) -> dict[str, Any]:
        if self.region in self.world.broken_regions:
            raise client_error("Throttling")
        self.world.metric_calls.append(kw)
        results = []
        start = kw["StartTime"]
        hours = int((kw["EndTime"] - start).total_seconds() // 3600)
        for q in kw["MetricDataQueries"]:
            stat = q["MetricStat"]
            name = stat["Metric"]["MetricName"]
            value = self.world.metric_values.get(name, 1.0)
            stamps = [start + timedelta(hours=h) for h in range(hours)]
            results.append({"Id": q["Id"], "Timestamps": stamps, "Values": [value] * len(stamps)})
        # Two pages, to exercise NextToken.
        if "NextToken" not in kw:
            return {"MetricDataResults": results[:1], "NextToken": "2"}
        return {"MetricDataResults": results[1:]}


class World:
    def __init__(self) -> None:
        self.regions = ["eu-west-1", "af-south-1"]
        self.broken_regions: set[str] = set()
        self.no_access: set[str] = set()
        self.volumes = 2
        self.assumed: list[str] = []
        self.metric_calls: list[dict[str, Any]] = []
        self.metric_values: dict[str, float] = {}


class FakeSession:
    def __init__(self, world: World) -> None:
        self.world = world

    def client(self, service: str, region: str = "us-east-1") -> Any:
        return {
            "ec2": lambda: FakeEc2(region, self.world),
            "elbv2": FakeElbv2,
            "elb": FakeElb,
            "rds": FakeRds,
            "cloudwatch": lambda: FakeCloudWatch(region, self.world),
        }[service]()


class FakeFactory:
    def __init__(self, world: World) -> None:
        self.world = world

    def assume(self, role_arn: str, external_id: str, name: str) -> FakeSession:
        account = role_arn.split(":")[4]
        self.world.assumed.append(role_arn)
        assert role_arn.endswith(":role/CloudopticsResourceReadOnly")
        assert name.endswith(("-inventory", "-metrics"))
        if account in self.world.no_access:
            raise client_error("AccessDenied", "AssumeRole")
        return FakeSession(self.world)


@pytest.fixture
def world():  # type: ignore[no-untyped-def]
    w = World()
    connectors.set_session_factory(FakeFactory(w))
    yield w
    connectors.set_session_factory(None)


async def setup(client: AsyncClient, login, owner_sql) -> tuple[str, str, str, dict[str, str]]:  # type: ignore[no-untyped-def]
    tenant, connection, headers = await connected(client, login, owner_sql)
    account = owner_sql("SELECT aws_account_id FROM cloud_connection WHERE id = %s", connection)[0][
        0
    ]
    owner_sql(
        "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, account_type) "
        "VALUES (gen_random_uuid(), %s, %s, %s, 'standalone'), "
        "(gen_random_uuid(), %s, %s, '555555555555', 'member')",
        tenant,
        connection,
        account,
        tenant,
        connection,
    )
    return tenant, connection, str(account), headers


async def test_scan_stores_resources_and_access(
    client: AsyncClient, login, owner_sql, world
) -> None:
    tenant, connection, account, headers = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    tid, cid = uuid.UUID(tenant), uuid.UUID(connection)

    assert await scanner.scan_connection(tid, cid) == "succeeded"
    rows = owner_sql(
        "SELECT region_id, resource_type, resource_id, name, state FROM resource "
        "WHERE connection_id = %s AND account_id = %s AND region_id = 'eu-west-1' "
        'ORDER BY resource_id COLLATE "C"',
        connection,
        account,
    )
    assert rows == [
        ("eu-west-1", "elb", "classic", None, "active"),
        ("eu-west-1", "eip", "eipalloc-free", None, "unassociated"),
        ("eu-west-1", "eip", "eipalloc-used", None, "associated"),
        ("eu-west-1", "ec2_instance", "i-eu-west-1-1", "web", "stopped"),
        ("eu-west-1", "elb", "idle", None, "idle"),
        ("eu-west-1", "rds_instance", "orders-db", None, "available"),
        ("eu-west-1", "ebs_snapshot", "snap-1", None, "completed"),
        ("eu-west-1", "ebs_volume", "vol-attached", None, "in-use"),
        ("eu-west-1", "ebs_volume", "vol-loose", None, "available"),
    ]
    config = owner_sql(
        "SELECT config, tags FROM resource WHERE resource_id = 'vol-loose' "
        "AND region_id = 'eu-west-1' AND connection_id = %s",
        connection,
    )[0]
    assert config[0]["sizeGiB"] == 100 and config[0]["volumeType"] == "gp2"
    assert config[1] == {"team": "data"}
    lb = owner_sql(
        "SELECT config FROM resource WHERE resource_id = 'idle' AND region_id = 'eu-west-1' "
        "AND connection_id = %s",
        connection,
    )[0][0]
    assert (lb["targets"], lb["healthyTargets"]) == (1, 0)

    access = dict(
        owner_sql(
            "SELECT aws_account_id, resource_access FROM cloud_account WHERE connection_id = %s",
            connection,
        )
    )
    assert access == {account: True, "555555555555": False}

    body = (await client.get(f"/v1/tenants/{tenant}/resources/summary", headers=headers)).json()
    assert body["lastScanAt"] is not None
    assert {c["resourceType"]: c["count"] for c in body["counts"]} == {
        "ebs_snapshot": 2,
        "ebs_volume": 4,
        "ec2_instance": 2,
        "eip": 4,
        "elb": 4,
        "rds_instance": 2,
    }


async def test_rescan_marks_missing_resources_but_not_in_failed_regions(
    client: AsyncClient, login, owner_sql, world
) -> None:
    tenant, connection, _, _ = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    tid, cid = uuid.UUID(tenant), uuid.UUID(connection)
    await scanner.scan_connection(tid, cid)

    world.volumes = 1  # vol-loose deleted everywhere ...
    world.broken_regions = {"af-south-1"}  # ... but af-south-1 fails to scan
    assert await scanner.scan_connection(tid, cid) == "partial"
    deleted = owner_sql(
        "SELECT region_id FROM resource WHERE connection_id = %s AND resource_id = 'vol-loose' "
        "AND deleted_at IS NOT NULL",
        connection,
    )
    assert deleted == [("eu-west-1",)]
    run = owner_sql(
        "SELECT status, error_code, error_detail FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'inventory.scan' ORDER BY started_at DESC LIMIT 1",
        connection,
    )[0]
    assert run[0] == "partial"
    assert "af-south-1: RequestLimitExceeded" in run[2]

    # Seen again: no longer deleted.
    world.volumes, world.broken_regions = 2, set()
    await scanner.scan_connection(tid, cid)
    assert owner_sql(
        "SELECT count(*) FROM resource WHERE connection_id = %s AND deleted_at IS NOT NULL",
        connection,
    ) == [(0,)]


async def test_no_access_anywhere_is_skipped(client: AsyncClient, login, owner_sql, world) -> None:
    tenant, connection, account, headers = await setup(client, login, owner_sql)
    world.no_access = {account, "555555555555"}
    assert await scanner.scan_connection(uuid.UUID(tenant), uuid.UUID(connection)) == "skipped"
    body = (await client.get(f"/v1/tenants/{tenant}/resources/summary", headers=headers)).json()
    assert body["counts"] == []

    owner_sql("UPDATE cloud_connection SET status = 'failed' WHERE id = %s", connection)
    assert await scanner.scan_connection(uuid.UUID(tenant), uuid.UUID(connection)) == "skipped"


async def test_discovery_queues_the_scan_and_poll_covers_usable_connections(
    client: AsyncClient, login, owner_sql, monkeypatch, world
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.connectors import jobs as connector_jobs
    from cloudoptics.inventory import jobs

    tenant, connection, _, _ = await setup(client, login, owner_sql)
    queued: list[str] = []

    async def fake_defer(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append(connection_id)

    monkeypatch.setattr(jobs, "defer_scan", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await connector_jobs.discover(tenant, connection) == "skipped"  # standalone
    assert queued == [connection]

    queued.clear()
    count = await jobs.poll(0)
    assert connection in queued and count == len(queued)


async def test_admins_scan_now_and_get_a_resource_quick_create_link(
    client: AsyncClient, login, owner_sql, monkeypatch, world
) -> None:
    from cloudoptics.platform.settings import get_settings

    tenant, connection, account, headers = await setup(client, login, owner_sql)
    base = f"/v1/tenants/{tenant}/connections/{connection}"
    world.no_access = {account, "555555555555"}
    r = await client.post(f"{base}/scan", headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "status": "skipped",
        "accounts": [
            {"awsAccountId": a, "access": False, "error": "AccessDenied", "rollout": None}
            for a in sorted([account, "555555555555"])
        ],
    }

    world.no_access = {"555555555555"}  # the resource stack is now in the account
    body = (await client.post(f"{base}/scan", headers=headers)).json()
    assert body["status"] == "succeeded"
    assert {a["awsAccountId"]: a["error"] for a in body["accounts"]} == {
        account: None,
        "555555555555": "AccessDenied",
    }
    accounts = (await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)).json()
    assert accounts["coverage"]["withResourceAccess"] == 1
    assert owner_sql("SELECT count(*) FROM recommendation WHERE tenant_id = %s", tenant)[0][0] > 0

    setup_body = (await client.get(f"{base}/setup", headers=headers)).json()
    assert setup_body["resourceQuickCreateUrl"] is None  # templates not in S3 in tests
    monkeypatch.setattr(get_settings(), "cfn_quick_create_enabled", True)
    url = (await client.get(f"{base}/setup", headers=headers)).json()["resourceQuickCreateUrl"]
    assert "stackName=cloudoptics-resource-access" in url
    assert "resource-role.yaml" in url and "param_CreateCurExport" not in url
    assert f"param_ExternalId={setup_body['externalId']}" in url.replace("%3D", "=")

    # A StackSet for member accounts is offered to management (payer) connections only.
    standalone = (await client.get(f"{base}/setup", headers=headers)).json()
    assert standalone["resourceStacksetQuickCreateUrl"] is None
    owner_sql("UPDATE cloud_connection SET account_type = 'management' WHERE id = %s", connection)
    payer = (await client.get(f"{base}/setup", headers=headers)).json()
    stackset = payer["resourceStacksetQuickCreateUrl"]
    assert "stackName=cloudoptics-resource-stackset" in stackset
    assert "resource-stackset.yaml" in stackset and "param_ResourceTemplateUrl=" in stackset
    assert payer["resourceStacksetDownloadUrl"].endswith("/resource-stackset.yaml")

    owner_sql("UPDATE cloud_connection SET status = 'failed' WHERE id = %s", connection)
    assert (await client.post(f"{base}/scan", headers=headers)).status_code == 409


async def test_queued_scan_shows_access_at_once(
    client: AsyncClient, login, owner_sql, monkeypatch, world
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.inventory import jobs
    from cloudoptics.platform.settings import get_settings

    tenant, connection, account, headers = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    queued: list[str] = []

    async def fake_defer(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append(connection_id)

    monkeypatch.setattr(jobs, "defer_scan", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    monkeypatch.setattr(get_settings(), "jobs_mode", "queue")
    r = await client.post(f"/v1/tenants/{tenant}/connections/{connection}/scan", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert queued == [connection]
    accounts = (await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)).json()
    assert {a["awsAccountId"]: a["resourceAccess"] for a in accounts["accounts"]} == {
        account: True,
        "555555555555": False,
    }
