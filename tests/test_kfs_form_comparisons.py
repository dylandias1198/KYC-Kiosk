"""Rendered KFS borrower comparisons and truthful evidence tones."""
from pathlib import Path
import shutil
import subprocess

import app as app_module


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates" / "index.html"


def render_kfs(row):
    script = TEMPLATE.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    helpers = (ROOT / "static" / "kfs-form.js").read_text()
    assert "renderKfsDetailContent(r)" in script
    renderer_start = script.index("function renderForm()")
    renderer_end = script.index("function updateFormFromRecord()", renderer_start)
    renderer = script[renderer_start:renderer_end]
    runner = r"""
const selectedIndex = 0;
const currentFormType = 'KFS';
const currentMitcProfile = 'LP';
const mitcProfileStates = {LP:'LP profile active'};
const recordCache = new Map([[0, ROW_DATA]]);
function selectedRecord(){ return recordCache.get(selectedIndex) || {}; }
function hasSuppliedValue(value){ return value !== null && value !== undefined && value !== ''; }
function escapeHtml(value){ return String(value ?? '').replace(/[&<>\"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[character])); }
function userDetailValue(value){ return hasSuppliedValue(value) ? String(value) : 'Not supplied'; }
function safeHttpUrl(value){ return /^https?:\/\//i.test(String(value || '')) ? String(value) : ''; }
function scoreProgress(score){ return hasSuppliedValue(score) ? `<div class="score-track"><span></span></div>` : `<div class="score-track unavailable"></div>`; }
function comparisonBadge(value){ return value === 'Match' ? {label:'Match',css:'match'} : value === 'Not a Match' ? {label:'Not a Match',css:'not-match'} : {label:hasSuppliedValue(value) ? String(value) : 'Missing',css:hasSuppliedValue(value) ? 'review' : 'missing'}; }
function fieldCard(label,value,key){ return `<div class="form-field" data-field-key="${key}"><dt>${label}</dt><dd>${value || 'Not available'}</dd></div>`; }
function reviewSection(){ return '<section class="form-review"></section>'; }
""" + helpers + "\n" + renderer + "\nprocess.stdout.write(renderForm());\n"
    runner = runner.replace("ROW_DATA", __import__("json").dumps(row))
    node = shutil.which("node")
    assert node, "Node.js required to exercise KFS Form rendering"
    result = subprocess.run([node, "-e", runner], check=True, text=True, capture_output=True)
    return result.stdout


def test_kfs_name_and_address_comparisons_render_source_kyc_status_and_scores():
    html = render_kfs({
        "kfs_link": "https://documents.example.test/kfs.pdf",
        "kfs_validation_status": "Pass",
        "kfs_borrower_name": "Asha Rao",
        "kyc_name": "Asha Rao KYC",
        "kfs_borrower_name_match_status": "Match",
        "kfs_borrower_name_match_score": 92,
        "kfs_borrower_address": "12 MG Road, Pune 411001",
        "kyc_permanent_address": "12 Mahatma Gandhi Road, Pune 411001",
        "kfs_borrower_address_match_status": "Not a Match",
        "kfs_borrower_address_match_score": 58,
    })
    assert 'data-comparison="kfs-name"' in html
    assert 'Borrower name vs KYC name' in html
    assert 'data-kfs-value-field="kfs_borrower_name">Asha Rao' in html
    assert 'data-kfs-value-field="kyc_name">Asha Rao KYC' in html
    assert 'data-kfs-status-field="kfs_borrower_name_match_status">Match' in html
    assert 'data-kfs-score-field="kfs_borrower_name_match_score">92' in html
    assert 'aria-label="Existing score 92"' in html
    assert 'data-comparison="kfs-address"' in html
    assert 'Borrower address vs KYC permanent address' in html
    assert 'data-kfs-value-field="kfs_borrower_address">12 MG Road, Pune 411001' in html
    assert 'data-kfs-value-field="kyc_permanent_address">12 Mahatma Gandhi Road, Pune 411001' in html
    assert 'data-kfs-status-field="kfs_borrower_address_match_status">Not a Match' in html
    assert 'data-kfs-score-field="kfs_borrower_address_match_score">58' in html
    assert 'aria-label="Existing score 58"' in html


