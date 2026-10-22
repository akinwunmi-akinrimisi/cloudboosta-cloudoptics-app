import json
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cur_fixture import BillingPeriod, generate, write_export
from cur_fixture.cli import main
from cur_fixture.generator import SP_DISCOUNT, SP_UTILISATION
from cur_fixture.schema import CUR2_SCHEMA, LINE_ITEM_TYPES

SEPT = BillingPeriod(2026, 9)


def rows_where(table, column, value):  # type: ignore[no-untyped-def]
    return table.filter(pc.equal(table[column], value))


def test_schema_and_line_item_types() -> None:
    fx = generate(SEPT)
    assert fx.table.schema == CUR2_SCHEMA
    assert set(fx.table["line_item_line_item_type"].to_pylist()) <= LINE_ITEM_TYPES


def test_is_deterministic_for_a_seed() -> None:
    assert generate(SEPT, seed=3).table.equals(generate(SEPT, seed=3).table)
    assert not generate(SEPT, seed=3).table.equals(generate(SEPT, seed=4).table)


def test_line_item_ids_are_unique() -> None:
    ids = generate(SEPT).table["identity_line_item_id"].to_pylist()
    assert len(ids) == len(set(ids))


def test_savings_plan_negation_offsets_covered_usage_and_fee_reflects_utilisation() -> None:
    t = generate(SEPT).table
    covered = rows_where(t, "line_item_line_item_type", "SavingsPlanCoveredUsage")
    negation = rows_where(t, "line_item_line_item_type", "SavingsPlanNegation")
    fee = rows_where(t, "line_item_line_item_type", "SavingsPlanRecurringFee")
    covered_cost = pc.sum(covered["line_item_unblended_cost"]).as_py()
    assert covered_cost + pc.sum(negation["line_item_unblended_cost"]).as_py() == pytest.approx(0)
    effective = pc.sum(covered["savings_plan_savings_plan_effective_cost"]).as_py()
    assert effective == pytest.approx(covered_cost * (1 - SP_DISCOUNT), rel=1e-6)
    assert pc.sum(fee["line_item_unblended_cost"]).as_py() == pytest.approx(
        effective / SP_UTILISATION, rel=1e-6
    )


def test_idle_nat_has_hours_but_no_bytes() -> None:
    t = generate(SEPT).table
    idle = rows_where(t, "line_item_resource_id", "nat-0idle00000000001")
    usage_types = set(idle["line_item_usage_type"].to_pylist())
    assert usage_types == {"AFS1-NatGateway-Hours"}
    busy = rows_where(t, "line_item_resource_id", "nat-0busy00000000001")
    assert "AFS1-NatGateway-Bytes" in set(busy["line_item_usage_type"].to_pylist())


def test_covers_af_south_1_local_zone_and_eu() -> None:
    t = generate(SEPT).table
    assert {"af-south-1", "eu-west-1"} <= set(t["product_region_code"].to_pylist())
    assert "af-south-1-los-1a" in set(t["line_item_availability_zone"].to_pylist())


def test_credits_are_negative_and_tax_is_positive() -> None:
    t = generate(SEPT).table
    credits = rows_where(t, "line_item_line_item_type", "Credit")
    assert pc.max(credits["line_item_unblended_cost"]).as_py() < 0
    tax = rows_where(t, "line_item_line_item_type", "Tax")
    assert tax.num_rows == 2
    assert pc.min(tax["line_item_unblended_cost"]).as_py() > 0


def test_restatement_adds_a_refund_and_keeps_other_lines() -> None:
    v1, v2 = generate(SEPT), generate(SEPT, restate=True)
    assert v2.version == 2
    assert v2.table.num_rows == v1.table.num_rows + 1
    assert rows_where(v2.table, "line_item_line_item_type", "Refund").num_rows == 1
    assert set(v1.table["identity_line_item_id"].to_pylist()) < set(
        v2.table["identity_line_item_id"].to_pylist()
    )


def test_expectations_cover_the_increment_rules() -> None:
    rules = {r for e in generate(SEPT).expectations for r in e["expected_rules"]}
    assert {"R1", "R2", "R3", "R6", "R6b", "R7", "R10"} <= rules


@settings(max_examples=25, deadline=None)
@given(year=st.integers(2020, 2035), month=st.integers(1, 12))
def test_every_usage_row_falls_inside_its_billing_period(year: int, month: int) -> None:
    period = BillingPeriod(year, month)
    t = generate(period).table
    starts = t["line_item_usage_start_date"]
    assert pc.min(starts).as_py() == period.start
    assert pc.max(t["line_item_usage_end_date"]).as_py() == period.end


def test_writer_layout_and_manifest(tmp_path: Path) -> None:
    manifest_path = write_export(generate(SEPT, restate=True), tmp_path)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["executionId"] == "2026-09-v2"
    data = pq.read_table(tmp_path / manifest["dataFiles"][0])
    assert data.num_rows == manifest["numRows"]
    expectations = tmp_path / "cloudoptics-cur2" / "expectations" / "BILLING_PERIOD=2026-09.json"
    assert expectations.exists()


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", "--out", str(tmp_path), "--period", "2026-02"]) == 0
    assert "version 1" in capsys.readouterr().out
    assert (tmp_path / "cloudoptics-cur2" / "data" / "BILLING_PERIOD=2026-02").is_dir()
