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


class FakeRolesAnywhere:
    """Hands out broker credentials valid for one hour from the clock's current time."""

    role_arn = BROKER

    def __init__(self, now: list[datetime]) -> None:
        self.now, self.calls = now, 0

    def fetch(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "AccessKeyId": f"ra-{self.calls}",
            "SecretAccessKey": "s",
            "SessionToken": "t",
            "Expiration": self.now[0] + timedelta(hours=1),
        }


def test_roles_anywhere_credentials_are_the_broker_and_refresh_before_expiry() -> None:
    FakeSession.log = []
    now = [NOW]
    ra = FakeRolesAnywhere(now)
    factory = SessionFactory(
        broker_role_arn=BROKER, make_session=FakeSession, clock=lambda: now[0], credentials=ra
    )
    factory.assume(CUSTOMER, "ext-1", "a")
    factory.assume(CUSTOMER, "ext-2", "b")
    # Roles Anywhere already gives the broker role: no second AssumeRole, one CreateSession.
    assert FakeSession.log == [("ra-1", CUSTOMER, "ext-1"), ("ra-1", CUSTOMER, "ext-2")]
    assert ra.calls == 1
    now[0] = NOW + timedelta(minutes=54)
    factory.assume(CUSTOMER, "ext-3", "c")
    assert ra.calls == 1
    now[0] = NOW + timedelta(minutes=55, seconds=1)  # inside the 5-minute window
    factory.assume(CUSTOMER, "ext-4", "d")
    assert ra.calls == 2
    assert FakeSession.log[-1] == ("ra-2", CUSTOMER, "ext-4")


def test_roles_anywhere_into_a_different_role_chains_to_the_broker() -> None:
    FakeSession.log = []
    now = [NOW]
    ra = FakeRolesAnywhere(now)
    ra.role_arn = "arn:aws:iam::111111111111:role/CloudopticsWorkload"
    factory = SessionFactory(
        broker_role_arn=BROKER, make_session=FakeSession, clock=lambda: now[0], credentials=ra
    )
    factory.assume(CUSTOMER, "ext", "a")
    assert FakeSession.log == [("ra-1", BROKER, None), ("key-CloudopticsBroker", CUSTOMER, "ext")]


def test_the_broker_client_is_rebuilt_only_when_the_session_is_renewed() -> None:
    now = [NOW]
    built: list[tuple[str, str]] = []

    class Session(FakeSession):
        def client(self, service: str, region_name: str | None = None, **_: Any) -> Any:
            built.append((self.key, service))
            return (self.key, service)

    ra = FakeRolesAnywhere(now)
    factory = SessionFactory(make_session=Session, clock=lambda: now[0], credentials=ra)
    assert factory.broker_client("kms", "eu-west-2") == ("ra-1", "kms")
    assert factory.broker_client("kms", "eu-west-2") == ("ra-1", "kms")
    assert built == [("ra-1", "kms")]
    now[0] = NOW + timedelta(minutes=58)
    assert factory.broker_client("kms", "eu-west-2") == ("ra-2", "kms")
    assert factory.broker_session().key == "ra-2"


def test_roles_anywhere_is_built_from_settings(monkeypatch: Any) -> None:
    from pydantic import SecretStr

    from cloudoptics.connectors import rolesanywhere, service
    from cloudoptics.platform.settings import Settings

    made: dict[str, Any] = {}

    class Creds:
        role_arn = BROKER

        def __init__(self, **kw: Any) -> None:
            made.update(kw)

    settings = Settings(
        broker_credentials="auto",
        roles_anywhere_certificate="CERT",
        roles_anywhere_private_key=SecretStr("KEY"),
        roles_anywhere_trust_anchor_arn="arn:aws:rolesanywhere:eu-west-2:1:trust-anchor/a",
        roles_anywhere_profile_arn="arn:aws:rolesanywhere:eu-west-2:1:profile/p",
        roles_anywhere_role_arn=BROKER,
        aws_broker_role_arn=BROKER,
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(rolesanywhere, "RolesAnywhereCredentials", Creds)
    monkeypatch.setattr(service, "_session_factory", None)
    factory = service.get_session_factory()
    assert made["private_key"] == "KEY" and made["role_arn"] == BROKER
    assert factory._broker_role_arn is None  # no chaining into the same role
    monkeypatch.setattr(service, "_session_factory", None)
