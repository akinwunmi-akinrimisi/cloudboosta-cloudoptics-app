"""Load one export version: download, normalise, check and aggregate (synchronous; the
service runs it in a worker thread)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from cloudoptics.ingestion.aggregate import DailyAggregator, DailyCost
from cloudoptics.ingestion.focus import (
    MAPPING_VERSION,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    normalise,
)
from cloudoptics.ingestion.quality import Check, QualityChecks, blocked
from cloudoptics.ingestion.source import ExportSource, Manifest

BATCH_ROWS = 200_000


@dataclass(frozen=True)
class LoadedFile:
    key: str
    sha256: str
    size_bytes: int
    row_count: int


@dataclass
class LoadedExport:
    manifest: Manifest
    files: list[LoadedFile]
    checks: list[Check]
    rows: list[DailyCost] = field(default_factory=list)
    tag_sets: dict[bytes, dict[str, str]] = field(default_factory=dict)
    accounts: dict[str, str | None] = field(default_factory=dict)
    mapping_version: str = MAPPING_VERSION

    @property
    def ok(self) -> bool:
        return not blocked(self.checks)

    @property
    def row_count(self) -> int:
        return sum(f.row_count for f in self.files)


def load_export(
    source: ExportSource, manifest: Manifest, normalised_dir: Path | None = None
) -> LoadedExport:
    """Read every data file of ``manifest``. Blocking checks leave ``rows`` empty."""
    quality = QualityChecks(manifest.billing_period)
    aggregator = DailyAggregator()
    files: list[LoadedFile] = []
    writer: pq.ParquetWriter | None = None
    out_path: Path | None = None
    if normalised_dir is not None:
        out_dir = (
            normalised_dir
            / f"billing_period={manifest.billing_period:%Y-%m}"
            / f"version={manifest.export_version}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "part-00000.parquet"

    try:
        with tempfile.TemporaryDirectory(prefix="cur-") as tmp:
            for index, key in enumerate(manifest.data_keys):
                local = Path(tmp) / f"{index:05d}.parquet"
                sha256, size = source.download(key, local)
                parquet = pq.ParquetFile(local)
                schema = parquet.schema_arrow
                quality.schema(schema)
                wanted = [
                    c for c in REQUIRED_COLUMNS + OPTIONAL_COLUMNS if schema.get_field_index(c) >= 0
                ]
                count = 0
                if all(schema.get_field_index(c) >= 0 for c in REQUIRED_COLUMNS):
                    for batch in parquet.iter_batches(batch_size=BATCH_ROWS, columns=wanted):
                        focus = normalise(batch)
                        quality.update(batch, focus)
                        aggregator.update(focus)
                        if out_path is not None:
                            lineage = _with_lineage(focus, key, manifest)
                            writer = writer or pq.ParquetWriter(
                                out_path, lineage.schema, compression="zstd"
                            )
                            writer.write_table(lineage)
                        count += batch.num_rows
                else:
                    count = parquet.metadata.num_rows
                files.append(LoadedFile(key, sha256, size, count))
    finally:
        if writer is not None:
            writer.close()

    checks = quality.finish(manifest.num_rows)
    loaded = LoadedExport(manifest, files, checks)
    if loaded.ok:
        loaded.rows = aggregator.finish()
        loaded.tag_sets = aggregator.tag_sets
        loaded.accounts = aggregator.accounts
    return loaded


def _with_lineage(focus: pa.Table, key: str, manifest: Manifest) -> pa.Table:
    """Lineage columns (docs/09 §2.2.1) live only in the Parquet output."""
    n = focus.num_rows
    return focus.append_column("_source_file", pa.array([key] * n, pa.string())).append_column(
        "_data_version", pa.array([manifest.export_version] * n, pa.string())
    )
