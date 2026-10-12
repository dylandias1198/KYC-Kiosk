"""KYC Details source mapping, match-display, safe media, and lazy-page tests."""
from pathlib import Path
import json
import shutil
import subprocess

import pandas as pd

import app as app_module
from app import app


KYC_SOURCE_MAP = {
    "KYC Type": "kyc_status_type",
    "KYC Mode": "kyc_mode",
    "KYC Status": "kyc_status",
    "KYC Name": "kyc_name",
    "KYC Father Name": "kyc_father_name",
    "KYC DOB": "kyc_dob",
    "KYC Gender": "kyc_gender",
    "Permanent Address": "kyc_permanent_address",
    "Current Address": "kyc_current_address",
}


def _execute_kyc_details(html_document, record):
    """Execute the production KYC markup functions with a text-only safe DOM stub."""
    script = html_document.split("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function escapeHtml(value)")
    end = script.index("function fieldCard(label, value, key)")
    functions = script[start:end]
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node.js is required to exercise KYC Details rendering")
    runner = r"""
const record = JSON.parse(process.argv[1]);
const document = {addEventListener: () => {}, getElementById: () => null, createElement: () => {
  let value = '';
  return {set textContent(input) { value = String(input); }, get innerHTML() {
    return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }};
}};
const recordCache = new Map([[0, record]]);
const userIds = [String(record.user_id || 'u1')];
let selectedIndex = 0;
""" + functions + "\nprocess.stdout.write(renderKycDetails());\n"
    result = subprocess.run([node, "-e", runner, json.dumps(record)], check=True,
                            capture_output=True, text=True)
    return result.stdout


def _page_with_frame(frame):
    app_module.STATE.clear()
    app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 91})
    with app.test_client() as client:
        response = client.get("/")
    return response


def test_kyc_details_source_mappings_and_rendered_values():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    record = {source: f"value-{source}" for source in KYC_SOURCE_MAP.values()}
    record.update({
        "user_name": "User Name Value", "user_dob": "1990-02-03", "user_gender": "female",
        "user_perm_address": "User permanent address", "user_curr_address": "User current address",
        "user_selfie": "https://images.example.test/user.png", "kyc_selfie_link": "https://images.example.test/kyc.png",
        "name_match_status": "Match", "name_match_score": 88,
        "dob_match_status": "Not a Match", "dob_match_score": 0,
        "gender_match": "Match", "gender_match_score": 100,
        "user_gender_normalized": "FEMALE", "kyc_gender_normalized": "FEMALE",
        "permanent_address_match_status": "Match", "permanent_address_match_score": 95,
        "current_address_match_status": "Review", "current_address_match_score": 59,
    })
    markup = _execute_kyc_details(template, record)
    for label, source in KYC_SOURCE_MAP.items():
        assert f"kycSourceCard('{label}', r.{source}, '{source}'" in template or label == 'KYC DOB' and "kycSourceCard('KYC Date of Birth', r.kyc_dob, 'kyc_dob')" in template
        assert f"<dt>{'KYC Date of Birth' if label == 'KYC DOB' else label}</dt><dd>value-{source}</dd>" in markup
    assert "Name Match" in markup
    assert "Date of Birth Match" in markup
    assert "Gender Match" in markup
    assert "Permanent Address Match" in markup
    assert "Current Address Match" in markup
    for value in ("Match", "Not a Match", "Review", "88", "0", "100", "95", "59", "FEMALE"):
        assert value in markup
    assert "User Name Value" in markup and "User permanent address" in markup
    assert "User current address" in markup


