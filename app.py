from __future__ import annotations

import io
import os
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for

from matching import enrich_row, match_name
from kyc_document import KYCDocumentError, fetch_kyc_document, SUPPORTED_TYPES, normalize_document_type, extract_xml_from_metadata
from document_checks import (APPLICATION_COLUMNS, KFS_COLUMNS, GTC_COLUMNS, apply_application_matches, apply_kfs_matches,
                             extract_application_from_url, extract_kfs_from_url, check_gtc_url)
from mitc_extraction import (
    MITC_COLUMNS, LP_MITC_COLUMNS, MITC_PROFILE_COLUMNS, MITC_WORKER_COUNT, MITCDocumentError, empty_lp_mitc_fields,
    extract_lp_mitc_from_url, extract_sanction_from_url, extract_sanction_from_pdf_bytes, fetch_mitc_pdf, _validate_url,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
APP_VERSION = "v1.4.25-checkbox-document-slots"
OUTPUT_FILENAME = f"kyc_matching_output_{APP_VERSION}.xlsx"
def extract_mitc_from_url(url: Any) -> dict[str, Any]:
    """Run only the LP parser for the active profile.

    ``empty_lp_mitc_fields`` retains blank/neutral generic workbook columns for
    compatibility; legacy FPL-style values are not parsed or displayed as LP.
    """
    return extract_lp_mitc_from_url(url)
LEGACY_REQUIRED_COLUMNS = [
    "user_id", "um_uuid", "lp_uuid", "tenant", "primary_mobile", "user_name", "user_dob", "user_gender",
    "user_perm_address", "user_curr_address", "mitc_type", "mitc_date_latest", "application_form_link",
    "mitc_form_link", "gtc_link", "sanction_letter_link", "kfs_link", "kyc_status_type", "products",
    "kyc_mode", "kyc_status", "kyc_name", "kyc_father_name", "kyc_dob", "kyc_gender",
    "kyc_permanent_address", "kyc_current_address",
]
# The expanded schema is accepted as a complete set; an older workbook with
# none of these additions remains valid for backward compatibility.
EXPANDED_OPTIONAL_COLUMNS = [
    "user_selfie", "kyc_selfie_link", "kyc_doc", "kyc_doc_link", "other_metadata",
    "verification_status", "validation_type", "nsdl_date",
    "user_selfie_doc", "all_doc_type", "all_other_metadata", "all_s3_url", "info_flag",
]
REQUIRED_COLUMNS = [
    "user_id", "um_uuid", "lp_uuid", "tenant", "primary_mobile", "user_name", "user_dob", "user_gender",
    "user_selfie", "user_perm_address", "user_curr_address", "mitc_type", "mitc_date_latest", "application_form_link",
    "mitc_form_link", "gtc_link", "sanction_letter_link", "kfs_link", "kyc_status_type", "products",
    "kyc_mode", "kyc_status", "kyc_name", "kyc_father_name", "kyc_dob", "kyc_gender",
    "kyc_permanent_address", "kyc_current_address", "kyc_selfie_link", "kyc_doc", "kyc_doc_link",
    "other_metadata", "verification_status", "validation_type", "nsdl_date",
    "user_selfie_doc", "all_doc_type", "all_other_metadata", "all_s3_url", "info_flag",
]
REVIEW_COLUMNS = [
    "user_details_validated", "user_details_comments", "kyc_details_validated",
    "kyc_details_comments", "kyc_face_match_validated", "form_validated", "form_comments",
]
REVIEW_COMMENT_MAX_LENGTH = 10_000
MATCH_COLUMNS = [
    "name_match_status", "name_match_score", "dob_match_status", "dob_match_score",
    "permanent_address_match_status", "permanent_address_match_score",
    "current_address_match_status", "current_address_match_score",
    "user_gender_normalized", "kyc_gender_normalized", "gender_match", "gender_match_score",
    "overall_match_status",
]

app = Flask(__name__)
app.secret_key = __name__
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
STATE: dict[str, Any] = {"df": None, "document_rows": None, "filename": None, "path": None, "generation": 0}
STATE_LOCK = threading.RLock()
WORKBOOK_WRITE_LOCK = threading.Lock()
WORKBOOK_WRITE_DEBOUNCE_SECONDS = 0.4
_PENDING_WORKBOOK_WRITE: threading.Timer | None = None
MITC_EXECUTOR = ThreadPoolExecutor(max_workers=MITC_WORKER_COUNT, thread_name_prefix="documents")
SANCTION_COLUMNS = [
    "sanction_letter_date", "sanction_letter_reference_number", "sanction_borrower_name",
    "sanction_borrower_name_match_status", "sanction_borrower_name_match_score",
    "sanction_borrower_details_match", "sanction_letterhead_logo_check", "sanction_letterhead_logo_evidence",
    "sanction_digital_signature_check", "sanction_digital_signature_evidence",
    "sanction_annexure_terms_check", "sanction_annexure_terms_evidence",
    "sanction_validation_status", "sanction_validation_error",
]
SANCTION_WORKER_COUNT = MITC_WORKER_COUNT
KYC_DOCUMENT_COLUMNS = ["kyc_document_status", "kyc_document_error", "kyc_document_source_type"]
_APPLICATION_INFLIGHT: dict[str, Future] = {}
_KFS_INFLIGHT: dict[str, Future] = {}
_GTC_INFLIGHT: dict[str, Future] = {}
# One future per URL means duplicate rows share one download/parse operation.
_INFLIGHT_MITC: dict[str, Future] = {}
_INFLIGHT_SANCTION: dict[str, Future] = {}


def _has_mitc_link(value: Any) -> bool:
    """Return whether a row has a nonblank MITC link for denominator counts."""
    if value is None or pd.isna(value):
        return False
    return str(value).strip().casefold() not in {"", "nan", "nat", "none", "null"}


def _mitc_status_summary(df: pd.DataFrame | None) -> dict[str, Any]:
    """Build truthful, row-state metrics for the live extraction header.

    ``eligible`` is deliberately based on a nonblank source link, while
    ``extracted`` only includes terminal parser states documented by the
    extractor. No queued or extracting row contributes to extracted.
    """
    if df is None:
        total_records = 0
        statuses: list[str] = []
        eligible = 0
    else:
        total_records = int(len(df))
        links = df["mitc_form_link"] if "mitc_form_link" in df.columns else pd.Series([], dtype=object)
        eligible = int(sum(_has_mitc_link(value) for value in links))
        statuses = [str(value) if not pd.isna(value) else ""
                    for value in (df["mitc_extraction_status"] if "mitc_extraction_status" in df.columns else [])]

    counts = {status: statuses.count(status) for status in
              ("success", "parsed_with_missing_fields", "failed", "no_link", "queued", "extracting")}
    extracted = counts["success"] + counts["parsed_with_missing_fields"]
    progress_percent = (extracted / eligible * 100.0) if eligible else 0.0
    return {
        "total_records": total_records,
        "eligible": eligible,
        "eligible_mitc_links": eligible,
        "extracted": extracted,
        "success": counts["success"],
        "parsed_with_missing_fields": counts["parsed_with_missing_fields"],
        "failed": counts["failed"],
        "no_link": counts["no_link"],
        "queued": counts["queued"],
        "extracting": counts["extracting"],
        "progress_percent": round(progress_percent, 1),
        "progress_percentage": round(progress_percent, 1),
    }



def _sanction_status_summary(df: pd.DataFrame | None) -> dict[str, Any]:
    if df is None:
        statuses: list[str] = []
        eligible = total_records = 0
    else:
        total_records = int(len(df))
        links = df["sanction_letter_link"] if "sanction_letter_link" in df.columns else pd.Series([], dtype=object)
        eligible = int(sum(_has_mitc_link(value) for value in links))
        statuses = [str(value) if not pd.isna(value) else "" for value in df.get("sanction_validation_status", [])]
    counts = {status: statuses.count(status) for status in ("success", "partial", "failed", "no_link", "queued", "extracting")}
    complete = counts["success"] + counts["partial"]
    return {"total_records": total_records, "eligible": eligible, "completed": complete, **counts,
            "progress_percent": round(complete / eligible * 100.0, 1) if eligible else 0.0}


def _is_info_flag(value: Any) -> bool:
    """Compare marker text robustly across Excel string-like scalar values."""
    if value is None or (not isinstance(value, (str, bytes)) and pd.isna(value)):
        return False
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip().casefold() == "info"


def _info_mask(df: pd.DataFrame) -> pd.Series:
    """Identify canonical Info rows, while allowing manually loaded legacy frames."""
    if "info_flag" not in df.columns:
        return pd.Series(True, index=df.index)
    return df["info_flag"].map(_is_info_flag)


def _canonical_rows(df: pd.DataFrame | None) -> pd.DataFrame:
    """Return only canonical Info records when the new row marker is available."""
    if df is None:
        return pd.DataFrame()
    if "info_flag" not in df.columns:
        return df
    return df.loc[_info_mask(df)].copy()


def _document_cell_values(value: Any) -> list[Any]:
    """Expand list-like document cells without splitting ordinary text values."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            import json
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return [value]
            if isinstance(parsed, list):
                return parsed
    return [value]


def _document_metadata_fields(metadata: Any) -> list[str]:
    """Return safe field-name summaries without exposing document metadata values."""
    import json
    import re
    from xml.etree import ElementTree

    if metadata is None or not _has_mitc_link(metadata):
        return []
    parsed = metadata
    if isinstance(metadata, str):
        text = metadata.strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            if text.startswith("<"):
                try:
                    root = ElementTree.fromstring(text)
                    names = [re.sub(r"^.*[{}]", "", node.tag) for node in root.iter()]
                    return list(dict.fromkeys(name for name in names if name))[:12]
                except ElementTree.ParseError:
                    return ["Source metadata supplied"]
            return ["Text metadata supplied"]

    names: list[str] = []
    def walk(value: Any, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                label = str(key).strip()
                if not label:
                    continue
                path = f"{prefix}.{label}" if prefix else label
                if isinstance(child, (dict, list, tuple)):
                    walk(child, path)
                else:
                    names.append(path)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, prefix)
        elif prefix:
            names.append(prefix)
    walk(parsed)
    return list(dict.fromkeys(names))[:12] or ["Source metadata supplied"]


def _associated_document_url(value: Any) -> str:
    """Validate associated links as credential-free HTTP(S) URLs."""
    from urllib.parse import urlsplit

    safe_url = _link_key(value)
    if not safe_url:
        return ""
    try:
        parsed = urlsplit(safe_url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
    except ValueError:
        return ""
    return safe_url


def _document_entries_for_user(user_id: Any) -> list[dict[str, Any]]:
    """Pair only all_doc_type, all_s3_url, and all_other_metadata by user."""
    with STATE_LOCK:
        df = STATE.get("df")
        stored_rows = STATE.get("document_rows")
        if df is None:
            return []
        canonical = _canonical_rows(df)
        if "user_id" not in canonical.columns or not canonical["user_id"].astype(str).eq(str(user_id)).any():
            return []
        if stored_rows is None:
            candidates = df.loc[df["user_id"].astype(str) == str(user_id)] if "user_id" in df else pd.DataFrame()
            rows = candidates.to_dict(orient="records")
        else:
            associated_rows = [row for row in stored_rows if str(row.get("user_id", "")) == str(user_id)]
            rows = [row for row in associated_rows if not _is_info_flag(row.get("info_flag"))]
            # Document arrays may reside on the canonical Info row; include them
            # alongside non-Info attachments without losing either source.
            info_rows = [row for row in associated_rows if _is_info_flag(row.get("info_flag"))]
            rows.extend(row for row in info_rows if any(_has_mitc_link(row.get(column)) for column in ("all_doc_type", "all_s3_url")))
    documents: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows):
        types = _document_cell_values(row.get("all_doc_type"))
        urls = _document_cell_values(row.get("all_s3_url"))
        metadata = _document_cell_values(row.get("all_other_metadata"))
        width = max(len(types), len(urls), len(metadata), 1)
        for offset in range(width):
            doc_type = types[offset] if offset < len(types) else (types[0] if len(types) == 1 else "")
            url = urls[offset] if offset < len(urls) else (urls[0] if len(urls) == 1 else "")
            doc_metadata = metadata[offset] if offset < len(metadata) else (metadata[0] if len(metadata) == 1 else None)
            if not _has_mitc_link(doc_type) and not _has_mitc_link(url):
                continue
            if not _has_mitc_link(doc_type):
                continue
            raw_url = "" if not _has_mitc_link(url) else str(url).strip()
            safe_url = _associated_document_url(raw_url)
            normalized_type = normalize_document_type(doc_type)
            conversion_required = normalized_type in {"AADHAAR_OTP", "DIGILOCKER_AADHAAR_XML"}
            metadata_fields = _document_metadata_fields(doc_metadata)
            conversion_metadata_ready = conversion_required and extract_xml_from_metadata(doc_metadata) is not None
            if conversion_metadata_ready:
                document_status = "Conversion required"
            elif conversion_required:
                document_status = "Conversion metadata unavailable"
            elif safe_url:
                document_status = "Available"
            elif not safe_url:
                document_status = "Link unavailable"
            else:
                document_status = "Unsupported type"
            documents.append({
                "document_id": len(documents), "document_type": str(doc_type).strip(),
                "url": safe_url, "source_row": row_number, "source_offset": offset,
                "_metadata": doc_metadata, "source_columns": "all_doc_type, all_s3_url, all_other_metadata",
                "metadata_fields": metadata_fields, "status": document_status,
                "conversion_required": conversion_required,
                "supported_type": normalized_type in SUPPORTED_TYPES,
            })
    return documents


def _summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = [{"metric": "records_loaded", "value": int(len(df))}]
    for col in MATCH_COLUMNS:
        if col.endswith("_status") or col == "gender_match":
            counts = df[col].fillna("Blank").value_counts()
            for status, count in counts.items():
                rows.append({"metric": f"{col}:{status}", "value": int(count)})
    return pd.DataFrame(rows)


def _output_columns(raw_columns: list[str]) -> list[str]:
    """Preserve every source column, then matching and MITC enrichment columns."""
    return list(dict.fromkeys([*raw_columns, *MATCH_COLUMNS, *MITC_COLUMNS, *MITC_PROFILE_COLUMNS, *LP_MITC_COLUMNS, *SANCTION_COLUMNS, *APPLICATION_COLUMNS, *KFS_COLUMNS, *GTC_COLUMNS, *KYC_DOCUMENT_COLUMNS, *REVIEW_COLUMNS]))


def _write_workbook_locked(enriched: pd.DataFrame, output_path: Path) -> None:
    """Write a complete workbook atomically; sensitive conversion payloads are omitted."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-", suffix=".xlsx", dir=output_path.parent
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        workbook_frame = _canonical_rows(enriched).copy(deep=True)
        # Preserve source fields but never write raw XML/base64 conversion
        # payloads to generated workbooks. In-memory sources remain intact.
        from kyc_document import contains_sensitive_metadata
        for metadata_column in ("other_metadata", "all_other_metadata"):
            if metadata_column in workbook_frame.columns:
                workbook_frame[metadata_column] = workbook_frame[metadata_column].astype("object")
                sensitive_rows = workbook_frame[metadata_column].map(contains_sensitive_metadata)
                workbook_frame.loc[sensitive_rows, metadata_column] = "[XML/base64 conversion metadata omitted]"
        with pd.ExcelWriter(temporary_path, engine="openpyxl") as writer:
            workbook_frame.to_excel(writer, sheet_name="Enriched Data", index=False)
            _summary(workbook_frame).to_excel(writer, sheet_name="Summary", index=False)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_workbook(enriched: pd.DataFrame, output_path: Path) -> None:
    """Write under the shared lock and replace only after a complete XLSX is ready."""
    with WORKBOOK_WRITE_LOCK:
        _write_workbook_locked(enriched, output_path)


def _persist_current_workbook(df: pd.DataFrame, generation: int) -> None:
    """Serialize a fresh state snapshot after acquiring the workbook lock.

    Taking the snapshot after the write lock prevents a queued older snapshot
    from overwriting a newer download or completion write.
    """
    with WORKBOOK_WRITE_LOCK:
        with STATE_LOCK:
            if STATE.get("df") is not df or STATE.get("generation") != generation:
                return
            path = STATE.get("path")
            if not path:
                return
            snapshot = df.copy(deep=True)
        _write_workbook_locked(snapshot, Path(path))


def _flush_pending_workbook_write() -> None:
    """Cancel a debounce timer and synchronously persist the latest in-memory state."""
    global _PENDING_WORKBOOK_WRITE
    with STATE_LOCK:
        timer = _PENDING_WORKBOOK_WRITE
        _PENDING_WORKBOOK_WRITE = None
        if timer is not None:
            timer.cancel()
        df = STATE.get("df")
        generation = STATE.get("generation")
    if df is not None:
        _persist_current_workbook(df, generation)


def _schedule_workbook_write(df: pd.DataFrame, generation: int) -> None:
    """Coalesce bursts of completed MITC rows into one atomic workbook update."""
    global _PENDING_WORKBOOK_WRITE

    def persist_if_current() -> None:
        global _PENDING_WORKBOOK_WRITE
        with STATE_LOCK:
            if _PENDING_WORKBOOK_WRITE is timer:
                _PENDING_WORKBOOK_WRITE = None
        _persist_current_workbook(df, generation)

    with STATE_LOCK:
        prior = _PENDING_WORKBOOK_WRITE
        if prior is not None:
            prior.cancel()
        timer = threading.Timer(WORKBOOK_WRITE_DEBOUNCE_SECONDS, persist_if_current)
        timer.daemon = True
        _PENDING_WORKBOOK_WRITE = timer
        timer.start()


def _coerce_workbook_review_bool(value: Any) -> bool:
    """Normalize review checkbox values stored in an input workbook."""
    if value is None or (not isinstance(value, (str, bytes)) and pd.isna(value)):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "false", "0", "no"}:
            return False
        if normalized in {"true", "1", "yes"}:
            return True
    raise ValueError("Review validation columns must contain boolean values.")


