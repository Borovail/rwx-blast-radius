#!/usr/bin/env python3
"""Reproduce the difference between owned and shared SQLGlot expression trees."""

import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo.resolve()))

    from sqlglot import Dialect, exp, parse_one
    from sqlglot.generators.duckdb import _SEQ_BYTE_WIDTH

    def literals(expression):
        return {literal.this for literal in expression.find_all(exp.Literal)}

    sql = "SELECT SEQ8() AS stream_index FROM TABLE(GENERATOR(ROWCOUNT => 17))"
    original_width = _SEQ_BYTE_WIDTH[exp.Seq8]
    original_max = str(2 ** (original_width * 8))
    changed_max = str(2 ** (3 * 8))
    baseline = parse_one(sql, read="snowflake")
    assert original_max in baseline.sql("duckdb", copy=False)
    assert original_max in literals(baseline)
    try:
        _SEQ_BYTE_WIDTH[exp.Seq8] = 3
        shared = parse_one(sql, read="snowflake")
        # A later caller can read the changed AST without reading the table.
        assert changed_max in shared.sql("duckdb", copy=False)
        assert changed_max in literals(shared) and original_max not in literals(shared)
        owned = parse_one(sql, read="snowflake")
        before = owned.dump()
        assert changed_max in owned.sql("duckdb")
        assert owned.dump() == before and changed_max not in literals(owned)
    finally:
        _SEQ_BYTE_WIDTH[exp.Seq8] = original_width

    defaults = Dialect.SET_OP_DISTINCT_BY_DEFAULT
    base = defaults[exp.Union]
    union = "SELECT 12 AS n UNION SELECT 13"
    previous = parse_one(union)
    assert previous.args["distinct"] is base
    try:
        defaults[exp.Union] = not base
        current = parse_one(union)
        assert current.args["distinct"] is not base
        assert previous.args["distinct"] is base
    finally:
        defaults[exp.Union] = base

    print("DuckDB copy=False retains the AST change. The default copy preserves the input.")
    print("The set-operation parser constructs a new AST with the changed boolean.")


if __name__ == "__main__":
    main()
