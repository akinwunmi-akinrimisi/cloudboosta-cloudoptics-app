"""Provision the Cloudoptics staging environment on DigitalOcean (ADR-013).

Idempotent: each step looks for the resource by name before creating it, so the script can
be re-run safely. Secrets (database passwords, generated keys) are never printed.

    python infra/digitalocean/provision.py database        # project, PostgreSQL, db, app user
    python infra/digitalocean/provision.py app [--branch B] # create or update the App Platform app
    python infra/digitalocean/provision.py status          # summary of what exists

Reads the API token from DIGITALOCEAN_TOKEN (or Digital_Ocean_Token).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.digitalocean.com/v2"
PROJECT = "Cloudoptics"
REGION = "lon1"
DB_CLUSTER = "cloudoptics-pg"
DB_NAME = "cloudoptics"
DB_APP_USER = "cloudoptics_app"
APP_NAME = "cloudoptics-staging"
SPEC = Path(__file__).with_name("app.yaml")


def _token() -> str:
    token = os.environ.get("DIGITALOCEAN_TOKEN") or os.environ.get("Digital_Ocean_Token")  # noqa: SIM112 - name as set in the environment
    if not token:
        sys.exit("Set DIGITALOCEAN_TOKEN")
    return token


def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 - fixed https API host
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:  # noqa: S310
            raw = res.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"{method} {path} failed: {exc.code} {detail}")
    return json.loads(raw) if raw else {}


def ensure_project() -> str:
    for p in call("GET", "/projects?per_page=200")["projects"]:
        if p["name"] == PROJECT:
            return str(p["id"])
    p = call(
        "POST",
        "/projects",
        {
            "name": PROJECT,
            "purpose": "Web Application",
            "environment": "Staging",
            "description": "Cloudoptics by Cloudboosta: AWS cost optimisation",
        },
    )["project"]
    print(f"created project {PROJECT}")
    return str(p["id"])


def find_cluster() -> dict[str, Any] | None:
    for c in call("GET", "/databases").get("databases") or []:
        if c["name"] == DB_CLUSTER:
            return c
    return None


def ensure_database(wait: bool = True) -> dict[str, Any]:
    project_id = ensure_project()
    cluster = find_cluster()
    if cluster is None:
        cluster = call(
            "POST",
            "/databases",
            {
                "name": DB_CLUSTER,
                "engine": "pg",
                "version": "16",
                "region": REGION,
                "size": "db-s-1vcpu-1gb",
                "num_nodes": 1,
                "project_id": project_id,
                "tags": ["cloudoptics", "staging"],
            },
        )["database"]
        print(f"created PostgreSQL cluster {DB_CLUSTER} (provisioning takes a few minutes)")
    while wait and cluster["status"] != "online":
        print(f"  cluster status: {cluster['status']} ...")
        time.sleep(30)
        cluster = call("GET", f"/databases/{cluster['id']}")["database"]
    if cluster["status"] == "online":
        cid = cluster["id"]
        if DB_NAME not in {d["name"] for d in call("GET", f"/databases/{cid}/dbs")["dbs"]}:
            call("POST", f"/databases/{cid}/dbs", {"name": DB_NAME})
            print(f"created database {DB_NAME}")
        if DB_APP_USER not in {u["name"] for u in call("GET", f"/databases/{cid}/users")["users"]}:
            call("POST", f"/databases/{cid}/users", {"name": DB_APP_USER})
            print(f"created database user {DB_APP_USER}")
    return cluster


def _url(conn: dict[str, Any], user: str, password: str) -> str:
    return f"postgresql://{user}:{password}@{conn['host']}:{conn['port']}/{DB_NAME}?sslmode=require"


def app_secrets(cluster: dict[str, Any]) -> dict[str, str]:
    """Database URLs and a fresh data key. Only used to fill SECRET env vars in the spec."""
    cid = cluster["id"]
    users = {u["name"]: u for u in call("GET", f"/databases/{cid}/users")["users"]}
    # App Platform apps are not in the database VPC, so use the public host; the database
    # firewall (set below) admits only this app, and connections require TLS.
    conn = cluster["connection"]
    return {
        "OWNER_DATABASE_URL": _url(conn, "doadmin", users["doadmin"]["password"]),
        "APP_DATABASE_URL": _url(conn, DB_APP_USER, users[DB_APP_USER]["password"]),
        "LOCAL_DATA_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    }


# Settings that come from the deploy environment (GitHub Actions, staging-setup.yml) rather
# than from app.yaml. Plain values are visible in the App Platform console; secrets are
# stored encrypted. When a run does not supply one, the value already on the app is kept.
RUNTIME_PLAIN = {
    "CLOUDOPTICS_AWS_BROKER_ROLE_ARN": "CLOUDOPTICS_AWS_BROKER_ROLE_ARN",
    "CLOUDOPTICS_CLOUDOPTICS_PRINCIPAL_ARN": "CLOUDOPTICS_AWS_BROKER_ROLE_ARN",
    "CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL": "CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL",
    "CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL": "CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL",
    "CLOUDOPTICS_WORKOS_CLIENT_ID": "WORKOS_CLIENT_ID",
}
RUNTIME_SECRET = {
    "AWS_ACCESS_KEY_ID": "BROKER_AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY": "BROKER_AWS_SECRET_ACCESS_KEY",
    "CLOUDOPTICS_WORKOS_API_KEY": "WORKOS_API_KEY",
}
RUNTIME_COMPONENTS = ("api", "worker")


def apply_runtime_config(spec: dict[str, Any], current: dict[str, Any] | None) -> None:
    supplied: dict[str, dict[str, str]] = {}
    for key, source in RUNTIME_PLAIN.items():
        if os.environ.get(source):
            supplied[key] = {"key": key, "value": os.environ[source], "scope": "RUN_TIME"}
    for key, source in RUNTIME_SECRET.items():
        if os.environ.get(source):
            supplied[key] = {
                "key": key,
                "value": os.environ[source],
                "type": "SECRET",
                "scope": "RUN_TIME",
            }
    if "CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL" in supplied:
        supplied["CLOUDOPTICS_CFN_QUICK_CREATE_ENABLED"] = {
            "key": "CLOUDOPTICS_CFN_QUICK_CREATE_ENABLED",
            "value": "true",
            "scope": "RUN_TIME",
        }
    managed = set(RUNTIME_PLAIN) | set(RUNTIME_SECRET) | {"CLOUDOPTICS_CFN_QUICK_CREATE_ENABLED"}
    previous: dict[str, dict[str, dict[str, Any]]] = {}
    for comp in (current or {}).get("services", []) + (current or {}).get("workers", []):
        previous[comp["name"]] = {e["key"]: e for e in comp.get("envs", []) if e["key"] in managed}
    for comp in spec.get("services", []) + spec.get("workers", []):
        if comp["name"] not in RUNTIME_COMPONENTS:
            continue
        merged = {**previous.get(comp["name"], {}), **supplied}
        comp["envs"] = [e for e in comp.get("envs", []) if e["key"] not in merged]
        comp["envs"] += list(merged.values())
    if supplied:
        print("runtime config supplied:", ", ".join(sorted(supplied)))


def find_app() -> dict[str, Any] | None:
    for a in call("GET", "/apps?per_page=200").get("apps") or []:
        if a["spec"]["name"] == APP_NAME:
            return a
    return None


def render_spec(values: dict[str, str]) -> dict[str, Any]:
    import yaml  # PyYAML, available in the dev environment

    text = SPEC.read_text()
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    spec: dict[str, Any] = yaml.safe_load(text)
    return spec


def deploy_app(branch: str = "main") -> None:
    cluster = ensure_database(wait=True)
    existing = find_app()
    values = app_secrets(cluster)
    if existing:
        # Keep the existing data key: replacing it would make stored External IDs unreadable.
        for svc in existing["spec"].get("services", []) + existing["spec"].get("workers", []):
            for env in svc.get("envs", []):
                if env["key"] == "CLOUDOPTICS_LOCAL_DATA_KEY" and env.get("value"):
                    values["LOCAL_DATA_KEY"] = env["value"]  # encrypted value, passed back as-is
    spec = render_spec(values)
    apply_runtime_config(spec, existing["spec"] if existing else None)
    for site in spec.get("static_sites", []):
        site["git"]["branch"] = branch  # main normally; a feature branch for a trial deploy
    proposal: dict[str, Any] = {"spec": spec}
    if existing:
        proposal["app_id"] = existing["id"]  # lets already-encrypted secrets validate
    call("POST", "/apps/propose", proposal)  # validates the spec
    if existing:
        app = call("PUT", f"/apps/{existing['id']}", {"spec": spec})["app"]
        print(f"updated app {APP_NAME} ({app['id']})")
    else:
        app = call("POST", "/apps", {"spec": spec, "project_id": ensure_project()})["app"]
        print(f"created app {APP_NAME} ({app['id']})")
    # Only the app may reach the database once it exists.
    call(
        "PUT",
        f"/databases/{cluster['id']}/firewall",
        {"rules": [{"type": "app", "value": app["id"]}]},
    )
    print("database firewall: only the app may connect")


def status() -> None:
    cluster = find_cluster()
    print("database:", cluster and {k: cluster[k] for k in ("name", "status", "region", "size")})
    app = find_app()
    if app:
        print(
            "app:",
            app["id"],
            app.get("live_url"),
            (app.get("active_deployment") or {}).get("phase"),
            (app.get("in_progress_deployment") or {}).get("phase"),
        )
    else:
        print("app: none")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "database":
        c = ensure_database(wait="--no-wait" not in sys.argv)
        print(f"{DB_CLUSTER}: {c['status']}")
    elif command == "app":
        branch = sys.argv[sys.argv.index("--branch") + 1] if "--branch" in sys.argv else "main"
        deploy_app(branch)
    else:
        status()
