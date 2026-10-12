"""Page-scoped deterministic extraction/checks for Application Form and KFS."""
from __future__ import annotations

import io
import re
import threading
from typing import Any

from matching import match_address, match_dob, match_gender, match_name
from mitc_extraction import _get_page_text, _page_has_images, _signature_structure, _validate_url, fetch_mitc_pdf

APPLICATION_FIELDS = [
    "application_borrower_name", "application_father_name", "application_mother_name",
    "application_nationality", "application_dob", "application_gender",
    "application_mobile_number", "application_email", "application_pan", "application_other",
    "application_communication_address", "application_communication_city", "application_communication_pincode",
    "application_communication_state", "application_communication_country", "application_current_address",
    "application_current_city", "application_current_pincode", "application_current_state",
    "application_current_country", "application_occupation_type",
]
REQUIRED_APPLICATION_FIELDS = [field for field in APPLICATION_FIELDS if field != "application_occupation_type"]
APPLICATION_COLUMNS = [
    *APPLICATION_FIELDS,
    "application_name_match_status", "application_name_match_score",
    "application_dob_match_status", "application_dob_match_score",
    "application_gender_match", "application_gender_match_score",
    "application_borrower_details_match", "application_letterhead_logo_check",
    "application_letterhead_logo_evidence", "application_photo_match",
    "application_validation_status", "application_validation_error",
]
KFS_COLUMNS = [
    "kfs_borrower_identification_number", "kfs_borrower_name", "kfs_borrower_address",
    "kfs_borrower_name_match_status", "kfs_borrower_name_match_score",
    "kfs_borrower_address_match_status", "kfs_borrower_address_match_score",
    "kfs_borrower_details_match", "kfs_letterhead_logo_check", "kfs_letterhead_logo_evidence",
    "kfs_digital_signature_check", "kfs_digital_signature_evidence",
    "kfs_penal_charges_check", "kfs_penal_charges_evidence", "kfs_validation_status", "kfs_validation_error",
]
GTC_COLUMNS = ["gtc_status", "gtc_validation_error"]

_APPLICATION_CACHE: dict[str, dict[str, Any]] = {}
_KFS_CACHE: dict[str, dict[str, Any]] = {}
_GTC_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()
APPLICATION_LINK_FIELD = "_".join(("application", "form", "link"))


def _empty_application(status: str = "Not requested", error: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {field: "" for field in APPLICATION_FIELDS}
    result.update({
        "application_name_match_status": "Not evaluated", "application_name_match_score": None,
        "application_dob_match_status": "Not evaluated", "application_dob_match_score": None,
        "application_gender_match": "Not evaluated", "application_gender_match_score": None,
        "application_borrower_details_match": "Not evaluated",
        "application_letterhead_logo_check": "Not available", "application_letterhead_logo_evidence": "",
        "application_photo_match": "Not implemented", "application_validation_status": status,
        "application_validation_error": error,
    })
    return result


def _label_regex(*labels: str) -> str:
    """Build label patterns tolerant of extraction whitespace and common punctuation."""
    patterns = []
    for label in labels:
        tokens = re.split(r"\s+", label.strip())
        patterns.append(r"\s*[,.:;]?\s*".join(re.escape(token) for token in tokens))
    return "|".join(sorted(patterns, key=len, reverse=True))


_APPLICATION_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("application_borrower_name", ("Name of the Borrower", "Borrower Name", "Name of Borrower")),
    ("application_father_name", ("Father's Name", "Father’s Name", "Father Name")),
    ("application_mother_name", ("Mother's Name", "Mother’s Name", "Mother Name")),
    ("application_nationality", ("Nationality",)),
    ("application_dob", ("Date of Birth", "DOB", "Birth Date")),
    ("application_gender", ("Gender", "Sex")),
    ("application_mobile_number", ("Mobile Number", "Mobile No", "Mobile")),
    ("application_email", ("Email", "Email Address")),
    ("application_pan", ("PAN", "PAN Number", "PAN No")),
    ("application_other", ("Other",)),
    ("application_communication_address", ("Communication Address",)),
    ("application_current_address", ("Current Address",)),
    ("_generic_city", ("City",)),
    ("_generic_pincode", ("Pincode", "Pin Code", "PIN Code")),
    ("_generic_state", ("State",)),
    ("_generic_country", ("Country",)),
    ("application_occupation_type", ("Occupation Type", "Occupation")),
)


