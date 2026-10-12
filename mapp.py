#!/usr/bin/env python3
"""Enrich FPL "Not Present Base" Excel files from ``datas.xlsx``.

The source files are joined with the lookup workbook as follows::

    source.client_account_id = lookup.clientAccountId

For each matching row, missing values are filled for ``loan_id``,
``account_id``, ``user_id`` and ``um_uuid``. Existing non-blank values are
never overwritten. A source workbook is skipped, without creating an output
copy, when any row already contains a non-blank ``loan_id``; this prevents an
already-processed workbook from being changed on a later run.

The output directory contains one enriched workbook per processed source file
and ``enrichment_summary.xlsx``. The summary workbook has:

* ``file_summary``: one row per input workbook with found/not-found counts;
* ``totals``: overall found/not-found counts and processing counts.

The original source files are never overwritten.

Example (run on the user's Mac):

    python3 enrich_not_present_base.py \
        --input-dir "/Users/dylan.dias/Desktop/FPL Recon/Not Present Base" \
        --mapping-file "/Users/dylan.dias/Desktop/FPL Recon/Data_Vals/datas.xlsx"

Dependencies: ``pandas`` and ``openpyxl``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd


LOOKUP_COLUMNS = ["clientAccountId", "loan_id", "user_id", "um_uuid", "account_id"]
TARGET_COLUMNS = ["loan_id", "account_id", "user_id", "um_uuid"]
SOURCE_REQUIRED_COLUMNS = ["client_account_id"]
OUTPUT_CORE_COLUMNS = [
    "fpl_id",
    "fpl_account_id",
    "client_account_id",
    "merchanttxnid",
    "transaction_type",
    "transaction_status",
    "amount",
    "lastmodified_date",
    "90_amount",
    "parent_txn_id",
    "loan_id",
    "account_id",
    "user_id",
    "um_uuid",
]
SUMMARY_COLUMNS = [
    "source_file",
    "status",
    "source_rows",
    "found_rows",
    "not_found_rows",
    "matched_rows",
    "unmatched_rows",
    "existing_loan_id_rows",
    "filled_loan_id_rows",
    "filled_account_id_rows",
    "filled_user_id_rows",
    "filled_um_uuid_rows",
    "output_file",
    "details",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fill missing FPL identifiers from datas.xlsx using "
            "client_account_id = clientAccountId."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("/Users/dylan.dias/Desktop/FPL Recon/Not Present Base"),
        help="Directory containing the source Excel files.",
    )
    parser.add_argument(
        "--mapping-file",
        type=Path,
        default=Path("/Users/dylan.dias/Desktop/FPL Recon/Data_Vals/datas.xlsx"),
        help="Lookup workbook containing datas.xlsx.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory; defaults to input-dir/processed.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=None,
        help="Summary workbook; defaults to output-dir/enrichment_summary.xlsx.",
    )
    return parser.parse_args()


def validate_columns(
    dataframe: pd.DataFrame, required_columns: Iterable[str], file_path: Path
) -> None:
    duplicate_columns = dataframe.columns[dataframe.columns.duplicated()].tolist()
    if duplicate_columns:
        raise ValueError(
            f"{file_path.name} contains duplicate column name(s): "
            f"{sorted(set(duplicate_columns))}"
        )

    missing = [column for column in required_columns if column not in dataframe.columns]
    if missing:
        raise ValueError(
            f"{file_path.name} is missing required column(s): {', '.join(missing)}"
        )


def normalize_key(values: pd.Series) -> pd.Series:
    """Normalize identifiers without converting them through floating point."""
    normalized = values.astype("string").str.strip()
    normalized = normalized.replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
    # Excel commonly exposes an integer identifier as 123.0. Remove only that
    # exact suffix; do not change genuine decimal-looking identifiers.
    return normalized.str.replace(r"^([+-]?\d+)\.0$", r"\1", regex=True)


def non_blank_mask(values: pd.Series) -> pd.Series:
    text = values.astype("string").str.strip()
    return text.notna() & text.ne("") & text.ne("<NA>")


def load_lookup(mapping_file: Path) -> pd.DataFrame:
    if not mapping_file.is_file():
        raise FileNotFoundError(f"Lookup workbook not found: {mapping_file}")

    lookup = pd.read_excel(mapping_file, dtype="string", engine="openpyxl")
    validate_columns(lookup, LOOKUP_COLUMNS, mapping_file)
    lookup = lookup[LOOKUP_COLUMNS].copy()
    lookup["_join_key"] = normalize_key(lookup["clientAccountId"])

    blank_key = ~non_blank_mask(lookup["_join_key"])
    if blank_key.any():
        print(
            f"Warning: ignoring {int(blank_key.sum()):,} lookup row(s) with "
            "blank clientAccountId.",
            file=sys.stderr,
        )
        lookup = lookup.loc[~blank_key].copy()

    duplicate_mask = lookup["_join_key"].duplicated(keep=False)
    if duplicate_mask.any():
        duplicate_counts = lookup.loc[duplicate_mask, "_join_key"].value_counts()
        examples = list(duplicate_counts.head(10).items())
        raise ValueError(
            "Lookup contains duplicate clientAccountId values. Refusing to "
            f"choose an arbitrary row: {examples}"
        )

    return lookup.set_index("_join_key", drop=True)


def excel_files_in(directory: Path) -> list[Path]:
    """Return direct .xlsx/.xlsm inputs, excluding temporary files."""
    return sorted(
        path
        for pattern in ("*.xlsx", "*.xlsm")
        for path in directory.glob(pattern)
        if path.is_file() and not path.name.startswith("~$")
    )


def reorder_output_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Put the requested FPL columns first and retain any extra source columns."""
    first = [column for column in OUTPUT_CORE_COLUMNS if column in dataframe.columns]
    remaining = [column for column in dataframe.columns if column not in first]
    return dataframe[first + remaining]


