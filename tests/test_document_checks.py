"""Synthetic/no-network tests for added page-scoped document checks."""
from io import BytesIO
from unittest.mock import patch

import pandas as pd
import openpyxl
from pypdf import PdfWriter

import app as app_module
from app import app
from document_checks import (apply_application_matches, apply_kfs_matches, check_gtc_url, check_kfs_page4_text,
                             extract_application_from_pdf_bytes, extract_kfs_from_pdf_bytes,
                             parse_application_page1)
from mitc_extraction import MITCDocumentError


def pdf_bytes(page_count=1):
    stream = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


def test_application_parser_matching_and_defaults():
    text = "Name of the Borrower: Asha Rao\nDate of Birth: 23-Nov-2001\nGender: F"
    parsed = parse_application_page1(text)
    matched = apply_application_matches(parsed, "Asha Rao", "2001-11-23", "Female")
    assert parsed["application_borrower_name"] == "Asha Rao"
    assert parsed["application_dob"] == "23-Nov-2001"
    assert matched["application_name_match_status"] == "Match"
    assert matched["application_dob_match_status"] == "Match"
    assert matched["application_gender_match"] == "Match"
    assert matched["application_borrower_details_match"] == "Match"
    assert matched["application_photo_match"] == "Not implemented"
    assert apply_application_matches(parsed, "Different", "2001-11-23", "Female")["application_name_match_status"] == "Not a Match"
    assert extract_application_from_pdf_bytes(pdf_bytes())["application_validation_status"] == "partial"


def test_kfs_page4_variants_and_short_pdf():
    for phrase in (
        "Penal Charges, if any, in case of delayed payment",
        "Penal charges/Late Payment Charges, if any, in case of delayed payment",
        "Other penal charges if any",
    ):
        result = check_kfs_page4_text("Section: " + phrase)
        assert result["kfs_penal_charges_check"] == "Pass"
        assert phrase.casefold() in result["kfs_penal_charges_evidence"].casefold()
    assert check_kfs_page4_text("unrelated text")["kfs_validation_status"] == "Review"
    short = extract_kfs_from_pdf_bytes(pdf_bytes(3))
    assert short["kfs_validation_status"] == "Review"
    assert "target page 4 is unavailable" in short["kfs_validation_error"]


def test_kfs_reads_only_page_four(monkeypatch):
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text
    class Reader:
        def __init__(self, _stream): self.pages = [Page("decoy") for _ in range(3)] + [Page("Other penal charges if any")]
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_kfs_from_pdf_bytes(b"synthetic")
    assert result["kfs_validation_status"] == "Pass"



def test_kfs_extracts_borrower_table_page3_and_penal_page4_only(monkeypatch):
    class Page:
        def __init__(self, text): self.text, self.calls = text, 0
        def extract_text(self): self.calls += 1; return self.text
        @property
        def images(self): return ()
    pages = [Page("DECOY page 1"), Page("DECOY page 2"), Page(
        "Borrower identification number: C-123\nName of the Borrower: Asha Rao\nAddress of the Borrower: 12 MG Road, Pune 411001"),
        Page("Other penal charges if any"), Page("DECOY page 5")]
    class Reader:
        def __init__(self, _stream):
            self.pages = pages
            self.trailer = {"/Root": {}}
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_kfs_from_pdf_bytes(b"synthetic")
    assert result["kfs_borrower_identification_number"] == "C-123"
    assert result["kfs_borrower_name"] == "Asha Rao"
    assert result["kfs_borrower_address"] == "12 MG Road, Pune 411001"
    assert result["kfs_penal_charges_check"] == "Pass"
    assert pages[0].calls == 1 and pages[1].calls == pages[4].calls == 0
    assert pages[2].calls == 1 and pages[3].calls == 1
    assert pages[4].calls == 0
    matched = apply_kfs_matches(result, "Asha Rao", "12 Mahatma Gandhi Road, Pune 411001")
    assert matched["kfs_borrower_name_match_status"] == "Match"
    assert matched["kfs_borrower_name_match_score"] >= 60
    assert matched["kfs_borrower_address_match_status"] == "Match"
    assert matched["kfs_borrower_address_match_score"] >= 60