def _coerce_workbook_review_comment(value: Any) -> str:
    """Normalize blank workbook comments without changing source columns."""
    if value is None or (not isinstance(value, (str, bytes)) and pd.isna(value)):
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value)
    if len(text) > REVIEW_COMMENT_MAX_LENGTH:
        raise ValueError(f"Review comments may not exceed {REVIEW_COMMENT_MAX_LENGTH} characters.")
    return text


def process_workbook(stream: io.BytesIO, original_name: str, *, persist: bool = True) -> tuple[pd.DataFrame, Path]:
    raw = pd.read_excel(stream, dtype=object)
    missing = [column for column in LEGACY_REQUIRED_COLUMNS if column not in raw.columns]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))
    document_rows = raw.to_dict(orient="records")
    original_columns = set(raw.columns)
    document_schema_columns = {"user_selfie_doc", "all_doc_type", "all_other_metadata", "all_s3_url"}
    if "info_flag" not in raw.columns and original_columns.intersection(document_schema_columns):
        affected_ids = raw["user_id"].map(lambda value: "(blank user_id)" if pd.isna(value) or not str(value).strip() else str(value).strip()).drop_duplicates().tolist()
        raise ValueError("Info-row validation failed — missing Info row (info_flag) for user_id(s): " + ", ".join(affected_ids))
    if "info_flag" in raw.columns:
        if "user_id" not in raw.columns:
            raise ValueError("Info-row validation failed: user_id is required.")
        normalized_info = raw["info_flag"].map(_is_info_flag)
        user_ids = raw["user_id"].map(lambda value: "(blank user_id)" if pd.isna(value) or not str(value).strip() else str(value).strip())
        info_counts = pd.DataFrame({"user_id": user_ids, "is_info": normalized_info}).groupby("user_id", sort=False, dropna=False)["is_info"].sum()
        missing_info = info_counts[info_counts == 0].index.tolist()
        duplicate_info = info_counts[info_counts > 1].index.tolist()
        if missing_info or duplicate_info:
            issues = []
            if missing_info:
                issues.append("missing Info row for user_id(s): " + ", ".join(missing_info))
            if duplicate_info:
                issues.append("duplicate Info rows for user_id(s): " + ", ".join(duplicate_info))
            raise ValueError("Info-row validation failed — " + "; ".join(issues))
        raw = raw.loc[normalized_info].copy().reset_index(drop=True)
        raw["info_flag"] = "Info"
    else:
        # Legacy one-row workbooks remain supported and are normalized to the
        # canonical marker. Multi-row data without explicit Info rows is ambiguous.
        if "user_id" in raw.columns:
            counts = raw["user_id"].astype(str).value_counts(dropna=False)
            duplicates = counts[counts > 1].index.tolist()
            if duplicates:
                raise ValueError("Info-row validation failed — missing Info row for user_id(s): " + ", ".join(map(str, duplicates)))
        raw = raw.copy()
        raw["info_flag"] = "Info"
    legacy_expanded = ["user_selfie", "kyc_selfie_link", "kyc_doc", "kyc_doc_link", "other_metadata",
                       "verification_status", "validation_type", "nsdl_date"]
    if original_columns.intersection(legacy_expanded):
        missing_expanded = [column for column in legacy_expanded if column not in original_columns]
        if missing_expanded:
            raise ValueError("Incomplete expanded schema; missing columns: " + ", ".join(missing_expanded))
    # New row/document columns are optional for legacy workbooks but always
    # represented on canonical output rows so downloads have a stable schema.
    for column in ("user_selfie_doc", "all_doc_type", "all_other_metadata", "all_s3_url"):
        if column not in raw.columns:
            raw[column] = ""
    enriched = pd.DataFrame([enrich_row(row) for row in raw.to_dict(orient="records")])
    # Review fields are carried forward from an already-reviewed workbook;
    # only absent/blank values receive the documented false/blank defaults.
    for column in REVIEW_COLUMNS:
        if column not in enriched.columns:
            enriched[column] = False if column.endswith("_validated") else ""
        elif column.endswith("_validated"):
            enriched[column] = enriched[column].map(_coerce_workbook_review_bool)
        else:
            enriched[column] = enriched[column].map(_coerce_workbook_review_comment)
    # Every row receives an explicit state before any remote work is submitted.
    # Blank links are terminal no-link rows; valid links are queued by upload.
    mitc_defaults = empty_lp_mitc_fields()
    # Output names can also arrive when a previously enriched workbook is
    # uploaded again; reset them for this active LP run rather than carrying
    # any stale FPL-era borrower/KYC values forward.
    for column, value in mitc_defaults.items():
        enriched[column] = value
    for row_index in enriched.index:
        link = enriched.at[row_index, "mitc_form_link"]
        if not _has_mitc_link(link):
            status = "no_link"
            error = "No mitc_form_link supplied"
        elif _link_key(link) is None:
            # A nonblank but malformed link belongs in the eligible denominator
            # and is terminally failed rather than left queued forever.
            status = "failed"
            error = "Invalid mitc_form_link"
        else:
            status = "queued"
            error = ""
        enriched.at[row_index, "mitc_extraction_status"] = status
        enriched.at[row_index, "mitc_extraction_error"] = error
        enriched.at[row_index, "mitc_validation_status"] = status
        enriched.at[row_index, "mitc_validation_error"] = error
        enriched.at[row_index, "mitc_profile"] = "LP"
        enriched.at[row_index, "lp_mitc_validation_status"] = status
        enriched.at[row_index, "lp_mitc_validation_error"] = error
    for column in SANCTION_COLUMNS:
        if column not in enriched:
            enriched[column] = ""
    for row_index in enriched.index:
        link = enriched.at[row_index, "sanction_letter_link"]
        if not _has_mitc_link(link):
            enriched.at[row_index, "sanction_validation_status"] = "no_link"
            enriched.at[row_index, "sanction_validation_error"] = "No sanction_letter_link supplied"
        elif _link_key(link) is None:
            enriched.at[row_index, "sanction_validation_status"] = "failed"
            enriched.at[row_index, "sanction_validation_error"] = "Invalid sanction_letter_link"
        else:
            enriched.at[row_index, "sanction_validation_status"] = "queued"
    for column in [*APPLICATION_COLUMNS, *KFS_COLUMNS, *GTC_COLUMNS]:
        if column not in enriched:
            enriched[column] = ""
    for row_index in enriched.index:
        enriched.at[row_index, "application_photo_match"] = "Not implemented"
        enriched.at[row_index, "application_borrower_details_match"] = "Not evaluated"
        for match_status_column in ("application_name_match_status", "application_dob_match_status", "application_gender_match"):
            enriched.at[row_index, match_status_column] = "Not evaluated"
        enriched.at[row_index, "application_letterhead_logo_check"] = "Not available"
        for link_column, status_column, error_column, label in (
            ("application_form_link", "application_validation_status", "application_validation_error", "application_form_link"),
            ("kfs_link", "kfs_validation_status", "kfs_validation_error", "kfs_link"),
        ):
            link = enriched.at[row_index, link_column]
            if not _has_mitc_link(link):
                enriched.at[row_index, status_column] = "Not available"
                enriched.at[row_index, error_column] = f"No {label} supplied"
            elif _link_key(link) is None:
                enriched.at[row_index, status_column] = "Failed"
                enriched.at[row_index, error_column] = f"Invalid {label}"
            else:
                enriched.at[row_index, status_column] = "queued"
        gtc_link = enriched.at[row_index, "gtc_link"]
        if not _has_mitc_link(gtc_link):
            enriched.at[row_index, "gtc_status"] = "Not available"
            enriched.at[row_index, "gtc_validation_error"] = "No gtc_link supplied"
        elif _link_key(gtc_link) is None:
            enriched.at[row_index, "gtc_status"] = "Failed"
            enriched.at[row_index, "gtc_validation_error"] = "Invalid gtc_link"
        else:
            enriched.at[row_index, "gtc_status"] = "Loading"
            enriched.at[row_index, "gtc_validation_error"] = ""
    for column in KYC_DOCUMENT_COLUMNS:
        if column not in enriched:
            enriched[column] = ""
    from kyc_document import CONVERSION_TYPES, DIRECT_TYPES, extract_xml_from_metadata, normalize_document_type
    for row_index in enriched.index:
        doc_link = enriched.at[row_index, "kyc_doc_link"] if "kyc_doc_link" in enriched.columns else ""
        source_type_value = enriched.at[row_index, "kyc_doc"] if "kyc_doc" in enriched.columns else ""
        source_type = normalize_document_type(source_type_value) if _has_mitc_link(source_type_value) else ""
        metadata = enriched.at[row_index, "other_metadata"] if "other_metadata" in enriched.columns else None
        if source_type not in DIRECT_TYPES | CONVERSION_TYPES:
            document_status, document_error = "Not available", "Unsupported KYC document type"
        elif source_type in CONVERSION_TYPES and extract_xml_from_metadata(metadata) is None:
            document_status, document_error = "Not available", "Conversion metadata not supplied"
        elif source_type in DIRECT_TYPES and not _has_mitc_link(doc_link):
            document_status, document_error = "Not available", "No kyc_doc_link supplied"
        else:
            document_status, document_error = "Not requested", ""
        enriched.at[row_index, "kyc_document_status"] = document_status
        enriched.at[row_index, "kyc_document_error"] = document_error
        enriched.at[row_index, "kyc_document_source_type"] = source_type_value
    enriched = enriched[_output_columns(list(raw.columns))].copy()
    output_path = OUTPUT_DIR / OUTPUT_FILENAME
    enriched.attrs["document_rows"] = document_rows
    if persist:
        _write_workbook(enriched, output_path)
    return enriched, output_path


