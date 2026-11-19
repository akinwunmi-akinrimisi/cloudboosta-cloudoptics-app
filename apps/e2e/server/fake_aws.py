"""A stand-in for every AWS API the API and its inline jobs call, for the E2E and load stacks.

Nothing here contacts AWS: ``FakeSessionFactory.assume`` never calls STS and each client
returns canned answers shaped like boto3's. One small account is described: a running idle
instance, an unattached gp2 volume, an old snapshot, an unused Elastic IP and an idle load
balancer in eu-west-1, so the first scan gives the rules (R1-R4, R10) something to find.
A call this file does not know answers ``AccessDenied``, as a role without that permission
would, so an unexpected call degrades a check instead of crashing a request.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

LAUNCHED = datetime(2026, 1, 5, tzinfo=UTC)
REGIONS = ["eu-west-1", "af-south-1"]
# Resources live in one region of one account (the fixture bill's payer); the other region
# and accounts are scanned and found empty.
HOME_REGION = "eu-west-1"
HOME_ACCOUNT = "111111111111"


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeClient:
    """Answers the named methods; anything else is ``AccessDenied``."""

    def __init__(self, service: str, methods: dict[str, Callable[..., Any]]) -> None:
        self._service = service
        self._methods = methods

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        method = self._methods.get(name)
        if method is None:

            def denied(**_: Any) -> Any:
                raise client_error("AccessDenied", f"{self._service}.{name}")

            return denied
        return method

    def get_paginator(self, name: str) -> Any:
        client = self

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                yield getattr(client, name)(**kwargs)

        return _Paginator()


def _const(value: Any) -> Callable[..., Any]:
    return lambda **_: value


# Cost Explorer's answer: unblended cost per billing month ("YYYY-MM") and linked account,
# the same bills the export folder holds (set by serve.py), so reconciliation finds a match.
BillTotals = dict[str, dict[str, float]]


def _cost_and_usage(totals: BillTotals) -> Callable[..., Any]:
    def answer(**kw: Any) -> Any:
        month = kw["TimePeriod"]["Start"][:7]
        groups = [
            {
                "Keys": [account],
                "Metrics": {"UnblendedCost": {"Amount": f"{cost:.6f}", "Unit": "USD"}},
            }
            for account, cost in sorted(totals.get(month, {}).items())
        ]
        if not kw.get("GroupBy"):
            return {"ResultsByTime": []}
        return {"ResultsByTime": [{"TimePeriod": kw["TimePeriod"], "Groups": groups}]}

    return answer


def _billing(account: str, totals: BillTotals) -> dict[str, FakeClient]:
    arn = f"arn:aws:bcm-data-exports:us-east-1:{account}:export/cloudoptics-cur2-e2e"
    export = {
        "Name": "cloudoptics-cur2",
        "DestinationConfigurations": {
            "S3Destination": {
                "S3Bucket": f"cloudoptics-cur-{account}",
                "S3Prefix": "cloudoptics",
                "S3Region": "us-east-1",
            }
        },
    }
    return {
        "ce": FakeClient(
            "ce",
            {
                "get_cost_and_usage": _cost_and_usage(totals),
                "get_savings_plans_coverage": _const({"SavingsPlansCoverages": []}),
                "get_savings_plans_utilization": _const({"Total": {}}),
            },
        ),
        "bcm-data-exports": FakeClient(
            "bcm-data-exports",
            {
                "list_exports": _const(
                    {"Exports": [{"ExportArn": arn, "ExportName": "cloudoptics-cur2"}]}
                ),
                "get_export": _const({"Export": export}),
            },
        ),
        "s3": FakeClient("s3", {"list_objects_v2": _const({"KeyCount": 3})}),
        "organizations": FakeClient(
            "organizations",
            {
                "describe_organization": _const({"Organization": {"MasterAccountId": account}}),
                "list_accounts": _const(
                    {"Accounts": [{"Id": account, "Name": "e2e-payer", "State": "ACTIVE"}]}
                ),
            },
        ),
        "savingsplans": FakeClient(
            "savingsplans", {"describe_savings_plans": _const({"savingsPlans": []})}
        ),
        "cloudformation": FakeClient("cloudformation", {"list_stack_instances": _no_stackset}),
    }


def _no_stackset(**_: Any) -> Any:
    raise client_error("StackSetNotFoundException", "ListStackInstances")


def _ec2(region: str, home: bool) -> FakeClient:

    def instances(**_: Any) -> Any:
        if not home:
            return {"Reservations": []}
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0e2eidle00000001",
                            "InstanceType": "m5.large",
                            "State": {"Name": "running"},
                            "LaunchTime": LAUNCHED,
                            "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-0e2eroot0001"}}],
                            "Tags": [
                                {"Key": "Name", "Value": "batch-runner"},
                                {"Key": "env", "Value": "dev"},
                            ],
                        }
                    ]
                }
            ]
        }

    def volumes(**_: Any) -> Any:
        if not home:
            return {"Volumes": []}
        return {
            "Volumes": [
                {
                    "VolumeId": "vol-0e2eroot0001",
                    "VolumeType": "gp3",
                    "Size": 8,
                    "State": "in-use",
                    "Attachments": [{"InstanceId": "i-0e2eidle00000001", "State": "attached"}],
                },
                {
                    "VolumeId": "vol-0e2eloose001",
                    "VolumeType": "gp2",
                    "Size": 500,
                    "State": "available",
                    "CreateTime": LAUNCHED,
                    "Tags": [{"Key": "team", "Value": "data"}],
                },
            ]
        }

    def snapshots(**_: Any) -> Any:
        if not home:
            return {"Snapshots": []}
        return {
            "Snapshots": [
                {
                    "SnapshotId": "snap-0e2eold00001",
                    "VolumeSize": 200,
                    "State": "completed",
                    "StartTime": LAUNCHED,
                    "Description": "nightly backup",
                }
            ]
        }

    def addresses(**_: Any) -> Any:
        if not home:
            return {"Addresses": []}
        return {
            "Addresses": [
                {"AllocationId": "eipalloc-0e2efree", "PublicIp": "203.0.113.10", "Domain": "vpc"}
            ]
        }

    return FakeClient(
        "ec2",
        {
            "describe_regions": _const({"Regions": [{"RegionName": r} for r in REGIONS]}),
            "describe_instances": instances,
            "describe_volumes": volumes,
            "describe_snapshots": snapshots,
            "describe_images": _const({"Images": []}),
            "describe_addresses": addresses,
            "describe_nat_gateways": _const({"NatGateways": []}),
            "describe_route_tables": _const({"RouteTables": []}),
            "describe_reserved_instances": _const({"ReservedInstances": []}),
        },
    )


def _elbv2(home: bool) -> FakeClient:
    return FakeClient(
        "elbv2",
        {
            "describe_load_balancers": _const(
                {
                    "LoadBalancers": [
                        {
                            "LoadBalancerArn": (
                                "arn:aws:elasticloadbalancing:eu-west-1:111111111111:"
                                "loadbalancer/app/e2e-idle/0123456789abcdef"
                            ),
                            "LoadBalancerName": "e2e-idle",
                            "Type": "application",
                            "State": {"Code": "active"},
                            "CreatedTime": LAUNCHED,
                        }
                    ]
                    if home
                    else []
                }
            ),
            "describe_target_groups": _const({"TargetGroups": []}),
            "describe_target_health": _const({"TargetHealthDescriptions": []}),
        },
    )


def _cloudwatch() -> FakeClient:
    """Hourly points at a low, steady value for every metric asked for."""

    def metric_data(**kw: Any) -> Any:
        start, end = kw["StartTime"], kw["EndTime"]
        hours = min(int((end - start).total_seconds() // 3600), 24 * 40)
        stamps = [start + timedelta(hours=h) for h in range(hours)]
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Timestamps": stamps, "Values": [0.5] * len(stamps)}
                for q in kw["MetricDataQueries"]
            ]
        }

    return FakeClient("cloudwatch", {"get_metric_data": metric_data})


def _resources(region: str, account: str) -> dict[str, Callable[[], FakeClient]]:
    home = region == HOME_REGION and account == HOME_ACCOUNT
    return {
        "ec2": lambda: _ec2(region, home),
        "elbv2": lambda: _elbv2(home),
        "elb": lambda: FakeClient(
            "elb", {"describe_load_balancers": _const({"LoadBalancerDescriptions": []})}
        ),
        "rds": lambda: FakeClient(
            "rds",
            {
                "describe_db_instances": _const({"DBInstances": []}),
                "describe_reserved_db_instances": _const({"ReservedDBInstances": []}),
            },
        ),
        "cloudwatch": _cloudwatch,
        "compute-optimizer": lambda: FakeClient("compute-optimizer", {}),
    }


class FakeSession:
    def __init__(self, account: str, totals: BillTotals) -> None:
        self._account = account
        self._totals = totals

    def client(self, service: str, region: str = "us-east-1") -> FakeClient:
        billing = _billing(self._account, self._totals)
        if service in billing:
            return billing[service]
        make = _resources(region, self._account).get(service)
        return make() if make else FakeClient(service, {})


class FakeSessionFactory:
    """Drop-in for ``cloudoptics.connectors.aws.SessionFactory``: every role can be assumed."""

    def __init__(self, totals: BillTotals | None = None) -> None:
        self.totals = totals or {}

    def assume(self, role_arn: str, external_id: str, name: str) -> FakeSession:
        return FakeSession(role_arn.split(":")[4], self.totals)