def test_kfs_label_variants_and_compact_checks_available():
    from document_checks import _kfs_borrower_value
    text = "Customer identification number C-9 Name of the Borrower Arpit Kaushik Address of the Borrower 1 Main Road"
    assert _kfs_borrower_value(text, ("Borrower identification number", "Customer identification number")) == "C-9"
    assert _kfs_borrower_value(text, ("Name of the Borrower", "Borrower Name")) == "Arpit Kaushik"
    assert _kfs_borrower_value(text, ("Address of the Borrower", "Borrower Address")) == "1 Main Road"

def test_gtc_blank_nonblank_and_failed_link(monkeypatch):
    with pytest.raises(ValueError, match="gtc_link is blank"):
        check_gtc_url("")
    monkeypatch.setattr("document_checks.fetch_mitc_pdf", lambda *_args, **_kwargs: b"%PDF-verified")
    assert check_gtc_url("https://source.test/gtc.pdf")["gtc_status"] == "Available"
    monkeypatch.setattr("document_checks.fetch_mitc_pdf", lambda *_args, **_kwargs: (_ for _ in ()).throw(MITCDocumentError("HTTP 404", 502)))
    with pytest.raises(MITCDocumentError, match="HTTP 404"):
        check_gtc_url("https://source.test/missing.pdf")


def test_upload_initializes_added_columns_without_fetching_blank_links(monkeypatch):
    from pathlib import Path
    fixture = Path(__file__).parents[1] / "sample_input.xlsx"
    frame = pd.read_excel(fixture, dtype=object).iloc[:1].copy()
    frame.loc[frame.index[0], ["application_form_link", "kfs_link", "gtc_link"]] = ""
    stream = BytesIO(); frame.to_excel(stream, index=False)
    monkeypatch.setattr("mitc_extraction.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected network")))
    enriched, _ = app_module.process_workbook(BytesIO(stream.getvalue()), "fixture.xlsx", persist=False)
    row = enriched.iloc[0]
    assert row["application_validation_status"] == "Not available"
    assert row["kfs_validation_status"] == "Not available"
    assert row["gtc_status"] == "Not available"


def test_additional_viewer_routes_return_inline_pdf_and_page_count():
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{
        "application_form_link": "https://source.test/application.pdf", "kfs_link": "https://source.test/kfs.pdf",
        "gtc_link": "https://source.test/gtc.pdf", "gtc_status": "Loading", "gtc_validation_error": "",
    }]), "filename": "fixture.xlsx", "path": None, "generation": 3})
    with patch.object(app_module, "fetch_mitc_pdf", return_value=pdf_bytes(4)):
        with app.test_client() as client:
            for kind in ("application", "kfs", "gtc"):
                response = client.get(f"/api/records/0/{kind}/document")
                assert response.status_code == 200
                assert response.content_type == "application/pdf"
                assert response.headers["Content-Disposition"].startswith("inline;")
                assert response.headers["X-PDF-Page-Count"] == "4"
    assert app_module.STATE["df"].iloc[0]["gtc_status"] == "Available"


def test_gtc_viewer_failure_updates_status_and_error():
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"gtc_link": "https://source.test/fail.pdf", "gtc_status": "Loading", "gtc_validation_error": ""}]), "filename": "fixture.xlsx", "path": None, "generation": 4})
    with patch.object(app_module, "fetch_mitc_pdf", side_effect=MITCDocumentError("HTTP 404", 502)):
        with app.test_client() as client:
            response = client.get("/api/records/0/gtc/document")
    assert response.status_code == 502
    assert app_module.STATE["df"].iloc[0]["gtc_status"] == "Failed"
    assert "HTTP 404" in app_module.STATE["df"].iloc[0]["gtc_validation_error"]