def _json_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    # Conversion metadata can contain sensitive XML/base64; keep it in the source
    # workbook/process and never serialize it into browser-facing JSON.
    import json
    safe_frame = df.drop(columns=["other_metadata", "all_other_metadata"], errors="ignore")
    return json.loads(safe_frame.fillna("").to_json(orient="records", date_format="iso"))


def _link_key(value: Any) -> str | None:
    try:
        return _validate_url(value)
    except ValueError:
        return None



def _match_document_name(borrower_name: Any, kyc_name: Any, document_label: str) -> tuple[str, float | None]:
    """Use existing normalization/scoring with document-specific missing labels."""
    status, score = match_name(borrower_name, kyc_name)
    if status == "Both not present":
        return status, None
    if status == "User name not present":
        return f"{document_label} not present", None
    if status == "KYC name not present":
        return status, None
    return status, score


def _set_url_rows_result(df: pd.DataFrame, url: str, result: dict[str, Any]) -> None:
    """Apply LP extraction to matching rows, comparing only with source kyc_name."""
    for row_index in df.index:
        if _link_key(df.at[row_index, "mitc_form_link"]) == url:
            row_result = dict(result)
            if result.get("mitc_profile") == "LP" or result.get("lp_mitc_borrower_name") is not None:
                match_status, match_score = match_name(
                    result.get("lp_mitc_borrower_name"),
                    df.at[row_index, "kyc_name"] if "kyc_name" in df.columns else None,
                )
                row_result["lp_mitc_borrower_name_match_status"] = match_status
                row_result["lp_mitc_borrower_name_match_score"] = match_score
                # The generic FPL-era borrower match is not meaningful for
                # LP and must remain neutral, even though the workbook keeps
                # its compatibility columns. Never substitute user_name.
                row_result["mitc_borrower_name_match_status"] = "Not applicable"
                row_result["mitc_borrower_name_match_score"] = None
                row_result["mitc_profile"] = "LP"
            else:
                # Compatibility for direct consumers of the pre-LP result setter.
                # Active LP extraction always carries lp_mitc_borrower_name/profile.
                row_result["mitc_borrower_name_match_status"], row_result["mitc_borrower_name_match_score"] = _match_document_name(
                    result.get("mitc_borrower_name"), df.at[row_index, "kyc_name"], "MITC borrower name"
                )
            for column in [*MITC_COLUMNS, *MITC_PROFILE_COLUMNS, *LP_MITC_COLUMNS]:
                df.at[row_index, column] = row_result.get(column, "")