def test_kyc_selfies_use_correct_sources_safely_and_report_blank_or_invalid_links():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    assert "kycSelfieCard('User Selfie', r.user_selfie, 'user_selfie'" in template
    assert "kycSelfieCard('KYC Selfie', r.kyc_selfie_link, 'kyc_selfie_link'" in template
    assert "safeHttpUrl(value)" in template
    assert 'referrerpolicy="no-referrer"' in template
    assert 'rel="noopener noreferrer"' in template
    assert 'alt="${escapeHtml(altText)}"' in template
    assert 'this.hidden=true; this.nextElementSibling.hidden=false' in template
    assert 'Image could not be loaded by the browser.' in template
    record = {source: "" for source in KYC_SOURCE_MAP.values()}
    record.update({
        "user_name": "", "user_dob": "", "user_gender": "", "user_perm_address": "", "user_curr_address": "",
        "user_selfie": "https://images.example.test/u.png?x=\" onerror=\"alert(1)",
        "kyc_selfie_link": "javascript:alert(1)", "kyc_father_name": None,
    })
    markup = _execute_kyc_details(template, record)
    assert 'data-kyc-source="user_selfie"' in markup
    assert 'data-kyc-source="kyc_selfie_link"' in markup
    assert markup.count('class="photo-zoom-button" data-photo-zoom') == 1
    assert 'aria-label="Zoom photo: User Selfie"' in markup
    assert 'alt="Selected user selfie"' in markup and 'alt="Selected KYC selfie"' not in markup  # Unsafe URL has no image element.
    assert 'No KYC selfie supplied' not in markup
    assert 'Supplied selfie link is not a valid HTTP(S) URL.' in markup
    assert 'No user selfie supplied' not in markup
    assert 'https://images.example.test/u.png?x=&quot; onerror=&quot;alert(1)' in markup
    assert 'onerror="alert(1)' not in markup
    assert '<dd>Not supplied</dd>' in markup

    blank_record = {source: None for source in KYC_SOURCE_MAP.values()}
    blank_record.update({"user_name": None, "user_dob": None, "user_gender": None,
                         "user_perm_address": None, "user_curr_address": None,
                         "user_selfie": "", "kyc_selfie_link": ""})
    blank_markup = _execute_kyc_details(template, blank_record)
    assert blank_markup.count("Not supplied") >= len(KYC_SOURCE_MAP) + 5
    assert "No user selfie supplied" in blank_markup
    assert "No KYC selfie supplied" in blank_markup
    assert '<img' not in blank_markup and 'Open image' not in blank_markup
    assert 'data-photo-zoom' not in blank_markup and 'Zoom photo' not in blank_markup


def test_kyc_details_is_lazy_and_selection_and_poll_refresh_it():
    very_long_selfie = "https://images.example.test/" + ("private-kyc-selfie-" * 100) + ".jpg"
    private_value = "Private KYC record value"
    frame = pd.DataFrame([{
        "user_id": "u1", "user_name": "User one", "kyc_name": private_value,
        "kyc_selfie_link": very_long_selfie, "kyc_status_type": "Digital", "kyc_status": "Verified",
        "name_match_status": "Match", "name_match_score": 80,
    }])
    response = _page_with_frame(frame)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert very_long_selfie not in html and private_value not in html
    assert "fetch(`/api/records/${index}`" in html
    assert "renderKycDetails()" in html and "fetchRecord(requestedIndex)" in html
    assert "active?.dataset.tab === 'kyc-details'" in html
    assert "['form', 'kyc-details'].includes(document.querySelector('.tab.active')?.dataset.tab)" in html
    with app.test_client() as client:
        selected = client.get("/api/records/0")
    assert selected.status_code == 200
    payload = selected.get_json()["record"]
    assert payload["kyc_name"] == private_value
    assert payload["kyc_selfie_link"] == very_long_selfie
    assert "kyc_status_type" in payload and "name_match_score" in payload