def test_form_routes_and_page_fragments_render():
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"mitc_form_link":"https://x/mitc.pdf", "application_form_link":"https://x/app.pdf", "kfs_link":"https://x/kfs.pdf", "gtc_link":"", "sanction_letter_link":"https://x/sanction.pdf"}]), "filename":"fixture.xlsx", "path":None, "generation":1})
    with app.test_client() as client:
        html = client.get("/").get_data(as_text=True)
        assert "application/document" in html and "kfs/document" in html and "gtc/document" in html
        assert "const MITC_PREVIEW_PAGE = 23;" in html
        assert "const page = isKfs ? 4 : 1;" in (app_module.BASE_DIR / "static" / "pdf-viewer.js").read_text() and "LP KFS borrower details use page 3 only" in html
        kfs_ui = (app_module.BASE_DIR / "static" / "kfs-form.js").read_text()
        assert "KFS Borrower Details" in kfs_ui and "Penal Charges check (page 4)" in kfs_ui
        assert "Source link" not in html and "Reference Number" not in html
        assert "application_photo_match" in html and "Not implemented" in html
        assert "Name of the Borrower" in html and "application_borrower_details_match" in html
    assert "application_letterhead_logo_evidence" not in html


# pytest import kept local to make the negative URL case explicit.
import pytest


def test_malformed_document_links_are_failed_before_queue():
    from pathlib import Path
    fixture = Path(__file__).parents[1] / "sample_input.xlsx"
    frame = pd.read_excel(fixture, dtype=object).iloc[:1].copy()
    frame.loc[frame.index[0], "application_form_link"] = "not-a-url"
    frame.loc[frame.index[0], "kfs_link"] = "http://"
    frame.loc[frame.index[0], "gtc_link"] = "bad-link"
    stream = BytesIO(); frame.to_excel(stream, index=False)
    enriched, _ = app_module.process_workbook(BytesIO(stream.getvalue()), "fixture.xlsx", persist=False)
    row = enriched.iloc[0]
    assert row["application_validation_status"] == "Failed"
    assert "Invalid application_form_link" in row["application_validation_error"]
    assert row["kfs_validation_status"] == "Failed"
    assert "Invalid kfs_link" in row["kfs_validation_error"]
    assert row["gtc_status"] == "Failed"
    assert "Invalid gtc_link" in row["gtc_validation_error"]


def test_application_worker_failure_isolated_and_persisted(monkeypatch, tmp_path):
    from pathlib import Path
    fixture = Path(__file__).parents[1] / "sample_input.xlsx"
    frame = pd.read_excel(fixture, dtype=object).iloc[:1].copy()
    frame.loc[frame.index[0], "application_form_link"] = "https://source.test/application-fail.pdf"
    frame.loc[frame.index[0], ["kfs_link", "gtc_link", "mitc_form_link", "sanction_letter_link"]] = ""
    source = BytesIO(); frame.to_excel(source, index=False)
    app_module.STATE.clear()
    app_module.STATE.update({"df": None, "filename": None, "path": None, "generation": 0})
    app_module._APPLICATION_INFLIGHT.clear()
    monkeypatch.setattr(app_module, "OUTPUT_FILENAME", "test-isolation.xlsx")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app_module, "extract_application_from_url", lambda _url: (_ for _ in ()).throw(RuntimeError("synthetic parse error")))
    with app.test_client() as client:
        response = client.post("/upload", data={"file": (BytesIO(source.getvalue()), "fixture.xlsx")}, content_type="multipart/form-data")
        assert response.status_code == 302
        # Await terminal callback state; submit returns while worker completion is asynchronous.
        import time
        deadline = time.monotonic() + 2
        row = client.get("/api/records/0").get_json()["record"]
        while row["application_validation_status"] == "extracting" and time.monotonic() < deadline:
            time.sleep(0.01)
            row = client.get("/api/records/0").get_json()["record"]
        assert row["application_validation_status"] == "Failed"
        assert "synthetic parse error" in row["application_validation_error"]
        assert row["kfs_validation_status"] == "Not available"
        assert row["gtc_status"] == "Not available"
    app_module._flush_pending_workbook_write()
    output = openpyxl.load_workbook(tmp_path / "test-isolation.xlsx", read_only=True)
    values = list(output["Enriched Data"].values)
    header = values[0]
    assert values[1][header.index("application_validation_status")] == "Failed"


