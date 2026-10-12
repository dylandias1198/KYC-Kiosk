"""Local route and row-application tests for alternate document checks."""
from io import BytesIO
from unittest.mock import patch

import pandas as pd

import app as app_module
from app import app, process_workbook, SANCTION_COLUMNS
from matching import NAME_MATCH_THRESHOLD
from mitc_extraction import MITC_COLUMNS, empty_mitc_fields


def test_workbook_adds_requested_columns_and_initial_document_statuses():
    fixture = app_module.BASE_DIR / "sample_input.xlsx"
    raw = pd.read_excel(fixture, dtype=object).iloc[:1].copy()
    raw.loc[raw.index[0], "kyc_name"] = "Siddharth Panwar"
    raw.loc[raw.index[0], "mitc_form_link"] = ""
    raw.loc[raw.index[0], "sanction_letter_link"] = ""
    from io import BytesIO
    stream = BytesIO(); raw.to_excel(stream, index=False); stream.seek(0)
    enriched, _ = process_workbook(stream, "local-fixture.xlsx", persist=False)
    assert set(MITC_COLUMNS).issubset(enriched.columns)
    assert set(SANCTION_COLUMNS).issubset(enriched.columns)
    assert enriched.iloc[0]["mitc_extraction_status"] == "no_link"
    assert enriched.iloc[0]["sanction_validation_status"] == "no_link"


def test_set_url_mitc_match_score_compares_only_to_kyc_name():
    frame = pd.DataFrame([{"mitc_form_link": "https://fixture.test/a.pdf", "sanction_letter_link": "",
                           "kyc_name": "Siddharth Panwar", "user_name": "Different User"}])
    result = empty_mitc_fields(status="success")
    result["mitc_borrower_name"] = "Siddharth Panwar"
    app_module._set_url_rows_result(frame, "https://fixture.test/a.pdf", result)
    assert frame.loc[0, "mitc_borrower_name_match_status"] == "Match"
    assert frame.loc[0, "mitc_borrower_name_match_score"] == 100.0
    assert frame.loc[0, "mitc_borrower_name_match_score"] >= NAME_MATCH_THRESHOLD
    frame.loc[0, "kyc_name"] = ""
    app_module._set_url_rows_result(frame, "https://fixture.test/a.pdf", result)
    assert frame.loc[0, "mitc_borrower_name_match_status"] == "KYC name not present"
    assert pd.isna(frame.loc[0, "mitc_borrower_name_match_score"])
    frame.loc[0, "kyc_name"] = "Siddharth Panwar"
    result["mitc_borrower_name"] = ""
    app_module._set_url_rows_result(frame, "https://fixture.test/a.pdf", result)
    assert frame.loc[0, "mitc_borrower_name_match_status"] == "MITC borrower name not present"
    assert pd.isna(frame.loc[0, "mitc_borrower_name_match_score"])


def test_sanction_route_uses_sanction_link_and_missing_document_is_truthful():
    frame = pd.DataFrame([{"user_id": "one", "sanction_letter_link": "", "mitc_form_link": "https://wrong.test/mitc.pdf",
                           "sanction_validation_status": "no_link", "sanction_validation_error": "No sanction_letter_link supplied"}])
    app_module.STATE.clear(); app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 8})
    with app.test_client() as client:
        result = client.post('/api/records/0/sanction')
        document = client.get('/api/records/0/sanction/document')
    assert result.status_code == 200
    assert result.get_json()['result']['sanction_validation_status'] == 'no_link'
    assert document.status_code == 400
    assert 'sanction_letter_link is blank' in document.get_json()['error']