def _complete_mitc_url(url: str, df: pd.DataFrame, generation: int, future: Future) -> None:
    try:
        result = future.result()
    except Exception as exc:  # defensive isolation if a worker implementation changes
        result = empty_lp_mitc_fields(status="failed", error=f"Could not extract LP MITC PDF: {exc}")
    with STATE_LOCK:
        # A stale worker from a prior upload must not remove a newer future for
        # the same URL. Its own callback still completes harmlessly, while the
        # current generation remains owned by the newer worker.
        if _INFLIGHT_MITC.get(url) is future:
            _INFLIGHT_MITC.pop(url, None)
        if STATE.get("generation") != generation or STATE.get("df") is not df:
            return
        _set_url_rows_result(df, url, result)
    try:
        _schedule_workbook_write(df, generation)
    except Exception:
        # In-memory row state is still current; /download synchronously flushes
        # the latest snapshot if a delayed filesystem write cannot be scheduled.
        pass


def _submit_mitc_url(url: str, df: pd.DataFrame, generation: int) -> Future | None:
    with STATE_LOCK:
        existing = _INFLIGHT_MITC.get(url)
        existing_owner = getattr(existing, "_mitc_owner", None) if existing is not None else None
        if (
            existing is not None
            and not existing.done()
            and existing_owner is not None
            and existing_owner[0] is df
            and existing_owner[1] == generation
        ):
            return existing
        for row_index in df.index:
            if _link_key(df.at[row_index, "mitc_form_link"]) == url:
                df.at[row_index, "mitc_extraction_status"] = "extracting"
                df.at[row_index, "mitc_extraction_error"] = ""
                df.at[row_index, "lp_mitc_validation_status"] = "extracting"
                df.at[row_index, "lp_mitc_validation_error"] = ""
                df.at[row_index, "mitc_profile"] = "LP"
        try:
            future = MITC_EXECUTOR.submit(extract_mitc_from_url, url)
        except Exception as exc:
            for row_index in df.index:
                if _link_key(df.at[row_index, "mitc_form_link"]) == url:
                    df.at[row_index, "mitc_extraction_status"] = "failed"
                    df.at[row_index, "mitc_extraction_error"] = f"Could not queue LP MITC extraction: {exc}"
                    df.at[row_index, "lp_mitc_validation_status"] = "failed"
                    df.at[row_index, "lp_mitc_validation_error"] = f"Could not queue LP MITC extraction: {exc}"
                    df.at[row_index, "mitc_validation_status"] = "failed"
                    df.at[row_index, "mitc_validation_error"] = f"Could not queue LP MITC extraction: {exc}"
            _schedule_workbook_write(df, generation)
            return None
        # Keep ownership on the Future so a still-running task from an older
        # upload cannot leave a replacement row stuck in ``extracting``.
        future._mitc_owner = (df, generation)  # type: ignore[attr-defined]
        _INFLIGHT_MITC[url] = future
        future.add_done_callback(lambda done: _complete_mitc_url(url, df, generation, done))
        return future


