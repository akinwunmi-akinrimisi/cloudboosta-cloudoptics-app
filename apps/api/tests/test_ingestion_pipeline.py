"""CUR 2.0 -> FOCUS -> daily aggregates, against the synthetic fixture (no database)."""

import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from cloudoptics.ingestion.aggregate import DailyAggregator, tag_hash
from cloudoptics.ingestion.focus import normalise
from cloudoptics.ingestion.pipeline import load_export
from cloudoptics.ingestion.quality import QualityChecks, blocked
from cloudoptics.ingestion.source import (
    ExportTooLargeError,
    LocalExportSource,
    Manifest,
    ManifestError,
    S3ExportSource,
    manifest_keys,
    parse_manifest,
)
from cur_fixture import BillingPeriod, generate, write_export
from cur_fixture.generator import SP_UTILISATION

SEPT = BillingPeriod(2026, 9)


def focus_of(table: pa.Table) -> pa.Table:
    return normalise(table.combine_chunks().to_batches()[0])


def sum_where(table: pa.Table, column: str, where: str, value: str) -> float:
    return pc.sum(table.filter(pc.equal(table[where], value))[column]).as_py() or 0.0


def test_billed_cost_is_the_invoice_total() -> None:
    raw = generate(SEPT).table
    focus = focus_of(raw)
    assert pc.sum(focus["BilledCost"]).as_py() == pytest.approx(
        pc.sum(raw["line_item_unblended_cost"]).as_py()
    )


def test_charge_categories_and_local_zone_region() -> None:
    focus = focus_of(generate(SEPT, restate=True).table)
    by_type = dict(
        zip(focus["x_LineItemType"].to_pylist(), focus["ChargeCategory"].to_pylist(), strict=True)
    )
    assert by_type["SavingsPlanCoveredUsage"] == "Usage"
    assert by_type["SavingsPlanRecurringFee"] == "Purchase"
    assert by_type["Credit"] == "Credit"
    assert by_type["Refund"] == "Credit"
    assert by_type["Tax"] == "Tax"
    lagos = focus.filter(pc.equal(focus["ResourceId"], "i-0lagosedge0000001"))
    assert set(lagos["RegionId"].to_pylist()) == {"af-south-1-los-1"}
    assert set(focus["ServiceCategory"].to_pylist()) >= {"Compute", "Databases", "Networking"}


def test_savings_plan_is_amortised_without_changing_the_total() -> None:
    raw = generate(SEPT).table
    agg = DailyAggregator()
    agg.update(focus_of(raw))
    rows = agg.finish()
    billed = sum(r.billed_cost for r in rows)
    effective = sum(r.effective_cost for r in rows)
    assert effective == pytest.approx(billed, abs=Decimal("0.0001"))

    fee = sum(
        r.effective_cost for r in rows if r.service_code == "ComputeSavingsPlans"
    )  # the unused commitment only
    fee_billed = sum(r.billed_cost for r in rows if r.service_code == "ComputeSavingsPlans")
    assert float(fee) == pytest.approx(float(fee_billed) * (1 - SP_UTILISATION), rel=1e-4)


def test_commitment_columns_take_precedence_when_exported() -> None:
    raw = generate(SEPT).table
    is_fee = pc.equal(raw["line_item_line_item_type"], "SavingsPlanRecurringFee")
    fee = raw["line_item_unblended_cost"]
    total = pc.if_else(is_fee, fee, pa.scalar(None, pa.float64()))
    used = pc.if_else(is_fee, pc.multiply(fee, 0.5), pa.scalar(None, pa.float64()))
    raw = raw.append_column("savings_plan_total_commitment_to_date", total).append_column(
        "savings_plan_used_commitment", used
    )
    focus = focus_of(raw)
    fees = focus.filter(pc.equal(focus["x_LineItemType"], "SavingsPlanRecurringFee"))
    assert pc.sum(fees["EffectiveCost"]).as_py() == pytest.approx(
        pc.sum(fees["BilledCost"]).as_py() * 0.5
    )
    assert set(fees["x_CommitmentKind"].to_pylist()) == {None}