def test_application_pdf_parser_uses_page_one_and_text_brand_evidence(monkeypatch):
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text
        @property
        def images(self): return []
    class Reader:
        def __init__(self, _stream):
            self.pages = [Page("PayU Finance\nName of the Borrower: Borrower X\nDate of Birth: 02/03/1990\nGender: Male"), Page("Decoy Gender: Other")]
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_application_from_pdf_bytes(b"synthetic")
    assert result["application_borrower_name"] == "Borrower X"
    assert result["application_dob"] == "02/03/1990"
    assert result["application_gender"] == "Male"
    assert result["application_letterhead_logo_check"] == "Available"
    assert "text evidence only" in result["application_letterhead_logo_evidence"]


def test_background_application_completion_updates_duplicate_rows(monkeypatch):
    from threading import Event
    completed = Event()
    calls = []
    def extraction(_url):
        calls.append(_url)
        completed.set()
        return {"application_borrower_name":"Asha Rao", "application_dob":"23-Nov-2001", "application_gender":"F",
                "application_validation_status":"success", "application_validation_error":"", "application_photo_match":"Not implemented"}
    monkeypatch.setattr(app_module, "extract_application_from_url", extraction)
    df = pd.DataFrame([{"application_form_link":"https://source.test/shared.pdf", "kyc_name":"Asha Rao", "kyc_dob":"2001-11-23", "kyc_gender":"Female",
                         **{column:"" for column in __import__("document_checks").APPLICATION_COLUMNS}},
                       {"application_form_link":"https://source.test/shared.pdf", "kyc_name":"Different", "kyc_dob":"2001-11-23", "kyc_gender":"Female",
                         **{column:"" for column in __import__("document_checks").APPLICATION_COLUMNS}}])
    app_module.STATE.clear(); app_module.STATE.update({"df":df, "filename":"fixture.xlsx", "path":None, "generation":21})
    app_module._APPLICATION_INFLIGHT.clear()
    app_module._dispatch_document_batch(df, 21, application=True)
    assert completed.wait(2)
    import time
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and (df.at[0,"application_validation_status"] != "success" or df.at[1,"application_validation_status"] != "success"):
        time.sleep(0.01)
    assert calls == ["https://source.test/shared.pdf"]
    assert list(df["application_validation_status"]) == ["success", "success"]
    assert list(df["application_name_match_status"]) == ["Match", "Not a Match"]
    assert list(df["application_photo_match"]) == ["Not implemented", "Not implemented"]


def test_blank_gtc_viewer_request_remains_not_available():
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"gtc_link": "", "gtc_status": "Not available", "gtc_validation_error": "No gtc_link supplied"}]), "filename": "fixture.xlsx", "path": None, "generation": 5})
    with app.test_client() as client:
        response = client.get("/api/records/0/gtc/document")
    assert response.status_code == 400
    assert app_module.STATE["df"].iloc[0]["gtc_status"] == "Not available"
    assert app_module.STATE["df"].iloc[0]["gtc_validation_error"] == "No gtc_link supplied"


def test_form_screen_simplification_for_sanction_gtc_and_kfs():
    from pathlib import Path
    template = (Path(__file__).parents[1] / "templates" / "index.html").read_text()
    sanction = template.split("} else if (isSanction)", 1)[1].split("} else if (isApplication)", 1)[0]
    assert "Sanction Letter Date" in sanction and "Borrower Name" in sanction and "Borrower name match against KYC" in sanction
    assert "Reference finding" in sanction and "sanctionReferenceFinding(r)" in sanction
    assert "function sanctionReferenceFinding(record)" in template
    assert "Reference number label is present but blank; not provided in source" in template
    assert "Reference number label was not found in scanned source" in template
    assert "fieldCard('Borrower Name',r.sanction_borrower_name,'sanction_borrower_name')" in sanction
    for hidden in ("Reference Number", "KYC Name", "Borrower details match", "Letterhead / Logo", "Digital Signature", "Annexure / Terms"):
        assert hidden not in sanction
    kfs = template.split("} else if (isKfs)", 1)[1].split("} else {", 1)[0]
    for visible in ("Borrower identification number", "Borrower name", "Name match", "Borrower address", "Address match",
                    "Borrower Details match", "Letterhead check (Logo)", "Digital signature check", "Penal Charges check (page 4)"):
        assert visible in kfs
    gtc = template.split("const gtcDisplay", 1)[1].split("  }", 1)[0]
    assert "GTC availability" in gtc and "Link status" in gtc
    assert "Source link" not in gtc and "A supplied nonblank link" not in gtc
    assert "Available" in gtc and "Not available" in gtc
    assert "isGtc ? ''" in template
    assert "checkCard('Penal Charges check (page 4)'" not in kfs
    assert "fieldCard('Penal Charges check (page 4)'" in (Path(__file__).parents[1] / "static" / "kfs-form.js").read_text()