def match_counts(source: pd.DataFrame, lookup: pd.DataFrame) -> tuple[pd.Series, int, int]:
    join_key = normalize_key(source["client_account_id"])
    found_mask = join_key.isin(lookup.index)
    return join_key, int(found_mask.sum()), int((~found_mask).sum())


def fill_missing_values(
    source: pd.DataFrame, lookup: pd.DataFrame, join_key: pd.Series
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Fill target columns only where the source value is blank."""
    result = source.copy()
    filled_counts: dict[str, int] = {}

    for column in TARGET_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA

        mapped_values = join_key.map(lookup[column])
        fill_mask = ~non_blank_mask(result[column]) & non_blank_mask(mapped_values)
        result.loc[fill_mask, column] = mapped_values.loc[fill_mask]
        filled_counts[column] = int(fill_mask.sum())

    return reorder_output_columns(result), filled_counts


def process_file(
    input_file: Path,
    output_file: Path,
    lookup: pd.DataFrame,
) -> dict[str, object]:
    source = pd.read_excel(input_file, dtype=object, engine="openpyxl")
    validate_columns(source, SOURCE_REQUIRED_COLUMNS, input_file)

    join_key, found_rows, not_found_rows = match_counts(source, lookup)
    existing_loan_id_rows = (
        int(non_blank_mask(source["loan_id"]).sum())
        if "loan_id" in source.columns
        else 0
    )

    # Per request, an already-populated workbook is not processed again. The
    # found/not-found counts are still reported so every input is accounted for.
    if existing_loan_id_rows:
        return {
            "source_file": input_file.name,
            "status": "SKIPPED_EXISTING_LOAN_ID",
            "source_rows": len(source),
            "found_rows": found_rows,
            "not_found_rows": not_found_rows,
            "matched_rows": found_rows,
            "unmatched_rows": not_found_rows,
            "existing_loan_id_rows": existing_loan_id_rows,
            "filled_loan_id_rows": 0,
            "filled_account_id_rows": 0,
            "filled_user_id_rows": 0,
            "filled_um_uuid_rows": 0,
            "output_file": "",
            "details": (
                "Skipped because at least one row already contains a non-blank "
                "loan_id; no source file was overwritten."
            ),
        }

    result, filled_counts = fill_missing_values(source, lookup, join_key)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    result.to_excel(output_file, index=False, engine="openpyxl")

    return {
        "source_file": input_file.name,
        "status": "PROCESSED",
        "source_rows": len(source),
        "found_rows": found_rows,
        "not_found_rows": not_found_rows,
        "matched_rows": found_rows,
        "unmatched_rows": not_found_rows,
        "existing_loan_id_rows": existing_loan_id_rows,
        "filled_loan_id_rows": filled_counts["loan_id"],
        "filled_account_id_rows": filled_counts["account_id"],
        "filled_user_id_rows": filled_counts["user_id"],
        "filled_um_uuid_rows": filled_counts["um_uuid"],
        "output_file": str(output_file),
        "details": "Existing non-blank target values were preserved.",
    }


def write_summary(summary_file: Path, rows: list[dict[str, object]]) -> None:
    summary = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    processed_rows = summary.loc[summary["status"] == "PROCESSED"]
    skipped_rows = summary.loc[summary["status"] == "SKIPPED_EXISTING_LOAN_ID"]

    totals = pd.DataFrame(
        [
            {"metric": "source_files", "value": len(summary)},
            {"metric": "processed_files", "value": len(processed_rows)},
            {
                "metric": "skipped_files_existing_loan_id",
                "value": len(skipped_rows),
            },
            {
                "metric": "source_rows",
                "value": int(summary["source_rows"].sum()) if len(summary) else 0,
            },
            {
                "metric": "found_rows",
                "value": int(summary["found_rows"].sum()) if len(summary) else 0,
            },
            {
                "metric": "not_found_rows",
                "value": int(summary["not_found_rows"].sum()) if len(summary) else 0,
            },
            {
                "metric": "filled_loan_id_rows",
                "value": int(summary["filled_loan_id_rows"].sum()) if len(summary) else 0,
            },
            {
                "metric": "filled_account_id_rows",
                "value": int(summary["filled_account_id_rows"].sum()) if len(summary) else 0,
            },
            {
                "metric": "filled_user_id_rows",
                "value": int(summary["filled_user_id_rows"].sum()) if len(summary) else 0,
            },
            {
                "metric": "filled_um_uuid_rows",
                "value": int(summary["filled_um_uuid_rows"].sum()) if len(summary) else 0,
            },
        ]
    )

    summary_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(summary_file, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="file_summary", index=False)
        totals.to_excel(writer, sheet_name="totals", index=False)

    # Read-back verification prevents reporting success if the workbook is
    # missing or has the wrong number of rows/sheets.
    check_summary = pd.read_excel(
        summary_file, sheet_name="file_summary", engine="openpyxl"
    )
    check_totals = pd.read_excel(
        summary_file, sheet_name="totals", engine="openpyxl"
    )
    if len(check_summary) != len(summary) or len(check_totals) != len(totals):
        raise RuntimeError(
            "Summary verification failed: the written workbook has an "
            "unexpected number of rows."
        )


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser()
    mapping_file = args.mapping_file.expanduser()
    output_dir = (
        args.output_dir.expanduser()
        if args.output_dir is not None
        else input_dir / "processed"
    )
    summary_file = (
        args.summary_file.expanduser()
        if args.summary_file is not None
        else output_dir / "enrichment_summary.xlsx"
    )

    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    output_resolved = output_dir.resolve()
    summary_resolved = summary_file.resolve()
    files = [
        path
        for path in excel_files_in(input_dir)
        if path.resolve() != summary_resolved
        and path.resolve().parent != output_resolved
    ]
    if not files:
        raise FileNotFoundError(f"No source Excel files found in: {input_dir}")

    lookup = load_lookup(mapping_file)
    print(f"Lookup rows loaded: {len(lookup):,}")
    print(f"Source files found: {len(files)}")
    print(f"Output directory: {output_dir}")

    summary_rows: list[dict[str, object]] = []
    for input_file in files:
        row = process_file(input_file, output_dir / input_file.name, lookup)
        summary_rows.append(row)
        print(
            f"{row['status']}: {input_file.name} | rows={row['source_rows']:,} | "
            f"found={row['found_rows']:,} | not_found={row['not_found_rows']:,}"
        )

    write_summary(summary_file, summary_rows)

    total_rows = sum(int(row["source_rows"]) for row in summary_rows)
    total_found = sum(int(row["found_rows"]) for row in summary_rows)
    total_not_found = sum(int(row["not_found_rows"]) for row in summary_rows)
    processed = sum(row["status"] == "PROCESSED" for row in summary_rows)
    skipped = sum(row["status"] == "SKIPPED_EXISTING_LOAN_ID" for row in summary_rows)
    print(
        f"Completed: {processed} processed, {skipped} skipped; "
        f"rows={total_rows:,}, found={total_found:,}, "
        f"not_found={total_not_found:,}."
    )
    print(f"Summary: {summary_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