def test_daily_grain_and_tag_sets() -> None:
    agg = DailyAggregator()
    focus = focus_of(generate(SEPT).table)
    for batch in focus.to_batches(max_chunksize=50):  # merging partial batches is exact
        agg.update(pa.Table.from_batches([batch]))
    rows = agg.finish()
    assert {r.usage_date.day for r in rows} == set(range(1, 31))
    assert {r.account_id for r in rows} == {"111111111111", "222222222222", "333333333333"}
    digest, pairs = tag_hash([("user_env", "prod"), ("user_app", "web")])
    assert digest in agg.tag_sets
    assert agg.tag_sets[digest] == pairs == {"user_app": "web", "user_env": "prod"}
    assert agg.accounts["333333333333"] == "demo-dev"

    single = DailyAggregator()
    single.update(focus)
    assert sum(r.effective_cost for r in single.finish()) == sum(r.effective_cost for r in rows)


def quality_of(table: pa.Table, manifest_rows: int | None = None):  # type: ignore[no-untyped-def]
    checks = QualityChecks(SEPT.start.date())
    checks.schema(table.schema)
    batch = table.combine_chunks().to_batches()[0]
    checks.update(batch, normalise(batch))
    return checks.finish(manifest_rows if manifest_rows is not None else table.num_rows)


def test_clean_fixture_passes_quality_checks() -> None:
    checks = quality_of(generate(SEPT).table)
    assert not blocked(checks)
    assert {c.id for c in checks} >= {"schema", "completeness", "nulls", "duplicates"}


def test_duplicates_row_count_and_currency_block_the_load() -> None:
    table = generate(SEPT).table
    duplicated = pa.concat_tables([table, table.slice(0, 1)])
    assert [c.id for c in quality_of(duplicated) if c.status == "error"] == ["duplicates"]

    short = [c.id for c in quality_of(table, manifest_rows=table.num_rows + 5)]
    assert "completeness" in short
    assert blocked(quality_of(table, manifest_rows=table.num_rows + 5))

    idx = table.schema.get_field_index("line_item_currency_code")
    zar = table.set_column(idx, "line_item_currency_code", pa.array(["ZAR"] * table.num_rows))
    assert [c.id for c in quality_of(zar) if c.status == "error"] == ["currency"]


def test_unexpected_negative_costs_and_unknown_types_only_warn() -> None:
    table = generate(SEPT).table
    idx = table.schema.get_field_index("line_item_line_item_type")
    types = table["line_item_line_item_type"].to_pylist()
    types[0] = "BrandNewType"
    table = table.set_column(idx, "line_item_line_item_type", pa.array(types))
    cost_idx = table.schema.get_field_index("line_item_unblended_cost")
    costs = table["line_item_unblended_cost"].to_pylist()
    costs[types.index("Usage")] = -5.0
    table = table.set_column(cost_idx, "line_item_unblended_cost", pa.array(costs))
    checks = quality_of(table)
    assert not blocked(checks)
    warned = {c.id for c in checks if c.status == "warning"}
    assert warned == {"line_item_types", "negative_costs"}


def write_fixture(root: Path, restate: bool = False) -> None:
    write_export(generate(SEPT, restate=restate), root / "cloudoptics")


def test_load_export_from_a_folder_writes_focus_parquet(tmp_path: Path) -> None:
    write_fixture(tmp_path / "bucket")
    source = LocalExportSource(tmp_path / "bucket")
    keys = manifest_keys(source.list_keys("cloudoptics/"))
    (key,) = keys.values()
    manifest = parse_manifest(key, source.read_text(key), bucket=None, prefix="cloudoptics")
    assert manifest.export_version == "2026-09-v1"

    loaded = load_export(source, manifest, tmp_path / "norm")
    assert loaded.ok
    assert loaded.row_count == manifest.num_rows
    (part,) = (tmp_path / "norm").rglob("*.parquet")
    assert "billing_period=2026-09/version=2026-09-v1" in str(part)
    written = pq.read_table(part)
    assert written.num_rows == manifest.num_rows
    assert {"BilledCost", "EffectiveCost", "_source_file", "_data_version"} <= set(
        written.column_names
    )


def test_missing_required_column_blocks_without_reading_rows(tmp_path: Path) -> None:
    table = generate(SEPT).table.drop_columns(["line_item_usage_account_id"])
    root = tmp_path / "bucket"
    data = root / "p" / "x" / "data" / "BILLING_PERIOD=2026-09" / "x-00001.snappy.parquet"
    data.parent.mkdir(parents=True)
    pq.write_table(table, data)
    meta = root / "p" / "x" / "metadata" / "BILLING_PERIOD=2026-09" / "x-Manifest.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps({"executionId": "e1", "dataFiles": [f"s3://b/{data.relative_to(root)}"]})
    )
    source = LocalExportSource(root)
    manifest = parse_manifest(str(meta.relative_to(root)), meta.read_text(), bucket="b", prefix="p")
    loaded = load_export(source, manifest)
    assert not loaded.ok
    assert loaded.rows == []
    assert [c.id for c in loaded.checks if c.status == "error"] == ["schema"]


