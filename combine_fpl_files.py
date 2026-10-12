from pathlib import Path
import sys

import pandas as pd


# =========================
# Editable paths
# =========================
INPUT_DIR = Path(
    "/Users/dylan.dias/Desktop/FPL Recon/Not Present Base_This is the main file"
)
MAPPING_FILE = Path(
    "/Users/dylan.dias/Desktop/FPL Recon/v3/account_mapping_final.xlsx"
)
OUTPUT_FILE = Path(
    "/Users/dylan.dias/Desktop/FPL Recon/combined_all_xlsx.xlsx"
)


MAIN_KEY = "client_account_id"
MAPPING_KEY = "clientAccountId"
MAPPING_COLUMNS = ["loan_id", "user_id", "um_uuid", "account_id"]
EXPECTED_SOURCE_COLUMNS = [
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
SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}


def clean_key(series: pd.Series) -> pd.Series:
    """Convert join keys to comparable strings while keeping blanks blank."""
    cleaned = series.astype("string").str.strip()
    # Excel may load an integer-like identifier as text such as "123.0".
    # Normalize only that safe pattern while preserving other leading-zero IDs.
    integer_like = cleaned.str.fullmatch(r"[+-]?\d+\.0+")
    cleaned = cleaned.mask(integer_like, cleaned.str.replace(r"\.0+$", "", regex=True))
    return cleaned.mask(cleaned.isna() | cleaned.eq(""), pd.NA)


def read_all_sheets(file_path: Path) -> list[pd.DataFrame]:
    """Read every worksheet from one Excel file."""
    sheets = pd.read_excel(file_path, sheet_name=None, dtype=object)
    frames = []

    for sheet_name, frame in sheets.items():
        if frame.empty and len(frame.columns) == 0:
            continue
        frame = frame.copy()
        # Some source files do not contain um_uuid or another expected field.
        # Add missing fields as blank columns so every output sheet has a
        # consistent schema.
        for column in EXPECTED_SOURCE_COLUMNS:
            if column not in frame.columns:
                frame[column] = pd.NA
        frame["_source_file"] = file_path.name
        frame["_source_sheet"] = sheet_name
        frames.append(frame)

    return frames


def validate_columns(frame: pd.DataFrame, required: list[str], file_label: str) -> None:
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(
            f"{file_label} is missing required column(s): {', '.join(missing)}"
        )


def main() -> None:
    if not INPUT_DIR.exists() or not INPUT_DIR.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {INPUT_DIR}")
    if not MAPPING_FILE.exists():
        raise FileNotFoundError(f"Mapping file does not exist: {MAPPING_FILE}")

    print(f"Reading mapping file: {MAPPING_FILE}")
    mapping = pd.read_excel(MAPPING_FILE, dtype=object)
    validate_columns(
        mapping,
        [MAPPING_KEY, *MAPPING_COLUMNS],
        str(MAPPING_FILE),
    )

    mapping = mapping.copy()
    mapping["_join_key"] = clean_key(mapping[MAPPING_KEY])
    mapping = mapping[mapping["_join_key"].notna()].copy()

    duplicate_keys = mapping.loc[
        mapping["_join_key"].duplicated(keep=False), "_join_key"
    ].drop_duplicates()
    if not duplicate_keys.empty:
        examples = ", ".join(duplicate_keys.astype(str).head(10).tolist())
        raise ValueError(
            "Duplicate clientAccountId values were found in the mapping file. "
            "The script stopped so that source rows are not duplicated. "
            f"Examples: {examples}"
        )

    mapping_lookup = mapping[["_join_key", *MAPPING_COLUMNS]].copy()
    mapping_lookup = mapping_lookup.rename(
        columns={column: f"_mapped_{column}" for column in MAPPING_COLUMNS}
    )

    input_files = sorted(
        file_path
        for file_path in INPUT_DIR.iterdir()
        if file_path.is_file()
        and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not file_path.name.startswith("~$")
        and file_path.resolve() != OUTPUT_FILE.resolve()
        and file_path.name != MAPPING_FILE.name
    )

    if not input_files:
        raise FileNotFoundError(f"No Excel files were found in: {INPUT_DIR}")

    print(f"Found {len(input_files)} input Excel file(s).")
    all_frames = []

    for number, file_path in enumerate(input_files, start=1):
        print(f"[{number}/{len(input_files)}] Reading {file_path.name}")
        frames = read_all_sheets(file_path)
        for frame in frames:
            validate_columns(frame, [MAIN_KEY], f"{file_path.name}")
            all_frames.append(frame)

    if not all_frames:
        raise ValueError("The input Excel files did not contain any data rows.")

    combined = pd.concat(all_frames, ignore_index=True, sort=False)
    combined["_join_key"] = clean_key(combined[MAIN_KEY])

    # many-to-one validation is intentional: every mapping key must be unique,
    # while source rows are preserved exactly as supplied.
    result = combined.merge(
        mapping_lookup,
        how="left",
        on="_join_key",
        sort=False,
        indicator=True,
        validate="many_to_one",
    )

    matched_mask = result["_merge"].eq("both")
    matched_count = int(matched_mask.sum())
    not_matched_count = int((~matched_mask).sum())

    # For matched rows, use mapping values when they are populated. If a mapped
    # value is blank, retain the value already present in the source row.
    for column in MAPPING_COLUMNS:
        mapped_column = f"_mapped_{column}"
        if column not in result.columns:
            result[column] = pd.NA
        result[column] = result[mapped_column].combine_first(result[column])

    result = result.drop(
        columns=[
            "_join_key",
            "_merge",
            *[f"_mapped_{column}" for column in MAPPING_COLUMNS],
        ]
    )

    # Keep the business columns only. The input-file and worksheet metadata are
    # used while reading but are not written to the requested output.
    result = result.drop(columns=["_source_file", "_source_sheet"], errors="ignore")

    # Put the expected business columns first and retain any additional source
    # columns after them.
    ordered_columns = [column for column in EXPECTED_SOURCE_COLUMNS if column in result.columns]
    ordered_columns.extend(column for column in result.columns if column not in ordered_columns)
    result = result[ordered_columns]

    match = result.loc[matched_mask].copy()
    not_match = result.loc[~matched_mask].copy()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        match.to_excel(writer, sheet_name="match", index=False)
        not_match.to_excel(writer, sheet_name="not_match", index=False)

    print("\nCompleted successfully.")
    print(f"Output file: {OUTPUT_FILE}")
    print(f"Rows in match: {matched_count}")
    print(f"Rows in not_match: {not_matched_count}")
    print(f"Total rows: {len(result)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