def test_kfs_letterhead_uses_text_only_and_sanction_date_has_display_format_helper():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "document_checks.py").read_text()
    template = (Path(__file__).parents[1] / "templates" / "index.html").read_text()
    assert "page 1 or 3; text evidence only, not visual logo recognition" in source
    assert "formatDisplayDate(r.sanction_letter_date)" in template


def test_application_page_one_all_fields_inline_wrapped_and_label_bounded():
    from document_checks import APPLICATION_FIELDS

    # Layout mirrors the supplied page-one form: inline identity fields, then two
    # address groups with repeated City/Pincode/State/Country labels.
    page_one = """Borrower Details
Name of the
Borrower: Asha Rao | Father's Name: N/A | Mother's Name: - | Nationality: Indian
Date of Birth: 02-Nov-2005 Gender Female Mobile Number: +91 98765 43210
Email asha@example.test PAN: AXXXX1234X Other: -
Communication Address: 12 Long Road, Central Block, Near City Market
City: Pune Pincode: 411001 State: Maharashtra Country: India
Current Address: Flat 4, Lake View
City: Mumbai Pincode: N/A State: Maharashtra Country: India
Occupation type Salaried"""
    parsed = parse_application_page1(page_one)
    expected = {
        "application_borrower_name": "Asha Rao",
        "application_father_name": "N/A",
        "application_mother_name": "-",
        "application_nationality": "Indian",
        "application_dob": "02-Nov-2005",
        "application_gender": "Female",
        "application_mobile_number": "+91 98765 43210",
        "application_email": "asha@example.test",
        "application_pan": "AXXXX1234X",
        "application_other": "-",
        "application_communication_address": "12 Long Road, Central Block, Near City Market",
        "application_communication_city": "Pune",
        "application_communication_pincode": "411001",
        "application_communication_state": "Maharashtra",
        "application_communication_country": "India",
        "application_current_address": "Flat 4, Lake View",
        "application_current_city": "Mumbai",
        "application_current_pincode": "N/A",
        "application_current_state": "Maharashtra",
        "application_current_country": "India",
        "application_occupation_type": "Salaried",
    }
    assert set(APPLICATION_FIELDS) == set(expected)
    assert {field: parsed[field] for field in APPLICATION_FIELDS} == expected
    assert parsed["application_validation_status"] == "success"
    assert parsed["application_borrower_name"] == "Asha Rao"
    assert "Father's Name" not in parsed["application_borrower_name"]


def test_application_page_one_missing_fields_and_blank_placeholders():
    parsed = parse_application_page1("Name of the Borrower: -\nFather's Name: N/A\nPAN: XXXX****X")
    assert parsed["application_borrower_name"] == "-"
    assert parsed["application_father_name"] == "N/A"
    assert parsed["application_pan"] == "XXXX****X"
    assert parsed["application_gender"] == ""
    assert parsed["application_validation_status"] == "partial"
    assert "gender" in parsed["application_validation_error"]
    empty = parse_application_page1("")
    assert empty["application_photo_match"] == "Not implemented"
    assert all(empty[field] == "" for field in __import__("document_checks").APPLICATION_FIELDS)