def test_sanction_background_worker_uses_kyc_name_and_persists_terminal_status(monkeypatch):
    from threading import Event
    frame = pd.DataFrame([{"user_id": "worker", "sanction_letter_link": "https://fixture.test/sanction.pdf",
                           "mitc_form_link": "https://wrong.test/mitc.pdf", "kyc_name": "Siddharth Panwar",
                           "sanction_validation_status": "queued"}])
    app_module.STATE.clear(); app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 41})
    app_module._INFLIGHT_SANCTION.clear()
    release = Event(); started = Event()
    result = {column: "" for column in SANCTION_COLUMNS}
    result.update({"sanction_borrower_name": "Siddharth Panwar", "sanction_validation_status": "success"})
    def extraction(url):
        assert url == "https://fixture.test/sanction.pdf"
        started.set(); assert release.wait(2); return result
    monkeypatch.setattr(app_module, "extract_sanction_from_url", extraction)
    with app.test_client() as client:
        response = client.post('/api/records/0/sanction')
        assert response.status_code == 200
        assert started.wait(1)
        assert response.get_json()['result']['sanction_validation_status'] == 'extracting'
        future = next(iter(app_module._INFLIGHT_SANCTION.values()))
        release.set(); future.result(timeout=2)
        record = client.get('/api/records/0').get_json()['record']
    assert record['sanction_validation_status'] == 'success'
    assert record['sanction_borrower_name_match_status'] == 'Match'
    assert record['sanction_borrower_name_match_score'] == 100.0
    assert record['sanction_borrower_details_match'] == 'Match'


def test_sanction_extractor_missing_link_malformed_and_unreachable_are_failures():
    from mitc_extraction import MITCDocumentError, extract_sanction_from_url
    try:
        extract_sanction_from_url("")
    except ValueError as exc:
        assert "sanction_letter_link is blank" in str(exc)
    else:
        raise AssertionError("blank sanction link accepted")
    with patch('mitc_extraction.fetch_mitc_pdf', side_effect=MITCDocumentError('Could not fetch PDF', 502)):
        try:
            extract_sanction_from_url('https://fixture.test/unreachable.pdf')
        except MITCDocumentError as exc:
            assert 'Could not fetch PDF' in str(exc)
        else:
            raise AssertionError("unreachable sanction document accepted")
    with patch('mitc_extraction.fetch_mitc_pdf', return_value=b'not a PDF'):
        try:
            extract_sanction_from_url('https://fixture.test/malformed.pdf')
        except Exception as exc:
            assert 'malformed' in str(exc).lower() or 'pdf' in str(exc).lower()
        else:
            raise AssertionError("malformed sanction bytes accepted")


def test_sanction_viewer_reads_only_sanction_letter_link():
    frame = pd.DataFrame([{"user_id": "one", "sanction_letter_link": "https://fixture.test/sanction.pdf",
                           "mitc_form_link": "https://wrong.test/mitc.pdf"}])
    app_module.STATE.clear(); app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 9})
    with patch.object(app_module, 'fetch_mitc_pdf', return_value=b'%PDF-fixture') as fetched:
        with app.test_client() as client:
            response = client.get('/api/records/0/sanction/document')
    assert response.status_code == 200
    assert fetched.call_args.args[0] == 'https://fixture.test/sanction.pdf'
    assert fetched.call_args.kwargs['field_name'] == 'sanction_letter_link'


def test_duplicate_sanction_url_shares_inflight_worker(monkeypatch):
    from threading import Event
    frame = pd.DataFrame([
        {"sanction_letter_link": "https://fixture.test/shared.pdf", "kyc_name": "Same Name", "sanction_validation_status": "queued"},
        {"sanction_letter_link": "https://fixture.test/shared.pdf", "kyc_name": "Same Name", "sanction_validation_status": "queued"},
    ])
    app_module.STATE.clear(); app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": None, "generation": 50})
    app_module._INFLIGHT_SANCTION.clear()
    release = Event(); calls=[]
    result={column:"" for column in SANCTION_COLUMNS}; result.update({"sanction_borrower_name":"Same Name", "sanction_validation_status":"success"})
    def extraction(url): calls.append(url); assert release.wait(2); return result
    monkeypatch.setattr(app_module, "extract_sanction_from_url", extraction)
    first=app_module._submit_sanction_url("https://fixture.test/shared.pdf",frame,50)
    second=app_module._submit_sanction_url("https://fixture.test/shared.pdf",frame,50)
    assert first is second
    release.set(); first.result(timeout=2)
    assert calls == ["https://fixture.test/shared.pdf"]
    assert list(frame["sanction_borrower_name_match_status"]) == ["Match","Match"]
