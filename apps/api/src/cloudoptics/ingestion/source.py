"""Where CUR 2.0 exports are read from: the customer's S3 bucket, or a local folder (tests,
local development with ``tools/cur-fixture``).

BCM Data Exports layout under the export prefix::

    <prefix>/<export>/data/BILLING_PERIOD=YYYY-MM/<export>-00001.snappy.parquet
    <prefix>/<export>/metadata/BILLING_PERIOD=YYYY-MM/<export>-Manifest.json

The manifest lists the data files of one delivery and carries the ``executionId`` that
identifies the version; ingestion keys on it (docs/11b §2.2).

The customer writes the bucket, so what is read from it is bounded: the manifest's size and
number of data files, each download's bytes (``ExportTooLargeError``), and the ``executionId``,
which becomes a folder name when normalised output is written (``VERSION_ID``).
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
#: An executionId is one path segment: no separators, and not "." or "..".
VERSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class ExportSource(Protocol):
    def list_keys(self, prefix: str) -> list[str]: ...

    def read_text(self, key: str) -> str: ...

    def download(self, key: str, dest: Path, max_bytes: int | None = None) -> tuple[str, int]:
        """Copy ``key`` to ``dest``; return its SHA-256 and size. Raises ``ExportTooLargeError``
        (leaving a partial file) past ``max_bytes``."""
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


class ExportTooLargeError(ManifestError):
    """The export is past one of the ingestion limits (settings ``cur_max_*``)."""


def _limits() -> Any:
    from cloudoptics.platform.settings import get_settings

    return get_settings()


def _check_size(key: str, size: int, limit: int | None, what: str) -> None:
    if limit is not None and size > limit:
        raise ExportTooLargeError(f"{what} {key} is larger than the {limit:,}-byte limit")


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
    if not VERSION_ID.fullmatch(version) or version in (".", ".."):
        raise ManifestError(f"Manifest {key} has an executionId that is not a plain name")
    max_files = _limits().cur_max_data_files
    if len(files) > max_files:
        raise ExportTooLargeError(
            f"Manifest {key} lists {len(files)} data files; the limit is {max_files}"
        )
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
        limit = _limits().cur_max_manifest_bytes
        obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        _check_size(key, int(obj.get("ContentLength") or 0), limit, "Manifest")
        data = obj["Body"].read(limit + 1)
        _check_size(key, len(data), limit, "Manifest")
        return str(data.decode("utf-8"))

    def download(self, key: str, dest: Path, max_bytes: int | None = None) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        _check_size(key, int(obj.get("ContentLength") or 0), max_bytes, "Data file")
        body = obj["Body"]
        with dest.open("wb") as out:
            while chunk := body.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                _check_size(key, size, max_bytes, "Data file")
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
        path = self._path(key)
        _check_size(key, path.stat().st_size, _limits().cur_max_manifest_bytes, "Manifest")
        return path.read_text()

    def download(self, key: str, dest: Path, max_bytes: int | None = None) -> tuple[str, int]:
        path = self._path(key)
        _check_size(key, path.stat().st_size, max_bytes, "Data file")
        data = path.read_bytes()
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)