def test_manifest_parsing() -> None:
    key = "cur/exp/metadata/BILLING_PERIOD=2026-10/exp-Manifest.json"
    body = json.dumps(
        {
            "executionId": "abc",
            "dataFiles": ["s3://bkt/cur/exp/data/BILLING_PERIOD=2026-10/exp-00001.snappy.parquet"],
            "numRows": 10,
        }
    )
    m = parse_manifest(key, body, bucket="bkt", prefix="cur")
    assert m.billing_period.isoformat() == "2026-10-01"
    assert m.data_keys == ("cur/exp/data/BILLING_PERIOD=2026-10/exp-00001.snappy.parquet",)
    assert m.num_rows == 10
    with pytest.raises(ManifestError, match="outside"):
        parse_manifest(key, body, bucket="other", prefix="cur")
    with pytest.raises(ManifestError):
        parse_manifest(key, "{}", bucket="bkt", prefix="cur")
    with pytest.raises(ManifestError):
        parse_manifest(key, "not json", bucket="bkt", prefix="cur")
    assert manifest_keys(
        [key, "cur/exp/data/BILLING_PERIOD=2026-10/exp-00001.snappy.parquet", "cur/other.json"]
    ) == {m.billing_period: key}


def test_local_source_refuses_paths_outside_its_folder(tmp_path: Path) -> None:
    with pytest.raises(ManifestError):
        LocalExportSource(tmp_path).read_text("../secret")


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_paginator(self, name: str):  # type: ignore[no-untyped-def]
        objects = self.objects

        class Paginator:
            def paginate(self, Bucket: str, Prefix: str):  # type: ignore[no-untyped-def]  # noqa: N803
                keys = [k for k in sorted(objects) if k.startswith(Prefix)]
                yield {"Contents": [{"Key": k} for k in keys[:1]]}
                yield {"Contents": [{"Key": k} for k in keys[1:]]}
                yield {}

        return Paginator()

    def get_object(self, Bucket: str, Key: str):  # type: ignore[no-untyped-def]  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


def test_s3_source_pages_listings_and_hashes_downloads(tmp_path: Path) -> None:
    source = S3ExportSource(FakeS3({"p/a": b"hello", "p/b": b"{}", "q/c": b""}), "bkt")
    assert source.list_keys("p/") == ["p/a", "p/b"]
    assert source.read_text("p/b") == "{}"
    sha, size = source.download("p/a", tmp_path / "a")
    assert size == 5
    assert sha == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert (tmp_path / "a").read_bytes() == b"hello"


# --- limits on what a customer's bucket can make the worker read (audit run 1, 13 and 16) ---


def export_folder(root: Path, tables: list[pa.Table], execution: str = "e1") -> Manifest:
    """An export laid out under ``root`` (keys with forward slashes on every OS)."""
    keys = []
    for i, table in enumerate(tables):
        data = root / "p" / "x" / "data" / "BILLING_PERIOD=2026-09" / f"x-{i:05d}.parquet"
        data.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data)
        keys.append(f"s3://b/{data.relative_to(root).as_posix()}")
    meta = root / "p" / "x" / "metadata" / "BILLING_PERIOD=2026-09" / "x-Manifest.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"executionId": execution, "dataFiles": keys}))
    return parse_manifest(
        meta.relative_to(root).as_posix(), meta.read_text(), bucket="b", prefix="p"
    )


@pytest.fixture
def limits(monkeypatch):  # type: ignore[no-untyped-def]
    """The process settings, with limits a test may lower (restored afterwards)."""
    from cloudoptics.platform.settings import get_settings

    settings = get_settings()

    def set_limit(**values: int) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return set_limit


@pytest.mark.parametrize(
    "version", ["x/../../../tenant=victim/billing_period=2026-01/version=v", "..", ".", "a\b",
                "a b", "x" * 129]
)  # fmt: skip
def test_execution_id_must_be_one_plain_name(version: str) -> None:
    key = "cur/exp/metadata/BILLING_PERIOD=2026-10/exp-Manifest.json"
    body = json.dumps({"executionId": version, "dataFiles": []})
    with pytest.raises(ManifestError, match="plain name"):
        parse_manifest(key, body, bucket="bkt", prefix="cur")


