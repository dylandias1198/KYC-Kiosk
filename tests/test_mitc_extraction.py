from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest

from mitc_extraction import MITC_COLUMNS, MITCDocumentError, extract_mitc_from_url, fetch_mitc_pdf, parse_mitc_text

SAMPLE_TEXT = """MITC
Date: 13/09/2026
Borrower Details:
Name: Subhankar Sen
Gender: M
Mobile Number and WhatsApp Number:
6296553432
Permanent Address:
NEW GOPALPUR, Pritinagar, Gopalpur, Ranaghat - I, Nadia, West Bengal, India, 741247
Correspondence Address:
Same as Permanent Address
KYC Details:
Name: Subhankar Sen
Gender: M
Address:
Same as Permanent Address
"""


def test_parse_mitc_standard_wrapped_text():
    result = parse_mitc_text(SAMPLE_TEXT)
    assert result["mitc_extraction_status"] == "success"
    assert result["mitc_form_date"] == "13/09/2026"
    assert result["mitc_borrower_name"] == "Subhankar Sen"
    assert result["mitc_borrower_gender"] == "M"
    assert result["mitc_borrower_mobile_number"] == "6296553432"
    assert result["mitc_borrower_whatsapp_number"] == "6296553432"
    assert result["mitc_borrower_permanent_address"].endswith("741247")
    assert result["mitc_borrower_correspondence_address"] == "Same as Permanent Address"
    assert result["mitc_kyc_name"] == "Subhankar Sen"
    assert result["mitc_kyc_gender"] == "M"
    assert result["mitc_kyc_address"] == "Same as Permanent Address"


def test_parser_does_not_cross_populate_repeated_name_labels():
    result = parse_mitc_text("""Date: 01/01/2026\nBorrower Details:\nName: Borrower\nKYC Details:\nName: KYC\nGender: F\nAddress: Address value""")
    assert result["mitc_borrower_name"] == "Borrower"
    assert result["mitc_kyc_name"] == "KYC"


def test_missing_link_is_truthful_and_has_all_columns():
    result = extract_mitc_from_url("")
    assert result["mitc_extraction_status"] == "failed"
    assert "blank" in result["mitc_extraction_error"]
    assert set(MITC_COLUMNS) == set(result)


def test_unreachable_is_truthful_without_network_call():
    with patch("mitc_extraction.urlopen", side_effect=TimeoutError("fixture timeout")):
        result = extract_mitc_from_url("https://example.invalid/mitc.pdf")
    assert result["mitc_extraction_status"] == "failed"
    assert "fetch" in result["mitc_extraction_error"].lower()


def test_malformed_pdf_does_not_crash_and_failed_result_can_retry():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b"not a pdf"

    with patch("mitc_extraction.urlopen", return_value=Response()) as mocked:
        first = extract_mitc_from_url("https://example.invalid/malformed.pdf")
        second = extract_mitc_from_url("https://example.invalid/malformed.pdf")
    assert first["mitc_extraction_status"] == "failed"
    assert second["mitc_extraction_status"] == "failed"
    assert mocked.call_count == 2


def test_pdf_extraction_uses_only_target_page(monkeypatch):
    """A 36-page fixture may be inspected only at page 23."""
    from mitc_extraction import MITC_TARGET_PAGE, extract_mitc_from_pdf_bytes

    class Page:
        def __init__(self, text):
            self.text = text
            self.calls = 0

        def extract_text(self):
            self.calls += 1
            return self.text

    pages = [Page("wrong page") for _ in range(36)]
    pages[MITC_TARGET_PAGE - 1] = Page(SAMPLE_TEXT)

    class Reader:
        def __init__(self, _stream):
            self.pages = pages

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_mitc_from_pdf_bytes(b"synthetic")
    assert result["mitc_borrower_name"] == "Subhankar Sen"
    assert pages[MITC_TARGET_PAGE - 1].calls == 1
    assert pages[0].calls == 1 and pages[1].calls == 1 and pages[2].calls == 1
    assert pages[-1].calls == 1
    assert sum(page.calls for page in pages) == 5  # page 23 plus early branding/last-page signature checks


def test_pdf_with_fewer_than_target_pages_is_truthful(monkeypatch):
    from mitc_extraction import extract_mitc_from_pdf_bytes

    class Reader:
        def __init__(self, _stream):
            self.pages = [object()] * 3

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_mitc_from_pdf_bytes(b"synthetic")
    assert result["mitc_extraction_status"] == "failed"
    assert "target page 23" in result["mitc_extraction_error"]

