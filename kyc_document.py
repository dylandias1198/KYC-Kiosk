"""Secure, credential-free KYC source-document retrieval and local XML conversion."""
from __future__ import annotations

import base64
import binascii
import io
import hashlib
import json
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

from mitc_extraction import (
    MITCDocumentError, PDF_MAX_BYTES, _CACHE_LOCK, _FETCH_LOCKS, _PDF_BYTES_CACHE,
    _validate_url, _verified_ssl_context,
)

DIRECT_TYPES = {"CERSAI_AADHAAR", "36", "37", "98", "DIGILOCKER_EAADHAAR"}
CONVERSION_TYPES = {"AADHAAR_OTP", "DIGILOCKER_AADHAAR_XML"}
SUPPORTED_TYPES = DIRECT_TYPES | CONVERSION_TYPES
MAX_SOURCE_BYTES = PDF_MAX_BYTES
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_CONVERTED_TEXT_CHARACTERS = 500_000
FETCH_TIMEOUT_SECONDS = 15.0
FETCH_RETRIES = 2
FETCH_BACKOFF_SECONDS = 0.2

_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
_CONTENT_TYPE_CACHE: dict[str, str] = {}
_KYC_IMAGE_CACHE: dict[tuple[str, str], tuple[bytes, str]] = {}
_KYC_CONVERSION_CACHE: dict[tuple[str, str], bytes] = {}
_KYC_CACHE_LOCK = threading.RLock()
_KYC_TYPE_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def normalize_document_type(value: Any) -> str:
    """Canonicalize labels for routing without changing the displayed source value."""
    raw = "" if value is None else str(value).strip()
    if re.fullmatch(r"(?:36|37|98)\.0+", raw):
        raw = raw.split(".", 1)[0]
    return raw.upper()


class KYCDocumentError(ValueError):
    """Safe, concise error with an HTTP status and workbook-safe status label."""

    def __init__(self, message: str, status_code: int = 502, status: str = "Failed"):
        super().__init__(message)
        self.status_code = status_code
        self.document_status = status


def _content_signature(payload: bytes, content_type: str) -> bool:
    if content_type == "application/pdf":
        if b"%PDF-" not in payload[:1024] or b"%%EOF" not in payload[-1024:]:
            return False
        try:
            from pypdf import PdfReader
            len(PdfReader(io.BytesIO(payload)).pages)
            return True
        except Exception:
            return False
    if content_type.startswith("image/"):
        expected_format = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}.get(content_type)
        if expected_format is None:
            return False
        try:
            from PIL import Image
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != expected_format or image.width < 1 or image.height < 1:
                    return False
                if image.width * image.height > 40_000_000:
                    return False
                image.verify()
            return True
        except Exception:
            return False
    return False


def _download(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "KYC-Match-Kiosk/1.0", "Accept": ", ".join(sorted(_CONTENT_TYPES))})
    total_attempts = FETCH_RETRIES + 1
    last_status = 502
    for attempt in range(total_attempts):
        try:
            kwargs: dict[str, Any] = {"timeout": FETCH_TIMEOUT_SECONDS}
            if url.casefold().startswith("https://"):
                kwargs["context"] = _verified_ssl_context()
            with urlopen(request, **kwargs) as response:
                content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type not in _CONTENT_TYPES:
                    raise KYCDocumentError("KYC document has an unsupported content type", 415, "Not available")
                declared_length = response.headers.get("Content-Length")
                expected_length = None
                if declared_length:
                    try:
                        expected_length = int(declared_length)
                    except ValueError:
                        raise KYCDocumentError("KYC document has an invalid content length", 502) from None
                    if expected_length > MAX_SOURCE_BYTES:
                        raise KYCDocumentError("KYC document exceeds the 20 MB viewer limit", 413)
                payload = response.read(MAX_SOURCE_BYTES + 1)
                if len(payload) > MAX_SOURCE_BYTES:
                    raise KYCDocumentError("KYC document exceeds the 20 MB viewer limit", 413)
                if expected_length is not None and len(payload) != expected_length:
                    raise KYCDocumentError("KYC document response was truncated", 502)
                if not payload:
                    raise KYCDocumentError("KYC document is empty", 502)
                if not _content_signature(payload, content_type):
                    raise KYCDocumentError("KYC document is truncated or malformed", 502)
                return payload, content_type
        except KYCDocumentError:
            raise
        except HTTPError as exc:
            last_status = 502
            if attempt + 1 >= total_attempts or exc.code < 500:
                raise KYCDocumentError(f"KYC document fetch failed (HTTP {exc.code})", last_status) from None
        except (URLError, TimeoutError, OSError, ssl.SSLError):
            last_status = 504
            if attempt + 1 >= total_attempts:
                raise KYCDocumentError("KYC document fetch timed out or failed", last_status) from None
        except Exception:
            raise KYCDocumentError("KYC document could not be retrieved", 502) from None
        time.sleep(min(FETCH_BACKOFF_SECONDS * (2 ** attempt), 1.0))
    raise KYCDocumentError(f"KYC document fetch failed ({last_status})", last_status)