def test_kfs_evidence_classes_signature_note_and_source_status_are_truthful():
    positive = render_kfs({
        "kfs_validation_status": "Pass",
        "kfs_letterhead_logo_check": "Detected",
        "kfs_digital_signature_check": "Pass",
    })
    assert 'class="status-badge success">Pass' in positive
    assert 'data-kfs-evidence="kfs_letterhead_logo_check"' in positive
    assert 'kfs-evidence-positive' in positive
    assert '.form-field.kfs-evidence-positive{border-color:#b9dfbc;background:#eaf7eb}' in (ROOT / "static" / "style.css").read_text()
    assert '<dd>Detected</dd>' in positive
    assert 'data-kfs-evidence="kfs_digital_signature_check"' in positive
    assert 'Structural presence only; not cryptographically verified.' in positive
    assert '<dd>Pass</dd>' in positive

    negative = render_kfs({
        "kfs_validation_status": "Failed",
        "kfs_letterhead_logo_check": "Not Detected",
        "kfs_digital_signature_check": "Not available",
    })
    assert 'class="status-badge failed">Failed' in negative
    assert 'kfs-evidence-negative' in negative
    assert '.form-field.kfs-evidence-negative{border-color:#efc1be;background:#fff0ee}' in (ROOT / "static" / "style.css").read_text()
    assert '<dd>Not Detected</dd>' in negative
    assert '<dd>Not available</dd>' in negative
    assert 'Structural presence only; not cryptographically verified.' in negative
    assert 'cryptographically valid' not in positive.lower()
    assert 'cryptographically valid' not in negative.lower()


def test_kfs_missing_source_data_is_not_fabricated_and_scores_stay_unavailable():
    html = render_kfs({"kfs_validation_status": "Not available"})
    assert 'data-kfs-value-field="kfs_borrower_name">Not supplied' in html
    assert 'data-kfs-value-field="kyc_name">Not supplied' in html
    assert 'data-kfs-score-field="kfs_borrower_name_match_score">Not available' in html
    assert 'aria-label="Score not available"' in html
    assert 'data-kfs-value-field="kfs_borrower_address">Not supplied' in html
    assert 'data-kfs-value-field="kyc_permanent_address">Not supplied' in html
    assert 'class="status-badge failed">Not available' in html
    assert 'kfs-evidence-negative' in html


def test_kfs_existing_extract_and_viewer_rules_are_unchanged():
    template = TEMPLATE.read_text()
    viewer = (ROOT / "static" / "pdf-viewer.js").read_text()
    checks = (ROOT / "document_checks.py").read_text()
    assert "LP KFS borrower details use page 3 only" in template
    assert "Penal Charges check uses page 4 only" in template
    assert "const page = isKfs ? 4 : 1;" in viewer
    assert "reader.pages[2]" in checks and "reader.pages[3]" in checks
    assert "id=\"documentViewer\"" in template
    assert "reviewSection('form', 'form_validated', 'form_comments')" in template
    assert "renderKfsDetailContent(r)" in template
    assert "function renderKfsDetailContent(r)" in (ROOT / "static" / "kfs-form.js").read_text()
    assert app_module.APP_VERSION == "v1.4.17-core-flow-click-fixed"


def test_kfs_form_asset_is_served_by_the_local_app():
    with app_module.app.test_client() as client:
        page = client.get("/")
        asset = client.get("/static/kfs-form.js")
    assert page.status_code == 200 and "kfs-form.js" in page.get_data(as_text=True)
    assert asset.status_code == 200
    assert "renderKfsDetailContent" in asset.get_data(as_text=True)