def test_kyc_redesign_uses_existing_scores_and_exact_match_status_count_only():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    record = {
        "user_id": "kyc-u", "user_name": "Borrower", "user_dob": "1990-01-02", "user_gender": "F",
        "user_perm_address": "Home", "user_curr_address": "Current", "kyc_status_type": "DIGITAL",
        "kyc_mode": "CKYC", "kyc_status": "Success", "kyc_name": "Borrower", "kyc_father_name": "Parent",
        "kyc_dob": "1990-01-02", "kyc_gender": "F", "kyc_permanent_address": "Home",
        "kyc_current_address": "Current", "name_match_status": "Match", "dob_match_status": "Match",
        "gender_match": "Match", "permanent_address_match_status": "Review", "current_address_match_status": "Match",
        "name_match_score": 78, "dob_match_score": 91, "gender_match_score": 66,
        "permanent_address_match_score": 42, "current_address_match_score": 100,
        "user_gender_normalized": "FEMALE", "kyc_gender_normalized": "FEMALE",
        "mitc_type": "FPL-only private value",
    }
    markup = _execute_kyc_details(template, record)
    assert 'OVERALL MATCH SCORE' in markup and '4 <small>/ 5 checks passed</small>' in markup
    assert 'ALL MATCHES' not in markup and 'REVIEW' in markup
    assert 'data-score-source="name_match_score"' in markup and '>78</b>' in markup
    assert 'data-score-source="permanent_address_match_score"' in markup and '>42</b>' in markup
    assert 'aria-label="Existing score 78"' in markup and 'width:78%' in markup
    assert 'Score:' not in markup and 'FPL-only private value' not in markup
    assert 'Normalized' not in markup
    for key in ('name_match_score', 'dob_match_score', 'gender_match_score',
                'permanent_address_match_score', 'current_address_match_score'):
        assert f'r.{key}' in template
    assert "String(value) === 'Match'" in template
    assert 'match_name(' not in template and '_score_name(' not in template and 'recalculate' not in template.lower()
    assert 'KYC SOURCE DATA' in markup and 'KYC IDENTITY' in markup and 'PERSONAL DETAILS' in markup
    assert 'kyc-selfie-grid' in markup and 'COMPARISON RESULTS' in markup
    assert 'overall-score-strip' in markup and 'score-cards' in markup and 'comparison-list' in markup
    assert 'comparison-status status-match' in markup and 'hero-avatar' in markup and 'hero-navigation' in markup
    assert 'KYC ADDRESSES' in markup and 'Name Match' in markup and 'Current Address Match' in markup
    assert 'User Value' in markup and 'KYC Value' in markup
    assert 'FEMALE' in markup and 'Date of Birth Match' in markup


def test_kyc_missing_statuses_have_missing_badges_and_never_count_as_pass():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    record = {source: None for source in KYC_SOURCE_MAP.values()}
    record.update({"user_id": "missing", "name_match_status": None, "dob_match_status": "Review",
                   "gender_match": "Match", "permanent_address_match_status": "Not a Match",
                   "current_address_match_status": "Both not present", "name_match_score": None,
                   "dob_match_score": 55, "gender_match_score": 100})
    markup = _execute_kyc_details(template, record)
    assert '1 <small>/ 5 checks passed</small>' in markup
    assert 'status-missing">Missing' in markup
    assert 'data-score-source="name_match_score"><span>Name</span><b>—</b>' in markup
    assert 'KYC Date of Birth' in markup and 'KYC Father Name' in markup


def test_initial_selected_html_excludes_full_user_and_kyc_media_urls_and_is_responsive():
    very_long_url = 'https://images.example.test/' + ('private-review-image-' * 90) + '.jpg'
    response = _page_with_frame(pd.DataFrame([{"user_id": "lazy-u", "user_name": "Lazy User",
                                               "user_selfie": very_long_url, "kyc_selfie_link": very_long_url,
                                               "kyc_name": "Private selected name"}]))
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert very_long_url not in html and 'Private selected name' not in html
    assert 'data-score-source=' in html and 'selected-user-hero' in html
    css = (app_module.BASE_DIR / 'static' / 'style.css').read_text()
    assert '@media(max-width:1100px)' in css and '@media(max-width:760px)' in css and '@media(max-width:520px)' in css