def _application_label_pattern() -> tuple[re.Pattern[str], tuple[str, ...]]:
    alternatives = [(field, _label_regex(*labels)) for field, labels in _APPLICATION_LABELS]
    # Prefer specific/multiword labels; each captured label has a stable field key.
    alternatives.sort(key=lambda item: len(item[1]), reverse=True)
    field_order = tuple(field for field, _pattern in alternatives)
    body = "|".join(f"(?P<L{index}>{pattern})" for index, (_field, pattern) in enumerate(alternatives))
    pattern = re.compile(rf"(?<![\w])(?:{body})(?![\w])\s*[:#.;)]?\s*", re.IGNORECASE)
    return pattern, field_order


_APPLICATION_LABEL_PATTERN, _APPLICATION_LABEL_ORDER = _application_label_pattern()
_APPLICATION_GROUP_FIELDS = {
    "_generic_city": ("application_communication_city", "application_current_city"),
    "_generic_pincode": ("application_communication_pincode", "application_current_pincode"),
    "_generic_state": ("application_communication_state", "application_current_state"),
    "_generic_country": ("application_communication_country", "application_current_country"),
}


def _clean_application_value(value: str) -> str:
    # Keep meaningful placeholders such as N/A and '-' intact; remove only separators.
    return re.sub(r"\s+", " ", value).strip(" \t\r\n:;,.#|")


def parse_application_page1(text: str) -> dict[str, Any]:
    """Parse page-one Application Form values, stopping each at the next known label."""
    if not isinstance(text, str) or not text.strip():
        return _empty_application(status="partial", error="Application Form page 1 has no extractable text")

    result = _empty_application(status="success")
    raw_matches = list(_APPLICATION_LABEL_PATTERN.finditer(text))
    raw_keys = []
    for candidate in raw_matches:
        label_index = next(i for i, field in enumerate(_APPLICATION_LABEL_ORDER) if candidate.group(f"L{i}") is not None)
        raw_keys.append(_APPLICATION_LABEL_ORDER[label_index])
    matches = []
    # Address prose often includes the word "City" (e.g. "Near City Market").
    # When flattened inline text produces several City candidates before Pincode,
    # the field label is the last such candidate nearest the Pincode value.
    discard_inline_city: set[int] = set()
    for group_start, group_end in (("application_communication_address", "application_current_address"),
                                   ("application_current_address", None)):
        starts = [i for i, key in enumerate(raw_keys) if key == group_start]
        for section_start in starts:
            section_end = next((i for i in range(section_start + 1, len(raw_keys))
                                if group_end is not None and raw_keys[i] == group_end), len(raw_keys))
            pincode_index = next((i for i in range(section_start + 1, section_end)
                                  if raw_keys[i] == "_generic_pincode"), None)
            if pincode_index is None:
                continue
            cities = [i for i in range(section_start + 1, pincode_index) if raw_keys[i] == "_generic_city"]
            discard_inline_city.update(cities[:-1])
    for raw_index, candidate in enumerate(raw_matches):
        if raw_index in discard_inline_city:
            continue
        field_name = raw_keys[raw_index]
        if field_name in _APPLICATION_GROUP_FIELDS:
            previous_section = next((key for key in reversed(raw_keys[:raw_index])
                                     if key in ("application_communication_address", "application_current_address")), None)
            if previous_section is None:
                continue
            section_end = next((i for i in range(raw_index + 1, len(raw_keys))
                                if raw_keys[i] in ("application_communication_address", "application_current_address")), len(raw_keys))
            line_start = text.rfind("\n", 0, candidate.start()) + 1
            prefix = text[line_start:candidate.start()].rstrip()
            explicit_label_separator = candidate.group().rstrip().endswith((":", "#", ";"))
            line_boundary = not prefix
            token_boundary = bool(prefix and prefix[-1] in ":;|")
            # Generic address subfield names (especially City/State) are common
            # words inside addresses. Require a structural boundary; inline form
            # is supported with a colon/hash/semicolon or separator.
            if not (explicit_label_separator or line_boundary or token_boundary):
                continue
        matches.append(candidate)
    found: set[str] = set()
    address_section: str | None = None
    for index, match in enumerate(matches):
        label_index = next(i for i, field in enumerate(_APPLICATION_LABEL_ORDER) if match.group(f"L{i}") is not None)
        field_name = _APPLICATION_LABEL_ORDER[label_index]
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = _clean_application_value(text[match.end():value_end])
        if field_name == "application_communication_address":
            address_section = "communication"
        elif field_name == "application_current_address":
            address_section = "current"
        if field_name in _APPLICATION_GROUP_FIELDS:
            candidates = _APPLICATION_GROUP_FIELDS[field_name]
            if address_section == "communication":
                target = candidates[0]
            elif address_section == "current":
                target = candidates[1]
            else:
                continue
            result[target] = value
            if value:
                found.add(target)
        else:
            result[field_name] = value
            if value:
                found.add(field_name)

    if not matches:
        result["application_validation_status"] = "partial"
        result["application_validation_error"] = "No requested Application Form fields found on page 1"
    elif not all(field in found for field in REQUIRED_APPLICATION_FIELDS):
        missing = [field.removeprefix("application_") for field in REQUIRED_APPLICATION_FIELDS if field not in found]
        result["application_validation_status"] = "partial"
        result["application_validation_error"] = "Fields not found on page 1: " + ", ".join(missing)
    return result