AJAY_PAGE_23_TEXT = """1. Borrower Information
2. Date 21/09/2026 3. Borrower Details (Name, Gender, Mobile Number, Address) Name: Ajay Kumar Mandal Gender: M Mobile Number and WhatsApp Number: 6205399022 Permanent Address: AT- KUSUMI, PO- KUSUMI, PS- KUSUMI, DIST- ..., India, 847236 Correspondence Address: Same as Permanent Address 4. KYC Details Name: Ajay Kumar Mandal Gender: M Address: AT- KUSUMI, PO- KUSUMI, PS- KUSUMI, DIST- ..., India, 847236 5. Lender 1 Name: Lender One Address: Lender address
"""


def test_parse_mitc_page_23_numbered_table_text_regression():
    result = parse_mitc_text(AJAY_PAGE_23_TEXT)
    assert result["mitc_extraction_status"] == "success"
    assert result["mitc_form_date"] == "21/09/2026"
    assert result["mitc_borrower_name"] == "Ajay Kumar Mandal"
    assert result["mitc_borrower_gender"] == "M"
    assert result["mitc_borrower_mobile_number"] == "6205399022"
    assert result["mitc_borrower_whatsapp_number"] == "6205399022"
    assert result["mitc_borrower_permanent_address"].endswith("847236")
    assert result["mitc_borrower_correspondence_address"] == "Same as Permanent Address"
    assert result["mitc_kyc_name"] == "Ajay Kumar Mandal"
    assert result["mitc_kyc_gender"] == "M"
    assert result["mitc_kyc_address"].endswith("847236")
    assert "Lender 1" not in result["mitc_kyc_address"]


def test_page_23_section_boundaries_support_separate_lines_and_inline_labels():
    text = """2. Date\n21/09/2026\n3. Borrower Details\n(Name, Gender, Mobile Number, Address)\nName:\nAjay Kumar Mandal\nGender:\nM\nMobile Number and WhatsApp Number:\n6205399022\nPermanent Address:\nWrapped house line\ncity and PIN 847236\nCorrespondence Address:\nSame as Permanent Address\n4. KYC Details\nName: Ajay Kumar Mandal\nGender: M\nAddress:\nKYC address line\nPIN 847236\n5. Lender\nName: Lender 1\nAddress: Lender address"""
    result = parse_mitc_text(text)
    assert result["mitc_form_date"] == "21/09/2026"
    assert result["mitc_borrower_name"] == "Ajay Kumar Mandal"
    assert result["mitc_borrower_permanent_address"].endswith("847236")
    assert result["mitc_kyc_address"].endswith("847236")
    assert "Lender" not in result["mitc_kyc_address"]


def test_kyc_address_stops_at_later_numbered_section_even_without_lender_keyword():
    text = "4. KYC Details Name: K Gender: F Address: KYC address 847236 5. Other Details Name: Other"
    result = parse_mitc_text(text)
    assert result["mitc_kyc_address"] == "KYC address 847236"


def _valid_pdf_bytes():
    from io import BytesIO
    from pypdf import PdfWriter

    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


def test_fetch_retries_read_timeout_then_succeeds(monkeypatch):
    attempts = []

    def fake_attempt(url, timeout):
        attempts.append((url, timeout))
        if len(attempts) == 1:
            raise TimeoutError("socket read timed out")
        return _valid_pdf_bytes()

    monkeypatch.setattr("mitc_extraction._fetch_mitc_attempt", fake_attempt)
    monkeypatch.setattr("mitc_extraction.MITC_FETCH_RETRIES", 2)
    monkeypatch.setattr("mitc_extraction.time.sleep", lambda _seconds: None)

    payload = fetch_mitc_pdf("https://fixture.test/retry.pdf", timeout=60)

    assert payload.startswith(b"%PDF-")
    assert len(attempts) == 2
    assert all(timeout == 60 for _, timeout in attempts)


def test_fetch_retries_timeout_until_final_truthful_failure(monkeypatch):
    attempts = []

    def always_timeout(url, timeout):
        attempts.append((url, timeout))
        raise URLError(TimeoutError("read operation timed out"))

    monkeypatch.setattr("mitc_extraction._fetch_mitc_attempt", always_timeout)
    monkeypatch.setattr("mitc_extraction.MITC_FETCH_RETRIES", 2)
    monkeypatch.setattr("mitc_extraction.time.sleep", lambda _seconds: None)

    with pytest.raises(MITCDocumentError, match=r"attempt 3/3.*timeout=60s.*retries=2"):
        fetch_mitc_pdf("https://fixture.test/timeout.pdf", timeout=60)
    assert len(attempts) == 3