def _dispatch_mitc_batch(df: pd.DataFrame, generation: int) -> None:
    for row_index in df.index:
        url = _link_key(df.at[row_index, "mitc_form_link"])
        if url:
            _submit_mitc_url(url, df, generation)



def _set_url_sanction_result(df: pd.DataFrame, url: str, result: dict[str, Any]) -> None:
    # Pre-create the output schema so pandas does not initialize new columns
    # with NaN for later rows sharing this source URL.
    for column in SANCTION_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    for row_index in df.index:
        if _link_key(df.at[row_index, "sanction_letter_link"]) == url:
            row_result = dict(result)
            match_status, match_score = _match_document_name(
                result.get("sanction_borrower_name"),
                df.at[row_index, "kyc_name"] if "kyc_name" in df.columns else None,
                "Sanction borrower name",
            )
            row_result["sanction_borrower_name_match_status"] = match_status
            row_result["sanction_borrower_name_match_score"] = match_score
            row_result["sanction_borrower_details_match"] = match_status
            for column in SANCTION_COLUMNS:
                df.at[row_index, column] = row_result.get(column, "")


def _complete_sanction_url(url: str, df: pd.DataFrame, generation: int, future: Future) -> None:
    try:
        result = future.result()
    except Exception as exc:
        result = {column: "" for column in SANCTION_COLUMNS}
        result.update({"sanction_validation_status": "failed", "sanction_validation_error": f"Could not extract sanction letter: {exc}"})
    with STATE_LOCK:
        if _INFLIGHT_SANCTION.get(url) is future:
            _INFLIGHT_SANCTION.pop(url, None)
        if STATE.get("generation") != generation or STATE.get("df") is not df:
            return
        _set_url_sanction_result(df, url, result)
    try:
        _schedule_workbook_write(df, generation)
    except Exception:
        pass


def _submit_sanction_url(url: str, df: pd.DataFrame, generation: int) -> Future | None:
    with STATE_LOCK:
        existing = _INFLIGHT_SANCTION.get(url)
        owner = getattr(existing, "_sanction_owner", None) if existing is not None else None
        if existing is not None and not existing.done() and owner is not None and owner[0] is df and owner[1] == generation:
            return existing
        for row_index in df.index:
            if _link_key(df.at[row_index, "sanction_letter_link"]) == url:
                df.at[row_index, "sanction_validation_status"] = "extracting"
                df.at[row_index, "sanction_validation_error"] = ""
        try:
            future = MITC_EXECUTOR.submit(extract_sanction_from_url, url)
        except Exception as exc:
            for row_index in df.index:
                if _link_key(df.at[row_index, "sanction_letter_link"]) == url:
                    df.at[row_index, "sanction_validation_status"] = "failed"
                    df.at[row_index, "sanction_validation_error"] = f"Could not queue sanction extraction: {exc}"
            _schedule_workbook_write(df, generation)
            return None
        future._sanction_owner = (df, generation)  # type: ignore[attr-defined]
        _INFLIGHT_SANCTION[url] = future
        future.add_done_callback(lambda done: _complete_sanction_url(url, df, generation, done))
        return future


def _dispatch_sanction_batch(df: pd.DataFrame, generation: int) -> None:
    for row_index in df.index:
        url = _link_key(df.at[row_index, "sanction_letter_link"])
        if url:
            _submit_sanction_url(url, df, generation)


def _set_document_url_result(df: pd.DataFrame, url: str, link_column: str, columns: list[str], result: dict[str, Any], *, application: bool = False) -> None:
    for row_index in df.index:
        if _link_key(df.at[row_index, link_column]) != url:
            continue
        row_result = dict(result)
        if application:
            row_result = apply_application_matches(row_result, df.at[row_index, "kyc_name"], df.at[row_index, "kyc_dob"], df.at[row_index, "kyc_gender"])
        elif "kfs" in columns:
            row_result = apply_kfs_matches(row_result, df.at[row_index, "kyc_name"], df.at[row_index, "kyc_permanent_address"])
        for column in columns:
            df.at[row_index, column] = row_result.get(column, "")


def _complete_document_url(url: str, df: pd.DataFrame, generation: int, future: Future, *, application: bool) -> None:
    columns = APPLICATION_COLUMNS if application else KFS_COLUMNS
    status_column = "application_validation_status" if application else "kfs_validation_status"
    error_column = "application_validation_error" if application else "kfs_validation_error"
    inflight = _APPLICATION_INFLIGHT if application else _KFS_INFLIGHT
    try:
        result = future.result()
    except Exception as exc:
        result = {column: "" for column in columns}
        result.update({status_column: "Failed", error_column: f"Could not process document: {exc}"})
        if application:
            result.update({"application_photo_match": "Not implemented", "application_borrower_details_match": "Review"})
        else:
            result.update({"kfs_penal_charges_check": "Failed", "kfs_validation_status": "Failed"})
    with STATE_LOCK:
        if inflight.get(url) is future:
            inflight.pop(url, None)
        if STATE.get("generation") != generation or STATE.get("df") is not df:
            return
        _set_document_url_result(df, url, "application_form_link" if application else "kfs_link", columns, result, application=application)
    try:
        _schedule_workbook_write(df, generation)
    except Exception:
        pass


def _submit_document_url(url: str, df: pd.DataFrame, generation: int, *, application: bool) -> Future | None:
    inflight = _APPLICATION_INFLIGHT if application else _KFS_INFLIGHT
    link_column = "application_form_link" if application else "kfs_link"
    status_column = "application_validation_status" if application else "kfs_validation_status"
    error_column = "application_validation_error" if application else "kfs_validation_error"
    worker = extract_application_from_url if application else extract_kfs_from_url
    with STATE_LOCK:
        existing = inflight.get(url)
        owner = getattr(existing, "_document_owner", None) if existing is not None else None
        if existing is not None and not existing.done() and owner is not None and owner[0] is df and owner[1] == generation:
            return existing
        for row_index in df.index:
            if _link_key(df.at[row_index, link_column]) == url:
                df.at[row_index, status_column] = "extracting"
                df.at[row_index, error_column] = ""
        try:
            future = MITC_EXECUTOR.submit(worker, url)
        except Exception as exc:
            for row_index in df.index:
                if _link_key(df.at[row_index, link_column]) == url:
                    df.at[row_index, status_column] = "Failed"
                    df.at[row_index, error_column] = f"Could not queue document: {exc}"
            _schedule_workbook_write(df, generation)
            return None
        future._document_owner = (df, generation)  # type: ignore[attr-defined]
        inflight[url] = future
        future.add_done_callback(lambda done: _complete_document_url(url, df, generation, done, application=application))
        return future


