"""Small fakes for boto3 clients. Each method returns a canned response or raises."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeClient:
    def __init__(self, **methods: Any) -> None:
        self._methods = methods

    def __getattr__(self, name: str) -> Any:
        if name not in self._methods:
            raise AttributeError(name)
        behaviour = self._methods[name]

        def call(**_: Any) -> Any:
            if isinstance(behaviour, Exception):
                raise behaviour
            return behaviour

        return call


def healthy_clients(account_id: str = "123456789012", objects: int = 3) -> dict[str, FakeClient]:
    arn = f"arn:aws:bcm-data-exports:us-east-1:{account_id}:export/cloudoptics-cur2-abc"
    return {
        "ce": FakeClient(get_cost_and_usage={"ResultsByTime": []}),
        "bcm-data-exports": FakeClient(
            list_exports={"Exports": [{"ExportArn": arn, "ExportName": "cloudoptics-cur2"}]},
            get_export={
                "Export": {
                    "Name": "cloudoptics-cur2",
                    "DestinationConfigurations": {
                        "S3Destination": {
                            "S3Bucket": f"cloudoptics-cur-{account_id}",
                            "S3Prefix": "cloudoptics",
                            "S3Region": "us-east-1",
                        }
                    },
                }
            },
        ),
        "s3": FakeClient(list_objects_v2={"KeyCount": objects}),
        "organizations": FakeClient(
            describe_organization={"Organization": {"MasterAccountId": account_id}}
        ),
        "savingsplans": FakeClient(describe_savings_plans={"savingsPlans": []}),
    }


class FakeSession:
    def __init__(self, clients: dict[str, FakeClient]) -> None:
        self._clients = clients

    def client(self, service: str, region: str = "us-east-1") -> FakeClient:
        return self._clients[service]


class FakeSessionFactory:
    def __init__(
        self, clients: dict[str, FakeClient] | None = None, assume_error: Exception | None = None
    ) -> None:
        self.clients = clients or healthy_clients()
        self.assume_error = assume_error
        self.calls: list[tuple[str, str, str]] = []

    def assume(self, role_arn: str, external_id: str, name: str) -> FakeSession:
        self.calls.append((role_arn, external_id, name))
        if self.assume_error:
            raise self.assume_error
        return FakeSession(self.clients)