def extract_application_from_pdf_bytes(payload: bytes) -> dict[str, Any]:
    """Parse page 1 only, plus the existing evidence-based one-page branding rule."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        if len(reader.pages) < 1:
            raise ValueError("Application Form PDF has no pages")
        page = reader.pages[0]
        text = _get_page_text(page)
        result = parse_application_page1(text)
        has_image = _page_has_images(page)
        has_brand = bool(re.search(r"PayU\s*Finance", text, re.IGNORECASE))
        if has_brand:
            result["application_letterhead_logo_check"] = "Available"
            result["application_letterhead_logo_evidence"] = "PayU Finance branding text found on page 1; text evidence only, not visual logo recognition"
        elif has_image:
            result["application_letterhead_logo_check"] = "Available"
            result["application_letterhead_logo_evidence"] = "Non-empty embedded image resource on page 1; image presence only, not confirmed as a logo"
        else:
            result["application_letterhead_logo_check"] = "Not available"
            result["application_letterhead_logo_evidence"] = "No branding-text or embedded-image evidence found on page 1"
        return result
    except Exception as exc:
        raise ValueError(f"Could not parse Application Form PDF: {exc}") from exc


def apply_application_matches(result: dict[str, Any], kyc_name: Any, kyc_dob: Any, kyc_gender: Any) -> dict[str, Any]:
    """Compare extracted values through the kiosk's existing deterministic rules."""
    out = dict(result)
    ns, nscore = match_name(out.get("application_borrower_name"), kyc_name)
    ds, dscore = match_dob(out.get("application_dob"), kyc_dob)
    gs, gscore = match_gender(out.get("application_gender"), kyc_gender)
    out.update({"application_name_match_status": ns, "application_name_match_score": nscore,
                "application_dob_match_status": ds, "application_dob_match_score": dscore,
                "application_gender_match": gs, "application_gender_match_score": gscore})
    statuses = [ns, ds, gs]
    if all(value == "Match" for value in statuses):
        overall = "Match"
    elif any(value == "Not a Match" for value in statuses):
        overall = "Not a Match"
    else:
        overall = "Review"
    out["application_borrower_details_match"] = overall
    return out


PENAL_CHARGES_VARIANTS = (
    "Penal Charges, if any, in case of delayed payment",
    "Penal charges/Late Payment Charges, if any, in case of delayed payment",
    "Other penal charges if any",
)


