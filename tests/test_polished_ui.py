"""Regression checks for the polished MITC PDF and KYC review layouts."""
from pathlib import Path

import pandas as pd

import app as app_module
from app import app


ROOT = Path(__file__).parents[1]

def set_review_frame(frame):
    app_module.STATE.clear()
    app_module.STATE.update({"df": frame, "filename": "synthetic-fixture.xlsx", "path": None, "generation": 987})


def test_form_markup_has_compact_extraction_controls_and_truthful_integrity_results():
    template = (ROOT / "templates" / "index.html").read_text()
    css = (ROOT / "static" / "style.css").read_text()
    viewer = (ROOT / "static" / "pdf-viewer.js").read_text()
    for marker in (
        "FORM EXTRACTION", "LP only", "id=\"formType\"", "id=\"mitcProfile\"",
        "Extract / Load", "mitc-extraction-stack", "Customer identification number",
        "Borrower name vs KYC", "Existing name score", "integrityCard('Page check'",
        "integrityCard('Signature page'", "integrityCard('Digital signature check'",
        "integrityCard('Letterhead check (Logo)'", "not cryptographically verified",
        "source-link-compact", "aria-label=\"Open source document\"",
    ):
        assert marker in template
    for marker in (
        "pdf-workspace", "role=\"toolbar\" aria-label=\"PDF controls\"",
        "aria-label=\"PDF page thumbnails\"", "id=\"pdfOpenNewTab\"",
        "data-pdf-action=\"previous\"", "data-pdf-action=\"next\"",
        "data-pdf-action=\"zoom-in\"", "pdfPageCheckStatus",
    ):
        assert marker in template
    for marker in (
        "pdfjs.getDocument", "pdf.min.mjs", "pdf.worker.min.mjs", "buildPdfPages",
        "IntersectionObserver", "sourcePdfPage", "page = isKfs ? 4 : 1",
        "pageCount < 4 ? 1 : page", "PDF parsed",
    ):
        assert marker in viewer
    assert 'aria-label="Open source PDF in a new tab"' in template
    assert "Structural presence only; not cryptographically verified." in template
    assert "Not cryptographically validated" not in template
    assert ".pdf-workspace-body{display:grid;grid-template-columns:88px" in css
    assert "@media(max-width:600px)" in css and "grid-template-rows:78px minmax(0,1fr)" in css
    assert not (ROOT / "static" / "vendor" / "pdfjs" / "pdf.min.mjs").stat().st_size == 0


def test_kyc_source_order_comparison_stack_badges_and_face_checkbox():
    template = (ROOT / "templates" / "index.html").read_text()
    css = (ROOT / "static" / "style.css").read_text()
    assert "kycSelfieCard('KYC Selfie', r.kyc_selfie_link, 'kyc_selfie_link'" in template
    assert "r.kyc_selfie_doc" in template and "source-type-badge" in template
    assert 'aria-label=\"KYC document\"' in template
    assert 'id=\"kycDocumentViewer\"' in template
    assert 'data-kyc-document-action=\"view\"' in template
    assert 'data-kyc-document-open' in template and 'KYC document viewer starts at page 1' in template
    start = template.index("${kycSelfieCard('User Selfie'")
    face = template.index('data-review-field=\"kyc_face_match_validated\"', start)
    document = template.index('${kycDocumentCard(r)}', start)
    assert start < face < document
    for text in (
        "KYC IDENTITY", "PERSONAL DETAILS", "KYC ADDRESSES", "COMPARISON RESULTS",
        "overall-score-strip", "score-cards", "comparison-list", "Name Match",
        "Date of Birth Match", "Gender Match", "Permanent Address Match", "Current Address Match",
        "/not present|invalid/i.test(String(status))" ,
    ):
        assert text in template
    for breakpoint in ('@media(max-width:1100px)', '@media(max-width:760px)', '@media(max-width:520px)'):
        assert breakpoint in css


def test_source_links_stay_out_of_initial_html_and_form_uses_only_selected_record():
    secret_url = 'https://documents.fixture.invalid/' + ('signed-private-token-' * 40) + '.pdf'
    private_value = 'Synthetic record detail must stay lazy'
    set_review_frame(pd.DataFrame([{
        'user_id': 'synthetic-user', 'user_name': 'Example Borrower',
        'mitc_form_link': secret_url, 'kyc_doc_link': secret_url, 'kyc_name': private_value,
    }]))
    with app.test_client() as client:
        response = client.get('/')
        record = client.get('/api/records/0')
    html = response.get_data(as_text=True)
    assert response.status_code == 200 and record.status_code == 200
    assert secret_url not in html and private_value not in html
    assert '/api/records/${index}' in html
    assert 'safeHttpUrl(sourceUrl)' in html
    assert record.get_json()['record']['kyc_name'] == private_value


def test_kyc_card_shows_only_available_state_for_supported_source_type():
    from test_kyc_details import _execute_kyc_details
    template = (ROOT / 'templates' / 'index.html').read_text()
    row = {
        'user_id': 'fake', 'kyc_doc': 'DIGILOCKER_EAADHAAR',
        'kyc_doc_link': 'https://documents.fixture.invalid/fake.pdf',
        'kyc_document_status': 'Not requested', 'kyc_selfie_doc': 'AADHAAR_XML_SELFIE',
        'kyc_selfie_link': 'https://images.fixture.invalid/fake.png',
    }
    rendered = _execute_kyc_details(template, row)
    assert 'kyc-document-status\" id=\"kycDocumentStatus\">Available' in rendered
    assert 'source-type-badge\">AADHAAR XML SELFIE' in rendered
    assert 'href="https://documents.fixture.invalid/fake.pdf"' in rendered and 'Open source link' in rendered
    unavailable = _execute_kyc_details(template, {'user_id': 'fake', 'kyc_doc': '', 'kyc_doc_link': ''})
    assert 'kyc-document-status\" id=\"kycDocumentStatus\">Not available' in unavailable
