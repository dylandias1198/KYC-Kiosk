"""LP-only MITC parser, structural signature and profile behavior tests; no S3."""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from pypdf import PdfWriter

import app as app_module
from app import app
from mitc_extraction import (
    LP_MITC_COLUMNS,
    extract_lp_mitc_from_pdf_bytes,
    parse_lp_mitc_text,
)


LP_TEXT = """Date: 01-08-2026
Customer identification number: LP-CUSTOMER-2094
Name of the Borrower: Ananya Rao
"""


LP_TABLE_SCREENSHOT_TEXT = """1. Date 01-08-2026
2. Customer identification number 8192016077
3. Name of the Borrower SAHDEV SINGH
4. PAN ******157M
5. Effective Date 01-08-2026
6. Facility content must not be included
"""


def test_lp_numbered_table_fixture_extracts_exact_fields_without_pan_or_later_rows():
    assert parse_lp_mitc_text(LP_TABLE_SCREENSHOT_TEXT) == {
        "lp_mitc_date": "01-08-2026",
        "lp_mitc_customer_identification_number": "8192016077",
        "lp_mitc_borrower_name": "SAHDEV SINGH",
    }


def test_lp_table_parser_supports_flattened_and_wrapped_pdf_text():
    flattened = "1. Date 01-08-2026 2. Customer identification number 8192016077 3. Name of the Borrower SAHDEV SINGH 4. PAN ******157M 5. Effective Date 15-08-2026 6. Facility details"
    wrapped = "1. Date\n01-08-2026\n2. Customer identification\nnumber\n8192016077\n3. Name of the Borrower\nSAHDEV\nSINGH\n4. PAN\n******157M\n5. Effective Date\n15-08-2026\nFacility details"
    for fixture in (flattened, wrapped):
        assert parse_lp_mitc_text(fixture) == {
            "lp_mitc_date": "01-08-2026",
            "lp_mitc_customer_identification_number": "8192016077",
            "lp_mitc_borrower_name": "SAHDEV SINGH",
        }


def test_lp_table_extraction_does_not_merge_fields_from_later_pages(monkeypatch):
    class Reader:
        def __init__(self, _stream):
            self.pages = [
                SimpleNamespace(extract_text=lambda: "1. Date 01-08-2026\n2. Customer identification number 8192016077"),
                SimpleNamespace(extract_text=lambda: "3. Name of the Borrower SAHDEV SINGH"),
            ]
            self.trailer = {"/Root": {}}
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_lp_mitc_from_pdf_bytes(b"synthetic")
    assert result["lp_mitc_date"] == "01-08-2026"
    assert result["lp_mitc_customer_identification_number"] == "8192016077"
    assert result["lp_mitc_borrower_name"] == ""
    assert result["lp_mitc_validation_status"] == "parsed_with_missing_fields"


def test_lp_full_borrower_name_is_matched_against_kyc_name_without_truncation():
    row = pd.DataFrame([{
        "user_id": "lp-1", "user_name": "Unrelated source name", "kyc_name": "SAHDEV SINGH",
        "mitc_form_link": "https://fixture.test/lp-full-name.pdf",
    }])
    app_module._set_url_rows_result(row, "https://fixture.test/lp-full-name.pdf", {
        "lp_mitc_borrower_name": "SAHDEV SINGH", "mitc_profile": "LP",
    })
    assert row.at[0, "lp_mitc_borrower_name"] == "SAHDEV SINGH"
    assert row.at[0, "lp_mitc_borrower_name_match_status"] == "Match"
    assert row.at[0, "lp_mitc_borrower_name_match_score"] == 100.0


def test_lp_parser_extracts_only_exact_label_values_and_includes_iso_date():
    parsed = parse_lp_mitc_text(LP_TEXT)
    assert parsed == {
        "lp_mitc_date": "01-08-2026",
        "lp_mitc_customer_identification_number": "LP-CUSTOMER-2094",
        "lp_mitc_borrower_name": "Ananya Rao",
    }
    assert parse_lp_mitc_text("Date: 2026-08-01\nCustomer identification number: C1\nName of the Borrower: N")['lp_mitc_date'] == "2026-08-01"


