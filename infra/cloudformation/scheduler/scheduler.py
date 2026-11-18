# Cloudoptics scheduler for cloudoptics-scheduler.yaml. Runs in the customer's account on an
# EventBridge Scheduler rate; Cloudoptics never assumes its role. The template carries this
# file inline (tests check the two match). It stops and starts the EC2 instances and RDS DB
# instances in its region that carry the schedule tag, for example
#   cloudoptics:schedule = 07:00-20:00;MO-FR;Africa/Lagos
# (RDS tag values cannot hold ";", so "07:00-20:00 MO-FR Africa/Lagos" works too). Days are
# MO TU WE TH FR SA SU, as a list (MO,WE,FR) or ranges (MO-FR); an end before the start runs
# overnight. Times are in the IANA time zone given.
# By default it acts only when a schedule changes state (at the start and at the end), so a
# server someone starts by hand in the evening keeps running until the next scheduled stop.
# With Enforce=true it puts every tagged resource in its scheduled state on every run.
import datetime as dt
import json
import os
import re
import zoneinfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

TAG_KEY = "cloudoptics:schedule"
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
SPEC = re.compile(r"^\s*(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})[;\s]+([A-Za-z,-]+)[;\s]+(\S+)\s*$")
BATCH = 50


class Schedule:
    def __init__(self, start, end, days, tz):
        self.start, self.end, self.days, self.tz = start, end, days, tz

    def wants_running(self, when):
        local = when.astimezone(self.tz)
        minute = local.hour * 60 + local.minute
        day = local.weekday()
        if self.start < self.end:
            return day in self.days and self.start <= minute < self.end
        # Overnight: on from the start on a listed day until the end the next morning.
        return (day in self.days and minute >= self.start) or (
            (day - 1) % 7 in self.days and minute < self.end
        )


def _days(text):
    days = set()
    for part in text.upper().split(","):
        ends = part.split("-")
        if len(ends) == 1 and ends[0] in DAYS:
            days.add(DAYS.index(ends[0]))
        elif len(ends) == 2 and ends[0] in DAYS and ends[1] in DAYS:
            first, last = DAYS.index(ends[0]), DAYS.index(ends[1])
            days.update((first + i) % 7 for i in range((last - first) % 7 + 1))
        else:
            return None
    return frozenset(days)


def parse(value):
    """The schedule a tag value describes, or None when it cannot be read."""
    m = SPEC.match(value or "")
    if m is None:
        return None
    h1, m1, h2, m2, days_text, tz_name = m.groups()
    if int(h1) > 23 or int(h2) > 24 or int(m1) > 59 or int(m2) > 59:
        return None
    start, end = int(h1) * 60 + int(m1), min(24 * 60, int(h2) * 60 + int(m2))
    days = _days(days_text)
    if start == end or not days:
        return None
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None
    return Schedule(start, end, days, tz)


def action(schedule, now, lookback, enforce):
    """``start``, ``stop`` or None. Without ``enforce``, only when the schedule changed
    state since the previous run (``lookback`` ago)."""
    want = schedule.wants_running(now)
    if not enforce and want == schedule.wants_running(now - lookback):
        return None
    return "start" if want else "stop"


def _instances(ec2):
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": "tag-key", "Values": [TAG_KEY]},
            {"Name": "instance-state-name", "Values": ["running", "stopped"]},
        ]
    )
    for page in pages:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                tags = {t["Key"]: t.get("Value", "") for t in instance.get("Tags", [])}
                yield instance["InstanceId"], instance["State"]["Name"], tags.get(TAG_KEY)


def _databases(rds):
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances", []):
            tags = {t["Key"]: t.get("Value", "") for t in db.get("TagList", [])}
            if TAG_KEY not in tags:
                continue
            if db.get("DBClusterIdentifier") or db.get("ReadReplicaSourceDBInstanceIdentifier"):
                continue  # Aurora members and replicas cannot be stopped on their own
            yield db["DBInstanceIdentifier"], db.get("DBInstanceStatus"), tags[TAG_KEY]


def _error(exc):
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "ClientError")
    return type(exc).__name__


def run(ec2, rds, now, lookback, enforce):
    result = {"started": [], "stopped": [], "invalid": [], "failed": []}
    todo = {"start": [], "stop": []}
    for instance_id, state, value in _instances(ec2):
        schedule = parse(value)
        if schedule is None:
            result["invalid"].append(instance_id)
            continue
        act = action(schedule, now, lookback, enforce)
        if (act, state) in (("start", "stopped"), ("stop", "running")):
            todo[act].append(instance_id)
    for act, call, done in (
        ("start", "start_instances", "started"),
        ("stop", "stop_instances", "stopped"),
    ):
        ids = todo[act]
        for i in range(0, len(ids), BATCH):
            batch = ids[i : i + BATCH]
            try:
                getattr(ec2, call)(InstanceIds=batch)
                result[done] += batch
            except (ClientError, BotoCoreError) as exc:
                result["failed"] += [f"{x}: {_error(exc)}" for x in batch]
    for name, status, value in _databases(rds):
        schedule = parse(value)
        if schedule is None:
            result["invalid"].append(name)
            continue
        act = action(schedule, now, lookback, enforce)
        try:
            if act == "start" and status == "stopped":
                rds.start_db_instance(DBInstanceIdentifier=name)
                result["started"].append(name)
            elif act == "stop" and status == "available":
                rds.stop_db_instance(DBInstanceIdentifier=name)
                result["stopped"].append(name)
        except (ClientError, BotoCoreError) as exc:
            result["failed"].append(f"{name}: {_error(exc)}")
    return result


def handler(event, context):
    # One minute more than the interval, so runs overlap and no change is missed.
    lookback = dt.timedelta(minutes=int(os.environ.get("INTERVAL_MINUTES", "15")) + 1)
    result = run(
        boto3.client("ec2"),
        boto3.client("rds"),
        dt.datetime.now(dt.UTC),
        lookback,
        os.environ.get("ENFORCE") == "true",
    )
    print(json.dumps(result))
    return result
