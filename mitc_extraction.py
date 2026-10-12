"""MITC PDF fetching and deterministic label-based extraction.

The fetcher is intentionally small and credential-free: S3 URLs are supplied by
uploaded workbook rows, and urllib performs a bounded GET. Results are cached
only for the lifetime of the local Flask process, keyed by URL.
"""
from __future__ import annotations

import io
import os
from datetime import datetime
import re
import socket
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MITC_TARGET_PAGE = 23
"""The printed MITC page containing borrower/KYC fields (1-based)."""
MITC_WORKER_COUNT = 4
ALTERNATE_MITC_MAX_PAGES = 12
SANCTION_MAX_TEXT_PAGES = 100
"""Maximum pages scanned for sanction fields (configurable, hard-capped at 500)."""
SANCTION_MAX_PAGE_TEXT_CHARS = 100_000
"""Per-page extracted-text cap that bounds memory during fallback scans."""


# Fetch settings are deliberately module-level so deployments can change them
# without changing the parser or application code. Each retry is a fresh GET
# with its own urllib timeout; the fixed retry count keeps the total bounded.
MITC_FETCH_TIMEOUT_SECONDS = 60.0
"""Default per-attempt socket/read timeout for MITC downloads."""
MITC_FETCH_RETRIES = 2
"""Default number of retries after the initial MITC download attempt."""
MITC_FETCH_BACKOFF_SECONDS = 0.25
"""Initial bounded delay between retry attempts."""
MITC_FETCH_MAX_BACKOFF_SECONDS = 2.0
"""Maximum delay between retry attempts."""


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return value if value > minimum else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value >= minimum else default
    except (TypeError, ValueError):
        return default


# Environment values are read once at process start, which makes a running
# process deterministic and still permits per-deployment configuration.
MITC_FETCH_TIMEOUT_SECONDS = _env_float("MITC_FETCH_TIMEOUT_SECONDS", MITC_FETCH_TIMEOUT_SECONDS, 0.0)
MITC_FETCH_RETRIES = _env_int("MITC_FETCH_RETRIES", MITC_FETCH_RETRIES, 0)
SANCTION_MAX_TEXT_PAGES = min(_env_int("SANCTION_MAX_TEXT_PAGES", SANCTION_MAX_TEXT_PAGES, 1), 250)
SANCTION_MAX_PAGE_TEXT_CHARS = min(_env_int("SANCTION_MAX_PAGE_TEXT_CHARS", SANCTION_MAX_PAGE_TEXT_CHARS, 1_000), 100_000)

LP_MITC_COLUMNS = [
    "lp_mitc_date", "lp_mitc_customer_identification_number", "lp_mitc_borrower_name",
    "lp_mitc_borrower_name_match_status", "lp_mitc_borrower_name_match_score",
    "lp_mitc_validation_status", "lp_mitc_validation_error",
    "lp_mitc_digital_signature_check", "lp_mitc_digital_signature_evidence",
    "lp_mitc_digital_signature_page", "lp_mitc_letterhead_logo_check", "lp_mitc_letterhead_logo_evidence",
]
MITC_PROFILE_COLUMNS = ["mitc_profile"]

MITC_COLUMNS = [
    "mitc_form_date",
    "mitc_borrower_name",
    "mitc_borrower_gender",
    "mitc_borrower_mobile_number",
    "mitc_borrower_whatsapp_number",
    "mitc_borrower_permanent_address",
    "mitc_borrower_correspondence_address",
    "mitc_kyc_name",
    "mitc_kyc_gender",
    "mitc_kyc_address",
    "mitc_extraction_status",
    "mitc_extraction_error",
    "mitc_customer_identification_number",
    "mitc_name_of_borrower",
    "mitc_borrower_name_match_status",
    "mitc_borrower_name_match_score",
    "mitc_letterhead_logo_check",
    "mitc_letterhead_logo_evidence",
    "mitc_digital_signature_check",
    "mitc_digital_signature_evidence",
    "mitc_validation_status",
    "mitc_validation_error",
]

# A process-local cache avoids repeated downloads after a user revisits a row.
_MITC_CACHE: dict[str, dict[str, Any]] = {}
_LP_MITC_CACHE: dict[str, dict[str, Any]] = {}
_PDF_BYTES_CACHE: dict[str, bytes] = {}
_SANCTION_CACHE: dict[str, dict[str, Any]] = {}
_FETCH_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCK = threading.RLock()
PDF_MAX_BYTES = 20 * 1024 * 1024
SANCTION_LINK_FIELD = "_".join(("sanction", "letter", "link"))


class MITCDocumentError(ValueError):
    """A truthful, user-facing error while retrieving a source MITC PDF."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def empty_mitc_fields(status: str = "Not requested", error: str = "") -> dict[str, Any]:
    """Return every output field without borrowing values from user/KYC columns."""
    return {
        "mitc_form_date": "",
        "mitc_borrower_name": "",
        "mitc_borrower_gender": "",
        "mitc_borrower_mobile_number": "",
        "mitc_borrower_whatsapp_number": "",
        "mitc_borrower_permanent_address": "",
        "mitc_borrower_correspondence_address": "",
        "mitc_kyc_name": "",
        "mitc_kyc_gender": "",
        "mitc_kyc_address": "",
        "mitc_extraction_status": status,
        "mitc_extraction_error": error,
        "mitc_customer_identification_number": "",
        "mitc_name_of_borrower": "",
        "mitc_borrower_name_match_status": "Not evaluated",
        "mitc_borrower_name_match_score": None,
        "mitc_letterhead_logo_check": "Not available",
        "mitc_letterhead_logo_evidence": "",
        "mitc_digital_signature_check": "Not available",
        "mitc_digital_signature_evidence": "",
        "mitc_validation_status": status,
        "mitc_validation_error": error,
    }


def _clean(value: str) -> str:
    # PDF text may put a label's value over several physical lines.
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip(" :\t\r\n")


def _heading_pattern(title: str) -> str:
    """Match a section title with or without the PDF table's number prefix."""
    return rf"(?<!\w)(?:(?P<number>\d+)\.\s*)?{title}\b(?:\s*\([^)]*\))?"


def _find_heading(text: str, title: str, start: int = 0) -> re.Match[str] | None:
    return re.search(_heading_pattern(title), text[start:], re.IGNORECASE)


