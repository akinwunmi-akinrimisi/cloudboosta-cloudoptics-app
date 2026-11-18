"""R7 non-production scheduling (PRD FR-ANA-07), on hand-built inputs and end to end."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from test_compute_rules import (
    NOW,
    context,
    database,
    fill,
    quiet_database,
    quiet_server,
    resource,
    run,
    server,
)
from test_inventory import world  # noqa: F401 - fixture
from test_metrics import scanned

from cloudoptics.analysis import engine, schedule
from cloudoptics.analysis.rules import Context, Resource
from cloudoptics.analysis.settings import RuleSettings, pattern_problem, window_problem
from cloudoptics.metrics import puller

OFF_MONTH = (168 - 65) * 730 / 168  # weekdays 07:00-20:00 is 65 of 168 hours


def busy(ctx: Context, r: Resource, days: int = 20, metrics: tuple[str, ...] = ()) -> None:
    """Running every hour, and not idle."""
    for metric in ("cpu", "cpu_max", *metrics):
        fill(ctx, r, metric, 30.0, days)
    fill(ctx, r, "network_in", 50e6, days)
    fill(ctx, r, "network_out", 50e6, days)


def staging_server(**tags: str) -> Resource:
    return server("m5.large", Environment="staging", **tags)


def test_tagged_server_running_all_week_gets_an_office_hours_schedule() -> None:
    ctx = context()
    ctx.time_zone = "Africa/Nairobi"
    ctx.scheduler_template_url = "https://example.test/scheduler/cloudoptics-scheduler.yaml"
    r = staging_server()
    busy(ctx, r)
    [c] = run(schedule.schedule_instances, ctx, r)
    assert c.rule_id == "R7_schedule_ec2" and c.type == "schedule_instance"
    assert c.cost_now == pytest.approx(0.096 * 730)
    assert c.savings == pytest.approx(0.096 * OFF_MONTH)
    assert c.confidence == "high"  # tagged, and on around the clock for 14 days
    assert c.restart_required and c.reversibility == "reversible" and c.effort == "M"
    assert "07:00-20:00 weekdays, Nairobi time" in c.title
    value = "07:00-20:00;MO-FR;Africa/Nairobi"
    assert c.recommended_config["schedule"] == value
    assert c.fix["schedule"] == {
        "tagKey": "cloudoptics:schedule",
        "tagValue": value,
        "start": "07:00",
        "end": "20:00",
        "days": "MO-FR",
        "timeZone": "Africa/Nairobi",
        "hoursOnPerWeek": 65.0,
        "stackName": "cloudoptics-scheduler",
        "templateUrl": ctx.scheduler_template_url,
        "quickCreateUrl": None,
        "rdsAutoStart": False,
    }
    assert c.fix["steps"] == [
        "aws ec2 create-tags --resources i-1 --tags "
        '\'[{"Key": "cloudoptics:schedule", "Value": "07:00-20:00;MO-FR;Africa/Nairobi"}]\' '
        "--region eu-west-1"
    ]
    assert c.fix["rollback"][0] == (
        "aws ec2 delete-tags --resources i-1 --tags Key=cloudoptics:schedule --region eu-west-1"
    )
    assert value in c.console_steps[-1]
    assert "schedule_saves_compute_only" in c.confidence_notes
    evidence = {e["kind"]: e for e in c.evidence}
    assert evidence["nonProduction"]["value"] == "tag Environment=staging"
    assert evidence["runningHoursPercent"] == {
        "kind": "runningHoursPercent",
        "value": 100.0,
        "days": 14,
    }
    assert evidence["hoursOffPerMonth"]["value"] == pytest.approx(OFF_MONTH, abs=0.1)
    scored = engine.score(c)
    assert scored.risk_level == "low" and scored.risk_score == 15


def test_named_only_is_less_certain_and_riskier() -> None:
    ctx = context()
    r = replace(server("m5.large"), name="payments-dev-01")
    busy(ctx, r, days=10)  # only 10 days of history
    [c] = run(schedule.schedule_instances, ctx, r)
    assert c.confidence == "low"
    assert {e["kind"]: e["value"] for e in c.evidence}["nonProduction"] == "name payments-dev-01"
    assert engine.score(c).risk_level == "medium"  # out-of-hours stop + guessed from the name

    ctx = context()
    busy(ctx, r)
    assert run(schedule.schedule_instances, ctx, r)[0].confidence == "medium"


@pytest.mark.parametrize(
    "change",
    [
        "production",
        "no_match",
        "already_scheduled",
        "instance_scheduler",
        "autoscaling",
        "spot",
        "stopped",
        "off_at_night",
        "missing_day",
        "unpriced",
    ],
)
def test_servers_that_are_not_scheduled(change: str) -> None:
    ctx = context()
    tags: dict[str, str] = {
        "already_scheduled": {"cloudoptics:schedule": "07:00-20:00;MO-FR;Africa/Lagos"},
        "instance_scheduler": {"Schedule": "office-hours"},
        "autoscaling": {"aws:autoscaling:groupName": "web"},
    }.get(change, {})
    r = staging_server(**tags)
    if change == "production":
        r = replace(server(Environment="prod"), name="api-dev")
    if change == "no_match":
        r = replace(server(Environment="shared"), name="payments")
    if change == "spot":
        r = replace(r, config={**r.config, "lifecycle": "spot"})
    if change == "stopped":
        r = replace(r, state="stopped")
    if change == "unpriced":
        r = replace(r, config={**r.config, "instanceType": "x9.large"})
    busy(ctx, r)
    if change == "off_at_night":
        for day in ctx.usage[r.id]["cpu"].values():
            day[0:6] = [None] * 6
    if change == "missing_day":
        del ctx.usage[r.id]["cpu"][NOW.date() - timedelta(days=2)]
    assert run(schedule.schedule_instances, ctx, r) == []


def test_idle_servers_and_databases_are_left_to_r4() -> None:
    ctx = context()
    r = staging_server()
    quiet_server(ctx, r)
    fill(ctx, r, "cpu", 1.0, 33)
    assert run(schedule.schedule_instances, ctx, r) == []
    found = {s.candidate.rule_id for s in engine.evaluate(ctx, [r])}
    assert "R4_idle_ec2" in found and "R7_schedule_ec2" not in found

    # With R4 turned off, the schedule is the best that is left.
    ctx.settings = RuleSettings(disabled_rules=frozenset({"R4_idle_ec2"}))
    assert [c.rule_id for c in run(schedule.schedule_instances, ctx, r)] == ["R7_schedule_ec2"]

    ctx = context()
    db = replace(database(), tags={"env": "dev"})
    quiet_database(ctx, db)
    assert run(schedule.schedule_databases, ctx, db) == []


def test_tagged_database_gets_a_schedule_with_the_seven_day_caveat() -> None:
    ctx = context()
    db = replace(database(), tags={"env": "QA"}, arn="arn:aws:rds:eu-west-1:1:db:orders")
    busy(ctx, db, metrics=("connections", "read_iops", "write_iops"))
    [c] = run(schedule.schedule_databases, ctx, db)
    assert c.rule_id == "R7_schedule_rds" and c.type == "schedule_database"
    assert c.cost_now == pytest.approx(0.178 * 2 * 730)  # Multi-AZ
    assert c.savings == pytest.approx(0.178 * 2 * OFF_MONTH)
    # RDS tag values may not contain ";": spaces instead.
    assert c.fix["schedule"]["tagValue"] == "07:00-20:00 MO-FR Africa/Lagos"
    assert c.fix["schedule"]["rdsAutoStart"] is True
    assert "arn:aws:rds:eu-west-1:1:db:orders" in c.fix["steps"][0]
    assert "start-db-instance" in c.fix["rollback"][1]
    assert "storage" in c.fix["notes"]
    assert {"rds_restarts_after_7_days", "storage_not_included"} <= set(c.confidence_notes)
    assert engine.score(c).risk_score == 25  # stop + database

    for config in ({"clusterId": "aurora"}, {"readReplicaOf": "main"}, {"engine": "oracle-ee"}):
        other = replace(db, config={**db.config, **config})
        busy(ctx, other, metrics=("connections", "read_iops", "write_iops"))
        assert run(schedule.schedule_databases, ctx, other) == []


def test_settings_set_the_pattern_tag_keys_and_hours() -> None:
    ctx = context()
    ctx.settings = RuleSettings.from_json(
        {
            "nonprodPattern": "^lab-",
            "nonprodTagKeys": ["Tier"],
            "scheduleStart": "08:30",
            "scheduleEnd": "18:00",
        }
    )
    lab = server("m5.large", Tier="lab-1")
    staging = staging_server()  # Environment is no longer a key the tenant uses
    busy(ctx, lab)
    busy(ctx, staging)
    [c] = run(schedule.schedule_instances, ctx, lab, staging)
    assert c.resource is lab
    assert c.fix["schedule"]["tagValue"] == "08:30-18:00;MO-FR;Africa/Lagos"
    on_week = 5 * 9.5
    assert c.cost_after == pytest.approx(0.096 * on_week * 730 / 168)
    assert c.rule_config["nonprodTagKeys"] == ["Tier"]

    # Bad stored values fall back to the defaults; the API refuses them (below).
    fallback = RuleSettings.from_json(
        {"nonprodPattern": "(a+)+$", "scheduleStart": "21:00", "scheduleEnd": "07:00"}
    )
    assert fallback == RuleSettings()
    assert RuleSettings().to_json()["nonprodTagKeys"] == ["env", "environment", "stage"]


def test_pattern_and_window_checks() -> None:
    assert pattern_problem("dev|test") is None
    assert pattern_problem("") is not None
    assert pattern_problem("x" * 201) is not None
    assert "slow" in str(pattern_problem("(a+)+$"))
    assert "not a valid" in str(pattern_problem("(dev"))
    assert window_problem("07:00", "20:00") is None
    assert window_problem("7:00", "20:00") is not None
    assert window_problem("20:00", "07:00") is not None


def test_unknown_time_zone_falls_back_to_utc_and_quick_create_when_hosted() -> None:
    ctx = context()
    ctx.time_zone = "Mars/Olympus"
    ctx.scheduler_quick_create_template_url = "https://bucket.s3/cfn/v1/cloudoptics-scheduler.yaml"
    r = staging_server()
    busy(ctx, r)
    [c] = run(schedule.schedule_instances, ctx, r)
    assert c.fix["schedule"]["timeZone"] == "UTC"
    url = c.fix["schedule"]["quickCreateUrl"]
    assert url.startswith("https://eu-west-1.console.aws.amazon.com/cloudformation/home")
    assert "stackName=cloudoptics-scheduler" in url


def test_r7_ranks_with_the_other_rules_on_one_resource() -> None:
    ctx = context()
    r = server("m4.large", Environment="test")
    busy(ctx, r)
    found = {s.candidate.rule_id for s in engine.evaluate(ctx, [r])}
    assert "R7_schedule_ec2" in found and "R6_newer_generation" in found


async def test_pulled_usage_and_tenant_time_zone_feed_the_schedule(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, connection = await scanned(client, login, owner_sql, world)
    owner_sql(
        "UPDATE resource SET state = 'running', tags = '{\"Environment\": \"staging\"}' "
        "WHERE resource_id = 'i-eu-west-1-1'"
    )
    owner_sql("UPDATE tenant SET time_zone = 'Africa/Johannesburg' WHERE id = %s", tenant)
    world.metric_values = {"CPUUtilization": 40.0, "NetworkIn": 1e8, "DatabaseConnections": 3.0}
    now = datetime.now(UTC)
    await puller.pull_connection(tenant, connection, now=now)
    await engine.run_analysis(tenant, now=now)

    rows: list[tuple[Any, ...]] = owner_sql(
        "SELECT resource_id, fix, primary_of FROM recommendation WHERE tenant_id = %s "
        "AND rule_id = 'R7_schedule_ec2'",
        tenant,
    )
    [(resource_id, fix, primary_of)] = rows
    assert resource_id == "i-eu-west-1-1" and primary_of is None
    assert fix["schedule"]["tagValue"] == "07:00-20:00;MO-FR;Africa/Johannesburg"
    assert fix["schedule"]["templateUrl"].endswith(
        "/infra/cloudformation/scheduler/cloudoptics-scheduler.yaml"
    )
    assert "#InstanceDetails:instanceId=i-eu-west-1-1" in fix["console"]["url"]


async def test_rule_settings_api_validates_the_schedule_fields(
    client: AsyncClient, login, owner_sql
) -> None:
    from test_inventory import setup

    tenant, _, _, headers = await setup(client, login, owner_sql)
    url = f"/v1/tenants/{tenant}/rule-settings"
    view = (await client.get(url, headers=headers)).json()
    assert {"R7_schedule_ec2", "R7_schedule_rds"} <= set(view["rules"])
    settings = view["settings"]
    assert settings["scheduleStart"] == "07:00" and settings["scheduleEnd"] == "20:00"
    for bad in (
        {"nonprodPattern": "(a+)+"},
        {"nonprodPattern": "(dev"},
        {"scheduleStart": "20:00", "scheduleEnd": "07:00"},
        {"nonprodTagKeys": []},
        {"nonprodTagKeys": ["bad;key"]},
    ):
        r = await client.put(url, json={**settings, **bad}, headers=headers)
        assert r.status_code == 422, bad
    good = {**settings, "nonprodPattern": "^lab-", "nonprodTagKeys": ["Tier"]}
    r = await client.put(url, json=good, headers=headers)
    assert r.status_code == 200 and r.json()["settings"]["nonprodTagKeys"] == ["Tier"]


def test_other_resource_types_are_ignored() -> None:
    other = resource("ebs_volume", "vol-1", "in-use", {"volumeType": "gp3"}, env="dev")
    ctx = context()
    assert run(schedule.schedule_instances, ctx, other) == []
    assert run(schedule.schedule_databases, ctx, other) == []
