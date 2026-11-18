"""The optional scheduler stack R7 offers (infra/cloudformation/scheduler), which customers
deploy themselves: its permissions, and its function with fake AWS clients."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import types
from typing import Any

import pytest
from test_access_stack import Fake, _actions
from test_cloudformation import TEMPLATES, load

from cloudoptics.analysis import schedule as rule

SOURCE = TEMPLATES / "scheduler" / "scheduler.py"
TEMPLATE = "scheduler/cloudoptics-scheduler.yaml"
UTC = dt.UTC
# Monday 17 November 2026, in UTC (Lagos is UTC+1, all year).
MONDAY = dt.datetime(2026, 11, 16, tzinfo=UTC)
LOOKBACK = dt.timedelta(minutes=16)


@pytest.fixture
def scheduler() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("cloudoptics_scheduler", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def at(hour: int, minute: int = 0, days: int = 0) -> dt.datetime:
    return MONDAY + dt.timedelta(days=days, hours=hour, minutes=minute)


def test_template_carries_the_source_and_matches_the_rule() -> None:
    template = load(TEMPLATE)
    code = template["Resources"]["SchedulerFunction"]["Properties"]["Code"]
    assert code["ZipFile"] == SOURCE.read_text(), "run infra/cloudformation/scheduler/embed.py"
    assert template["Outputs"]["TagKey"]["Value"] == rule.SCHEDULE_TAG
    assert rule.TEMPLATE_PATH == TEMPLATE


def test_function_only_starts_and_stops_tagged_resources() -> None:
    resources = load(TEMPLATE)["Resources"]
    [policy] = resources["SchedulerRole"]["Properties"]["Policies"]
    writes = set()
    for stmt in policy["PolicyDocument"]["Statement"]:
        if "If" in stmt:  # Fn::If [condition, statement, NoValue]
            stmt = stmt["If"][1]
        assert stmt["Effect"] == "Allow"
        for action in _actions(stmt):
            service, verb = action.split(":")
            assert service not in {"iam", "sts", "ssm", "secretsmanager"}, action
            if verb.startswith(("Start", "Stop")):
                writes.add(action)
                tag = f"aws:ResourceTag/{rule.SCHEDULE_TAG}"
                assert stmt["Condition"] == {"Null": {tag: "false"}}, action
            else:
                assert action in {
                    "ec2:DescribeInstances",
                    "rds:DescribeDBInstances",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "kms:CreateGrant",
                }, action
    assert writes == {
        "ec2:StartInstances",
        "ec2:StopInstances",
        "rds:StartDBInstance",
        "rds:StopDBInstance",
    }
    [trust] = resources["ScheduleRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert resources["Schedule"]["Properties"]["FlexibleTimeWindow"] == {"Mode": "OFF"}


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("07:00-20:00;MO-FR;Africa/Lagos", True),
        ("07:00-20:00 MO-FR Africa/Lagos", True),
        ("8:30-18:00; mo,we,fr ;Africa/Nairobi", True),
        ("22:00-06:00;FR-MO;Africa/Johannesburg", True),
        ("07:00-24:00;SA-SU;UTC", True),
        ("07:00-20:00;MO-FR;Mars/Olympus", False),
        ("07:00-20:00;MO-XX;Africa/Lagos", False),
        ("07:00-07:00;MO-FR;Africa/Lagos", False),
        ("25:00-20:00;MO-FR;Africa/Lagos", False),
        ("07:00-20:00;MO-FR", False),
        ("", False),
        (None, False),
    ],
)
def test_tag_values_it_reads(scheduler: types.ModuleType, value: str | None, ok: bool) -> None:
    assert (scheduler.parse(value) is not None) is ok


def test_weekday_office_hours_in_the_tenant_time_zone(scheduler: types.ModuleType) -> None:
    s = scheduler.parse("07:00-20:00;MO-FR;Africa/Lagos")
    assert not s.wants_running(at(5, 59))  # 06:59 in Lagos
    assert s.wants_running(at(6))  # 07:00
    assert s.wants_running(at(18, 59))  # 19:59
    assert not s.wants_running(at(19))  # 20:00
    assert not s.wants_running(at(10, days=5))  # Saturday

    overnight = scheduler.parse("22:00-06:00;FR;UTC")
    assert overnight.wants_running(at(23, days=4))  # Friday night
    assert overnight.wants_running(at(5, days=5))  # Saturday early morning
    assert not overnight.wants_running(at(7, days=5))
    assert not overnight.wants_running(at(23, days=3))  # Thursday


def test_acts_at_the_changes_unless_enforced(scheduler: types.ModuleType) -> None:
    s = scheduler.parse("07:00-20:00;MO-FR;Africa/Lagos")
    assert scheduler.action(s, at(6, 5), LOOKBACK, False) == "start"
    assert scheduler.action(s, at(19, 10), LOOKBACK, False) == "stop"
    assert scheduler.action(s, at(12), LOOKBACK, False) is None
    assert scheduler.action(s, at(12), LOOKBACK, True) == "start"
    assert scheduler.action(s, at(23), LOOKBACK, True) == "stop"


def _ec2(*instances: tuple[str, str, str | None], **answers: Any) -> Fake:
    reservations = [
        {
            "Instances": [
                {
                    "InstanceId": i,
                    "State": {"Name": state},
                    "Tags": [] if value is None else [{"Key": rule.SCHEDULE_TAG, "Value": value}],
                }
            ]
        }
        for i, state, value in instances
    ]
    return Fake(describe_instances={"Reservations": reservations}, **answers)


def _rds(*dbs: dict[str, Any], **answers: Any) -> Fake:
    return Fake(describe_db_instances={"DBInstances": list(dbs)}, **answers)


def _db(name: str, status: str, value: str, **extra: Any) -> dict[str, Any]:
    tags = [{"Key": rule.SCHEDULE_TAG, "Value": value}]
    return {"DBInstanceIdentifier": name, "DBInstanceStatus": status, "TagList": tags, **extra}


def test_run_stops_at_the_end_of_the_day(scheduler: types.ModuleType) -> None:
    lagos = "07:00-20:00;MO-FR;Africa/Lagos"
    ec2 = _ec2(("i-1", "running", lagos), ("i-2", "stopped", lagos), ("i-3", "running", "bad"))
    rds = _rds(
        _db("dev-db", "available", "07:00-20:00 MO-FR Africa/Lagos"),
        _db("aurora-1", "available", lagos, DBClusterIdentifier="c"),
        {"DBInstanceIdentifier": "untagged", "DBInstanceStatus": "available"},
        _db("bad-db", "available", "whenever"),
    )
    result = scheduler.run(ec2, rds, at(19, 5), LOOKBACK, False)
    assert result == {
        "started": [],
        "stopped": ["i-1", "dev-db"],
        "invalid": ["i-3", "bad-db"],
        "failed": [],
    }
    assert ("stop_instances", {"InstanceIds": ["i-1"]}) in ec2.calls
    assert ("stop_db_instance", {"DBInstanceIdentifier": "dev-db"}) in rds.calls
    filters = next(kw for name, kw in ec2.calls if name == "describe_instances")["Filters"]
    assert filters[0] == {"Name": "tag-key", "Values": [rule.SCHEDULE_TAG]}


def test_run_starts_in_the_morning_and_reports_failures(scheduler: types.ModuleType) -> None:
    from botocore.exceptions import ClientError

    lagos = "07:00-20:00;MO-FR;Africa/Lagos"
    denied = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "StartInstances")
    ec2 = _ec2(("i-1", "stopped", lagos), start_instances=denied)
    rds = _rds(_db("dev-db", "stopped", lagos), _db("qa-db", "stopped", lagos))
    result = scheduler.run(ec2, rds, at(6, 5), LOOKBACK, False)
    assert result["failed"] == ["i-1: UnauthorizedOperation"]
    assert result["started"] == ["dev-db", "qa-db"]

    # Mid-morning, nothing changes unless the schedule is enforced.
    ec2 = _ec2(("i-1", "stopped", lagos))
    assert scheduler.run(ec2, _rds(), at(10), LOOKBACK, False)["started"] == []
    assert scheduler.run(ec2, _rds(), at(10), LOOKBACK, True)["started"] == ["i-1"]

    rds = _rds(
        _db("dev-db", "available", lagos),
        stop_db_instance=ClientError({"Error": {"Code": "InvalidDBInstanceState"}}, "Stop"),
    )
    assert scheduler.run(_ec2(), rds, at(19, 5), LOOKBACK, False)["failed"] == [
        "dev-db: InvalidDBInstanceState"
    ]


def test_handler_uses_the_stack_settings(
    scheduler: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clients = {"ec2": _ec2(), "rds": _rds()}
    monkeypatch.setattr(scheduler.boto3, "client", lambda name, **_: clients[name])
    monkeypatch.setenv("INTERVAL_MINUTES", "5")
    monkeypatch.setenv("ENFORCE", "true")
    seen: dict[str, Any] = {}
    real = scheduler.run

    def spy(ec2: Any, rds: Any, now: Any, lookback: Any, enforce: Any) -> Any:
        seen.update(lookback=lookback, enforce=enforce)
        return real(ec2, rds, now, lookback, enforce)

    monkeypatch.setattr(scheduler, "run", spy)
    result = scheduler.handler({}, None)
    assert seen == {"lookback": dt.timedelta(minutes=6), "enforce": True}
    assert json.loads(capsys.readouterr().out) == result
