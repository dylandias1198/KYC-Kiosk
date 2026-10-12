"""Deterministic KYC source-document viewer and local conversion tests."""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

@pytest.fixture(autouse=True)
def clear_document_caches():
    with kyc_document._KYC_CACHE_LOCK:
        kyc_document._CONTENT_TYPE_CACHE.clear()
        kyc_document._KYC_IMAGE_CACHE.clear()
        kyc_document._KYC_CONVERSION_CACHE.clear()
    with kyc_document._CACHE_LOCK:
        kyc_document._PDF_BYTES_CACHE.clear()
        kyc_document._FETCH_LOCKS.clear()
    yield
from pypdf import PdfReader, PdfWriter

import app as app_module
import kyc_document
from app import app, process_workbook
from kyc_document import KYCDocumentError


def synthetic_pdf() -> bytes:
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


def synthetic_image(image_format: str) -> bytes:
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), color=(80, 140, 200)).save(stream, format=image_format)
    return stream.getvalue()


PNG = synthetic_image("PNG")
JPEG = synthetic_image("JPEG")
WEBP = synthetic_image("WEBP")


class MockResponse:
    def __init__(self, payload: bytes, content_type: str):
        self.payload = payload
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, amount=-1):
        return self.payload if amount < 0 else self.payload[:amount]


def loaded_frame(record: dict | None = None) -> pd.DataFrame:
    row = {"user_id": "lazy-doc", "user_name": "Selected User", "kyc_doc": "36",
           "kyc_doc_link": "https://documents.example.test/selected.pdf",
           "other_metadata": "<SensitiveKyc><Name>Private XML</Name></SensitiveKyc>"}
    row.update(record or {})
    frame = pd.DataFrame([row])
    for column in app_module.KYC_DOCUMENT_COLUMNS:
        frame[column] = "Not requested" if column == "kyc_document_status" else ""
    return frame


def set_state(frame: pd.DataFrame):
    app_module.STATE.clear()
    app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 554})


@pytest.mark.parametrize(("payload", "content_type"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_document_route_serves_images_with_correct_content_type(payload, content_type, monkeypatch):
    set_state(loaded_frame({"kyc_doc": "37"}))
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: MockResponse(payload, content_type))
    with app.test_client() as client:
        response = client.get("/api/records/0/kyc/document")
    assert response.status_code == 200 and response.data == payload
    assert response.content_type == content_type
    assert response.headers["Content-Disposition"].startswith("inline;")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_direct_pdf_route_serves_validated_pdf_inline_with_safe_headers(monkeypatch):
    frame = loaded_frame({"kyc_doc": "CERSAI_AADHAAR"})
    set_state(frame)
    payload = synthetic_pdf()
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: MockResponse(payload, "application/pdf"))
    with app.test_client() as client:
        response = client.get("/api/records/0/kyc/document")
    assert response.status_code == 200
    assert response.data == payload
    assert response.content_type == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline;")
    assert response.headers["X-KYC-Document-Status"] == "Available"
    assert response.headers["X-KYC-Document-Source-Type"] == "CERSAI_AADHAAR"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    assert frame.at[0, "kyc_document_status"] == "Available"


@pytest.mark.parametrize("doc_type", ["CERSAI_AADHAAR", "36", "37", "98", "DIGILOCKER_EAADHAAR"])
def test_all_direct_types_use_selected_type_and_supplied_file_link(doc_type, monkeypatch):
    frame = loaded_frame({"kyc_doc": doc_type, "kyc_doc_link": f"https://docs.test/not-inferred-by-name-{doc_type}"})
    set_state(frame)
    payload = synthetic_pdf()
    calls = []
    def fake_urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return MockResponse(payload, "application/pdf")
    monkeypatch.setattr(kyc_document, "urlopen", fake_urlopen)
    result, content_type = kyc_document.fetch_kyc_document(frame.at[0, "kyc_doc_link"], doc_type)
    assert (result, content_type) == (payload, "application/pdf")
    assert calls == [f"https://docs.test/not-inferred-by-name-{doc_type}"]


