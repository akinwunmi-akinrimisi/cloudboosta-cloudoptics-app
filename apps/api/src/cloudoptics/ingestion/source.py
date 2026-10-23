"""Where CUR 2.0 exports are read from: the customer's S3 bucket, or a local folder (tests,
local development with ``tools/cur-fixture``).

BCM Data Exports layout under the export prefix::

    <prefix>/<export>/data/BILLING_PERIOD=YYYY-MM/<export>-00001.snappy.parquet
    <prefix>/<export>/metadata/BILLING_PERIOD=YYYY-MM/<export>-Manifest.json

The manifest lists the data files of one delivery and carries the ``executionId`` that
identifies the version; ingestion keys on it (docs/11b §2.2).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

MANIFEST_KEY = re.compile(
    r"(^|/)metadata/BILLING_PERIOD=(\d{4})-(\d{2})/(.+/)?[^/]*Manifest\.json$"
)
CHUNK = 8 * 1024 * 1024


class ExportSource(Protocol):
    def list_keys(self, prefix: str) -> list[str]: ...

    def read_text(self, key: str) -> str: ...

    def download(self, key: str, dest: Path) -> tuple[str, int]:
        """Copy ``key`` to ``dest``; return its SHA-256 and size."""
        ...


@dataclass(frozen=True)
class Manifest:
    key: str
    billing_period: date
    export_version: str
    data_keys: tuple[str, ...]
    num_rows: int | None


class ManifestError(ValueError):
    pass


def _join(prefix: str, rel: str) -> str:
    prefix = prefix.strip("/")
    return f"{prefix}/{rel.lstrip('/')}" if prefix else rel.lstrip("/")


def parse_manifest(key: str, body: str, *, bucket: str | None, prefix: str) -> Manifest:
    match = MANIFEST_KEY.search(key)
    if not match:
        raise ManifestError(f"Not a manifest key: {key}")
    period = date(int(match.group(2)), int(match.group(3)), 1)
    try:
        doc: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Manifest {key} is not JSON") from exc
    version = doc.get("executionId")
    files = doc.get("dataFiles")
    if not isinstance(version, str) or not version or not isinstance(files, list):
        raise ManifestError(f"Manifest {key} has no executionId or dataFiles")
    keys = []
    for ref in files:
        ref = str(ref)
        if ref.startswith("s3://"):
            ref_bucket, _, ref_key = ref[5:].partition("/")
            if bucket is not None and ref_bucket != bucket:
                raise ManifestError(f"Manifest {key} points outside the export bucket")
            keys.append(ref_key)
        else:
            keys.append(_join(prefix, ref))
    rows = doc.get("numRows")
    return Manifest(
        key=key,
        billing_period=period,
        export_version=version,
        data_keys=tuple(keys),
        num_rows=int(rows) if isinstance(rows, int | str) and str(rows).isdigit() else None,
    )


def manifest_keys(keys: list[str]) -> dict[date, str]:
    """The newest manifest key per billing period (overwrite exports have one each)."""
    latest: dict[date, str] = {}
    for key in sorted(keys):
        match = MANIFEST_KEY.search(key)
        if match:
            latest[date(int(match.group(2)), int(match.group(3)), 1)] = key
    return latest


class S3ExportSource:
    def __init__(self, client: Any, bucket: str) -> None:
        self._s3 = client
        self.bucket = bucket

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def read_text(self, key: str) -> str:
        body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        return str(body.read().decode("utf-8"))

    def download(self, key: str, dest: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        with dest.open("wb") as out:
            while chunk := body.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        return digest.hexdigest(), size


class LocalExportSource:
    """A folder laid out like the export bucket."""

    bucket = None

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ManifestError(f"Key {key} is outside the export folder")
        return path

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(
            str(p.relative_to(self._root))
            for p in self._root.rglob("*")
            if p.is_file() and str(p.relative_to(self._root)).startswith(prefix)
        )

    def read_text(self, key: str) -> str:
        return self._path(key).read_text()

    def download(self, key: str, dest: Path) -> tuple[str, int]:
        data = self._path(key).read_bytes()
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)