def test_photo_zoom_modal_controls_accessibility_delegation_and_viewport_css():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    css = (app_module.BASE_DIR / "static" / "style.css").read_text()
    for fragment in (
        'id="photoZoomModal"', 'role="dialog" aria-modal="true"',
        'data-photo-zoom-action="out" aria-label="Zoom out"',
        'data-photo-zoom-action="in" aria-label="Zoom in"',
        'data-photo-zoom-action="fit" aria-label="Reset photo to fit"',
        'data-photo-zoom-action="close" class="photo-zoom-close" aria-label="Close photo preview"',
        "document.addEventListener('click'",
        "document.addEventListener('wheel'", "safeHttpUrl(source?.currentSrc || source?.src || '')",
        "photoZoomModal.hidden = true", "photoZoomModal.setAttribute('aria-hidden', 'false')",
    ):
        assert fragment in template
    assert "'Escape'" in template
    assert '.photo-zoom-modal[hidden]{display:none}' in css
    assert 'max-height:calc(92vh - 86px)' in css
    assert '@media(max-width:600px)' in css
    assert 'referrerpolicy="no-referrer"' in template


def test_photo_zoom_open_zoom_wheel_and_escape_close_behavior():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function hasSuppliedValue(value)")
    end = script.index("function userDetailCard(label, value, key, address = false, matchStatus = '')")
    zoom_logic = script[start:end]
    runner = r"""
const handlers = {};
const image = {style:{}, hidden:false, src:'', currentSrc:'https://images.example.test/photo.png',
  removeAttribute(name){ if(name==='src') this.src=''; }, addEventListener(){}};
const error = {hidden:true};
const close = {focus(){ document.activeElement=this; }};
const controls = [{}, {}, {}, close];
const modal = {hidden:true, attrs:{}, closest(){return null;}, querySelector(selector){
  if(selector==='.photo-zoom-image') return image;
  if(selector==='.photo-zoom-error') return error;
  if(selector==='[data-photo-zoom-action="close"]') return close;
  return null;
}, querySelectorAll(){return controls;}, setAttribute(key,value){this.attrs[key]=value;}};
const source={currentSrc:'https://images.example.test/photo.png',src:'https://images.example.test/photo.png'};
const card={querySelector(){return source;}};
const opener={isConnected:true, focused:false, closest(selector){return selector==='[data-photo-zoom]'?this:(selector.includes('user-photo-card')?card:null);}, focus(){this.focused=true;}};
const actions={in:{dataset:{photoZoomAction:'in'}},out:{dataset:{photoZoomAction:'out'}},fit:{dataset:{photoZoomAction:'fit'}},close:{dataset:{photoZoomAction:'close'}}};
const document={activeElement:null, listeners:{}, getElementById(){return modal;}, addEventListener(name,fn){this.listeners[name]=fn;}};
""" + zoom_logic + r"""
const fireClick=(target)=>document.listeners.click({target});
fireClick(opener);
if(modal.hidden || image.src!==source.src || modal.attrs['aria-hidden']!=='false' || document.activeElement!==close) throw Error('modal open did not set image/focus/ARIA state');
fireClick({closest(selector){return selector==='[data-photo-zoom]'?null:actions.in;}});
if(image.style.transform!=='scale(1.25)') throw Error('zoom in did not scale image');
fireClick({closest(selector){return selector==='[data-photo-zoom]'?null:actions.out;}});
if(image.style.transform!=='scale(1)') throw Error('zoom out did not scale image down');
fireClick({closest(selector){return selector==='[data-photo-zoom]'?null:actions.in;}});
fireClick({closest(selector){return selector==='[data-photo-zoom]'?null:actions.fit;}});
if(image.style.transform!=='scale(1)') throw Error('fit did not reset scale');
let prevented=false;
document.listeners.wheel({target:{closest(){return true;}},deltaY:-1,preventDefault(){prevented=true;}});
if(!prevented || image.style.transform!=='scale(1.1)') throw Error('wheel zoom did not work while modal open');
let escapePrevented=false;
document.listeners.keydown({key:'Escape',preventDefault(){escapePrevented=true;}});
if(!escapePrevented || !modal.hidden || modal.attrs['aria-hidden']!=='true' || !opener.focused || image.src!=='') throw Error('Escape close did not restore focus/reset modal');
fireClick(opener);
fireClick({closest(selector){return selector==='[data-photo-zoom]'?null:actions.close;}});
if(!modal.hidden) throw Error('close button did not close modal');
fireClick(opener);
fireClick(modal);
if(!modal.hidden) throw Error('backdrop click did not close modal');
process.stdout.write('PHOTO ZOOM INTERACTION PASS');
"""
    result = subprocess.run([shutil.which("node") or "node", "-e", runner], check=True,
                            capture_output=True, text=True)
    assert result.stdout == "PHOTO ZOOM INTERACTION PASS"