@pytest.mark.parametrize(("payload", "content_type"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_image_document_content_types_are_supported(payload, content_type, monkeypatch):
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: MockResponse(payload, content_type))
    result, mime = kyc_document.fetch_kyc_document(f"https://images.test/source-{content_type}", "37")
    assert result == payload and mime == content_type


def test_numeric_document_type_with_excel_float_representation_is_accepted(monkeypatch):
    payload = synthetic_pdf()
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: MockResponse(payload, "application/pdf"))
    result, mime = kyc_document.fetch_kyc_document("https://docs.test/number", 36.0)
    assert result == payload and mime == "application/pdf"


def test_missing_link_and_unsupported_type_fail_without_network(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("unsupported or blank input must not fetch")
    monkeypatch.setattr(kyc_document, "urlopen", unexpected)
    with pytest.raises(KYCDocumentError, match="kyc_doc_link is blank") as missing:
        kyc_document.fetch_kyc_document("", "36")
    assert missing.value.document_status == "Not available"
    with pytest.raises(KYCDocumentError, match="Unsupported KYC document type") as unsupported:
        kyc_document.fetch_kyc_document("https://docs.test/file.pdf", "SOME_OTHER_TYPE")
    assert unsupported.value.status_code == 415


@pytest.mark.parametrize(("payload", "content_type", "expected"), [
    (b"x" * (kyc_document.MAX_SOURCE_BYTES + 1), "image/png", "20 MB"),
    (b"\x89PNG\r\n\x1a\ntruncated", "image/png", "malformed"),
    (b"not-a-pdf", "application/pdf", "malformed"),
    (b"<html>not a document</html>", "text/html", "unsupported content type"),
    (b"%PDF-1.7 truncated", "application/pdf", "malformed"),
])
def test_oversized_unsupported_and_malformed_responses_are_rejected(payload, content_type, expected, monkeypatch):
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: MockResponse(payload, content_type))
    with pytest.raises(KYCDocumentError, match=expected):
        kyc_document.fetch_kyc_document("https://docs.test/file", "98")


def test_fetch_rejects_content_length_over_limit_before_body_read(monkeypatch):
    response = MockResponse(b"", "image/png")
    response.headers["Content-Length"] = str(kyc_document.MAX_SOURCE_BYTES + 1)
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: response)
    with pytest.raises(KYCDocumentError, match="20 MB"):
        kyc_document.fetch_kyc_document("https://docs.test/large", "36")


def test_metadata_xml_and_nested_base64_extraction():
    xml = b"<Kyc><Name>Fixture</Name></Kyc>"
    encoded = base64.b64encode(xml).decode("ascii")
    assert kyc_document.extract_xml_from_metadata(xml.decode()) == xml
    assert kyc_document.extract_xml_from_metadata({"response": {"data": encoded}}) == xml
    assert kyc_document.extract_xml_from_metadata(json.dumps({"payload": {"xml_base64": encoded}})) == xml
    assert kyc_document.extract_xml_from_metadata({"metadata": "no xml here"}) is None
    arbitrary_sensitive = base64.b64encode(b"Sensitive payload not necessarily XML").decode()
    assert kyc_document.contains_sensitive_metadata({"opaque": arbitrary_sensitive})
    assert kyc_document.contains_sensitive_metadata("data:application/xml;base64," + encoded)
    assert not kyc_document.contains_sensitive_metadata({"review": "ok"})


@pytest.mark.parametrize("doc_type", ["AADHAAR_OTP", "DIGILOCKER_AADHAAR_XML"])
def test_conversion_types_create_local_display_pdf_and_keep_raw_payload_out_of_api(doc_type):
    xml = "<Kyc><Name>Fixture Borrower</Name><Address>Example Place</Address></Kyc>"
    metadata = {"response": {"data": base64.b64encode(xml.encode()).decode()}}
    payload, content_type = kyc_document.fetch_kyc_document("", doc_type, metadata)
    assert content_type == "application/pdf"
    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages)
    assert "Fixture Borrower" in text and "Example Place" in text
    frame = loaded_frame({"kyc_doc": doc_type, "kyc_doc_link": "", "other_metadata": metadata})
    set_state(frame)
    with app.test_client() as client:
        response = client.get("/api/records/0")
    assert response.status_code == 200
    assert "other_metadata" not in response.get_json()["record"]
    assert xml not in response.get_data(as_text=True)