def test_application_occupation_is_optional_and_other_missing_warnings_remain():
    parsed = parse_application_page1("Name of the Borrower: Asha Rao\nGender: Female")
    assert parsed["application_occupation_type"] == ""
    assert parsed["application_validation_status"] == "partial"
    error = parsed["application_validation_error"]
    assert error.startswith("Fields not found on page 1: ")
    assert "occupation_type" not in error
    assert "father_name" in error
    assert "dob" in error

    complete_without_occupation = parse_application_page1("""Borrower Details
Name of the Borrower: Asha Rao
Father's Name: N/A
Mother's Name: N/A
Nationality: Indian
Date of Birth: 02-Nov-2005
Gender: Female
Mobile Number: +91 98765 43210
Email: asha@example.test
PAN: AXXXX1234X
Other: -
Communication Address: 12 Long Road, Block A
City: Pune
Pincode: 411001
State: Maharashtra
Country: India
Current Address: Flat 4, Lake View
City: Mumbai
Pincode: 400001
State: Maharashtra
Country: India""")
    assert complete_without_occupation["application_occupation_type"] == ""
    assert complete_without_occupation["application_validation_status"] == "success"
    assert complete_without_occupation["application_validation_error"] == ""


def test_application_occupation_is_extracted_when_present_without_becoming_required():
    parsed = parse_application_page1("Name of the Borrower: Asha Rao\nGender: Female\nOccupation type: Salaried")
    assert parsed["application_occupation_type"] == "Salaried"
    assert "occupation_type" not in parsed["application_validation_error"]

def test_application_matching_is_limited_to_name_dob_gender_and_compact_evidence():
    base = parse_application_page1(
        "Name of the Borrower: Asha Rao Date of Birth: 02-Nov-2005 Gender: Female "
        "Father's Name: unrelated extra details"
    )
    matched = apply_application_matches(base, "Asha Rao", "2005-11-02", "F")
    assert matched["application_name_match_status"] == "Match"
    assert matched["application_dob_match_status"] == "Match"
    assert matched["application_gender_match"] == "Match"
    assert matched["application_borrower_details_match"] == "Match"
    assert matched["application_photo_match"] == "Not implemented"
    assert matched["application_father_name"] == "unrelated extra details"
    missing_match = apply_application_matches(base, "Asha Rao", "02-Nov-2006", "Female")
    assert missing_match["application_borrower_details_match"] == "Not a Match"


def test_application_logo_compact_status_and_page_one_only(monkeypatch):
    class Page:
        def __init__(self, text): self.text, self.calls = text, 0
        def extract_text(self): self.calls += 1; return self.text
        @property
        def images(self): return []
    pages = [Page("PayU Finance\nName of the Borrower: Asha Rao"), Page("Decoy branding")]
    class Reader:
        def __init__(self, _stream): self.pages = pages
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    branded = extract_application_from_pdf_bytes(b"synthetic")
    assert branded["application_letterhead_logo_check"] == "Available"
    assert "text evidence only" in branded["application_letterhead_logo_evidence"]
    assert pages[0].calls == 1 and pages[1].calls == 0
    pages[:] = [Page("No branding text"), Page("PayU Finance")]
    plain = extract_application_from_pdf_bytes(b"synthetic")
    assert plain["application_letterhead_logo_check"] == "Not available"
    assert plain["application_photo_match"] == "Not implemented"
    assert pages[1].calls == 0


def test_application_form_markup_includes_all_field_cards_and_compact_checks():
    from pathlib import Path
    html = (Path(__file__).parents[1] / "templates" / "index.html").read_text()
    for field in __import__("document_checks").APPLICATION_FIELDS:
        assert f"'{field}'" in html
    assert "Letterhead check (Logo)" in html
    assert "Photo match" in html and "Not implemented" in html
    # Every field value is placed through the escaping fieldCard helper, not raw HTML.
    assert "function fieldCard(label, value, key)" in html
    assert "escapeHtml(value || 'Not available')" in html
    assert "application_letterhead_logo_evidence" not in html