def test_kyc_photo_controls_use_card_gap_markup():
    from pathlib import Path
    html = (Path(__file__).parents[1] / "templates" / "index.html").read_text()
    assert 'class="kyc-photo-card"' in html and 'class="photo-zoom-button"' in html and 'Open image' in html


def test_kyc_document_type_link_and_selected_viewer_render_from_exact_source_columns():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    record = {
        "user_id": "document-card-user", "user_name": "Borrower", "kyc_doc": "DIGILOCKER_EAADHAAR",
        "kyc_doc_link": "https://files.example.test/exact-kyc-document.pdf", "kyc_document_status": "Not requested",
        "kyc_selfie_link": "https://images.example.test/selfie.jpg", "kyc_selfie_doc": "unrelated-selfie-document",
        "other_metadata": "must-not-render", "name_match_status": "Match", "dob_match_status": "Match",
        "gender_match": "Match", "permanent_address_match_status": "Match", "current_address_match_status": "Match",
    }
    markup = _execute_kyc_details(template, record)
    assert '<dt>KYC Document Type</dt><dd data-kyc-source="kyc_doc">DIGILOCKER_EAADHAAR</dd>' in markup
    assert '<dt>KYC Document Link</dt><dd><a class="kyc-document-source-link" href="https://files.example.test/exact-kyc-document.pdf"' in markup
    assert '/api/records/0/kyc/document' in markup
    assert 'data-kyc-document-action="view"' in markup and 'Open in new tab' in markup
    assert 'starts at page 1' in markup
    assert 'source-type-badge' in markup and 'unrelated-selfie-document' in markup
    assert 'must-not-render' not in markup
    blank_record = dict(record, kyc_doc=None, kyc_doc_link="", kyc_document_status="Not available")
    blank_markup = _execute_kyc_details(template, blank_record)
    assert '<dd data-kyc-source="kyc_doc">Not supplied</dd>' in blank_markup
    assert '<dd><span class="kyc-document-missing">Not supplied</span></dd>' in blank_markup
    assert 'data-kyc-document-action="view"' not in blank_markup