def test_fetch_does_not_retry_http_404_or_invalid_url(monkeypatch):
    attempts = []

    def not_found(url, timeout):
        attempts.append(url)
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr("mitc_extraction._fetch_mitc_attempt", not_found)
    monkeypatch.setattr("mitc_extraction.MITC_FETCH_RETRIES", 2)
    with pytest.raises(MITCDocumentError, match="HTTP 404"):
        fetch_mitc_pdf("https://fixture.test/missing.pdf")
    assert attempts == ["https://fixture.test/missing.pdf"]

    monkeypatch.setattr("mitc_extraction._fetch_mitc_attempt", lambda *_args: (_ for _ in ()).throw(AssertionError("network retry")))
    with pytest.raises(MITCDocumentError, match=r"not a valid http\(s\) URL"):
        fetch_mitc_pdf("https://[invalid")


def test_failed_fetch_is_not_cached_and_selected_retry_can_succeed(monkeypatch):
    calls = []

    def fake_fetch(url, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise MITCDocumentError("Could not fetch MITC PDF: read timed out (attempt 3/3)")
        return _valid_pdf_bytes()

    monkeypatch.setattr("mitc_extraction.fetch_mitc_pdf", fake_fetch)
    monkeypatch.setattr("mitc_extraction.MITC_FETCH_RETRIES", 0)
    from mitc_extraction import clear_cache
    clear_cache()
    first = extract_mitc_from_url("https://fixture.test/retry-after-failure.pdf")
    second = extract_mitc_from_url("https://fixture.test/retry-after-failure.pdf")
    assert first["mitc_extraction_status"] == "failed"
    assert second["mitc_extraction_status"] == "failed"  # one-page fixture lacks target page 23
    assert len(calls) == 2

ALTERNATE_MITC_TEXT = """1. Date 01-08-2026
2. Customer identification number
8447399937
3. Name of the Borrower
Siddharth
Panwar
"""


def test_alternate_mitc_scoped_fields_and_wrapped_values():
    from mitc_extraction import _parse_alternate_mitc
    parsed = _parse_alternate_mitc(ALTERNATE_MITC_TEXT)
    assert parsed["mitc_form_date"] == "01-08-2026"
    assert parsed["mitc_customer_identification_number"] == "8447399937"
    assert parsed["mitc_name_of_borrower"] == "Siddharth Panwar"
    assert parsed["mitc_borrower_name"] == "Siddharth Panwar"


def test_mitc_name_compare_uses_kyc_name_and_explicit_missing_status():
    from matching import match_name
    assert match_name("Siddharth Panwar", "Siddharth Panwar")[0] == "Match"
    status, score = match_name("Siddharth Panwar", "")
    assert status == "KYC name not present" and score is None


def test_evidence_checks_are_truthful_for_text_and_embedded_images():
    from mitc_extraction import _evidence_checks

    class Page:
        def __init__(self, text="", images=()):
            self.text, self.images = text, images
        def extract_text(self): return self.text

    branded = _evidence_checks([Page("PayU Finance"), Page(), Page("Digitally signed by Example")], 3)
    assert branded["letterhead_logo_check"] == "Pass"
    assert "text evidence only" in branded["letterhead_logo_evidence"]
    assert branded["digital_signature_check"] == "Pass"
    assert "last page 3" in branded["digital_signature_evidence"]
    image_only = _evidence_checks([Page("Document", images=[object()]), Page(), Page("", images=[object()])], 3)
    assert image_only["letterhead_logo_check"] == "Pass"
    assert "not confirmed as a logo" in image_only["letterhead_logo_evidence"]
    assert image_only["digital_signature_check"] == "Review"
    assert "not classified as a signature" in image_only["digital_signature_evidence"]


def test_alternate_pdf_extraction_early_stops_and_checks_last_page(monkeypatch):
    from mitc_extraction import extract_mitc_from_pdf_bytes

    class Page:
        def __init__(self, text="", images=()): self.text, self.images = text, images
        def extract_text(self): return self.text
    pages = [Page(ALTERNATE_MITC_TEXT), Page(), Page("Digitally signed by Issuer")]
    class Reader:
        def __init__(self, _stream): self.pages = pages
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_mitc_from_pdf_bytes(b"fixture")
    assert result["mitc_extraction_status"] == "success"
    assert result["mitc_borrower_name"] == "Siddharth Panwar"
    assert result["mitc_digital_signature_check"] == "Pass"
    assert pages[0].text and pages[2].text
    assert pages[1].text == ""


def test_sanction_parser_extracts_fields_and_requires_both_terms_phrases():
    from mitc_extraction import _parse_sanction_text
    text = '''01/08/2026
Sanction Letter Reference No: SL-REF-42
Name: Mr./ Ms. Siddharth Panwar ("Borrower" or "you")
Annexure
Terms and conditions of the sanction'''
    result = _parse_sanction_text([text])
    assert result["sanction_letter_date"] == "01/08/2026"
    assert result["sanction_letter_reference_number"] == "SL-REF-42"
    assert result["sanction_borrower_name"] == "Siddharth Panwar"
    assert result["sanction_annexure_terms_check"] == "Pass"
    assert _parse_sanction_text([text.replace("Annexure", "Schedule")])["sanction_annexure_terms_check"] == "Fail"


def test_sanction_pdf_checks_signature_and_branded_early_page(monkeypatch):
    from mitc_extraction import extract_sanction_from_pdf_bytes

    class Page:
        def __init__(self, text="", images=()): self.text, self.images = text, images
        def extract_text(self): return self.text
    pages = [Page('01-08-2026\nReference No: ABC-7\nName: Ms. Sample Person ("Borrower" or "you")\nPayU Finance'),
             Page('Annexure\nTerms and conditions of the sanction'),
             Page('Digitally signed by Issuer')]
    class Reader:
        def __init__(self, _stream): self.pages = pages
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    assert result["sanction_letter_date"] == "01/08/2026"
    assert result["sanction_letter_reference_number"] == "ABC-7"
    assert result["sanction_borrower_name"] == "Sample Person"
    assert result["sanction_annexure_terms_check"] == "Pass"
    assert result["sanction_digital_signature_check"] == "Pass"
    assert result["sanction_letterhead_logo_check"] == "Pass"


def test_pdf_cache_fetches_one_url_once_for_viewer_and_extract(monkeypatch):
    from mitc_extraction import clear_cache, fetch_mitc_pdf
    clear_cache()
    payload = _valid_pdf_bytes()
    calls = []
    monkeypatch.setattr("mitc_extraction._download_mitc_pdf", lambda url, timeout=None: calls.append(url) or payload)
    assert fetch_mitc_pdf("https://fixture.test/shared.pdf") == payload
    assert fetch_mitc_pdf("https://fixture.test/shared.pdf") == payload
    assert calls == ["https://fixture.test/shared.pdf"]
    clear_cache()

def test_sanction_scans_later_pages_and_normalizes_label_layouts(monkeypatch):
    from mitc_extraction import extract_sanction_from_pdf_bytes

    class Page:
        def __init__(self, text="", images=()):
            self.text, self.images = text, images
        def extract_text(self):
            return self.text

    pages = [Page(f"Cover page {number}") for number in range(24)]
    pages.extend([
        Page("Sanction Letter Date\n1 August 2026\nReference No.\nSL/LATE-42"),
        Page("Borrower Name - Dr. Siddharth Panwar\nAnnexure A"),
        Page("Terms and conditions of the sanction"),
        Page("Digitally signed by Issuer"),
    ])

    class Reader:
        def __init__(self, _stream):
            self.pages = pages

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    monkeypatch.setattr("mitc_extraction.SANCTION_MAX_TEXT_PAGES", 40)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    # Later pages remain evidence-only; they cannot populate page-one identity.
    assert result["sanction_letter_date"] == ""
    assert result["sanction_letter_reference_number"] == ""
    assert result["sanction_borrower_name"] == ""
    assert result["sanction_annexure_terms_check"] == "Pass"
    assert result["sanction_validation_status"] == "partial"
    assert "Sanction letter date was not found" in result["sanction_validation_error"]
    assert "Reference number label was not found" in result["sanction_validation_error"]
    assert "Borrower name was not found" in result["sanction_validation_error"]
    assert len([page for page in pages if page.text.startswith("Cover page") and page.text]) == 24


def test_sanction_missing_fields_report_names_and_bounded_scan(monkeypatch):
    from mitc_extraction import extract_sanction_from_pdf_bytes

    class Page:
        def __init__(self, text=""):
            self.text, self.images = text, ()
        def extract_text(self):
            return self.text

    pages = [Page("Cover text") for _ in range(30)]

    class Reader:
        def __init__(self, _stream):
            self.pages = pages

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    monkeypatch.setattr("mitc_extraction.SANCTION_MAX_TEXT_PAGES", 25)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    assert result["sanction_validation_status"] == "partial"
    error = result["sanction_validation_error"]
    for missing in ("Sanction letter date", "Reference number", "Borrower name",
                    "Annexure evidence", "Terms and conditions of the sanction evidence"):
        assert missing in error
    assert "scanned pages 1-25 of 30" in error
    assert result["sanction_annexure_terms_check"] == "Review"



def test_sanction_full_name_and_blank_reference_truthful_state(monkeypatch):
    from mitc_extraction import _parse_sanction_text, extract_sanction_from_pdf_bytes

    text = '''01/08/2026
Sanction Letter Reference No:
Name: Mr./ Ms. SAHDEV SINGH ("Borrower" or "you")
Annexure
Terms and conditions of the sanction'''
    parsed = _parse_sanction_text([text])
    assert parsed["sanction_borrower_name"] == "SAHDEV SINGH"
    assert parsed["sanction_letter_reference_number"] == "Not provided in source"
    assert parsed["_sanction_reference_label_present"] is True

    class Page:
        images = ()
        def extract_text(self): return text

    class Reader:
        pages = [Page()]
        def __init__(self, _stream): pass

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    assert result["sanction_borrower_name"] == "SAHDEV SINGH"
    assert result["sanction_letter_reference_number"] == "Not provided in source"
    assert result["sanction_validation_status"] == "partial"
    assert "reference number label is present but blank" in result["sanction_validation_error"].lower()
    assert "not found" not in result["sanction_validation_error"].lower().split("reference", 1)[-1].split(";")[0]


def test_sanction_absent_reference_label_is_not_claimed_blank():
    from mitc_extraction import _parse_sanction_text
    parsed = _parse_sanction_text(['''01/08/2026
Name: Ms. SAHDEV SINGH ("Borrower" or "you")'''])
    assert parsed["sanction_letter_reference_number"] == ""
    assert parsed["_sanction_reference_label_present"] is False
    assert parsed["sanction_borrower_name"] == "SAHDEV SINGH"


def test_sanction_absent_reference_label_is_explicit_in_extraction_error(monkeypatch):
    from mitc_extraction import extract_sanction_from_pdf_bytes
    text = '''01/08/2026
Name: Ms. SAHDEV SINGH ("Borrower" or "you")
Annexure
Terms and conditions of the sanction'''
    class Page:
        images = ()
        def extract_text(self): return text
    class Reader:
        pages = [Page()]
        def __init__(self, _stream): pass
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    assert result["sanction_letter_reference_number"] == ""
    assert "Reference number label was not found" in result["sanction_validation_error"]
    assert "present but blank" not in result["sanction_validation_error"]


def test_sanction_supplied_page_one_fixture_extracts_exact_identity_fields(monkeypatch):
    from mitc_extraction import _parse_sanction_text, extract_sanction_from_pdf_bytes

    page_one = '''01/08/2026
Sanction Letter Reference No:
Name: Mr./ Ms. SAHDEV SINGH ("Borrower" or "you")
Dear SAHDEV SINGH,
Subject: Sanction Letter'''
    parsed = _parse_sanction_text([page_one])
    assert parsed["sanction_letter_date"] == "01/08/2026"
    assert parsed["sanction_borrower_name"] == "SAHDEV SINGH"
    assert parsed["sanction_letter_reference_number"] == "Not provided in source"

    class Page:
        images = ()
        def extract_text(self): return page_one

    class Reader:
        pages = [Page()]
        def __init__(self, _stream): pass

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    assert result["sanction_letter_date"] == "01/08/2026"
    assert result["sanction_borrower_name"] == "SAHDEV SINGH"
    assert result["sanction_letter_reference_number"] == "Not provided in source"
    assert "Reference number label is present but blank" in result["sanction_validation_error"]
    assert "Reference number label was not found" not in result["sanction_validation_error"]


def test_sanction_page_one_identity_never_uses_later_page_text(monkeypatch):
    from mitc_extraction import extract_sanction_from_pdf_bytes

    page_one = '''01/08/2026
Sanction Letter Reference No:
Name: Mr./ Ms. SAHDEV SINGH ("Borrower" or "you")
Dear SAHDEV SINGH,
Subject: Sanction Letter'''
    pages = [
        type("Page", (), {"images": (), "extract_text": lambda self: page_one})(),
        type("Page", (), {"images": (), "extract_text": lambda self: '''Date: 15/09/2026
Reference No: LATER-999
Borrower Name: DIFFERENT LATER PERSON
Annexure
Terms and conditions of the sanction'''})(),
        type("Page", (), {"images": (), "extract_text": lambda self: "Digitally signed by Issuer"})(),
    ]

    class Reader:
        def __init__(self, _stream): self.pages = pages

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = extract_sanction_from_pdf_bytes(b"fixture")
    assert result["sanction_letter_date"] == "01/08/2026"
    assert result["sanction_borrower_name"] == "SAHDEV SINGH"
    assert result["sanction_letter_reference_number"] == "Not provided in source"
    assert "LATER-999" not in result["sanction_validation_error"]
