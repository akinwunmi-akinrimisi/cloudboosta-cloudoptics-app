"""Moving stored External IDs from the local key to KMS (ADR-007): lazily, and by the job."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from test_crypto_kms import KEY_ARN, FakeKms, InvalidCiphertextError

from cloudoptics.connectors import crypto, reencrypt
from cloudoptics.connectors.service import secret_tag
from cloudoptics.platform.settings import get_settings


async def workspace(client: AsyncClient, login: Any) -> tuple[str, dict[str, str]]:
    headers = await login()
    r = await client.post("/v1/tenants", json={"name": "W", "countryCode": "KE"}, headers=headers)
    return r.json()["id"], headers


async def connect(client: AsyncClient, tenant: str, headers: dict[str, str], acct: str) -> Any:
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": acct}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def stored(owner_sql: Any, connection_id: str) -> bytes:
    [(enc,)] = owner_sql(
        "SELECT external_id_enc FROM cloud_connection WHERE id = %s", connection_id
    )
    return bytes(enc)


async def test_reading_a_legacy_secret_rewrites_it_under_kms(
    client: AsyncClient, login: Any, owner_sql: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, headers = await workspace(client, login)
    body = await connect(client, tenant, headers, "424242424242")
    cid = body["connection"]["id"]
    assert stored(owner_sql, cid)[:1] == b"\x01"

    fake = FakeKms()
    monkeypatch.setattr(crypto, "_broker_kms_client", lambda: fake)
    monkeypatch.setattr(get_settings(), "kms_key_id", KEY_ARN)
    crypto.get_encryptor.cache_clear()
    try:
        r = await client.get(f"/v1/tenants/{tenant}/connections/{cid}/setup", headers=headers)
        assert r.json()["externalId"] == body["externalId"]
        upgraded = stored(owner_sql, cid)
        assert upgraded[:1] == b"\x02"
        assert fake.generated == [{"tenant_id": tenant, "purpose": "connection-secret"}]
        r = await client.get(f"/v1/tenants/{tenant}/connections/{cid}/setup", headers=headers)
        assert r.json()["externalId"] == body["externalId"]
        assert stored(owner_sql, cid) == upgraded  # version 2 is left alone
    finally:
        monkeypatch.undo()
        crypto.get_encryptor.cache_clear()


async def test_the_job_re_encrypts_every_legacy_secret_once(
    client: AsyncClient, login: Any, owner_sql: Any
) -> None:
    tenant, headers = await workspace(client, login)
    first = await connect(client, tenant, headers, "515151515151")
    second = await connect(client, tenant, headers, "525252525252")
    ids = [first["connection"]["id"], second["connection"]["id"]]
    assert {stored(owner_sql, c)[:1] for c in ids} == {b"\x01"}

    before = await reencrypt.reencrypt_all()  # no KMS yet: counts only
    assert not before.kms and before.connections >= 2
    fake = FakeKms()
    settings = get_settings()
    original = crypto._broker_kms_client
    crypto._broker_kms_client = lambda: fake  # type: ignore[assignment]
    settings.kms_key_id = KEY_ARN
    crypto.get_encryptor.cache_clear()
    try:
        check = await reencrypt.reencrypt_all(dry_run=True)
        assert check.connections >= 2 and {stored(owner_sql, c)[:1] for c in ids} == {b"\x01"}
        report = await reencrypt.reencrypt_all()
        # Other tests' rows share this database; some are tampered with on purpose.
        assert report.kms and report.connections >= 2
        assert {stored(owner_sql, c)[:1] for c in ids} == {b"\x02"}
        again = await reencrypt.reencrypt_all()
        assert (again.tenants, again.connections, again.links) == (0, 0, 0)
        r = await client.get(f"/v1/tenants/{tenant}/connections/{ids[0]}/setup", headers=headers)
        assert r.json()["externalId"] == first["externalId"]

        # A ciphertext copied into another workspace's row does not decrypt there.
        other, other_headers = await workspace(client, login)
        third = await connect(client, other, other_headers, "535353535353")
        owner_sql(
            "UPDATE cloud_connection SET external_id_enc = %s WHERE id = %s",
            stored(owner_sql, ids[0]),
            third["connection"]["id"],
        )
        with pytest.raises(InvalidCiphertextError):  # KMS refuses tenant B's context
            crypto.get_encryptor().decrypt(
                stored(owner_sql, third["connection"]["id"]), uuid.UUID(other)
            )
        owner_sql(
            "UPDATE cloud_connection SET external_id_enc = %s WHERE id = %s",
            crypto.get_encryptor().encrypt(third["externalId"], uuid.UUID(other)),
            third["connection"]["id"],
        )
    finally:
        # The job ran over the whole test database: put every row back under the local key,
        # which later tests (without KMS) read.
        kms_encryptor = crypto.get_encryptor()
        local = crypto.LocalKeyEncryptor(settings.local_data_key)
        for table in ("cloud_connection", "connection_intent"):
            for row_id, tid, enc in owner_sql(
                f"SELECT id, tenant_id, external_id_enc FROM {table}"  # noqa: S608 - fixed names
            ):
                # A disconnected DigitalOcean connection has no secret left (ADR-021).
                if enc is None or bytes(enc)[:1] != crypto.VERSION_KMS:
                    continue
                try:
                    plain = kms_encryptor.decrypt(bytes(enc), tid)
                except Exception:  # noqa: S112 - the row copied across tenants above
                    continue
                owner_sql(
                    f"UPDATE {table} SET external_id_enc = %s WHERE id = %s",  # noqa: S608
                    local.encrypt(plain, tid),
                    row_id,
                )
        crypto._broker_kms_client = original  # type: ignore[assignment]
        settings.kms_key_id = ""
        crypto.get_encryptor.cache_clear()


def test_the_command_prints_counts_and_fails_while_secrets_remain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = iter(
        [
            reencrypt.Report(kms=True, tenants=1, connections=2),
            reencrypt.Report(kms=True),
            reencrypt.Report(kms=True, tenants=1, connections=1, failed=1),
            reencrypt.Report(kms=False),
        ]
    )

    async def fake(*, dry_run: bool = False) -> reencrypt.Report:
        return next(results)

    monkeypatch.setattr(reencrypt, "reencrypt_all", fake)
    assert reencrypt.main(["--check"]) == 1
    assert reencrypt.main(["--check"]) == 0
    assert reencrypt.main([]) == 1  # one row did not decrypt
    assert reencrypt.main([]) == 1  # no KMS key: nothing was re-encrypted
    out = capsys.readouterr().out
    assert '"connections": 2' in out and "ext" not in out


def test_the_check_tag_survives_re_encryption() -> None:
    tag = secret_tag("an-external-id")
    assert tag == secret_tag("an-external-id") and len(tag) == 16
    assert tag != secret_tag("another-external-id")


async def test_the_hourly_job_does_nothing_without_kms(client: AsyncClient) -> None:
    from cloudoptics.connectors.jobs import reencrypt_secrets

    crypto.get_encryptor.cache_clear()
    assert await reencrypt_secrets(0) == "kms not configured"