def extract_xml_from_metadata(metadata: Any) -> bytes | None:
    """Find XML text or a base64-encoded XML payload in nested source metadata."""
    if metadata is None:
        return None
    values: list[tuple[str, str]] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key).casefold())
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            values.append((key, value))

    if isinstance(metadata, str):
        try:
            decoded_json = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            decoded_json = None
        if isinstance(decoded_json, (dict, list)):
            walk(decoded_json)
        else:
            values.append(("", metadata))
    else:
        walk(metadata)

    xml_pattern = re.compile(r"<\s*[A-Za-z_][\w:.-]*(?:\s|>)")
    max_encoded_length = ((MAX_SOURCE_BYTES + 2) // 3) * 4 + 4096
    for key, raw in values:
        candidate = raw.strip()
        data_uri = re.match(r"^data:[^,]*;base64,(.+)$", candidate, flags=re.IGNORECASE | re.DOTALL)
        if data_uri:
            candidate = data_uri.group(1).strip()
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", candidate, flags=re.IGNORECASE):
            return candidate.encode("utf-8", errors="strict")
        if xml_pattern.search(candidate) and "<?xml" in candidate[:256].casefold() or xml_pattern.match(candidate):
            xml_bytes = candidate.encode("utf-8", errors="strict")
            if len(xml_bytes) <= MAX_XML_BYTES:
                return xml_bytes
        if len(candidate) > max_encoded_length:
            continue
        compact = re.sub(r"\s+", "", candidate)
        possible_b64 = "base64" in key or "xml" in key or "data" in key or bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact))
        if not possible_b64 or len(compact) < 16:
            continue
        try:
            if "-" in compact or "_" in compact:
                raw_bytes = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
            else:
                raw_bytes = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw_bytes) <= MAX_XML_BYTES and xml_pattern.search(raw_bytes[:4096].decode("utf-8", errors="ignore")):
            return raw_bytes
    return None