def test_conversion_unavailable_and_unsafe_xml_are_truthful_and_redacted(monkeypatch):
    xml = b"<Kyc>private-value</Kyc>"
    with patch.object(kyc_document, "_local_xml_pdf", side_effect=KYCDocumentError(
            "Conversion unavailable/configuration required: local PDF conversion dependency is missing", 503,
            "Conversion unavailable/configuration required")):
        with pytest.raises(KYCDocumentError, match="Conversion unavailable/configuration required") as unavailable:
            kyc_document.fetch_kyc_document("", "AADHAAR_OTP", base64.b64encode(xml).decode())
    assert unavailable.value.document_status == "Conversion unavailable/configuration required"
    with pytest.raises(KYCDocumentError, match="Conversion metadata not supplied"):
        kyc_document.fetch_kyc_document("", "DIGILOCKER_AADHAAR_XML", {"irrelevant": "metadata"})
    with pytest.raises(KYCDocumentError, match="not safe to convert"):
        kyc_document.fetch_kyc_document("", "AADHAAR_OTP", "<!DOCTYPE x [<!ENTITY y 'secret'>]><x>&y;</x>")


def test_document_error_response_does_not_echo_source_url_or_sensitive_metadata(monkeypatch):
    secret_url = "https://private.example.test/" + "secret-url" * 30
    xml = "<Kyc>private-xml-value</Kyc>"
    frame = loaded_frame({"kyc_doc": "36", "kyc_doc_link": secret_url, "other_metadata": xml})
    set_state(frame)
    monkeypatch.setattr(kyc_document, "urlopen", lambda *_args, **_kwargs: MockResponse(b"<html/>", "text/html"))
    with app.test_client() as client:
        response = client.get("/api/records/0/kyc/document")
    assert response.status_code == 415
    assert secret_url not in response.get_data(as_text=True)
    assert xml not in response.get_data(as_text=True)
    assert frame.at[0, "kyc_document_status"] == "Not available"


def test_kyc_details_markup_maps_exact_fields_viewer_and_lazy_selected_record():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    assert "KYC Document Type</dt>" in template and "r.kyc_doc" in template
    assert "KYC Document Link</dt>" in template and "r.kyc_doc_link" in template
    assert "data-kyc-document-action=\"view\"" in template
    assert "Open in new tab" in template and "kycDocumentViewer" in template
    assert "KYC document viewer starts at page 1" in template
    assert "kycSelfieCard('KYC Selfie', r.kyc_selfie_link" in template
    assert "data-kyc-source=\"kyc_doc\"" in template
    assert "data-kyc-source=\"kyc_doc_link\"" in template
    assert "fetch(`/api/records/${recordIndex}/kyc/document`" in template
    frame = loaded_frame({"kyc_doc": "36", "kyc_doc_link": "https://documents.test/private.pdf"})
    set_state(frame)
    with app.test_client() as client:
        page = client.get("/")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "https://documents.test/private.pdf" not in html
    assert "Private XML" not in html
    selected = app.test_client().get("/api/records/0")
    assert selected.get_json()["record"]["kyc_doc"] == "36"
    assert selected.get_json()["record"]["kyc_doc_link"] == "https://documents.test/private.pdf"