def _dispatch_document_batch(df: pd.DataFrame, generation: int, *, application: bool) -> None:
    link_column = "application_form_link" if application else "kfs_link"
    seen: set[str] = set()
    for row_index in df.index:
        url = _link_key(df.at[row_index, link_column])
        if url and url not in seen:
            seen.add(url)
            _submit_document_url(url, df, generation, application=application)


def _complete_gtc_url(url: str, df: pd.DataFrame, generation: int, future: Future) -> None:
    try:
        result = future.result()
    except Exception as exc:
        result = {"gtc_status": "Failed", "gtc_validation_error": f"Could not verify GTC link: {exc}"}
    with STATE_LOCK:
        if _GTC_INFLIGHT.get(url) is future:
            _GTC_INFLIGHT.pop(url, None)
        if STATE.get("generation") != generation or STATE.get("df") is not df:
            return
        for row_index in df.index:
            if _link_key(df.at[row_index, "gtc_link"]) == url:
                df.at[row_index, "gtc_status"] = result.get("gtc_status", "Failed")
                df.at[row_index, "gtc_validation_error"] = result.get("gtc_validation_error", "")
    try:
        _schedule_workbook_write(df, generation)
    except Exception:
        pass


def _submit_gtc_url(url: str, df: pd.DataFrame, generation: int) -> Future | None:
    with STATE_LOCK:
        existing = _GTC_INFLIGHT.get(url)
        owner = getattr(existing, "_gtc_owner", None) if existing is not None else None
        if existing is not None and not existing.done() and owner is not None and owner[0] is df and owner[1] == generation:
            return existing
        for row_index in df.index:
            if _link_key(df.at[row_index, "gtc_link"]) == url:
                df.at[row_index, "gtc_status"] = "Loading"
                df.at[row_index, "gtc_validation_error"] = ""
        try:
            future = MITC_EXECUTOR.submit(check_gtc_url, url)
        except Exception as exc:
            for row_index in df.index:
                if _link_key(df.at[row_index, "gtc_link"]) == url:
                    df.at[row_index, "gtc_status"] = "Failed"
                    df.at[row_index, "gtc_validation_error"] = f"Could not queue GTC check: {exc}"
            _schedule_workbook_write(df, generation)
            return None
        future._gtc_owner = (df, generation)  # type: ignore[attr-defined]
        _GTC_INFLIGHT[url] = future
        future.add_done_callback(lambda done: _complete_gtc_url(url, df, generation, done))
        return future


def _dispatch_gtc_batch(df: pd.DataFrame, generation: int) -> None:
    seen: set[str] = set()
    for row_index in df.index:
        url = _link_key(df.at[row_index, "gtc_link"])
        if url and url not in seen:
            seen.add(url)
            _submit_gtc_url(url, df, generation)


def _ensure_document_task(record_index: int, *, application: bool) -> None:
    with STATE_LOCK:
        df = STATE.get("df")
        generation = STATE.get("generation")
        if df is None or record_index < 0 or record_index >= len(df):
            return
        url = _link_key(df.iloc[record_index].get("application_form_link" if application else "kfs_link"))
        if url:
            _submit_document_url(url, df, generation, application=application)


def _ensure_sanction_task(record_index: int) -> None:
    with STATE_LOCK:
        df = STATE.get("df")
        generation = STATE.get("generation")
        if df is None or record_index < 0 or record_index >= len(df):
            return
        url = _link_key(df.iloc[record_index].get("sanction_letter_link"))
        if url:
            _submit_sanction_url(url, df, generation)


def _ensure_record_task(record_index: int) -> None:
    with STATE_LOCK:
        df = STATE.get("df")
        generation = STATE.get("generation")
        if df is None or record_index < 0 or record_index >= len(df):
            return
        url = _link_key(df.iloc[record_index].get("mitc_form_link"))
        if url:
            _submit_mitc_url(url, df, generation)



def _lightweight_user_rows(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    """Return only fields needed to build the searchable, navigable left list."""
    if df is None:
        return []
    df = _canonical_rows(df)
    columns = {}
    for column in ("user_id", "user_name", "mitc_extraction_status", "overall_match_status", "lp_mitc_validation_status", "mitc_profile"):
        columns[column] = df[column].tolist() if column in df.columns else [""] * len(df)

    def display(value: Any) -> str:
        return "" if value is None or pd.isna(value) else str(value)

    return [
        {
            "index": index,
            "user_id": display(columns["user_id"][index]),
            "user_name": display(columns["user_name"][index]),
            "mitc_extraction_status": display(columns["mitc_extraction_status"][index]),
            "overall_match_status": display(columns["overall_match_status"][index]),
            "lp_mitc_validation_status": display(columns["lp_mitc_validation_status"][index]),
            "mitc_profile": display(columns["mitc_profile"][index]),
        }
        for index in range(len(df))
    ]


@app.get("/")
def index():
    with STATE_LOCK:
        df = _canonical_rows(STATE.get("df"))
        user_rows = _lightweight_user_rows(df)
        filename = STATE.get("filename")
        summary = _mitc_status_summary(df)
    return render_template(
        "index.html", user_rows=user_rows,
        filename=filename, required_columns=REQUIRED_COLUMNS,
        mitc_summary=summary,
    )


@app.post("/upload")
def upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Choose an .xlsx file before uploading.", "error")
        return redirect(url_for("index"))
    if not uploaded.filename.lower().endswith(".xlsx"):
        flash("Only .xlsx files are supported.", "error")
        return redirect(url_for("index"))
    try:
        _flush_pending_workbook_write()
        df, path = process_workbook(io.BytesIO(uploaded.read()), uploaded.filename, persist=False)
        # Serialize output replacement and generation swap against any old
        # completion writer, so an old upload cannot replace this workbook.
        with WORKBOOK_WRITE_LOCK:
            with STATE_LOCK:
                STATE["generation"] = int(STATE.get("generation", 0)) + 1
                generation = STATE["generation"]
                STATE.update({"df": df, "document_rows": df.attrs.get("document_rows", []), "filename": uploaded.filename, "path": path})
                _write_workbook_locked(df.copy(deep=True), path)
        _dispatch_mitc_batch(df, generation)
        _dispatch_sanction_batch(df, generation)
        _dispatch_document_batch(df, generation, application=True)
        _dispatch_document_batch(df, generation, application=False)
        _dispatch_gtc_batch(df, generation)
        flash(f"Matching complete — {len(df)} canonical Info records processed; document checks queued in the background.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.get("/api/records/<int:record_index>")
def api_record(record_index: int):
    """Fetch only the requested full record for the currently selected user."""
    with STATE_LOCK:
        df = _canonical_rows(STATE.get("df"))
        if df.empty:
            return jsonify({"error": "Upload a workbook before requesting a record."}), 400
        if record_index < 0 or record_index >= len(df):
            return jsonify({"error": "Selected user is out of range."}), 404
        record = _json_records(df.iloc[[record_index]])[0]
    # Conversion material may contain identity XML/base64; retain it only in the source workbook/process.
    record.pop("other_metadata", None)
    return jsonify({"index": record_index, "record": record})


@app.get("/api/records/<int:record_index>/documents")
def api_record_documents(record_index: int):
    with STATE_LOCK:
        df = _canonical_rows(STATE.get("df"))
        if df.empty:
            return jsonify({"error": "Upload a workbook before requesting documents."}), 400
        if record_index < 0 or record_index >= len(df):
            return jsonify({"error": "Selected user is out of range."}), 404
        user_id = df.iloc[record_index].get("user_id", "")
    documents = _document_entries_for_user(user_id)
    public_documents = [{key: value for key, value in document.items()
                         if not key.startswith("_") and key not in {"source_row", "source_offset"}}
                        for document in documents]
    return jsonify({"user_id": "" if pd.isna(user_id) else str(user_id), "documents": public_documents})


@app.get("/api/records/<int:record_index>/documents/<int:document_id>")
def api_record_document(record_index: int, document_id: int):
    with STATE_LOCK:
        df = _canonical_rows(STATE.get("df"))
        if df.empty:
            return jsonify({"error": "Upload a workbook before viewing a document."}), 400
        if record_index < 0 or record_index >= len(df):
            return jsonify({"error": "Selected user is out of range."}), 404
        user_id = df.iloc[record_index].get("user_id", "")
        associated = _document_entries_for_user(user_id)
        if document_id < 0 or document_id >= len(associated):
            return jsonify({"error": "Selected document is out of range."}), 404
        document = associated[document_id]
        metadata = document.get("_metadata")
        doc_type = document["document_type"]
        source_url = document["url"]
        if normalize_document_type(doc_type) not in {"AADHAAR_OTP", "DIGILOCKER_AADHAAR_XML"}:
            return jsonify({"error": "Only conversion-required associated documents are served through this route."}), 400
    try:
        payload, content_type = fetch_kyc_document(source_url, doc_type, metadata)
    except KYCDocumentError as exc:
        return jsonify({"error": str(exc), "status": exc.document_status}), exc.status_code
    response = send_file(io.BytesIO(payload), mimetype=content_type, as_attachment=False, download_name="associated_document")
    response.headers["Content-Disposition"] = "inline; filename=associated_document"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/records/<int:record_index>/review", methods=["PATCH", "POST"])
def update_record_review(record_index: int):
    """Validate and persist only selected-row operations review fields."""
    updates = request.get_json(silent=True)
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "Provide a non-empty JSON object of review fields."}), 400
    unknown = sorted(set(updates) - set(REVIEW_COLUMNS))
    if unknown:
        return jsonify({"error": "Unsupported review field(s): " + ", ".join(unknown)}), 400
    for field, value in updates.items():
        if field.endswith("_validated") and type(value) is not bool:
            return jsonify({"error": f"{field} must be a boolean."}), 400
        if field.endswith("_comments"):
            if not isinstance(value, str):
                return jsonify({"error": f"{field} must be text."}), 400
            if len(value) > REVIEW_COMMENT_MAX_LENGTH:
                return jsonify({"error": f"{field} may not exceed {REVIEW_COMMENT_MAX_LENGTH} characters."}), 400
            if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
                return jsonify({"error": f"{field} contains unsupported control characters."}), 400
    with STATE_LOCK:
        df = STATE.get("df")
        generation = STATE.get("generation")
        if df is None:
            return jsonify({"error": "Upload a workbook before updating review controls."}), 400
        canonical_df = _canonical_rows(df)
        if record_index < 0 or record_index >= len(canonical_df):
            return jsonify({"error": "Selected user is out of range."}), 404
        row_key = canonical_df.index[record_index]
        for field, value in updates.items():
            df.at[row_key, field] = value
        review_state = {}
        for field in REVIEW_COLUMNS:
            value = df.at[row_key, field]
            if field.endswith("_validated"):
                review_state[field] = bool(value) if not pd.isna(value) else False
            else:
                review_state[field] = "" if pd.isna(value) else str(value)
    _schedule_workbook_write(df, generation)
    return jsonify({"index": record_index, "review": review_state})