def contains_sensitive_metadata(metadata: Any) -> bool:
    """Identify XML or base64 strings that must not be copied into output files."""
    if extract_xml_from_metadata(metadata) is not None:
        return True
    values: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            values.append(value.strip())

    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            values.append(metadata.strip())
        else:
            walk(parsed)
    else:
        walk(metadata)
    for value in values:
        data_uri = re.match(r"^data:[^,]*;base64,(.+)$", value, flags=re.IGNORECASE | re.DOTALL)
        candidate = re.sub(r"\s+", "", data_uri.group(1) if data_uri else value)
        if len(candidate) < 32 or len(candidate) > ((MAX_SOURCE_BYTES + 2) // 3) * 4 + 4096:
            continue
        if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", candidate):
            continue
        try:
            decoded = (base64.urlsafe_b64decode(candidate + "=" * (-len(candidate) % 4))
                       if "-" in candidate or "_" in candidate
                       else base64.b64decode(candidate, validate=True))
        except (binascii.Error, ValueError):
            continue
        if decoded:
            return True
    return False


def _local_xml_pdf(xml_bytes: bytes) -> bytes:
    if len(xml_bytes) > MAX_XML_BYTES:
        raise KYCDocumentError("KYC XML exceeds the local conversion limit", 413)
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", xml_bytes, flags=re.IGNORECASE):
        raise KYCDocumentError("XML document is not safe to convert", 422, "Failed")
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_bytes)
        lines: list[str] = []
        for element in root.iter():
            text = (element.text or "").strip()
            if text:
                lines.append(text)
        plain_text = "\n".join(lines)
        if len(plain_text) > MAX_CONVERTED_TEXT_CHARACTERS:
            plain_text = plain_text[:MAX_CONVERTED_TEXT_CHARACTERS]
        if not plain_text:
            raise ValueError("empty XML text")
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        stream = io.BytesIO()
        page_width, page_height = A4
        pdf = canvas.Canvas(stream, pagesize=A4, pageCompression=1)
        y = page_height - 48
        pdf.setFont("Helvetica", 9)
        for line in plain_text.splitlines():
            # Wrap long untrusted XML text to the available printable width.
            words = line.split()
            current = ""
            for word in words:
                trial = f"{current} {word}".strip()
                if pdf.stringWidth(trial, "Helvetica", 9) > page_width - 96 and current:
                    pdf.drawString(48, y, current)
                    y -= 13
                    current = word
                else:
                    current = trial
            if current:
                pdf.drawString(48, y, current)
                y -= 13
            if y < 48:
                pdf.showPage()
                pdf.setFont("Helvetica", 9)
                y = page_height - 48
        pdf.save()
        return stream.getvalue()
    except KYCDocumentError:
        raise
    except ImportError:
        raise KYCDocumentError("Conversion unavailable/configuration required: local PDF conversion dependency is missing", 503,
                               "Conversion unavailable/configuration required") from None
    except Exception:
        raise KYCDocumentError("KYC XML could not be converted to a displayable PDF", 422) from None


def fetch_kyc_document(url: Any, document_type: Any, metadata: Any = None) -> tuple[bytes, str]:
    """Return validated PDF/image bytes and MIME type for selected KYC source."""
    doc_type = normalize_document_type(document_type)
    if doc_type not in SUPPORTED_TYPES:
        raise KYCDocumentError("Unsupported KYC document type", 415, "Not available")
    if doc_type in CONVERSION_TYPES:
        xml_bytes = extract_xml_from_metadata(metadata)
        if xml_bytes is None:
            raise KYCDocumentError("Conversion metadata not supplied", 404, "Not available")
        conversion_key = (doc_type, hashlib.sha256(xml_bytes).hexdigest())
        with _KYC_CACHE_LOCK:
            pdf = _KYC_CONVERSION_CACHE.get(conversion_key)
        if pdf is None:
            pdf = _local_xml_pdf(xml_bytes)
            with _KYC_CACHE_LOCK:
                _KYC_CONVERSION_CACHE[conversion_key] = pdf
        if len(pdf) > MAX_SOURCE_BYTES or not _content_signature(pdf, "application/pdf"):
            raise KYCDocumentError("Locally converted KYC PDF is malformed or oversized", 502)
        return pdf, "application/pdf"

    try:
        key = _validate_url(url, field_name="kyc_doc_link")
    except ValueError as exc:
        raise KYCDocumentError(str(exc), 400, "Not available") from None
    lock_key = (key, doc_type)
    with _KYC_CACHE_LOCK:
        lock = _KYC_TYPE_LOCKS.setdefault(lock_key, threading.Lock())
    with lock:
        with _KYC_CACHE_LOCK:
            cached_image = _KYC_IMAGE_CACHE.get(lock_key)
            cached_type = _CONTENT_TYPE_CACHE.get(key)
        with _CACHE_LOCK:
            cached_payload = _PDF_BYTES_CACHE.get(key)
        if cached_image is not None and _content_signature(*cached_image):
            return cached_image
        if cached_payload is not None:
            if cached_type is None and _content_signature(cached_payload, "application/pdf"):
                return cached_payload, "application/pdf"
            if cached_type is not None and _content_signature(cached_payload, cached_type):
                return cached_payload, cached_type
        payload, content_type = _download(key)
        if content_type == "application/pdf":
            with _CACHE_LOCK:
                _PDF_BYTES_CACHE[key] = payload
        else:
            with _KYC_CACHE_LOCK:
                _KYC_IMAGE_CACHE[lock_key] = (payload, content_type)
        with _KYC_CACHE_LOCK:
            _CONTENT_TYPE_CACHE[key] = content_type
        return payload, content_type
