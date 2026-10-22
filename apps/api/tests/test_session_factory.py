"""Role chaining: process credentials -> Cloudoptics broker role -> customer role."""

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from cloudoptics.connectors.aws import SessionFactory

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


class FakeSts:
    def __init__(self, owner: str, log: list[tuple[str, str, str | None]]) -> None:
        self.owner, self.log = owner, log

    def assume_role(self, **kw: Any) -> dict[str, Any]:
        self.log.append((self.owner, kw["RoleArn"], kw.get("ExternalId")))
        name = kw["RoleArn"].rsplit("/", 1)[-1]
        return {
            "Credentials": {
                "AccessKeyId": f"key-{name}",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": NOW + timedelta(hours=1),
            }
        }


class FakeSession:
    log: ClassVar[list[tuple[str, str, str | None]]] = []

    def __init__(self, aws_access_key_id: str = "process", **_: Any) -> None:
        self.key = aws_access_key_id

    def client(self, service: str, region_name: str | None = None) -> FakeSts:
        assert service == "sts"
        return FakeSts(self.key, self.log)


BROKER = "arn:aws:iam::111111111111:role/CloudopticsBroker"
CUSTOMER = "arn:aws:iam::222222222222:role/CloudopticsBillingReadOnly"


def test_without_broker_the_process_assumes_the_customer_role() -> None:
    FakeSession.log = []
    session = SessionFactory(make_session=FakeSession).assume(CUSTOMER, "ext", "n")
    assert FakeSession.log == [("process", CUSTOMER, "ext")]
    assert session.session.key == "key-CloudopticsBillingReadOnly"


def test_with_broker_the_broker_assumes_the_customer_role_and_is_reused() -> None:
    FakeSession.log = []
    now = [NOW]
    factory = SessionFactory(broker_role_arn=BROKER, make_session=FakeSession, clock=lambda: now[0])
    factory.assume(CUSTOMER, "ext-1", "a")
    factory.assume(CUSTOMER, "ext-2", "b")
    assert FakeSession.log == [
        ("process", BROKER, None),
        ("key-CloudopticsBroker", CUSTOMER, "ext-1"),
        ("key-CloudopticsBroker", CUSTOMER, "ext-2"),
    ]
    now[0] = NOW + timedelta(minutes=56)  # inside the 5-minute refresh window
    factory.assume(CUSTOMER, "ext-3", "c")
    assert FakeSession.log[-2:] == [
        ("process", BROKER, None),
        ("key-CloudopticsBroker", CUSTOMER, "ext-3"),
    ]
