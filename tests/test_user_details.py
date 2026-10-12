"""Expanded source-schema and lazy User Details review tests."""
from io import BytesIO
import shutil
import subprocess

import pandas as pd

import app as app_module
from app import LEGACY_REQUIRED_COLUMNS, REQUIRED_COLUMNS, app, process_workbook


USER_DETAIL_MAP = {
    "Name": "user_name",
    "Mobile": "primary_mobile",
    "Date of Birth": "user_dob",
    "Permanent Address": "user_perm_address",
    "Current Address": "user_curr_address",
    "MITC Type": "mitc_type",
    "MITC Date": "mitc_date_latest",
    "APV Status": "verification_status",
    "NSDL Pull Date": "nsdl_date",
}
EXPANDED_COLUMNS = {
    "user_gender", "user_selfie", "kyc_selfie_link", "kyc_doc", "kyc_doc_link",
    "other_metadata", "verification_status", "validation_type", "nsdl_date",
}


def _upload_frame(frame):
    buffer = BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def test_expanded_schema_processes_preserves_source_column_order_and_extras(tmp_path):
    raw = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:1].copy()
    for column in EXPANDED_COLUMNS:
        if column not in raw:
            raw[column] = ""
    raw["user_selfie"] = "https://images.example.test/profile.png"
    raw["verification_status"] = "Verified"
    raw["nsdl_date"] = "2026-09-29"
    raw["other_metadata"] = '{"review":"ok"}'
    raw["user_gender"] = "Female"
    raw["custom_source_field"] = "keep-me"
    source_columns = list(raw.columns)

    enriched, output = process_workbook(_upload_frame(raw), "expanded-schema.xlsx", persist=True)

    assert set(REQUIRED_COLUMNS).issubset(enriched.columns)
    assert all(column in REQUIRED_COLUMNS for column in EXPANDED_COLUMNS)
    assert list(enriched.columns[:len(source_columns)]) == source_columns
    assert enriched.loc[0, "user_selfie"] == "https://images.example.test/profile.png"
    assert enriched.loc[0, "verification_status"] == "Verified"
    assert enriched.loc[0, "custom_source_field"] == "keep-me"
    assert output.name == f"kyc_matching_output_{app_module.APP_VERSION}.xlsx"
    with app.test_client() as client:
        response = client.post('/upload', data={'file': (_upload_frame(raw), 'expanded-schema.xlsx')},
                               content_type='multipart/form-data', follow_redirects=True)
        assert response.status_code == 200
        selected = client.get('/api/records/0')
        assert selected.status_code == 200
        assert selected.get_json()['record']['user_selfie'] == "https://images.example.test/profile.png"
        assert selected.get_json()['record']['verification_status'] == "Verified"
    book = pd.ExcelFile(output)
    try:
        restored = pd.read_excel(book, sheet_name="Enriched Data", dtype=object)
        summary = pd.read_excel(book, sheet_name="Summary", dtype=object)
    finally:
        book.close()
    assert len(restored) == 1 and len(summary) > 0
    assert list(restored.columns[:len(source_columns)]) == source_columns
    assert restored.loc[0, "custom_source_field"] == "keep-me"


def test_incomplete_expanded_schema_is_rejected_truthfully():
    frame = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:1].copy()
    frame = frame.drop(columns=["other_metadata"])
    try:
        process_workbook(_upload_frame(frame), "partial-expanded.xlsx", persist=False)
    except ValueError as error:
        assert "Incomplete expanded schema" in str(error)
        assert "other_metadata" in str(error)
    else:
        raise AssertionError("partial expanded schema accepted without the missing source field")


def test_legacy_schema_remains_accepted_with_expanded_columns_absent():
    frame = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:1].copy()
    frame = frame[[column for column in frame.columns if column in LEGACY_REQUIRED_COLUMNS]]
    enriched, _ = process_workbook(_upload_frame(frame), "legacy-schema.xlsx", persist=False)
    assert set(LEGACY_REQUIRED_COLUMNS).issubset(enriched.columns)
    assert "user_selfie" not in frame.columns
    assert "user_selfie" not in enriched.columns