def _next_numbered_section(text: str, start: int, current_number: int | None) -> int:
    """Find a later section boundary without cutting normal address text.

    MITC PDFs commonly flatten ``5. Lender`` onto the same line as the prior
    field. Line-start numbered headings are always boundaries; inline headings
    are accepted for the conventional lender/terms section names and when the
    number follows the current section number.
    """
    line_heading = re.search(r"(?m)^\s*\d+\.\s+[A-Za-z]", text[start:])
    candidates: list[int] = []
    if line_heading:
        candidates.append(start + line_heading.start())
    # Inline extraction can flatten the following heading onto the address.
    # Require a capitalized heading word and a later number; this distinguishes
    # ``5. Other Details`` from ordinary decimal/address text.
    inline = re.compile(
        r"(?<!\w)(?P<number>\d+)\.\s+(?P<title>[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,4})"
    )
    for match in inline.finditer(text, start):
        number = int(match.group("number"))
        if current_number is None or number > current_number:
            candidates.append(match.start())
            break
    return min(candidates) if candidates else len(text)


def _label_value(text: str, label: str, stop_labels: list[str]) -> str:
    """Read a colon-terminated label until the next real label boundary.

    Requiring the colon is intentional: the borrower heading contains a table
    descriptor such as ``(Name, Gender, ...)``. Treating bare words in that
    descriptor as labels was the source of the original page-23 regression.
    """
    label_expression = rf"(?<!\w){re.escape(label)}\s*:"
    if stop_labels:
        stops = "|".join(rf"(?<!\w){re.escape(item)}\s*:" for item in stop_labels)
        ending = rf"(?=\s*(?:{stops})|\Z)"
    else:
        ending = r"(?=\Z)"
    pattern = rf"{label_expression}\s*(.*?){ending}"
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return _clean(match.group(1)) if match else ""


def _extract_form_date(text: str, before: int) -> str:
    """Extract the date immediately associated with the Date table label."""
    prefix = text[:before]
    match = re.search(
        r"(?<!\w)Date\s*:?\s*(?:\r?\n\s*)?(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
        prefix,
        re.IGNORECASE,
    )
    return match.group("date") if match else ""


def parse_mitc_text(text: str) -> dict[str, Any]:
    """Extract only the documented MITC fields from page-23 PDF text.

    Section boundaries tolerate numeric table prefixes, while field boundaries
    require the actual colon-terminated labels. This keeps the descriptor in
    ``Borrower Details (Name, Gender, ...)`` out of the extracted values and
    prevents KYC fields from absorbing a following numbered section.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("PDF text extraction returned no text")

    borrower_match = _find_heading(text, r"Borrower\s+Details")
    if borrower_match is None:
        borrower = ""
        borrower_start = len(text)
        borrower_end = borrower_start
    else:
        borrower_start = borrower_match.start()
        borrower_end = borrower_match.end()
        kyc_after_borrower = _find_heading(text, r"KYC\s+Details", borrower_end)
        borrower_limit = borrower_end + kyc_after_borrower.start() if kyc_after_borrower else len(text)
        borrower = re.sub(r"^\s*:\s*", "", text[borrower_end:borrower_limit])

    kyc_search_start = borrower_end if borrower_match is not None else 0
    kyc_match = _find_heading(text, r"KYC\s+Details", kyc_search_start)
    if kyc_match is None:
        kyc = ""
        kyc_number = None
    else:
        kyc_start = kyc_search_start + kyc_match.start()
        kyc_end = kyc_search_start + kyc_match.end()
        number_text = kyc_match.group("number")
        kyc_number = int(number_text) if number_text else None
        kyc_limit = _next_numbered_section(text, kyc_end, kyc_number)
        kyc = re.sub(r"^\s*:\s*", "", text[kyc_end:kyc_limit])

    borrower_labels = [
        "Name", "Gender", "Mobile Number and WhatsApp Number", "Permanent Address", "Correspondence Address",
    ]
    kyc_labels = ["Name", "Gender", "Address"]
    result = empty_mitc_fields(status="success")
    result["mitc_form_date"] = _extract_form_date(text, borrower_start)
    result["mitc_borrower_name"] = _label_value(borrower, "Name", borrower_labels[1:])
    result["mitc_borrower_gender"] = _label_value(borrower, "Gender", borrower_labels[2:])
    mobile = _label_value(borrower, "Mobile Number and WhatsApp Number", borrower_labels[3:])
    # This source label represents both numbers. Keep the source value in both
    # explicitly named columns; no value is inferred from a different column.
    result["mitc_borrower_mobile_number"] = mobile
    result["mitc_borrower_whatsapp_number"] = mobile
    result["mitc_borrower_permanent_address"] = _label_value(borrower, "Permanent Address", [borrower_labels[4]])
    result["mitc_borrower_correspondence_address"] = _label_value(borrower, "Correspondence Address", [])
    result["mitc_kyc_name"] = _label_value(kyc, "Name", kyc_labels[1:])
    result["mitc_kyc_gender"] = _label_value(kyc, "Gender", [kyc_labels[2]])
    result["mitc_kyc_address"] = _label_value(kyc, "Address", [])
    if not any(result[column] for column in MITC_COLUMNS[:10]):
        result["mitc_extraction_status"] = "parsed_with_missing_fields"
        result["mitc_extraction_error"] = "No standard MITC field labels were found in the extracted PDF text"
    return result


def _validate_url(url: Any, field_name: str = "mitc_form_link") -> str:
    value = "" if url is None else str(url).strip()
    if not value or value.casefold() in {"nan", "nat", "none", "null"}:
        raise ValueError(f"{field_name} is blank")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        raise ValueError(f"{field_name} must be an http(s) URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} is not a valid http(s) URL") from None
    return value


def _verified_ssl_context() -> ssl.SSLContext:
    """Build a verified HTTPS context using certifi when it is available.

    Python.org installations on macOS can have an empty platform trust store
    until their bundled certificate installer is run. certifi supplies a
    maintained CA bundle without weakening certificate or hostname checks. If
    certifi is not installed (or its bundle cannot be used), retain the
    platform's normal verified defaults.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError, TypeError, ValueError):
        return ssl.create_default_context()