def test_kyc_identity_and_addresses_markup_order_and_full_selected_values():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    permanent = ("Permanent address starts with Unit 7, North Tower\n" + "Long address segment, " * 80
                 + "Permanent address end marker 560001")
    current = ("Current address starts with Apartment 12, East Block\n" + "Current address segment, " * 75
               + "Current address end marker 400001")
    record = {source: f"source-{source}" for source in KYC_SOURCE_MAP.values()}
    record.update({
        "user_id": "address-review", "user_name": "Fixture Borrower",
        "kyc_permanent_address": permanent, "kyc_current_address": current,
        "name_match_status": "Match", "dob_match_status": "Match", "gender_match": "Match",
        "permanent_address_match_status": "Review", "current_address_match_status": "Match",
        "kyc_face_match_validated": True,
    })
    markup = _execute_kyc_details(template, record)

    identity = markup.index("KYC IDENTITY")
    personal = markup.index("PERSONAL DETAILS")
    comparison = markup.index("COMPARISON RESULTS")
    addresses = markup.index("KYC ADDRESSES")
    operations = markup.index("OPERATIONS REVIEW")
    assert identity < personal < comparison < addresses < operations
    assert 'aria-label="KYC addresses"' in markup
    assert 'class="kyc-address-grid"' in markup
    assert 'data-kyc-source="kyc_permanent_address"' in markup
    assert 'data-kyc-source="kyc_current_address"' in markup
    assert 'Source: kyc_permanent_address' in markup and 'Source: kyc_current_address' in markup
    assert 'Face match validated' in markup
    assert 'data-review-field="kyc_face_match_validated" checked' in markup
    for address in (permanent, current):
        escaped = address.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        assert escaped in markup
    assert "Permanent address end marker 560001" in markup
    assert "Current address end marker 400001" in markup
    assert "Unrelated PDF text" not in markup

    frame = pd.DataFrame([{
        "user_id": "address-review", "user_name": "Fixture Borrower",
        "kyc_permanent_address": permanent, "kyc_current_address": current,
        "kyc_doc_link": "https://documents.fixture.invalid/private-kyc.pdf",
        "kyc_selfie_link": "https://images.fixture.invalid/private-kyc.jpg",
    }])
    _page_with_frame(frame)
    with app.test_client() as client:
        page = client.get("/")
        selected = client.get("/api/records/0")
    initial_html = page.get_data(as_text=True)
    assert page.status_code == 200 and selected.status_code == 200
    assert "https://documents.fixture.invalid/private-kyc.pdf" not in initial_html
    assert "https://images.fixture.invalid/private-kyc.jpg" not in initial_html
    selected_record = selected.get_json()["record"]
    assert selected_record["kyc_permanent_address"] == permanent
    assert selected_record["kyc_current_address"] == current


def test_kyc_address_responsive_layout_and_bounded_scroll_markers():
    css = (app_module.BASE_DIR / "static" / "style.css").read_text()
    assert ".kyc-addresses-panel" in css
    assert ".kyc-address-grid{grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert ".kyc-address-grid .kyc-source-card.address-card dd{max-height:260px;overflow:auto" in css
    assert "overflow-wrap:anywhere;white-space:pre-wrap" in css
    assert "@media(max-width:850px){.kyc-addresses-panel" in css
    assert "@media(max-width:520px){.kyc-personal-grid" in css
    assert ".kyc-address-grid{grid-template-columns:1fr}" in css


def test_kyc_source_card_groups_are_stacked_at_all_viewports_without_changing_comparison():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    css = (app_module.BASE_DIR / "static" / "style.css").read_text()
    record = {source: f"source-{source}" for source in KYC_SOURCE_MAP.values()}
    record.update({"user_id": "stacked", "name_match_status": "Match", "name_match_score": 81})
    markup = _execute_kyc_details(template, record)

    expected_groups = {
        "kyc-identity-grid": ("KYC Type", "KYC Mode", "KYC Status"),
        "kyc-personal-grid": ("KYC Name", "KYC Father Name", "KYC Date of Birth", "KYC Gender"),
        "kyc-address-grid": ("Permanent Address", "Current Address"),
    }
    for class_name, labels in expected_groups.items():
        assert f'class="{class_name}"' in markup
        group_markup = markup.split(f'class="{class_name}"', 1)[1].split("</dl>", 1)[0]
        assert all(f"<dt>{label}</dt>" in group_markup for label in labels)
        assert group_markup.count('class="kyc-source-card') == len(labels)

    stacked_rule = (
        ".kyc-identity-grid,.kyc-personal-grid,.kyc-address-grid{"
        "grid-template-columns:minmax(0,1fr);gap:10px}"
    )
    assert stacked_rule in css
    assert css.rfind(stacked_rule) > css.rfind("@media(max-width:520px)")
    assert ".kyc-address-grid .kyc-source-card.address-card dd{max-height:260px;overflow:auto" in css
    assert "overflow-wrap:anywhere;white-space:pre-wrap" in css

    layout = markup.split('aria-label="Comparison results"', 1)[1]
    assert "COMPARISON RESULTS" in markup
    assert "Name Match" in layout and "81" in layout
    assert 'data-review-field="kyc_face_match_validated"' in markup
    assert "KYC Document Type" in markup and "kycDocumentViewer" in markup
    assert "OPERATIONS REVIEW" in markup
