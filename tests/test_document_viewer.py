import ssl
from io import BytesIO
from unittest.mock import patch

import pandas as pd
from pypdf import PdfWriter

import app as app_module
from app import app
from mitc_extraction import MITCDocumentError, fetch_mitc_pdf


def synthetic_pdf() -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


def loaded_state(link: str):
    return {"df": pd.DataFrame([{"mitc_form_link": link}]), "filename": "fixture.xlsx", "path": None}


def test_document_route_success_has_inline_pdf_headers():
    payload = synthetic_pdf()
    app_module.STATE.clear()
    app_module.STATE.update(loaded_state("https://source.test/mitc.pdf"))
    with patch.object(app_module, "fetch_mitc_pdf", return_value=payload):
        with app.test_client() as client:
            response = client.get("/api/records/0/mitc/document")
    assert response.status_code == 200
    assert response.data == payload
    assert response.content_type == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline;")


def test_document_route_missing_url_is_truthful_error():
    app_module.STATE.clear()
    app_module.STATE.update(loaded_state(""))
    with app.test_client() as client:
        response = client.get("/api/records/0/mitc/document")
    assert response.status_code == 400
    assert "mitc_form_link is blank" in response.get_json()["error"]


def test_document_route_surfaces_non_pdf_or_failed_fetch():
    app_module.STATE.clear()
    app_module.STATE.update(loaded_state("https://source.test/not-a-pdf"))
    failure = MITCDocumentError("MITC URL did not return PDF bytes", status_code=502)
    with patch.object(app_module, "fetch_mitc_pdf", side_effect=failure):
        with app.test_client() as client:
            response = client.get("/api/records/0/mitc/document")
    assert response.status_code == 502
    assert response.get_json() == {"error": "MITC URL did not return PDF bytes"}


def test_fetch_mitc_pdf_rejects_non_pdf_response():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"not a pdf"

    with patch("mitc_extraction.urlopen", return_value=Response()):
        try:
            fetch_mitc_pdf("https://source.test/not-a-pdf")
        except MITCDocumentError as exc:
            assert exc.status_code == 502
            assert str(exc) == "MITC URL did not return PDF bytes"
        else:
            raise AssertionError("non-PDF response was accepted")


def test_fetch_mitc_pdf_passes_verified_context_for_https():
    payload = synthetic_pdf()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return payload

    with patch("mitc_extraction.urlopen", return_value=Response()) as mocked_urlopen:
        assert fetch_mitc_pdf("https://source.test/synthetic.pdf", timeout=7.5) == payload

    kwargs = mocked_urlopen.call_args.kwargs
    context = kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert kwargs["timeout"] == 7.5


def test_fetch_mitc_pdf_surfaces_failed_fetch():
    with patch("mitc_extraction.urlopen", side_effect=TimeoutError("fixture timeout")):
        try:
            fetch_mitc_pdf("https://source.test/timeout.pdf")
        except MITCDocumentError as exc:
            assert exc.status_code == 502
            assert "Could not fetch MITC PDF" in str(exc)
        else:
            raise AssertionError("failed fetch was accepted")


def test_fetch_mitc_pdf_validates_local_pdf_signature_and_parser():
    payload = synthetic_pdf()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return payload

    with patch("mitc_extraction.urlopen", return_value=Response()):
        assert fetch_mitc_pdf("https://source.test/synthetic.pdf") == payload


def test_form_viewer_starts_lp_mitc_at_page_1_and_keeps_scroll_note():
    app_module.STATE.clear()
    app_module.STATE.update(loaded_state("https://source.test/mitc.pdf"))
    with app.test_client() as client:
        response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "const MITC_PREVIEW_PAGE = 23;" in html
    assert "#page=${MITC_PREVIEW_PAGE}" in html
    assert "LP MITC fields are on page 1; LP parsing and viewer target page 1 / early pages only (never page 23)." in html
    assert "Viewer pages remain scrollable." in html


def test_viewer_page_constant_and_status_endpoint_are_present():
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"mitc_form_link": ""}]), "filename": "fixture.xlsx", "path": None, "generation": 1})
    with app.test_client() as client:
        response = client.get("/api/records/status")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["rows"][0]["extraction_status"] == ""
        html = client.get("/").get_data(as_text=True)
    assert "MITC_PREVIEW_PAGE = 23" in html
    assert "/api/records/status" in html