def test_lp_match_uses_only_kyc_name_and_missing_or_mismatch_is_truthful():
    row = pd.DataFrame([{
        "user_id": "u1", "user_name": "Ananya Rao", "kyc_name": "Different Person",
        "mitc_form_link": "https://fixture.test/lp.pdf",
    }])
    app_module._set_url_rows_result(row, "https://fixture.test/lp.pdf", {
        "lp_mitc_borrower_name": "Ananya Rao",
        "lp_mitc_validation_status": "success",
        "mitc_profile": "LP",
    })
    assert row.at[0, "lp_mitc_borrower_name_match_status"] == "Not a Match"
    assert row.at[0, "lp_mitc_borrower_name_match_score"] < 60
    row.at[0, "kyc_name"] = "Ananya Rao"
    app_module._set_url_rows_result(row, "https://fixture.test/lp.pdf", {"lp_mitc_borrower_name": "Ananya Rao", "mitc_profile": "LP"})
    assert row.at[0, "lp_mitc_borrower_name_match_status"] == "Match"
    assert row.at[0, "lp_mitc_borrower_name_match_score"] == 100.0
    row.at[0, "kyc_name"] = ""
    app_module._set_url_rows_result(row, "https://fixture.test/lp.pdf", {"lp_mitc_borrower_name": "Ananya Rao"})
    assert row.at[0, "lp_mitc_borrower_name_match_status"] == "KYC name not present"
    assert pd.isna(row.at[0, "lp_mitc_borrower_name_match_score"])
    assert row.at[0, "mitc_borrower_gender"] == ""
    assert row.at[0, "mitc_kyc_name"] == ""
    assert row.at[0, "mitc_borrower_name"] == ""
    assert row.at[0, "mitc_borrower_name_match_status"] == "Not applicable"


def test_reupload_resets_legacy_mitc_fields_for_lp():
    raw = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:1].copy()
    raw.loc[raw.index[0], "mitc_form_link"] = ""
    legacy_values = {
        "mitc_borrower_name": "Old FPL name", "mitc_borrower_gender": "Female",
        "mitc_borrower_mobile_number": "9999999999", "mitc_borrower_whatsapp_number": "9999999999",
        "mitc_borrower_permanent_address": "Old permanent address",
        "mitc_borrower_correspondence_address": "Old correspondence address",
        "mitc_kyc_name": "Old KYC name", "mitc_kyc_gender": "Female", "mitc_kyc_address": "Old KYC address",
    }
    for column, value in legacy_values.items():
        raw[column] = value
    stream = BytesIO()
    raw.to_excel(stream, index=False)
    enriched, _ = app_module.process_workbook(BytesIO(stream.getvalue()), "reuploaded.xlsx", persist=False)
    row = enriched.iloc[0]
    for column in legacy_values:
        assert row[column] == ""
    assert row["mitc_borrower_name_match_status"] == "Not applicable"


def pdf_with_visible_signature_text():
    writer = PdfWriter()
    page = writer.add_blank_page(width=72, height=72)
    page.extract_text = lambda: "Digitally signed by Example"  # type: ignore[method-assign]
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def reader_mock(fields):
    class Reader:
        def __init__(self, _stream):
            self.pages = [SimpleNamespace(extract_text=lambda: LP_TEXT + "\nDigitally signed by Example", images=(object(),))]
            self.trailer = {"/Root": {"/AcroForm": {"/Fields": fields}}}
    return Reader


def test_no_structural_signature_even_with_visible_phrase(monkeypatch):
    monkeypatch.setattr("pypdf.PdfReader", reader_mock([]))
    result = extract_lp_mitc_from_pdf_bytes(pdf_with_visible_signature_text())
    assert result["lp_mitc_digital_signature_check"] == "Not Detected"
    assert "visible page text is not treated" in result["lp_mitc_digital_signature_evidence"]
    assert "Not Detected" == result["lp_mitc_digital_signature_check"]  # visible image and phrase are not proof