def test_ordinary_execution_ids_are_kept() -> None:
    key = "cur/exp/metadata/BILLING_PERIOD=2026-10/exp-Manifest.json"
    for version in ("2026-09-v1", "a1b2c3d4-0000-4000-8000-0123456789ab", "run.1_x"):
        body = json.dumps({"executionId": version, "dataFiles": []})
        assert parse_manifest(key, body, bucket="bkt", prefix="cur").export_version == version


def test_normalised_output_stays_in_its_folder(tmp_path: Path) -> None:
    manifest = export_folder(tmp_path / "bucket", [generate(SEPT).table])
    # The audit's example: into another tenant's folder next to this one.
    escaping = replace(
        manifest, export_version="x/../../../tenant=victim/billing_period=2026-01/version=v"
    )
    norm = tmp_path / "norm" / "tenant=attacker"
    with pytest.raises(ManifestError, match="outside its folder"):
        load_export(LocalExportSource(tmp_path / "bucket"), escaping, norm)
    assert not (tmp_path / "norm" / "tenant=victim").exists()


def test_rows_files_and_bytes_are_limited(tmp_path: Path, limits) -> None:  # type: ignore[no-untyped-def]
    table = generate(SEPT).table
    source = LocalExportSource(tmp_path / "bucket")
    manifest = export_folder(tmp_path / "bucket", [table, table.slice(0, 10)])
    assert load_export(source, manifest).row_count == table.num_rows + 10

    limits(cur_max_period_rows=table.num_rows)
    with pytest.raises(ExportTooLargeError, match="rows in the period"):
        load_export(source, manifest)
    # A manifest that says so up front is refused before anything is downloaded.
    with pytest.raises(ExportTooLargeError, match="lists"):
        load_export(LocalExportSource(tmp_path / "nothing"), replace(manifest, num_rows=10**9))

    limits(cur_max_period_rows=10**9, cur_max_period_bytes=100)
    with pytest.raises(ExportTooLargeError, match="byte limit for a period"):
        load_export(source, manifest)

    limits(cur_max_data_files=1)
    with pytest.raises(ExportTooLargeError, match="2 data files"):
        export_folder(tmp_path / "other", [table, table])

    limits(cur_max_manifest_bytes=10)
    with pytest.raises(ExportTooLargeError, match="Manifest"):
        source.read_text(manifest.key)


def test_s3_reads_stop_at_the_limit(tmp_path: Path, limits) -> None:  # type: ignore[no-untyped-def]
    source = S3ExportSource(FakeS3({"p/a": b"hello", "p/m": b"{" + b" " * 40 + b"}"}), "bkt")
    with pytest.raises(ExportTooLargeError):
        source.download("p/a", tmp_path / "a", max_bytes=3)
    assert source.download("p/a", tmp_path / "a", max_bytes=5)[1] == 5
    limits(cur_max_manifest_bytes=20)
    with pytest.raises(ExportTooLargeError, match="Manifest"):
        source.read_text("p/m")


def test_duplicates_are_found_across_batches_and_files(tmp_path: Path) -> None:
    table = generate(SEPT).table
    source = LocalExportSource(tmp_path / "bucket")
    manifest = export_folder(tmp_path / "bucket", [table, table.slice(5, 3)])
    loaded = load_export(source, manifest)
    [dup] = [c for c in loaded.checks if c.id == "duplicates"]
    assert (dup.status, dup.message.split()[0]) == ("error", "3")

    checks = QualityChecks(SEPT.start.date())
    focus = focus_of(table)
    raw = table.combine_chunks().to_batches()[0]
    for start in range(0, table.num_rows, 7):  # many small batches, no duplicates
        checks.update(raw.slice(start, 7), focus.slice(start, 7))
    spill = checks._spill
    assert spill is not None
    [dup] = [c for c in checks.finish(None) if c.id == "duplicates"]
    assert dup.status == "ok"
    assert not Path(spill.name).exists()  # the fingerprint files are removed


def test_only_real_account_ids_become_accounts() -> None:
    focus = focus_of(generate(SEPT).table)
    ids = focus["SubAccountId"].to_pylist()
    bad = ["12345", "abcdefghijkl", "1234567890123", "١٢٣٤٥٦٧٨٩٠١٢"]
    for i, value in enumerate(bad):
        ids[i] = value
    idx = focus.schema.get_field_index("SubAccountId")
    agg = DailyAggregator()
    agg.update(focus.set_column(idx, "SubAccountId", pa.array(ids)))
    assert set(agg.accounts) == {"111111111111", "222222222222", "333333333333"}