def _retryable_fetch_error(exc: BaseException) -> bool:
    """Return whether a failed network attempt is safe to retry.

    Validation, HTTP client errors, and PDF validation happen outside this
    predicate. Only socket/read timeouts and errors commonly caused by a
    transient connection are retried.
    """
    if isinstance(exc, HTTPError):
        return 500 <= exc.code < 600
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ValueError):
            return False
        return _retryable_fetch_error(reason) if isinstance(reason, BaseException) else True
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError)):
        return True
    return False


def _format_fetch_error(exc: BaseException, attempt: int, total_attempts: int, timeout: float, retries: int) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, HTTPError):
        message = f"HTTP {exc.code} while fetching MITC PDF"
    else:
        message = f"Could not fetch MITC PDF: {reason}"
    return (
        f"{message} (attempt {attempt}/{total_attempts}; "
        f"timeout={timeout:g}s; retries={retries})"
    )


def _fetch_mitc_attempt(key: str, timeout: float) -> bytes:
    request = Request(key, headers={"User-Agent": "KYC-Match-Kiosk/1.0"})
    urlopen_kwargs: dict[str, Any] = {"timeout": timeout}
    if request.full_url.casefold().startswith("https://"):
        # Keep TLS verification enabled while making the macOS Python/venv
        # trust-store issue explicit. HTTP URLs do not accept this kwarg.
        urlopen_kwargs["context"] = _verified_ssl_context()
    with urlopen(request, **urlopen_kwargs) as response:  # no credentials are added
        return response.read()


def _download_mitc_pdf(url: Any, timeout: float | None = None) -> bytes:
    """Fetch one source document with bounded transient-error retries.

    A timeout is applied independently to every attempt. HTTP 4xx responses,
    invalid URLs, non-PDF responses, and malformed PDFs fail immediately and
    are never retried. TLS verification remains enabled for HTTPS requests.
    """
    try:
        key = _validate_url(url)
    except ValueError as exc:
        raise MITCDocumentError(str(exc), status_code=400) from exc

    effective_timeout = MITC_FETCH_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if effective_timeout <= 0:
        raise MITCDocumentError("MITC fetch timeout must be greater than zero", status_code=500)
    retries = MITC_FETCH_RETRIES
    total_attempts = retries + 1
    payload: bytes | None = None
    for attempt in range(1, total_attempts + 1):
        try:
            payload = _fetch_mitc_attempt(key, effective_timeout)
            break
        except HTTPError as exc:
            if attempt < total_attempts and _retryable_fetch_error(exc):
                time.sleep(min(MITC_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1)), MITC_FETCH_MAX_BACKOFF_SECONDS))
                continue
            raise MITCDocumentError(
                _format_fetch_error(exc, attempt, total_attempts, effective_timeout, retries), status_code=502
            ) from exc
        except (URLError, TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as exc:
            if attempt < total_attempts and _retryable_fetch_error(exc):
                time.sleep(min(MITC_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1)), MITC_FETCH_MAX_BACKOFF_SECONDS))
                continue
            raise MITCDocumentError(
                _format_fetch_error(exc, attempt, total_attempts, effective_timeout, retries), status_code=502
            ) from exc
        except ValueError as exc:
            # urllib can raise ValueError for a malformed URL after the basic
            # scheme check. It is user input, not a transient fetch failure.
            raise MITCDocumentError(f"MITC URL is invalid: {exc}", status_code=400) from exc
        except Exception as exc:
            # Do not turn parser or response-validation errors into retries.
            raise MITCDocumentError(
                _format_fetch_error(exc, attempt, total_attempts, effective_timeout, retries), status_code=502
            ) from exc

    if payload is None:  # defensive: every loop path either returns or raises
        raise MITCDocumentError("Could not fetch MITC PDF: no response", status_code=502)
    if not payload:
        raise MITCDocumentError("MITC URL returned an empty response", status_code=502)
    if len(payload) > PDF_MAX_BYTES:
        raise MITCDocumentError("MITC PDF exceeds the 20 MB viewer limit", status_code=502)
    if b"%PDF-" not in payload[:1024]:
        raise MITCDocumentError("MITC URL did not return PDF bytes", status_code=502)
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        # Touch the page collection so truncated PDFs fail before browser delivery.
        len(reader.pages)
    except ImportError as exc:
        raise MITCDocumentError("PDF parser dependency pypdf is not installed", status_code=500) from exc
    except Exception as exc:
        raise MITCDocumentError(f"MITC URL returned a malformed PDF: {exc}", status_code=502) from exc
    return payload


def fetch_mitc_pdf(url: Any, timeout: float | None = None, *, field_name: str = "mitc_form_link") -> bytes:
    """Fetch a PDF once per URL for this process; all document routes share bytes."""
    try:
        key = _validate_url(url, field_name=field_name)
    except ValueError as exc:
        raise MITCDocumentError(str(exc), status_code=400) from exc
    with _CACHE_LOCK:
        lock = _FETCH_LOCKS.setdefault(key, threading.Lock())
    with lock:
        with _CACHE_LOCK:
            payload = _PDF_BYTES_CACHE.get(key)
        if payload is not None:
            return payload
        payload = _download_mitc_pdf(key, timeout=timeout)
        with _CACHE_LOCK:
            _PDF_BYTES_CACHE[key] = payload
        return payload


def extract_mitc_from_url(url: Any, timeout: float | None = None) -> dict[str, Any]:
    """Fetch and parse one URL; return a truthful, workbook-safe result."""
    try:
        key = _validate_url(url)
    except ValueError as exc:
        return empty_mitc_fields(status="failed", error=str(exc))
    with _CACHE_LOCK:
        cached = _MITC_CACHE.get(key)
        if cached is not None:
            # Compatibility with a process that loaded an older implementation:
            # a stale failed entry must never block a selected-row retry.
            if cached.get("mitc_extraction_status") != "failed":
                return dict(cached)
            _MITC_CACHE.pop(key, None)
    try:
        payload = fetch_mitc_pdf(key, timeout=timeout)
        result = extract_mitc_from_pdf_bytes(payload)
    except MITCDocumentError as exc:
        result = empty_mitc_fields(status="failed", error=str(exc))
        result["mitc_validation_status"] = "failed"
        result["mitc_validation_error"] = str(exc)
    except Exception as exc:  # parser errors must not crash the app
        result = empty_mitc_fields(status="failed", error=f"Could not parse MITC PDF: {exc}")
        result["mitc_validation_status"] = "failed"
        result["mitc_validation_error"] = result["mitc_extraction_error"]
    # Successful/partial parses are reusable. Failed results intentionally do
    # not enter the cache: a selected-row POST or a later upload can retry a
    # previously timed-out/transient URL without restarting the process.
    if result.get("mitc_extraction_status") != "failed":
        with _CACHE_LOCK:
            _MITC_CACHE[key] = dict(result)
    return dict(result)