def test_selected_user_record_contains_exact_source_mapping_and_keeps_kyc_separate():
    expected_values = {
        "user_name": "Source Name",
        "primary_mobile": "+91 90000 12345",
        "user_dob": "1990-02-03",
        "user_perm_address": "Source permanent address",
        "user_curr_address": "Source current address",
        "mitc_type": "Source MITC type",
        "mitc_date_latest": "2026-01-02",
        "verification_status": "APV source status",
        "nsdl_date": "2026-03-04",
        "user_selfie": "https://images.example.test/photo.png",
        "kyc_name": "Different KYC Name",
        "kyc_permanent_address": "KYC address must not replace source",
    }
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"user_id": "selected", **expected_values}]),
                             "filename": "fixture.xlsx", "path": None, "generation": 5})
    with app.test_client() as client:
        response = client.get("/api/records/0")
    assert response.status_code == 200
    record = response.get_json()["record"]
    assert {source: record[source] for source in USER_DETAIL_MAP.values()} == {
        source: expected_values[source] for source in USER_DETAIL_MAP.values()
    }
    assert record["kyc_name"] == "Different KYC Name"
    assert record["kyc_permanent_address"] == "KYC address must not replace source"


def _execute_user_detail_markup(html_document, record):
    """Run the actual rendering functions with a tiny safe DOM text-node stub."""
    script = html_document.split("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function escapeHtml(value)")
    end = script.index("function fieldCard(label, value, key)")
    functions = script[start:end]
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node.js is required to exercise selected User Details markup")
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
""" + functions + "\nprocess.stdout.write(renderUserDetails());\n"
    result = subprocess.run([node, "-e", runner, __import__("json").dumps(record)], check=True,
                            capture_output=True, text=True)
    return result.stdout


def test_user_details_markup_maps_labels_values_handles_missing_and_escapes_photo_url():
    source_template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    app_module.STATE.clear()
    app_module.STATE.update({"df": pd.DataFrame([{"user_id": "u1", "user_name": "safe"}]),
                             "filename": "fixture.xlsx", "path": None, "generation": 1})
    with app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    assert "fetchRecord(requestedIndex)" in source_template
    assert "fetch(`/api/records/${index}`" in source_template
    assert "const requestedIndex = selectedIndex" in source_template
    for label, source in USER_DETAIL_MAP.items():
        assert f"userDetailCard('{label}', r.{source}, '{source}'" in source_template or (label in {'Permanent Address', 'Current Address'} and f"userDetailCard('{label}', r.{source}, '{source}', true" in source_template)
    assert "No user selfie supplied" in source_template
    assert "alt=\"Selected user\'s photo\"" in source_template
    assert "Open image" in source_template
    assert "this.hidden=true; this.nextElementSibling.hidden=false" in source_template
    assert "['http:', 'https:'].includes(parsed.protocol)" in source_template
    assert 'referrerpolicy=\"no-referrer\"' in source_template

    record = {source: f"value-for-{source}" for source in USER_DETAIL_MAP.values()}
    record.update({"user_selfie": 'https://images.example.test/a.png?x=" onerror="alert(1)',
                   "kyc_name": "KYC-only-name", "kyc_permanent_address": "KYC-only-address"})
    markup = _execute_user_detail_markup(page, record)
    for label, source in USER_DETAIL_MAP.items():
        assert f'data-user-field="{source}"' in markup
        assert f'<dt>{label}</dt>' in markup
        assert f'<dd>value-for-{source}</dd>' in markup
    assert 'class="user-photo"' in markup and 'alt="Selected user' in markup and 'photo"' in markup and 'Open image' in markup
    assert 'data-photo-zoom' in markup and 'aria-label="Zoom photo: selected user\'s photo"' in markup
    assert 'Zoom photo' in markup
    assert 'https://images.example.test/a.png?x=&quot; onerror=&quot;alert(1)' in markup
    assert 'onerror="alert(1)' not in markup
    assert "KYC-only-name" not in markup and "KYC-only-address" not in markup
    unsafe_text_record = dict(record, user_name='<script>bad()</script>')
    escaped_text = _execute_user_detail_markup(page, unsafe_text_record)
    assert '<dd>&lt;script&gt;bad()&lt;/script&gt;</dd>' in escaped_text
    assert '<dd><script>bad()</script></dd>' not in escaped_text

    missing = {source: None for source in USER_DETAIL_MAP.values()}
    missing["user_selfie"] = ""
    blank_markup = _execute_user_detail_markup(page, missing)
    assert blank_markup.count("Not supplied") == len(USER_DETAIL_MAP) + 1
    null_tokens = dict(missing, user_name="null", primary_mobile="NaN", user_dob="None")
    null_markup = _execute_user_detail_markup(page, null_tokens)
    assert null_markup.count("Not supplied") == len(USER_DETAIL_MAP) + 1
    assert "No user selfie supplied" in blank_markup
    assert "<img" not in blank_markup and "Open image" not in blank_markup
    assert 'data-photo-zoom' not in blank_markup and 'Zoom photo' not in blank_markup
    invalid = dict(missing, user_selfie="javascript:alert(1)")
    invalid_markup = _execute_user_detail_markup(page, invalid)
    assert "Supplied selfie link is not a valid HTTP(S) URL." in invalid_markup
    assert "javascript:" not in invalid_markup
    assert 'data-photo-zoom' not in invalid_markup and 'Zoom photo' not in invalid_markup


def test_initial_html_stays_lightweight_and_user_selfie_not_embedded():
    long_url = "https://images.example.test/" + "private-photo-name-" * 100 + ".png"
    frame = pd.DataFrame([{"user_id": "u1", "user_name": "Selected", "user_selfie": long_url,
                           "kyc_doc_link": "https://documents.example.test/private.pdf",
                           "verification_status": "Sensitive details"}])
    app_module.STATE.clear()
    app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 2})
    with app.test_client() as client:
        response = client.get("/")
    html_page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert long_url not in html_page
    assert "Sensitive details" not in html_page
    assert "private.pdf" not in html_page
    assert 'id="photoZoomModal"' in html_page and long_url not in html_page
    assert "fetch(`/api/records/${index}`" in html_page
    assert len(response.data) < 80000  # review controls add bounded inline markup while source rows remain lazy


def test_user_details_redesign_uses_only_requested_profile_fields_and_truthful_address_badges():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    record = {source: f"value-for-{source}" for source in USER_DETAIL_MAP.values()}
    record.update({"user_id": "u-1", "permanent_address_match_status": "Match",
                   "current_address_match_status": "Review", "kyc_status": "Verified"})
    markup = _execute_user_detail_markup(template, record)
    for section in ("selected-user-hero", "PERSONAL INFORMATION", "ADDRESS INFORMATION", "APPLICATION METADATA"):
        assert section in markup
    for marker in ("hero-avatar", "identity-chips", "hero-navigation", "Use ",
                   "User 1 of 1", "User Photo", "source-label", "user-personal-cards",
                   "address-information-grid", "metadata-grid"):
        assert marker in markup
    assert "data-score-source=" not in markup
    assert "KYC SOURCE DATA" not in markup
    for source in ("user_name", "primary_mobile", "user_dob", "user_perm_address", "user_curr_address",
                   "mitc_type", "mitc_date_latest", "nsdl_date", "verification_status"):
        assert f'data-user-field="{source}"' in markup
    assert 'data-user-field="user_perm_address"' in markup and 'address-match-badge">Match' in markup
    current = markup.split('data-user-field="user_curr_address"', 1)[1].split('</div>', 1)[0]
    assert 'address-match-badge' not in current
    assert 'Not supplied' not in markup
    assert "data-user-field=\"user_gender\"" not in markup
    assert 'Status: ACTIVE' not in markup and 'KYC VERIFIED' not in markup
    assert 'User 1 of 1' in markup


def test_photo_controls_have_spaced_wrapper_class():
    from pathlib import Path
    css = (Path(__file__).parents[1] / "static" / "style.css").read_text()
    assert ".user-photo-card,.kyc-photo-card{display:flex;flex-direction:column;gap:8px}" in css


def test_reference_layout_css_has_compact_header_and_left_user_list_structure():
    css = (app_module.BASE_DIR / "static" / "style.css").read_text()
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    assert '.sidebar{width:286px' in css and '.user-list{height:min(60vh,520px)' in css
    assert '.selected-user-hero{min-height:0;padding:16px 21px' in css
    assert '.hero-avatar{flex-basis:54px;width:54px;height:54px' in css
    assert '.personal-information-grid{grid-template-columns:minmax(0,1fr) 205px' in css
    assert '.kyc-details-layout{grid-template-columns:minmax(0,.95fr) minmax(0,1.05fr)' in css
    assert '.score-cards{gap:6px}' in css and '.comparison-list{gap:7px}' in css
    assert 'id="userList" class="user-list"' in template
    assert 'aria-label="KYC source data"' in template and 'aria-label="Comparison results"' in template
    assert 'PERSONAL INFORMATION' in template and 'ADDRESS INFORMATION' in template and 'APPLICATION METADATA' in template
