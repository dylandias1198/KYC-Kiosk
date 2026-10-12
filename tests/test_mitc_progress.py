"""Local tests for truthful MITC progress counts and live header markup."""
from io import BytesIO

import pandas as pd

import app as app_module
from app import app
from mitc_extraction import empty_mitc_fields


def test_summary_uses_nonblank_links_as_denominator_and_terminal_states_as_extracted():
    frame = pd.DataFrame(
        [
            {"mitc_form_link": "https://fixture.test/a.pdf", "mitc_extraction_status": "success"},
            {"mitc_form_link": "https://fixture.test/b.pdf", "mitc_extraction_status": "parsed_with_missing_fields"},
            {"mitc_form_link": "https://fixture.test/c.pdf", "mitc_extraction_status": "failed"},
            {"mitc_form_link": "https://fixture.test/d.pdf", "mitc_extraction_status": "queued"},
            {"mitc_form_link": "https://fixture.test/e.pdf", "mitc_extraction_status": "extracting"},
            {"mitc_form_link": "", "mitc_extraction_status": "no_link"},
            {"mitc_form_link": None, "mitc_extraction_status": "no_link"},
        ]
    )
    summary = app_module._mitc_status_summary(frame)
    assert summary == {
        "total_records": 7,
        "eligible": 5,
        "eligible_mitc_links": 5,
        "extracted": 2,
        "success": 1,
        "parsed_with_missing_fields": 1,
        "failed": 1,
        "no_link": 2,
        "queued": 1,
        "extracting": 1,
        "progress_percent": 40.0,
        "progress_percentage": 40.0,
    }


def test_status_endpoint_exposes_summary_without_changing_row_compatibility():
    app_module.STATE.clear()
    app_module.STATE.update(
        {
            "df": pd.DataFrame(
                [
                    {"mitc_form_link": "https://fixture.test/a.pdf", "mitc_extraction_status": "success"},
                    {"mitc_form_link": "", "mitc_extraction_status": "no_link"},
                ]
            ),
            "filename": "fixture.xlsx",
            "path": None,
            "generation": 1,
        }
    )
    with app.test_client() as client:
        payload = client.get("/api/records/status").get_json()
    assert payload["summary"]["total_records"] == 2
    assert payload["summary"]["eligible"] == 1
    assert payload["summary"]["extracted"] == 1
    assert payload["summary"]["progress_percent"] == 100.0
    assert payload["pending"] is False
    assert payload["rows"][0]["extraction_status"] == "success"


def test_header_has_truthful_counts_denominator_progress_bar_and_poll_update_markup():
    app_module.STATE.clear()
    app_module.STATE.update(
        {
            "df": pd.DataFrame(
                [
                    {"user_id": "u1", "mitc_form_link": "https://fixture.test/a.pdf", "mitc_extraction_status": "queued"},
                    {"user_id": "u2", "mitc_form_link": "", "mitc_extraction_status": "no_link"},
                ]
            ),
            "filename": "fixture.xlsx",
            "path": None,
            "generation": 1,
        }
    )
    with app.test_client() as client:
        html = client.get("/").get_data(as_text=True)
    assert "MITC extracted 0 / 1 eligible" in html
    assert 'id="mitcProgressBar"' in html
    assert 'role="progressbar"' in html
    assert 'data-metric="queued"' in html
    assert 'data-metric="extracting"' in html
    assert "payload.summary" in html
    assert "renderMitcSummary(payload.summary)" in html