def _get_page_text(page: Any) -> str:
    try:
        return page.extract_text() or ""
    except Exception:
        return ""


def _page_has_images(page: Any) -> bool:
    """Report only whether pypdf exposes a non-empty embedded image resource."""
    try:
        images = getattr(page, "images", ())
        return bool(len(images))
    except Exception:
        return False


def _evidence_checks(pages: Any, page_count: int, *, early_pages: int = 3, text_cache: dict[int, str] | None = None) -> dict[str, str]:
    early_parts: list[str] = []
    early_image_pages: list[int] = []
    for index in range(min(page_count, early_pages)):
        page = pages[index]
        text = (text_cache or {}).get(index)
        if text is None:
            text = _get_page_text(page)
            if text_cache is not None:
                text_cache[index] = text
        if text:
            early_parts.append(text)
        if _page_has_images(page):
            early_image_pages.append(index + 1)
    early_text = "\n".join(early_parts)
    brand_matches = re.findall(r"PayU\s*Finance", early_text, re.IGNORECASE)
    if brand_matches:
        logo_check = "Pass"
        logo_evidence = f"PayU Finance branding text found on early page(s) 1-{min(page_count, early_pages)}; text evidence only, not visual logo recognition"
    elif early_image_pages:
        logo_check = "Pass"
        logo_evidence = "Non-empty embedded image resource(s) on early page(s) " + ", ".join(map(str, early_image_pages)) + "; image presence only, not confirmed as a logo"
    elif early_text.strip():
        logo_check = "Fail"
        logo_evidence = f"No PayU Finance branding text or embedded image resource found on inspected early page(s) 1-{min(page_count, early_pages)}"
    else:
        logo_check = "Not available"
        logo_evidence = f"No extractable text or embedded image evidence on inspected early page(s) 1-{min(page_count, early_pages)}"

    last_index = page_count - 1
    last_text = ((text_cache or {}).get(last_index) if page_count else "")
    if page_count and last_text is None:
        last_text = _get_page_text(pages[last_index])
        if text_cache is not None:
            text_cache[last_index] = last_text
    signature_pattern = re.compile(r"digitally\s+signed\s+by|digital\s+signature|electronically\s+signed", re.IGNORECASE)
    signature_match = signature_pattern.search(last_text)
    if signature_match:
        context = _clean(last_text[max(0, signature_match.start() - 40):signature_match.end() + 100])
        signature_check = "Pass"
        signature_evidence = f"Signature phrase {context!r} found on last page {page_count}; textual evidence only"
    elif page_count and _page_has_images(pages[last_index]):
        signature_check = "Review"
        signature_evidence = f"Embedded image resource found on last page {page_count}; image is not classified as a signature"
    elif last_text.strip():
        signature_check = "Fail"
        signature_evidence = f"No recognized digital-signature phrase on inspected last page {page_count}"
    else:
        signature_check = "Not available"
        signature_evidence = f"No extractable text or image evidence on last page {page_count}"
    return {
        "letterhead_logo_check": logo_check,
        "letterhead_logo_evidence": logo_evidence,
        "digital_signature_check": signature_check,
        "digital_signature_evidence": signature_evidence,
        "last_page_number": str(page_count) if page_count else "",
    }


def _alternate_field(text: str, label: str) -> str:
    label_re = re.compile(r"(?<![A-Za-z])" + re.escape(label).replace(r"\ ", r"\s+") + r"\s*:?\s*", re.IGNORECASE)
    match = label_re.search(text)
    if not match:
        return ""
    tail = text[match.end():]
    boundary = re.search(r"(?<!\w)\d+\.\s*(?=[A-Za-z])", tail)
    if boundary:
        tail = tail[:boundary.start()]
    else:
        # Labels/values are frequently separated by newlines in the text layer.
        # Stop at the next line-level enumerated item when present.
        boundary = re.search(r"(?m)^\s*\d+\.\s+", tail)
        if boundary:
            tail = tail[:boundary.start()]
    return _clean(tail[:300])


def _parse_alternate_mitc(text: str) -> dict[str, str]:
    date_value = _alternate_field(text, "Date")
    date_match = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", date_value)
    customer = _alternate_field(text, "Customer identification number")
    customer_match = re.search(r"\b[0-9]{5,24}\b", customer)
    borrower = _alternate_field(text, "Name of the Borrower")
    borrower = re.split(r"\s*\(", borrower, maxsplit=1)[0].strip(" :;,-")
    return {
        "mitc_form_date": date_match.group(0) if date_match else "",
        "mitc_customer_identification_number": customer_match.group(0) if customer_match else "",
        "mitc_name_of_borrower": borrower,
        "mitc_borrower_name": borrower,
    }