def test_application_fields_are_lazy_and_private_in_initial_html():
    synthetic_url = "https://private.fixture.test/" + ("application-object-" * 40) + ".pdf"
    private_value = "SYNTHETIC-PRIVATE-PAN-9999"
    app_module.STATE.clear()
    app_module.STATE.update({
        "df": pd.DataFrame([{
            "user_id": "privacy-test-user", "user_name": "Synthetic Reviewer", "application_form_link": synthetic_url,
            "application_borrower_name": "Synthetic Borrower", "application_pan": private_value,
            "application_validation_status": "success", "application_photo_match": "Not implemented",
        }]),
        "filename": "synthetic.xlsx", "path": None, "generation": 901,
    })
    with app.test_client() as client:
        response = client.get("/")
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "privacy-test-user" in html
        assert synthetic_url not in html and private_value not in html and "Synthetic Borrower" not in html
        record_response = client.get("/api/records/0")
        assert record_response.status_code == 200
        record = record_response.get_json()["record"]
        assert record["application_form_link"] == synthetic_url
        assert record["application_pan"] == private_value
        assert record["application_photo_match"] == "Not implemented"
        # Selected-record GET is the supported lazy-loading API; POST queues a
        # network fetch only when the URL is valid, so use no external call here.
        assert record["application_pan"] == private_value


def test_application_inline_city_labels_do_not_split_address_text():
    parsed = parse_application_page1(
        "Communication Address: 22 Near City Market Pune City: Pune Pincode: 411001 State: Maharashtra Country: India "
        "Current Address: 8 City Centre Mumbai City: Mumbai Pincode: 400001 State: Goa Country: India"
    )
    assert parsed["application_communication_address"] == "22 Near City Market Pune"
    assert parsed["application_communication_city"] == "Pune"
    assert parsed["application_current_address"] == "8 City Centre Mumbai"
    assert parsed["application_current_city"] == "Mumbai"
    assert parsed["application_communication_pincode"] == "411001"
    assert parsed["application_current_pincode"] == "400001"


def test_kfs_numbered_flattened_rows_stop_at_pan_and_part1_boundaries():
    from document_checks import _parse_kfs_page3_text

    # Fixture mirrors page-3 table text where the PDF text layer joins row
    # serial numbers and wraps a long address across separate text lines.
    fixture = """1 Borrower identification number 8666285688444545118
2 Name of the Borrower SAHDEV SINGH
3 Address of the Borrower S O RAJVEER SINGH GRAM MAHU
NANGALI NAKEEBPUR,BIJNOR UTTAR PRADESH 246733,Bijnor,Bijnor,UP,IN,246733
4 PAN ABCDE1234F
Part 1 General information that must not be included
"""
    parsed = _parse_kfs_page3_text(fixture)
    assert parsed == {
        "kfs_borrower_identification_number": "8666285688444545118",
        "kfs_borrower_name": "SAHDEV SINGH",
        "kfs_borrower_address": "S O RAJVEER SINGH GRAM MAHU NANGALI NAKEEBPUR,BIJNOR UTTAR PRADESH 246733,Bijnor,Bijnor,UP,IN,246733",
    }
    assert not any(token in " ".join(parsed.values()) for token in ("ABCDE1234F", "Part 1"))


def test_kfs_flattened_address_row_serial_is_not_kept():
    from document_checks import _parse_kfs_page3_text
    fixture = (
        "1 Borrower identification number 8666285688444545118 "
        "2 Name of the Borrower SAHDEV SINGH "
        "3 Address of the Borrower S O RAJVEER SINGH GRAM MAHU NANGALI NAKEEBPUR,"
        "BIJNOR UTTAR PRADESH 246733,Bijnor,Bijnor,UP,IN,246733 4 PAN ABCDE1234F Part 1"
    )
    parsed = _parse_kfs_page3_text(fixture)
    assert parsed["kfs_borrower_identification_number"] == "8666285688444545118"
    assert parsed["kfs_borrower_name"] == "SAHDEV SINGH"
    assert parsed["kfs_borrower_address"] == (
        "S O RAJVEER SINGH GRAM MAHU NANGALI NAKEEBPUR,"
        "BIJNOR UTTAR PRADESH 246733,Bijnor,Bijnor,UP,IN,246733"
    )
    assert "PAN" not in parsed["kfs_borrower_address"]
