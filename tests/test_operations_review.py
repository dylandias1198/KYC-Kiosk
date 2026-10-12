"""Operations review controls, API validation, and workbook persistence."""
from io import BytesIO

import pandas as pd
import pytest

import app as app_module
from app import REVIEW_COLUMNS, REVIEW_COMMENT_MAX_LENGTH, app, process_workbook


def upload_frame(frame):
    buffer = BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def base_frame():
    source = pd.read_excel(app_module.BASE_DIR / "sample_input.xlsx", dtype=object).iloc[:2].copy()
    return source


def test_new_review_columns_default_false_blank_and_survive_workbook_reload(tmp_path):
    frame = base_frame()
    frame["custom_source_field"] = ["source-a", "source-b"]
    frame.loc[0, "user_details_validated"] = True
    frame.loc[0, "user_details_comments"] = "Reviewed and approved"
    enriched, _ = process_workbook(upload_frame(frame), "reviewed.xlsx", persist=False)
    assert set(REVIEW_COLUMNS).issubset(enriched.columns)
    assert bool(enriched.loc[0, "user_details_validated"]) is True
    assert enriched.loc[0, "user_details_comments"] == "Reviewed and approved"
    assert bool(enriched.loc[1, "user_details_validated"]) is False
    assert enriched.loc[1, "user_details_comments"] == ""
    output = tmp_path / "reviewed.xlsx"
    app_module._write_workbook(enriched, output)
    restored = pd.read_excel(output, sheet_name="Enriched Data", dtype=object)
    assert len(restored) == 2
    assert set(REVIEW_COLUMNS).issubset(restored.columns)
    assert restored.loc[0, "user_details_validated"] in (True, 1)
    assert restored.loc[0, "user_details_comments"] == "Reviewed and approved"
    assert restored.loc[1, "user_details_validated"] in (False, 0)
    assert pd.isna(restored.loc[1, "user_details_comments"])
    assert restored.loc[0, "custom_source_field"] == "source-a"
    assert restored.loc[1, "custom_source_field"] == "source-b"


def test_review_endpoint_saves_only_selected_row_and_validates_values(tmp_path):
    frame = base_frame()
    enriched, _ = process_workbook(upload_frame(frame), "review-api.xlsx", persist=False)
    output = tmp_path / "review-api.xlsx"
    app_module.STATE.clear()
    app_module.STATE.update({"df": enriched, "filename": "review-api.xlsx", "path": output, "generation": 99})
    with app.test_client() as client:
        response = client.patch("/api/records/1/review", json={
            "user_details_validated": True,
            "user_details_comments": "Reviewed\nwith note",
            "kyc_details_validated": True,
            "kyc_details_comments": "KYC looks consistent",
            "kyc_face_match_validated": False,
            "form_validated": True,
            "form_comments": "Document checked",
        })
        assert response.status_code == 200
        state = response.get_json()["review"]
        assert state["user_details_validated"] is True
        assert state["kyc_details_validated"] is True
        assert state["kyc_face_match_validated"] is False
        assert state["form_validated"] is True
        assert state["form_comments"] == "Document checked"
        app_module._flush_pending_workbook_write()
        restored = pd.read_excel(output, sheet_name="Enriched Data", dtype=object)
        assert pd.isna(restored.loc[0, "form_comments"])
        assert restored.loc[1, "user_details_comments"] == "Reviewed\nwith note"
        assert restored.loc[1, "kyc_details_comments"] == "KYC looks consistent"
        assert restored.loc[1, "form_comments"] == "Document checked"
        assert restored.loc[1, "form_validated"] in (True, 1)
        assert restored.loc[0, "user_id"] == enriched.loc[0, "user_id"]
        assert restored.loc[1, "user_id"] == enriched.loc[1, "user_id"]
        assert client.patch("/api/records/2/review", json={"form_validated": True}).status_code == 404
        assert client.patch("/api/records/0/review", json={"form_validated": "true"}).status_code == 400
        assert client.patch("/api/records/0/review", json={"form_comments": 10}).status_code == 400
        assert client.patch("/api/records/0/review", json={"form_comments": "x" * (REVIEW_COMMENT_MAX_LENGTH + 1)}).status_code == 400
        assert client.patch("/api/records/0/review", json={"unknown": True}).status_code == 400
        assert client.patch("/api/records/0/review", json=[]).status_code == 400