def test_blank_document_fields_render_not_supplied_and_source_columns_survive_workbook():
    frame = loaded_frame({"kyc_doc": "", "kyc_doc_link": "", "other_metadata": "<Kyc>private-value</Kyc>"})
    set_state(frame)
    client = app.test_client()
    selected = client.get("/api/records/0").get_json()["record"]
    assert selected["kyc_doc"] == "" and selected["kyc_doc_link"] == ""
    assert "Not supplied" in (app_module.BASE_DIR / "templates" / "index.html").read_text()

    raw = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:1].copy()
    raw["kyc_doc"] = "AADHAAR_OTP"
    raw["kyc_doc_link"] = ""
    raw["other_metadata"] = "<Kyc><Name>private-value</Name></Kyc>"
    buffer = io.BytesIO()
    raw.to_excel(buffer, index=False)
    buffer.seek(0)
    enriched, output = process_workbook(buffer, "fixture.xlsx", persist=True)
    assert {"kyc_doc", "kyc_doc_link", "other_metadata", *app_module.KYC_DOCUMENT_COLUMNS}.issubset(enriched.columns)
    assert enriched.at[0, "kyc_doc"] == "AADHAAR_OTP"
    assert enriched.at[0, "kyc_document_status"] == "Not requested"
    saved = pd.read_excel(output, sheet_name="Enriched Data", dtype=object)
    assert saved.at[0, "kyc_doc"] == "AADHAAR_OTP"
    assert pd.isna(saved.at[0, "kyc_doc_link"])
    assert "private-value" not in str(saved.at[0, "other_metadata"])
    assert "omitted" in str(saved.at[0, "other_metadata"])

    raw["other_metadata"] = json.dumps({"opaque": base64.b64encode(b"Sensitive payload not necessarily XML").decode()})
    buffer = io.BytesIO(); raw.to_excel(buffer, index=False); buffer.seek(0)
    _enriched, opaque_output = process_workbook(buffer, "base64-redaction.xlsx", persist=True)
    saved_opaque = pd.read_excel(opaque_output, sheet_name="Enriched Data", dtype=object)
    assert "Sensitive payload" not in str(saved_opaque.at[0, "other_metadata"])
    assert "omitted" in str(saved_opaque.at[0, "other_metadata"])


def test_kyc_viewer_javascript_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    html = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    script = script.replace("{{ user_rows|tojson }}", "[]").replace("{{ mitc_summary|tojson }}", "{}")
    result = subprocess.run([node, "--check"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_selected_conversion_route_returns_pdf_and_records_status_without_serializing_xml():
    xml = "<Kyc><Name>Private Converted Detail</Name></Kyc>"
    frame = loaded_frame({"kyc_doc": "AADHAAR_OTP", "kyc_doc_link": "",
                          "other_metadata": {"xml_base64": base64.b64encode(xml.encode()).decode()}})
    set_state(frame)
    with app.test_client() as client:
        response = client.get("/api/records/0/kyc/document")
        selected = client.get("/api/records/0")
    assert response.status_code == 200 and response.content_type == "application/pdf"
    assert response.headers["X-KYC-Document-Source-Type"] == "AADHAAR_OTP"
    assert frame.at[0, "kyc_document_status"] == "Available"
    assert "other_metadata" not in selected.get_json()["record"]
    assert xml not in selected.get_data(as_text=True)


def test_invalid_type_and_missing_conversion_metadata_are_terminal_not_available_states():
    raw = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:1].copy()
    raw["kyc_doc"] = "UNKNOWN_SOURCE"
    raw["kyc_doc_link"] = "https://docs.test/file.pdf"
    raw["other_metadata"] = "{}"
    buffer = io.BytesIO(); raw.to_excel(buffer, index=False); buffer.seek(0)
    enriched, _output = process_workbook(buffer, "invalid-type.xlsx", persist=False)
    assert enriched.at[0, "kyc_document_status"] == "Not available"
    assert enriched.at[0, "kyc_document_error"] == "Unsupported KYC document type"

    raw["kyc_doc"] = "AADHAAR_OTP"; raw["kyc_doc_link"] = ""
    buffer = io.BytesIO(); raw.to_excel(buffer, index=False); buffer.seek(0)
    enriched, _output = process_workbook(buffer, "missing-xml.xlsx", persist=False)
    assert enriched.at[0, "kyc_document_status"] == "Not available"
    assert enriched.at[0, "kyc_document_error"] == "Conversion metadata not supplied"