def test_signature_field_structure_is_review_not_crypto_pass(monkeypatch):
    signature_field = {"/FT": "/Sig", "/T": "ApprovalSignature"}
    monkeypatch.setattr("pypdf.PdfReader", reader_mock([signature_field]))
    result = extract_lp_mitc_from_pdf_bytes(pdf_with_visible_signature_text())
    assert result["lp_mitc_digital_signature_check"] == "Detected"
    assert "not cryptographically verified" in result["lp_mitc_digital_signature_evidence"]
    assert "ApprovalSignature" in result["lp_mitc_digital_signature_evidence"]


def test_signature_dictionary_is_detected(monkeypatch):
    value = {"/ByteRange": [0, 1, 2, 3], "/Contents": b"opaque"}
    signature_field = {"/FT": "/Sig", "/T": "Signed", "/V": value}
    monkeypatch.setattr("pypdf.PdfReader", reader_mock([signature_field]))
    result = extract_lp_mitc_from_pdf_bytes(pdf_with_visible_signature_text())
    assert "signature dictionary (/ByteRange and /Contents)" in result["lp_mitc_digital_signature_evidence"]
    assert result["lp_mitc_digital_signature_check"] == "Detected"
    assert "not cryptographically verified" in result["lp_mitc_digital_signature_evidence"]


def test_lp_page_one_target_never_accesses_page_23(monkeypatch):
    class Page:
        def __init__(self, text): self.text, self.calls = text, 0
        def extract_text(self): self.calls += 1; return self.text
        @property
        def images(self): return ()
    pages = [Page(LP_TEXT)] + [Page("irrelevant") for _ in range(22)] + [Page("PAGE 23 MUST NOT BE READ")]
    class Reader:
        def __init__(self, _stream):
            self.pages = pages
            self.trailer = {"/Root": {}}
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_lp_mitc_from_pdf_bytes(b"mock")
    assert result["lp_mitc_validation_status"] == "success"
    assert pages[0].calls >= 1
    assert pages[22].calls == 0
    assert sum(page.calls for page in pages) <= 5  # page 1 + early branding pages; not page 23



def test_signature_page_number_is_retained_when_structural_widget_has_page(monkeypatch):
    page_refs = [SimpleNamespace(idnum=index, generation=0) for index in range(1, 38)]
    field = {"/FT": "/Sig", "/T": "approval", "/P": page_refs[36]}
    class Page:
        def __init__(self, reference, text=""):
            self.indirect_reference = reference
            self.text = text
        def extract_text(self): return self.text
        @property
        def images(self): return ()
    pages = [Page(ref, LP_TEXT if index == 0 else "") for index, ref in enumerate(page_refs)]
    class Reader:
        def __init__(self, _stream):
            self.pages = pages
            self.trailer = {"/Root": {"/AcroForm": {"/Fields": [field]}}}
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_lp_mitc_from_pdf_bytes(b"synthetic")
    assert result["lp_mitc_digital_signature_check"] == "Detected"
    assert result["lp_mitc_digital_signature_page"] == "37"
    assert all(page.text == "" for page in pages[1:])