def test_initial_html_is_lazy_and_review_markup_has_expected_screens_and_face_position():
    app_module.STATE.clear()
    app_module.STATE.update({"df": None, "filename": None, "path": None, "generation": 100})
    with app.test_client() as client:
        initial = client.get("/").get_data(as_text=True)
        # Initial HTML contains the render code but not selected-record controls/data.
        assert 'id="user-details-operations-review"' not in initial
        assert 'id="kyc-details-operations-review"' not in initial
        assert 'id="form-operations-review"' not in initial
        assert "https://images.example.test" not in initial
        assert "https://documents.example.test" not in initial
        assert "const records =" not in initial
        # Runtime markup is produced only for the selected record by these three screen renderers.
        assert "reviewSection('user-details', 'user_details_validated', 'user_details_comments')" in initial
        assert "reviewSection('kyc-details', 'kyc_details_validated', 'kyc_details_comments')" in initial
        assert "reviewSection('form', 'form_validated', 'form_comments')" in initial
        assert 'data-tab="summary"' in initial
        assert "reviewSection('summary'" not in initial
        source = initial.index("${kycSelfieCard('User Selfie'")
        checkbox = initial.index('data-review-field="kyc_face_match_validated"', source)
        document_viewer = initial.index('${kycDocumentCard(r)}', source)
        assert source < checkbox < document_viewer
        assert "Face match validated" in initial
        assert 'maxlength="10000"' in initial


def test_kyc_render_positions_face_checkbox_after_both_photo_cards():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function renderKycDetails()")
    end = script.index("function formatDisplayDate", start)
    renderer = script[start:end]
    runner = r"""
const recordCache = new Map([[0, {user_name:'Selected user',user_id:'u1',
  user_selfie:'https://example.test/user.png',kyc_selfie_link:'https://example.test/kyc.png',
  kyc_face_match_validated:true,kyc_details_validated:false,kyc_details_comments:'note'}]]);
let selectedIndex = 0;
const userIds = ['u1'];
function selectedRecord(){ return recordCache.get(selectedIndex); }
function hasSuppliedValue(value){ return value !== null && value !== undefined && value !== ''; }
function escapeHtml(value){ return String(value ?? ''); }
function selectedUserHero(){ return '<header>Selected user</header>'; }
function sectionHeading(title){ return `<h3>${title}</h3>`; }
function kycSourceCard(label){ return `<div>${label}</div>`; }
function kycSelfieCard(label){ return `<section class="photo-card">${label}</section>`; }
function kycDocumentCard(){ return '<section id="kyc-doc">KYC document</section>'; }
function scoreProgress(){ return '<i></i>'; }
function comparisonCard(label){ return `<article>${label}</article>`; }
function reviewSection(scope, validated, comments){ return `<section id="${scope}-operations-review"><input data-review-field="${validated}"><textarea data-review-field="${comments}"></textarea></section>`; }
""" + renderer + "\nprocess.stdout.write(renderKycDetails());\n"
    import json, shutil, subprocess
    node = shutil.which("node")
    assert node, "Node.js required to exercise KYC Details rendering"
    result = subprocess.run([node, "-e", runner], check=True, text=True, capture_output=True)
    rendered = result.stdout
    user_photo = rendered.index('class="photo-card">User Selfie')
    kyc_photo = rendered.index('class="photo-card">KYC Selfie')
    face_checkbox = rendered.index('data-review-field="kyc_face_match_validated"')
    document = rendered.index('id="kyc-doc"')
    operations = rendered.index('id="kyc-details-operations-review"')
    assert user_photo < face_checkbox and kyc_photo < face_checkbox < document < operations
    assert 'checked' in rendered[face_checkbox-120:face_checkbox+150]
    assert 'data-review-field="kyc_details_validated"' in rendered
    assert 'data-review-field="kyc_details_comments"' in rendered


def test_form_renderer_places_review_controls_after_document_viewer():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function renderForm()")
    end = script.index("function updateFormFromRecord()", start)
    renderer = script[start:end]
    runner = r"""
const recordCache = new Map([[0, {user_id:'u1', user_name:'Selected user', gtc_link:''}]]);
let selectedIndex = 0;
let currentFormType = 'GTC';
let currentMitcProfile = 'LP';
const mitcProfileStates = {LP:'LP profile active'};
function selectedRecord(){ return recordCache.get(selectedIndex); }
function safeHttpUrl(value){ return value || ''; }
function hasSuppliedValue(value){ return value !== null && value !== undefined && value !== ''; }
function userDetailValue(value){ return value || 'Not supplied'; }
function escapeHtml(value){ return String(value ?? ''); }
function fieldCard(label, value, key){ return `<div data-field-key="${key}">${label}: ${value || 'Not available'}</div>`; }
function reviewSection(scope, validated, comments){ return `<section id="${scope}-operations-review"><input data-review-field="${validated}"><textarea data-review-field="${comments}"></textarea></section>`; }
""" + renderer + "\nprocess.stdout.write(renderForm());\n"
    import shutil, subprocess
    node = shutil.which("node")
    assert node, "Node.js required to exercise Form rendering"
    result = subprocess.run([node, "-e", runner], check=True, text=True, capture_output=True)
    rendered = result.stdout
    assert rendered.index('id="documentViewer"') < rendered.index('id="form-operations-review"')
    assert 'data-review-field="form_validated"' in rendered
    assert 'data-review-field="form_comments"' in rendered
