"""Write a fixture in the BCM Data Exports folder layout.

    <root>/<export>/data/BILLING_PERIOD=YYYY-MM/<export>-00001.snappy.parquet
    <root>/<export>/metadata/BILLING_PERIOD=YYYY-MM/<export>-Manifest.json
    <root>/<export>/expectations/BILLING_PERIOD=YYYY-MM.json   (fixture only)

A restatement overwrites the same billing period, as AWS does. The manifest carries an
``executionId`` that changes per version, which is what ingestion keys on. The manifest is
a simplified version of the real one; verify field names against a real export in Sprint 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from cur_fixture.generator import Fixture


def write_export(fixture: Fixture, root: Path, export_name: str = "cloudoptics-cur2") -> Path:
    period = f"BILLING_PERIOD={fixture.period.label}"
    data_dir = root / export_name / "data" / period
    meta_dir = root / export_name / "metadata" / period
    exp_dir = root / export_name / "expectations"
    for d in (data_dir, meta_dir, exp_dir):
        d.mkdir(parents=True, exist_ok=True)

    data_file = data_dir / f"{export_name}-00001.snappy.parquet"
    pq.write_table(fixture.table, data_file, compression="snappy")

    manifest = {
        "exportName": export_name,
        "executionId": f"{fixture.period.label}-v{fixture.version}",
        "billingPeriod": {
            "start": fixture.period.start.isoformat(),
            "end": fixture.period.end.isoformat(),
        },
        "dataFiles": [str(data_file.relative_to(root))],
        "numRows": fixture.table.num_rows,
        "columns": [{"name": f.name, "type": str(f.type)} for f in fixture.table.schema],
    }
    manifest_path = meta_dir / f"{export_name}-Manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    (exp_dir / f"{period}.json").write_text(json.dumps(fixture.expectations, indent=2))
    return manifest_path