def test_lp_form_ui_only_exposes_lp_fields_and_profile_placeholders():
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"user_id": "u1", "user_name": "Test", "kyc_name": "Test", "mitc_form_link": ""}]), "filename": "test.xlsx", "path": None, "generation": 1})
    with app.test_client() as client:
        html = client.get('/').get_data(as_text=True)
        assert 'for="mitcProfile"' in html
        assert "['LP','FPL','MPL']" in html
        assert "const mitcProfileStates = {LP: 'LP profile active', FPL: 'FPL MITC profile not implemented yet', MPL: 'MPL MITC profile not implemented yet'};" in html
        assert 'FPL MITC profile not implemented yet' in html
        assert 'MPL MITC profile not implemented yet' in html
        lp_view = html.split("if (currentMitcProfile !== 'LP')", 1)[1].split('} else if (isSanction)', 1)[0]
        for label in ('Date', 'Customer identification number', 'Name of the Borrower / Borrower Name', 'KYC name',
                      'Borrower name vs KYC', 'Existing name score'):
            assert label in lp_view
        for fpl_only in ("fieldCard('Gender',r.mitc_borrower_gender", "fieldCard('Mobile',r.mitc_borrower_mobile_number",
                        "fieldCard('WhatsApp',r.mitc_borrower_whatsapp_number", "fieldCard('Permanent address',r.mitc_borrower_permanent_address",
                        "fieldCard('Correspondence address',r.mitc_borrower_correspondence_address", "r.mitc_kyc_name",
                        "r.mitc_kyc_gender", "r.mitc_kyc_address"):
            assert fpl_only not in lp_view
        assert "integrityCard('Digital signature check'" in lp_view
        assert "integrityCard('Letterhead check (Logo)'" in lp_view
        assert 'LP validation status' not in lp_view and 'LP validation error' not in lp_view
        assert 'Structural signature check' not in lp_view
        assert 'not cryptographically verified' in lp_view
        status = client.get('/api/records/status/summary?profile=FPL').get_json()
        assert status['profile'] == 'FPL'
        assert status['profile_state'] == 'FPL profile not implemented yet'
        response = client.post('/api/records/0/mitc?profile=MPL')
        assert response.status_code == 200
        assert response.get_json()['implemented'] is False
        assert response.get_json()['profile_state'] == 'MPL MITC profile not implemented yet'


def test_lp_worker_does_not_run_fpl_legacy_parser_or_fill_compatibility_fields(monkeypatch):
    lp = app_module.empty_lp_mitc_fields(status="success")
    lp.update({"lp_mitc_date": "2025-02-01", "lp_mitc_customer_identification_number": "C-1",
               "lp_mitc_borrower_name": "LP Name"})
    monkeypatch.setattr(app_module, "extract_lp_mitc_from_url", lambda _url: dict(lp))
    result = app_module.extract_mitc_from_url("https://fixture.test/lp-only.pdf")
    assert result["lp_mitc_date"] == "2025-02-01"
    assert result["lp_mitc_customer_identification_number"] == "C-1"
    assert result["lp_mitc_borrower_name"] == "LP Name"
    for column in ("mitc_borrower_name", "mitc_borrower_gender", "mitc_borrower_mobile_number",
                   "mitc_borrower_whatsapp_number", "mitc_borrower_permanent_address",
                   "mitc_borrower_correspondence_address", "mitc_kyc_name", "mitc_kyc_gender", "mitc_kyc_address"):
        assert result[column] == ""
    assert result["mitc_borrower_name_match_status"] == "Not applicable"


def test_lp_columns_are_defined():
    assert set(LP_MITC_COLUMNS) == {
        "lp_mitc_date", "lp_mitc_customer_identification_number", "lp_mitc_borrower_name",
        "lp_mitc_borrower_name_match_status", "lp_mitc_borrower_name_match_score",
        "lp_mitc_validation_status", "lp_mitc_validation_error",
        "lp_mitc_digital_signature_check", "lp_mitc_digital_signature_evidence", "lp_mitc_digital_signature_page",
        "lp_mitc_letterhead_logo_check", "lp_mitc_letterhead_logo_evidence",
    }


def test_lp_form_hides_validation_but_shows_compact_checks_and_page_one_route():
    from pathlib import Path
    template = (Path(__file__).parents[1] / "templates" / "index.html").read_text()
    lp = template.split("if (currentMitcProfile !== 'LP')", 1)[1].split('} else if (isSanction)', 1)[0]
    assert "fieldCard('LP validation status'" not in lp
    assert "fieldCard('LP validation error'" not in lp
    assert "integrityCard('Page check'" in lp and "integrityCard('Signature page'" in lp
    assert "integrityCard('Digital signature check'" in lp and "integrityCard('Letterhead check (Logo)'" in lp
    assert "const page = isKfs ? 4 : 1;" in (Path(__file__).parents[1] / "static" / "pdf-viewer.js").read_text()
    assert "currentFormType === 'MITC' && currentMitcProfile === 'LP'" in template