def check_kfs_page4_text(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        return {"kfs_penal_charges_check": "Review", "kfs_penal_charges_evidence": "No extractable text on target page 4",
                "kfs_validation_status": "Review", "kfs_validation_error": "No extractable text on KFS target page 4"}
    normalized = re.sub(r"\s+", " ", text).casefold()
    patterns = (
        re.compile(r"penal\s+charges\s*,?\s*if\s+any\s*,?\s*in\s+case\s+of\s+delayed\s+payment", re.I),
        re.compile(r"penal\s+charges\s*/\s*late\s+payment\s+charges\s*,?\s*if\s+any\s*,?\s*in\s+case\s+of\s+delayed\s+payment", re.I),
        re.compile(r"other\s+penal\s+charges\s+if\s+any", re.I),
    )
    for pattern in patterns:
        match = pattern.search(normalized)
        if match:
            evidence = match.group(0)
            return {"kfs_penal_charges_check": "Pass", "kfs_penal_charges_evidence": f"Found on page 4: {evidence}",
                    "kfs_validation_status": "Pass", "kfs_validation_error": ""}
    return {"kfs_penal_charges_check": "Review", "kfs_penal_charges_evidence": "Target penal-charges text not found on page 4",
            "kfs_validation_status": "Review", "kfs_validation_error": "Target penal-charges text not found on KFS page 4"}


_KFS_BORROWER_LABELS = (
    "Borrower identification number", "Customer identification number", "Name of the Borrower",
    "Borrower Name", "Address of the Borrower", "Borrower Address",
)
_KFS_NEXT_FIELD_LABELS = (
    "PAN", "PAN Number", "PAN No", "Part 1", "Part I", "Aadhaar", "Aadhaar Number",
    "Date of Birth", "DOB", "Gender", "Mobile Number", "Email", "Loan Account Number",
    "Loan Amount", "Sanctioned Amount", "Annual Percentage Rate", "Interest Rate",
    "Processing Fee", "Other Charges", "Account Number",
)


def _kfs_borrower_value(page3_text: str, labels: tuple[str, ...]) -> str:
    """Extract one numbered page-3 borrower row, stopping at row/field boundaries.

    PDF text extraction often removes table cells and joins row numbers to the
    preceding value. Row numbers are treated as boundaries only when followed
    by a recognized next-row label, so numeric address content (including PINs)
    remains part of the borrower address.
    """
    label_pattern = _label_regex(*labels)
    all_labels = _label_regex(*_KFS_BORROWER_LABELS)
    next_fields = _label_regex(*_KFS_NEXT_FIELD_LABELS)
    next_row = rf"(?<!\d)\s+\d{{1,2}}[.)]?\s*(?=(?:{all_labels}|{next_fields})(?![\w]))"
    boundary = rf"(?=(?:{all_labels})\s*[:#-]?|(?:{next_fields})(?![\w])|{next_row}|$)"
    match = re.search(
        rf"(?:{label_pattern})\s*[:#-]?\s*(.*?){boundary}",
        page3_text, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip(" :#-\t\r\n")
    # Table extractors sometimes append a row number to a value without
    # retaining the next row's label. Remove only the tiny serial token; never
    # remove multi-digit address numbers or PINs.
    return re.sub(r"\s+[1-4]$", "", value).strip()


def _parse_kfs_page3_text(page3_text: str) -> dict[str, str]:
    """Parse only the three requested borrower fields from KFS page 3."""
    return {
        "kfs_borrower_identification_number": _kfs_borrower_value(
            page3_text, ("Borrower identification number", "Customer identification number")),
        "kfs_borrower_name": _kfs_borrower_value(
            page3_text, ("Name of the Borrower", "Borrower Name")),
        "kfs_borrower_address": _kfs_borrower_value(
            page3_text, ("Address of the Borrower", "Borrower Address")),
    }


def extract_kfs_from_pdf_bytes(payload: bytes) -> dict[str, Any]:
    """Parse LP KFS borrower data from page 3 and penal charges from page 4 only."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        if len(reader.pages) < 3:
            page3_text = ""
            result = {"kfs_borrower_identification_number": "", "kfs_borrower_name": "", "kfs_borrower_address": "",
                      "kfs_borrower_name_match_status": "Not evaluated", "kfs_borrower_name_match_score": None,
                      "kfs_borrower_address_match_status": "Not evaluated", "kfs_borrower_address_match_score": None,
                      "kfs_borrower_details_match": "Not available", "kfs_letterhead_logo_check": "Not available",
                      "kfs_letterhead_logo_evidence": "KFS page 3 is unavailable", "kfs_digital_signature_check": "Not available",
                      "kfs_digital_signature_evidence": "", "kfs_penal_charges_check": "Review",
                      "kfs_penal_charges_evidence": f"PDF has {len(reader.pages)} page(s); target page 4 is unavailable",
                      "kfs_validation_status": "Review", "kfs_validation_error": f"PDF has {len(reader.pages)} page(s); KFS target page 4 is unavailable"}
            return result
        page3 = reader.pages[2]
        page3_text = _get_page_text(page3)
        page1_text = _get_page_text(reader.pages[0])
        result = _parse_kfs_page3_text(page3_text)
        branding_text = "\n".join((page1_text, page3_text))
        brand_hit = bool(re.search(r"PayU\s*Finance", branding_text, re.IGNORECASE))
        result["kfs_letterhead_logo_check"] = "Detected" if brand_hit else ("Not Detected" if branding_text.strip() else "Not available")
        result["kfs_letterhead_logo_evidence"] = (
            "PayU Finance branding text found on page 1 or 3; text evidence only, not visual logo recognition" if brand_hit
            else "No PayU Finance branding text found on inspected page 1 or 3" if branding_text.strip()
            else "No extractable text on inspected KFS pages 1 or 3"
        )
        signature = _signature_structure(reader, include_page_numbers=False)
        result["kfs_digital_signature_check"] = signature["check"]
        result["kfs_digital_signature_evidence"] = signature["evidence"]
        result["kfs_borrower_details_match"] = "Available" if all(result[key] for key in ("kfs_borrower_identification_number", "kfs_borrower_name", "kfs_borrower_address")) else "Not available"
        result.update(check_kfs_page4_text(_get_page_text(reader.pages[3])) if len(reader.pages) >= 4 else {
            "kfs_penal_charges_check": "Review", "kfs_penal_charges_evidence": f"PDF has {len(reader.pages)} page(s); target page 4 is unavailable",
            "kfs_validation_status": "Review", "kfs_validation_error": f"PDF has {len(reader.pages)} page(s); KFS target page 4 is unavailable",
        })
        return result
    except Exception as exc:
        raise ValueError(f"Could not parse KFS PDF: {exc}") from exc


def apply_kfs_matches(result: dict[str, Any], kyc_name: Any, kyc_permanent_address: Any) -> dict[str, Any]:
    """Compare KFS borrower name/address using existing 60-point matching rules."""
    out = dict(result)
    out["kfs_borrower_name_match_status"], out["kfs_borrower_name_match_score"] = match_name(out.get("kfs_borrower_name"), kyc_name)
    out["kfs_borrower_address_match_status"], out["kfs_borrower_address_match_score"] = match_address(out.get("kfs_borrower_address"), kyc_permanent_address, "permanent")
    return out


def extract_application_from_url(url: Any) -> dict[str, Any]:
    key = _validate_url(url, APPLICATION_LINK_FIELD)
    with _CACHE_LOCK:
        cached = _APPLICATION_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    result = extract_application_from_pdf_bytes(fetch_mitc_pdf(key, field_name=APPLICATION_LINK_FIELD))
    with _CACHE_LOCK:
        _APPLICATION_CACHE[key] = dict(result)
    return result


def extract_kfs_from_url(url: Any) -> dict[str, Any]:
    key = _validate_url(url, "kfs_link")
    with _CACHE_LOCK:
        cached = _KFS_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    result = extract_kfs_from_pdf_bytes(fetch_mitc_pdf(key, field_name="kfs_link"))
    with _CACHE_LOCK:
        _KFS_CACHE[key] = dict(result)
    return result


def check_gtc_url(url: Any) -> dict[str, str]:
    key = _validate_url(url, "gtc_link")
    with _CACHE_LOCK:
        cached = _GTC_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    fetch_mitc_pdf(key, field_name="gtc_link")
    result = {"gtc_status": "Available", "gtc_validation_error": ""}
    with _CACHE_LOCK:
        _GTC_CACHE[key] = dict(result)
    return result


def clear_document_check_caches() -> None:
    with _CACHE_LOCK:
        _APPLICATION_CACHE.clear()
        _KFS_CACHE.clear()
        _GTC_CACHE.clear()