def extract_mitc_from_pdf_bytes(payload: bytes) -> dict[str, Any]:
    """Keep standard MITC scoped to page 23; scan at most 12 early pages for alternate labels."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        pages = reader.pages
        page_count = len(pages)
        result = empty_mitc_fields(status="parsed_with_missing_fields")
        text_cache: dict[int, str] = {}
        standard_text = _get_page_text(pages[MITC_TARGET_PAGE - 1]) if page_count >= MITC_TARGET_PAGE else ""
        if page_count >= MITC_TARGET_PAGE:
            text_cache[MITC_TARGET_PAGE - 1] = standard_text
        if standard_text.strip():
            result.update(parse_mitc_text(standard_text))
        if result.get("mitc_extraction_status") != "success":
            for index in range(min(page_count, ALTERNATE_MITC_MAX_PAGES)):
                page_text = text_cache.get(index)
                if page_text is None:
                    page_text = _get_page_text(pages[index])
                    text_cache[index] = page_text
                fields = _parse_alternate_mitc(page_text)
                for key, value in fields.items():
                    if value:
                        result[key] = value
                if all(result.get(key) for key in ("mitc_form_date", "mitc_customer_identification_number", "mitc_name_of_borrower")):
                    break
        checks = _evidence_checks(pages, page_count, text_cache=text_cache)
        result["mitc_letterhead_logo_check"] = checks["letterhead_logo_check"]
        result["mitc_letterhead_logo_evidence"] = checks["letterhead_logo_evidence"]
        result["mitc_digital_signature_check"] = checks["digital_signature_check"]
        result["mitc_digital_signature_evidence"] = checks["digital_signature_evidence"]
        standard_found = any(result.get(column) for column in MITC_COLUMNS[:10])
        alternate_found = any(result.get(key) for key in ("mitc_customer_identification_number", "mitc_name_of_borrower"))
        if standard_found or alternate_found:
            alternate_complete = all(result.get(key) for key in ("mitc_form_date", "mitc_customer_identification_number", "mitc_name_of_borrower"))
            if alternate_complete or result.get("mitc_extraction_status") == "success":
                result["mitc_extraction_status"] = "success"
                result["mitc_extraction_error"] = ""
            else:
                result["mitc_extraction_status"] = "parsed_with_missing_fields"
                result["mitc_extraction_error"] = "Some standard or alternate MITC fields were not found"
        else:
            result["mitc_extraction_status"] = "parsed_with_missing_fields"
            result["mitc_extraction_error"] = f"No standard fields on page 23 or alternate MITC labels within first {min(page_count, ALTERNATE_MITC_MAX_PAGES)} page(s)"
            if page_count < MITC_TARGET_PAGE:
                result["mitc_extraction_status"] = "failed"
                result["mitc_extraction_error"] = f"PDF has {page_count} page(s); MITC target page {MITC_TARGET_PAGE} is unavailable and no alternate fields were found"
        result["mitc_validation_status"] = "success" if result["mitc_extraction_status"] == "success" else ("failed" if result["mitc_extraction_status"] == "failed" else "partial")
        result["mitc_validation_error"] = result["mitc_extraction_error"]
        return result
    except ImportError as exc:
        raise MITCDocumentError("PDF parser dependency pypdf is not installed", status_code=500) from exc
    except Exception as exc:
        raise ValueError(f"Could not parse MITC PDF: {exc}") from exc



def empty_lp_mitc_fields(status: str = "Not requested", error: str = "") -> dict[str, Any]:
    """Return LP-specific outputs, leaving FPL borrower/KYC fields blank."""
    result = empty_mitc_fields(status=status, error=error)
    result.update({
        "lp_mitc_date": "", "lp_mitc_customer_identification_number": "", "lp_mitc_borrower_name": "",
        "lp_mitc_borrower_name_match_status": "Not evaluated", "lp_mitc_borrower_name_match_score": None,
        "lp_mitc_validation_status": status, "lp_mitc_validation_error": error,
        "lp_mitc_digital_signature_check": "Not available", "lp_mitc_digital_signature_evidence": "",
        "lp_mitc_digital_signature_page": "", "lp_mitc_letterhead_logo_check": "Not available",
        "lp_mitc_letterhead_logo_evidence": "", "mitc_profile": "LP",
        "mitc_borrower_name_match_status": "Not applicable", "mitc_borrower_name_match_score": None,
        "mitc_digital_signature_check": "Not detected", "mitc_digital_signature_evidence": "",
    })
    return result


def _lp_label_pattern(label: str) -> str:
    """Build a whitespace-tolerant pattern for an LP table field label."""
    return re.escape(label).replace(r"\ ", r"\s+")


def _lp_label_matches(text: str) -> list[tuple[str, re.Match[str]]]:
    """Locate relevant labels so row values have deterministic stop boundaries."""
    labels = (
        "Customer identification number", "Name of the Borrower", "Effective Date", "Date", "PAN",
    )
    matches: list[tuple[str, re.Match[str]]] = []
    for label in labels:
        pattern = _lp_label_pattern(label)
        for found in re.finditer(rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", text, re.IGNORECASE):
            if label == "Date":
                before = text[max(0, found.start() - 24):found.start()]
                if re.search(r"Effective\s+$", before, re.IGNORECASE):
                    continue
            matches.append((label, found))
    return sorted(matches, key=lambda item: item[1].start())


def _lp_field_value(text: str, label: str) -> str:
    """Read one LP table value up to the next known label or numbered row."""
    matches = [(kind, found) for kind, found in _lp_label_matches(text)
               if kind.casefold() == label.casefold()]
    if not matches:
        return ""
    match = matches[0][1]
    value_start = match.end()
    stop_positions = [found.start() for _kind, found in _lp_label_matches(text)
                      if found.start() >= value_start]
    # The numbered table's next row is a boundary even if its label is not in
    # the displayed schema (for example, 5. Effective Date or facility rows).
    # Keep source offsets for the row-marker scan; the intentionally explicit
    # loop avoids confusing a numbered row with a number embedded in an ID.
    for marker in re.finditer(r"(?m)(?:^|\s)(\d{1,2})\.\s*(?=[A-Za-z])", text[value_start:]):
        stop_positions.append(value_start + marker.start(1))
    end = min((position for position in stop_positions if position > value_start), default=len(text))
    candidate = _clean(text[value_start:end])
    return candidate[:300]


def parse_lp_mitc_text(text: str) -> dict[str, str]:
    """Extract only LP's date, customer identification number and borrower name."""
    if not isinstance(text, str) or not text.strip():
        return {"lp_mitc_date": "", "lp_mitc_customer_identification_number": "", "lp_mitc_borrower_name": ""}
    date_value = _lp_field_value(text, "Date")
    date_match = re.search(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b", date_value)
    return {
        "lp_mitc_date": date_match.group(0) if date_match else "",
        "lp_mitc_customer_identification_number": _lp_field_value(text, "Customer identification number"),
        "lp_mitc_borrower_name": _lp_field_value(text, "Name of the Borrower"),
    }


def _deref(value: Any) -> Any:
    try:
        return value.get_object()
    except (AttributeError, TypeError):
        return value


def _signature_structure(reader: Any, *, include_page_numbers: bool = True) -> dict[str, str]:
    """Inspect AcroForm signature-field/value structures without validating crypto."""
    try:
        root = _deref(reader.trailer.get("/Root", {}))
        acroform = _deref(root.get("/AcroForm", {})) if hasattr(root, "get") else {}
        fields = _deref(acroform.get("/Fields", [])) if hasattr(acroform, "get") else []
    except Exception:
        root, fields = {}, []
    page_refs: dict[tuple[int, int], int] = {}
    if include_page_numbers:
        try:
            for number, page in enumerate(reader.pages, start=1):
                ref = getattr(page, "indirect_reference", None)
                if ref is not None:
                    page_refs[(ref.idnum, ref.generation)] = number
        except Exception:
            pass

    signatures: list[tuple[str, str, str]] = []
    visited: set[tuple[Any, ...]] = set()

    def object_key(obj: Any) -> tuple[Any, ...]:
        ref = getattr(obj, "indirect_reference", None)
        if ref is None and hasattr(obj, "idnum"):
            ref = obj
        if ref is not None and hasattr(ref, "idnum"):
            return ("ref", ref.idnum, getattr(ref, "generation", 0))
        return ("obj", id(obj))

    def visit(field_ref: Any, inherited_type: str = "", parent_name: str = "") -> None:
        field = _deref(field_ref)
        if not hasattr(field, "get"):
            return
        key = object_key(field_ref)
        if key in visited:
            return
        visited.add(key)
        field_type = str(_deref(field.get("/FT", inherited_type)))
        title = str(_deref(field.get("/T", ""))).strip("()")
        full_name = f"{parent_name}.{title}".strip(".")
        value = _deref(field.get("/V"))
        value_is_sig = hasattr(value, "get") and "/ByteRange" in value and "/Contents" in value
        is_sig_field = field_type == "/Sig"
        if is_sig_field or value_is_sig:
            page_no = ""
            kids = _deref(field.get("/Kids", []))
            widgets = list(kids) if isinstance(kids, (list, tuple)) else []
            for widget_ref in widgets or [field_ref]:
                widget = _deref(widget_ref)
                page_ref = widget.get("/P") if hasattr(widget, "get") else None
                if page_ref is None:
                    continue
                page_no = str(page_refs.get((getattr(page_ref, "idnum", -1), getattr(page_ref, "generation", 0)), ""))
                if page_no:
                    break
            kind = "signature dictionary (/ByteRange and /Contents)" if value_is_sig else "signature field (/FT /Sig)"
            signatures.append((kind, full_name or "(unnamed signature field)", page_no))
        kids = _deref(field.get("/Kids", []))
        if isinstance(kids, (list, tuple)):
            for child in kids:
                visit(child, field_type, full_name)

    if isinstance(fields, (list, tuple)):
        for field in fields:
            visit(field)
    try:
        perms = _deref(root.get("/Perms", {}))
        doc_mdp = _deref(perms.get("/DocMDP")) if hasattr(perms, "get") else None
        if hasattr(doc_mdp, "get") and "/ByteRange" in doc_mdp and "/Contents" in doc_mdp:
            signatures.append(("signature dictionary (/ByteRange and /Contents)", "Catalog /Perms /DocMDP", ""))
    except Exception:
        pass
    if not signatures:
        return {"check": "Not Detected", "evidence": "No structural signature field (/FT /Sig) or signature dictionary (/ByteRange and /Contents) found in the PDF catalog/AcroForm; visible page text is not treated as signature evidence", "page": ""}
    evidence = "; ".join(f"{kind}; field={name}; page={page or 'not recorded'}" for kind, name, page in signatures)
    return {"check": "Detected", "evidence": evidence + "; structural presence only, not cryptographically verified", "page": ", ".join(dict.fromkeys(item[2] for item in signatures if item[2]))}


def extract_lp_mitc_from_pdf_bytes(payload: bytes) -> dict[str, Any]:
    """Parse LP's three fields from page 1 and early pages only (never page 23)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        pages = reader.pages
        count = len(pages)
        found = {"lp_mitc_date": "", "lp_mitc_customer_identification_number": "", "lp_mitc_borrower_name": ""}
        # LP's identity table is on the first relevant page. Do not combine
        # partially missing values from later pages into a fabricated row.
        text_cache: dict[int, str] = {}
        for index in range(min(count, 1)):
            page_text = text_cache.setdefault(index, _get_page_text(pages[index]))
            for key, value in parse_lp_mitc_text(page_text).items():
                if value and not found[key]:
                    found[key] = value
            if all(found.values()):
                break
        missing = [label for key, label in (("lp_mitc_date", "Date"), ("lp_mitc_customer_identification_number", "Customer identification number"), ("lp_mitc_borrower_name", "Name of the Borrower")) if not found[key]]
        status = "success" if not missing else "parsed_with_missing_fields"
        error = "" if not missing else "LP MITC labels not found: " + ", ".join(missing)
        result = empty_lp_mitc_fields(status=status, error=error)
        result.update(found)
        result["mitc_extraction_status"] = status
        result["mitc_extraction_error"] = error
        result["mitc_validation_status"] = status
        result["mitc_validation_error"] = error
        result["lp_mitc_validation_status"] = status
        result["lp_mitc_validation_error"] = error
        early_parts = []
        early_image_pages = []
        for index in range(min(count, 3)):
            page_text = text_cache.get(index)
            if page_text is None:
                page_text = _get_page_text(pages[index])
                text_cache[index] = page_text
            if page_text:
                early_parts.append(page_text)
            if _page_has_images(pages[index]):
                early_image_pages.append(index + 1)
        brand_hit = bool(re.search(r"PayU\s*Finance", "\n".join(early_parts), re.IGNORECASE))
        result["lp_mitc_letterhead_logo_check"] = "Detected" if brand_hit else ("Not Detected" if early_parts else "Not available")
        if brand_hit:
            result["lp_mitc_letterhead_logo_evidence"] = "PayU Finance branding text found on page(s) 1-3; text evidence only, not visual logo recognition"
        elif early_image_pages:
            result["lp_mitc_letterhead_logo_evidence"] = "Embedded images on early page(s) " + ", ".join(map(str, early_image_pages)) + "; image presence is not treated as logo recognition"
        elif early_parts:
            result["lp_mitc_letterhead_logo_evidence"] = "No PayU Finance branding text found in extracted early-page text"
        else:
            result["lp_mitc_letterhead_logo_evidence"] = "No extractable text on early pages 1-3"
        signature = _signature_structure(reader)
        result["lp_mitc_digital_signature_check"] = signature["check"]
        result["lp_mitc_digital_signature_evidence"] = signature["evidence"]
        result["lp_mitc_digital_signature_page"] = signature["page"]
        result["mitc_digital_signature_check"] = signature["check"]
        result["mitc_digital_signature_evidence"] = signature["evidence"]
        return result
    except ImportError as exc:
        raise MITCDocumentError("PDF parser dependency pypdf is not installed", status_code=500) from exc
    except Exception as exc:
        raise ValueError(f"Could not parse LP MITC PDF: {exc}") from exc


def extract_lp_mitc_from_url(url: Any, timeout: float | None = None) -> dict[str, Any]:
    """Fetch cached PDF bytes and cache LP-only parsed results by source URL."""
    try:
        key = _validate_url(url)
    except ValueError as exc:
        return empty_lp_mitc_fields(status="failed", error=str(exc))
    with _CACHE_LOCK:
        cached = _LP_MITC_CACHE.get(key)
        if cached is not None and cached.get("lp_mitc_validation_status") != "failed":
            return dict(cached)
    try:
        result = extract_lp_mitc_from_pdf_bytes(fetch_mitc_pdf(key, timeout=timeout))
    except MITCDocumentError as exc:
        result = empty_lp_mitc_fields(status="failed", error=str(exc))
    except Exception as exc:
        result = empty_lp_mitc_fields(status="failed", error=f"Could not parse LP MITC PDF: {exc}")
    if result.get("lp_mitc_validation_status") != "failed":
        with _CACHE_LOCK:
            _LP_MITC_CACHE[key] = dict(result)
    return result

def _valid_sanction_date(match: re.Match[str]) -> str:
    """Return a supported date token only when it represents a real calendar date."""
    token = _clean(match.group(0)).strip(".,;:")
    formats = (
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d",
        "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
        "%B %d, %Y", "%b %d, %Y",
    )
    for date_format in formats:
        try:
            parsed = datetime.strptime(token, date_format)
            return parsed.strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


_SANCTION_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}\.\d{1,2}\.\d{4}|"
    r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}|(?:Jan(?:uary)?|Feb(?:ruary)?|"
    r"Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4})(?!\d)",
    re.IGNORECASE,
)


def _sanction_date_from_text(text: str) -> str:
    date_label = re.compile(r"(?i)\b(?:sanction\s+letter\s+date|date\s+of\s+sanction|sanction\s+date|date|dated)\b\s*[:#-]?\s*")
    for label in date_label.finditer(text):
        match = _SANCTION_DATE_RE.search(text, label.end(), min(len(text), label.end() + 180))
        if match:
            value = _valid_sanction_date(match)
            if value:
                return value
    for match in _SANCTION_DATE_RE.finditer(text):
        value = _valid_sanction_date(match)
        if value:
            return value
    return ""


def _sanction_labeled_value(text: str, labels: list[str], *, max_length: int = 300) -> str:
    """Read a label/value on one line or the next non-empty line, without swallowing other fields."""
    alternatives = "|".join(labels)
    pattern = re.compile(rf"(?im)^\s*(?:{alternatives})\s*[:#-]?\s*(?P<value>[^\r\n]*)")
    next_label = re.compile(
        r"(?i)\b(?:sanction\s+letter\s+date|date\s+of\s+sanction|sanction\s+date|"
        r"dated?|reference(?:\s+(?:number|no\.?))?|ref\.?\s*(?:number|no\.?)|"
        r"name(?:\s+of\s+(?:the\s+)?)?(?:borrower|applicant)?|borrower(?:'s)?\s+name|"
        r"annexure|terms\s+and\s+conditions)\b\s*[:#-]?"
    )
    lines = text.replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if not match:
            continue
        value = match.group("value").strip()
        if not value:
            for following in lines[index + 1:]:
                candidate = following.strip()
                if not candidate:
                    continue
                if next_label.search(candidate):
                    break
                value = candidate
                break
        if not value:
            continue
        boundary = next_label.search(value)
        if boundary:
            value = value[:boundary.start()]
        value = re.split(r"\s+(?:\(\s*[\"']?Borrower\b|\(\s*[\"']?you\b)", value, maxsplit=1, flags=re.IGNORECASE)[0]
        value = _clean(value).strip(" \t:;,#-")
        if value:
            return value[:max_length]
    return ""


def _parse_sanction_text(texts: list[str]) -> dict[str, Any]:
    """Parse required identity fields only from page one; scan all supplied text for evidence checks."""
    identity_text = texts[0] if texts else ""
    joined = "\n".join(texts)
    date = _sanction_date_from_text(identity_text)
    reference_labels = [
        r"Sanction\s+Letter\s+Reference\s+(?:No\.?|Number)",
        r"Sanction\s+Letter\s+Ref(?:erence)?\s*(?:No\.?|Number)?",
        r"Sanction\s+Reference\s+(?:No\.?|Number)",
        r"Reference\s+(?:Number|No\.?)", r"Ref\.?\s*(?:Number|No\.?)",
        r"Letter\s+(?:Number|No\.?)",
    ]
    reference_label_pattern = re.compile(
        rf"(?im)^\s*(?:{'|'.join(reference_labels)})\s*[:#-]?\s*(?P<value>[^\r\n]*)"
    )
    reference_match = reference_label_pattern.search(identity_text)
    reference_label_present = reference_match is not None
    reference = _sanction_labeled_value(identity_text, reference_labels)
    reference = reference or ("Not provided in source" if reference_label_present else "")
    borrower = _sanction_labeled_value(identity_text, [
        r"Name\s+of\s+(?:the\s+)?Borrower", r"Borrower(?:'s)?\s+Name",
        r"Name\s+of\s+(?:the\s+)?Applicant", r"Applicant\s+Name", r"Name",
    ])
    borrower = re.sub(r"^(?:(?:Mr|Mrs|Ms|Miss|Dr|Smt|Shri)\.?\s*(?:/\s*)?)+", "", borrower, flags=re.IGNORECASE)
    borrower = re.split(r"\s*(?:\(|,\s*(?:Borrower|you)\b)", borrower, maxsplit=1, flags=re.IGNORECASE)[0].strip(" :;,-")
    annexure = bool(re.search(r"\bAnnexure\b", joined, re.IGNORECASE))
    terms = bool(re.search(r"Terms\s+and\s+conditions\s+of\s+the\s+sanction", joined, re.IGNORECASE))
    return {
        "sanction_letter_date": date,
        "sanction_letter_reference_number": reference,
        "_sanction_reference_label_present": reference_label_present,
        "sanction_borrower_name": _clean(borrower),
        "sanction_annexure_terms_check": "Pass" if annexure and terms else ("Fail" if joined.strip() else "Not available"),
        "sanction_annexure_terms_evidence": f"Annexure={'found' if annexure else 'not found'}; Terms and conditions of the sanction={'found' if terms else 'not found'} in scanned text",
    }


def extract_sanction_from_pdf_bytes(payload: bytes) -> dict[str, Any]:
    """Extract sanction fields by bounded page scan, with last-page signature checks."""
    field_labels = [
        ("sanction_letter_date", "Sanction letter date"),
        ("sanction_letter_reference_number", "Reference number"),
        ("sanction_borrower_name", "Borrower name"),
    ]
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        pages = reader.pages
        page_count = len(pages)
        text_cache: dict[int, str] = {}
        max_pages = min(page_count, SANCTION_MAX_TEXT_PAGES)
        parsed = _parse_sanction_text([])
        identity_page_parsed = False
        reference_label_found = False
        annexure_found = False
        terms_found = False
        for index in range(max_pages):
            page_text = _get_page_text(pages[index])[:SANCTION_MAX_PAGE_TEXT_CHARS]
            if index < 3:
                text_cache[index] = page_text
            # Required identity values are page-one-only. Never use overlap or
            # later-page text to backfill a missing date/reference/name.
            if index == 0 and not identity_page_parsed:
                parsed = _parse_sanction_text([page_text])
                reference_label_found = bool(parsed.get("_sanction_reference_label_present"))
                identity_page_parsed = True
            annexure_found = annexure_found or bool(re.search(r"\bAnnexure\b", page_text, re.IGNORECASE))
            terms_found = terms_found or bool(re.search(
                r"Terms\s+and\s+conditions\s+of\s+the\s+sanction", page_text, re.IGNORECASE
            ))
            if identity_page_parsed and annexure_found and terms_found:
                parsed["sanction_annexure_terms_check"] = "Pass"
                break
        parsed["sanction_annexure_terms_check"] = (
            "Pass" if annexure_found and terms_found
            else "Fail" if page_count and (page_count <= max_pages or max_pages == page_count)
            else "Not available" if not page_count
            else "Review"
        )
        parsed["sanction_annexure_terms_evidence"] = (
            f"Annexure={'found' if annexure_found else 'not found'}; "
            f"Terms and conditions of the sanction={'found' if terms_found else 'not found'} in scanned text"
        )

        checks = _evidence_checks(pages, page_count, text_cache=text_cache)
        findings = []
        if not parsed.get("sanction_letter_date"):
            findings.append("Sanction letter date was not found in scanned source")
        if not reference_label_found:
            findings.append("Reference number label was not found in scanned source")
        elif parsed.get("sanction_letter_reference_number") == "Not provided in source":
            findings.append("Reference number label is present but blank; not provided in source")
        if not parsed.get("sanction_borrower_name"):
            findings.append("Borrower name was not found in scanned source")
        if not annexure_found:
            findings.append("Annexure evidence was not found in scanned source")
        if not terms_found:
            findings.append("Terms and conditions of the sanction evidence was not found in scanned source")
        fully_scanned = page_count <= max_pages
        terms_status = parsed["sanction_annexure_terms_check"]
        if terms_status != "Pass" and not fully_scanned:
            terms_status = "Review"
        status = "success" if not findings else "partial"
        error = "" if not findings else "Sanction source findings: " + "; ".join(findings)
        if findings and page_count > max_pages:
            error += f" (scanned pages 1-{max_pages} of {page_count}; configured scan limit is {max_pages})"
        result = {
            "sanction_letter_date": parsed["sanction_letter_date"],
            "sanction_letter_reference_number": parsed["sanction_letter_reference_number"],
            "sanction_borrower_name": parsed["sanction_borrower_name"],
            "sanction_borrower_name_match_status": "Not evaluated",
            "sanction_borrower_name_match_score": None,
            "sanction_borrower_details_match": "Not evaluated",
            "sanction_letterhead_logo_check": checks["letterhead_logo_check"],
            "sanction_letterhead_logo_evidence": checks["letterhead_logo_evidence"],
            "sanction_digital_signature_check": checks["digital_signature_check"],
            "sanction_digital_signature_evidence": checks["digital_signature_evidence"],
            "sanction_annexure_terms_check": terms_status,
            "sanction_annexure_terms_evidence": parsed["sanction_annexure_terms_evidence"],
            "sanction_validation_status": status,
            "sanction_validation_error": error,
        }
        if not page_count:
            result["sanction_validation_status"] = "failed"
            result["sanction_validation_error"] = "PDF has no pages; " + "; ".join(findings)
        return result
    except ImportError as exc:
        raise MITCDocumentError("PDF parser dependency pypdf is not installed", status_code=500) from exc
    except Exception as exc:
        raise ValueError(f"Could not parse sanction PDF: {exc}") from exc


def extract_sanction_from_url(url: Any, timeout: float | None = None) -> dict[str, Any]:
    key = _validate_url(url, field_name=SANCTION_LINK_FIELD)
    with _CACHE_LOCK:
        cached = _SANCTION_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    payload = fetch_mitc_pdf(key, timeout=timeout, field_name=SANCTION_LINK_FIELD)
    result = extract_sanction_from_pdf_bytes(payload)
    with _CACHE_LOCK:
        _SANCTION_CACHE[key] = dict(result)
    return result

def clear_cache() -> None:
    with _CACHE_LOCK:
        _MITC_CACHE.clear()
        _SANCTION_CACHE.clear()
        _PDF_BYTES_CACHE.clear()
        _FETCH_LOCKS.clear()