@app.get("/static/vendor/pdfjs/pdf.worker.min.mjs")
def serve_pdfjs_worker():
    """Deliver the locally bundled gzip worker as JavaScript with normal browser decoding."""
    worker_path = BASE_DIR / "static" / "vendor" / "pdfjs" / "pdf.worker.min.mjs.gz"
    response = send_file(worker_path, mimetype="text/javascript", conditional=False)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/api/records/<int:record_index>/kyc/document")
def kyc_document(record_index: int):
    """Lazy-load the selected row's exact KYC document source through a validated inline response."""
    with STATE_LOCK:
        df = STATE.get("df")
        generation = STATE.get("generation")
        if df is None:
            return jsonify({"error": "Upload a workbook before viewing a KYC document."}), 400
        if record_index < 0 or record_index >= len(df):
            return jsonify({"error": "Selected user is out of range."}), 404
        row = df.iloc[record_index]
        source_type = row.get("kyc_doc", "")
        source_link = row.get("kyc_doc_link", "")
        metadata = row.get("other_metadata")
    try:
        payload, content_type = fetch_kyc_document(source_link, source_type, metadata)
    except KYCDocumentError as exc:
        changed = False
        with STATE_LOCK:
            if STATE.get("df") is df and STATE.get("generation") == generation:
                row_index = df.index[record_index]
                changed = (str(df.at[row_index, "kyc_document_status"]) != exc.document_status
                           or str(df.at[row_index, "kyc_document_error"]) != str(exc))
                df.at[row_index, "kyc_document_status"] = exc.document_status
                df.at[row_index, "kyc_document_error"] = str(exc)
                df.at[row_index, "kyc_document_source_type"] = source_type
        if changed and generation is not None:
            _schedule_workbook_write(df, generation)
        return jsonify({"error": str(exc), "status": exc.document_status}), exc.status_code
    except Exception:
        safe_error = KYCDocumentError("KYC document could not be processed", 502, "Failed")
        changed = False
        with STATE_LOCK:
            if STATE.get("df") is df and STATE.get("generation") == generation:
                row_index = df.index[record_index]
                changed = str(df.at[row_index, "kyc_document_status"]) != safe_error.document_status
                df.at[row_index, "kyc_document_status"] = safe_error.document_status
                df.at[row_index, "kyc_document_error"] = str(safe_error)
                df.at[row_index, "kyc_document_source_type"] = source_type
        if changed and generation is not None:
            _schedule_workbook_write(df, generation)
        return jsonify({"error": str(safe_error), "status": safe_error.document_status}), safe_error.status_code
    changed = False
    with STATE_LOCK:
        if STATE.get("df") is df and STATE.get("generation") == generation:
            row_index = df.index[record_index]
            changed = (str(df.at[row_index, "kyc_document_status"]) != "Available"
                       or bool(str(df.at[row_index, "kyc_document_error"] or "")))
            df.at[row_index, "kyc_document_status"] = "Available"
            df.at[row_index, "kyc_document_error"] = ""
            df.at[row_index, "kyc_document_source_type"] = source_type
    if changed and generation is not None:
        _schedule_workbook_write(df, generation)
    extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    response = send_file(io.BytesIO(payload), mimetype=content_type, as_attachment=False,
                         download_name=f"kyc_document.{extension}")
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"inline; filename=kyc_document.{extension}"
    response.headers["X-KYC-Document-Status"] = "Available"
    response.headers["X-KYC-Document-Source-Type"] = normalize_document_type(source_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/records/<int:record_index>/mitc")
def extract_mitc(record_index: int):
    profile = str(request.args.get("profile", request.form.get("profile", "LP"))).strip().upper()
    if profile not in {"LP", "FPL", "MPL"}:
        return jsonify({"error": "Unknown MITC profile. Choose LP, FPL, or MPL."}), 400
    if profile != "LP":
        return jsonify({"profile": profile, "profile_state": f"{profile} MITC profile not implemented yet", "implemented": False}), 200
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before extracting MITC."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    _ensure_record_task(record_index)
    with STATE_LOCK:
        record = _json_records(df.iloc[[record_index]])[0]
    return jsonify({"index": record_index, "profile": profile, "profile_state": "LP profile active", "record": record, "result": {column: record.get(column, "") for column in [*MITC_COLUMNS, *MITC_PROFILE_COLUMNS, *LP_MITC_COLUMNS]}})


@app.post("/api/records/<int:record_index>/sanction")
def extract_sanction(record_index: int):
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before extracting a sanction letter."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    _ensure_sanction_task(record_index)
    with STATE_LOCK:
        record = _json_records(df.iloc[[record_index]])[0]
    return jsonify({"index": record_index, "record": record, "result": {column: record.get(column, "") for column in SANCTION_COLUMNS}})


@app.get("/api/records/<int:record_index>/sanction/document")
def sanction_document(record_index: int):
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before viewing a sanction letter."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    try:
        payload = fetch_mitc_pdf(df.iloc[record_index].get("sanction_letter_link"), field_name="sanction_letter_link")
    except MITCDocumentError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    response = send_file(io.BytesIO(payload), mimetype="application/pdf", as_attachment=False, download_name="sanction.pdf")
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "inline; filename=sanction.pdf"
    return response


@app.get("/api/records/<int:record_index>/mitc/document")
def mitc_document(record_index: int):
    """Serve the selected row's validated MITC PDF for an inline browser viewer."""
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before viewing an MITC document."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    try:
        payload = fetch_mitc_pdf(df.iloc[record_index].get("mitc_form_link"))
    except MITCDocumentError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    response = send_file(io.BytesIO(payload), mimetype="application/pdf", as_attachment=False, download_name="mitc.pdf")
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "inline; filename=mitc.pdf"
    return response


@app.post("/api/records/<int:record_index>/application")
def extract_application(record_index: int):
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before checking an Application Form."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    _ensure_document_task(record_index, application=True)
    with STATE_LOCK:
        record = _json_records(df.iloc[[record_index]])[0]
    return jsonify({"index": record_index, "record": record})


@app.post("/api/records/<int:record_index>/kfs")
def extract_kfs(record_index: int):
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before checking a KFS."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    _ensure_document_task(record_index, application=False)
    with STATE_LOCK:
        record = _json_records(df.iloc[[record_index]])[0]
    return jsonify({"index": record_index, "record": record})


@app.get("/api/records/<int:record_index>/<document_type>/document")
def additional_document(record_index: int, document_type: str):
    if document_type not in {"application", "kfs", "gtc"}:
        return jsonify({"error": "Unknown document type."}), 404
    df = STATE.get("df")
    if df is None:
        return jsonify({"error": "Upload a workbook before viewing a document."}), 400
    if record_index < 0 or record_index >= len(df):
        return jsonify({"error": "Selected user is out of range."}), 404
    link_column = {"application": "application_form_link", "kfs": "kfs_link", "gtc": "gtc_link"}[document_type]
    field = link_column
    try:
        payload = fetch_mitc_pdf(df.iloc[record_index].get(link_column), field_name=field)
    except MITCDocumentError as exc:
        if document_type == "gtc":
            blank_gtc_link = not _has_mitc_link(df.iloc[record_index].get("gtc_link"))
            with STATE_LOCK:
                if STATE.get("df") is df:
                    df.at[df.index[record_index], "gtc_status"] = "Not available" if blank_gtc_link else "Failed"
                    df.at[df.index[record_index], "gtc_validation_error"] = "No gtc_link supplied" if blank_gtc_link else str(exc)
                    generation = STATE.get("generation")
                else:
                    generation = None
            if generation is not None:
                _schedule_workbook_write(df, generation)
        return jsonify({"error": str(exc)}), exc.status_code
    if document_type == "gtc":
        with STATE_LOCK:
            if STATE.get("df") is df:
                df.at[df.index[record_index], "gtc_status"] = "Available"
                df.at[df.index[record_index], "gtc_validation_error"] = ""
                generation = STATE.get("generation")
            else:
                generation = None
        if generation is not None:
            _schedule_workbook_write(df, generation)
    response = send_file(io.BytesIO(payload), mimetype="application/pdf", as_attachment=False, download_name=f"{document_type}.pdf")
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"inline; filename={document_type}.pdf"
    try:
        from pypdf import PdfReader
        response.headers["X-PDF-Page-Count"] = str(len(PdfReader(io.BytesIO(payload)).pages))
    except Exception:
        response.headers["X-PDF-Page-Count"] = ""
    return response


@app.get("/download")
def download():
    with STATE_LOCK:
        path = STATE.get("path")
    if not path or not Path(path).exists():
        flash("Upload a workbook first.", "error")
        return redirect(url_for("index"))
    try:
        _flush_pending_workbook_write()
    except Exception as exc:
        return jsonify({"error": f"Could not save the latest workbook state: {exc}"}), 500
    return send_file(path, as_attachment=True, download_name=OUTPUT_FILENAME)


def _status_payload(include_full_records: bool, profile: str = "LP") -> dict[str, Any]:
    if profile not in {"LP", "FPL", "MPL"}:
        profile = "LP"
    with STATE_LOCK:
        df = STATE.get("df")
        if df is None:
            rows = []
            summary = _mitc_status_summary(None)
            sanction_summary = _sanction_status_summary(None)
            sanction_rows = []
            document_rows = []
            pending = False
            records = []
        else:
            df = _canonical_rows(df)
            statuses = df.get("mitc_extraction_status", pd.Series([""] * len(df), index=df.index))
            sanction_statuses = df.get("sanction_validation_status", pd.Series([""] * len(df), index=df.index))
            application_statuses = df.get("application_validation_status", pd.Series([""] * len(df), index=df.index))
            kfs_statuses = df.get("kfs_validation_status", pd.Series([""] * len(df), index=df.index))
            gtc_statuses = df.get("gtc_status", pd.Series([""] * len(df), index=df.index))
            rows = [{"index": index, "extraction_status": "" if pd.isna(status) else str(status)}
                    for index, status in enumerate(statuses)]
            sanction_rows = [{"index": index, "validation_status": "" if pd.isna(status) else str(status)}
                             for index, status in enumerate(sanction_statuses)]
            document_rows = [["" if pd.isna(application_statuses.iloc[index]) else str(application_statuses.iloc[index]),
                              "" if pd.isna(kfs_statuses.iloc[index]) else str(kfs_statuses.iloc[index]),
                              "" if pd.isna(gtc_statuses.iloc[index]) else str(gtc_statuses.iloc[index])]
                             for index in range(len(df))]
            pending = (any(row["extraction_status"] in {"queued", "extracting"} for row in rows)
                       or any(row["validation_status"] in {"queued", "extracting"} for row in sanction_rows)
                       or any(row[0] in {"queued", "extracting"} or row[1] in {"queued", "extracting"} for row in document_rows)
                       or any(row[2] in {"queued", "extracting", "Loading"} for row in document_rows))
            summary = _mitc_status_summary(df)
            sanction_summary = _sanction_status_summary(df)
            records = _json_records(df) if include_full_records else None
    if include_full_records:
        full_rows = [{"index": row["index"], "extraction_status": row["extraction_status"], "record": records[row["index"]]}
                     for row in rows]
        return {"records": records, "rows": full_rows, "pending": pending, "summary": summary, "sanction_summary": sanction_summary, "sanction_rows": sanction_rows, "document_rows": document_rows, "profile": profile, "profile_state": "LP profile active" if profile == "LP" else f"{profile} profile not implemented yet"}
    return {"rows": rows, "pending": pending, "summary": summary, "sanction_summary": sanction_summary, "sanction_rows": sanction_rows, "document_rows": document_rows, "profile": profile, "profile_state": "LP profile active" if profile == "LP" else f"{profile} profile not implemented yet"}


@app.get("/api/records/status")
def api_records_status():
    include_full_records = request.args.get("full", "1").strip().lower() not in {"0", "false", "no"}
    profile = str(request.args.get("profile", "LP")).strip().upper()
    return jsonify(_status_payload(include_full_records, profile))


@app.get("/api/records/status/summary")
def api_records_status_summary():
    profile = str(request.args.get("profile", "LP")).strip().upper()
    return jsonify(_status_payload(include_full_records=False, profile=profile))


@app.get("/api/records")
def api_records():
    with STATE_LOCK:
        df = _canonical_rows(STATE.get("df"))
        return jsonify([] if df.empty else _json_records(df))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
