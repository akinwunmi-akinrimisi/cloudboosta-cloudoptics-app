"""``cur-fixture generate --out DIR --period 2026-09 [--seed 7] [--restate]``"""

from __future__ import annotations

import argparse
from pathlib import Path

from cur_fixture.generator import BillingPeriod, generate
from cur_fixture.writer import write_export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cur-fixture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="write one billing period in BCM export layout")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--period", type=BillingPeriod.parse, required=True, help="YYYY-MM")
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--restate", action="store_true", help="write version 2 with a refund")
    gen.add_argument("--export-name", default="cloudoptics-cur2")
    args = parser.parse_args(argv)

    fixture = generate(args.period, seed=args.seed, restate=args.restate)
    manifest = write_export(fixture, args.out, args.export_name)
    print(f"{fixture.table.num_rows} rows, version {fixture.version} -> {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
