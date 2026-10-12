"""Local tests for lazy records, compact status polling, and coalesced writes."""
from pathlib import Path
import re
from threading import Event
from unittest.mock import patch

import pandas as pd

import app as app_module
from app import app


def set_frame(frame, path=None):
    app_module.STATE.clear()
    app_module.STATE.update({"df": frame, "filename": "fixture.xlsx", "path": path, "generation": 71})
    app_module._INFLIGHT_MITC.clear()


def test_selected_record_endpoint_returns_only_requested_current_record():
    frame = pd.DataFrame([
        {"user_id": "first", "user_name": "First User", "mitc_form_link": "https://fixture.test/first.pdf", "mitc_extraction_status": "queued"},
        {"user_id": "second", "user_name": "Second User", "mitc_form_link": "https://fixture.test/second.pdf", "mitc_extraction_status": "success", "mitc_borrower_name": "Second Borrower"},
    ])
    set_frame(frame)
    with app.test_client() as client:
        response = client.get("/api/records/1")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["index"] == 1
        assert payload["record"]["user_id"] == "second"
        assert payload["record"]["mitc_borrower_name"] == "Second Borrower"
        assert client.get("/api/records/2").status_code == 404


def test_lazy_record_errors_are_truthful():
    set_frame(None)
    with app.test_client() as client:
        missing = client.get("/api/records/0")
        assert missing.status_code == 400
        assert "Upload a workbook" in missing.get_json()["error"]
        set_frame(pd.DataFrame([{"user_id": "only"}]))
        out_of_range = client.get("/api/records/1")
        assert out_of_range.status_code == 404
        assert "out of range" in out_of_range.get_json()["error"]


def test_lightweight_status_payload_omits_full_records_and_compatibility_remains():
    long_url = "https://s3.fixture.test/" + ("long-object-name-" * 80) + ".pdf"
    frame = pd.DataFrame([
        {"user_id": "u1", "mitc_form_link": long_url, "mitc_extraction_status": "extracting", "mitc_borrower_name": "Large details"},
        {"user_id": "u2", "mitc_form_link": "", "mitc_extraction_status": "no_link", "some_extra_column": "extra"},
    ])
    set_frame(frame)
    with app.test_client() as client:
        summary_response = client.get("/api/records/status/summary")
        assert summary_response.status_code == 200
        summary = summary_response.get_json()
        assert summary["pending"] is True
        assert summary["summary"]["total_records"] == 2
        assert summary["rows"] == [
            {"index": 0, "extraction_status": "extracting"},
            {"index": 1, "extraction_status": "no_link"},
        ]
        assert "records" not in summary
        assert len(summary_response.data) < 1200
        compat = client.get("/api/records/status")
        assert len(compat.get_json()["records"]) == 2
        compact_compat = client.get("/api/records/status?full=0").get_json()
        assert "records" not in compact_compat
        assert "record" not in compact_compat["rows"][0]
        assert "some_extra_column" in client.get("/api/records").get_json()[1]  # compatibility includes full rows


def test_initial_html_is_light_and_frontend_uses_lazy_record_fetch_for_form():
    long_url = "https://s3.fixture.test/" + ("private-long-mitc-object-" * 100) + ".pdf"
    frame = pd.DataFrame([
        {"user_id": "u-1", "user_name": "Borrower One", "mitc_form_link": long_url,
         "mitc_extraction_status": "queued", "mitc_borrower_name": "Sensitive detailed field",
         "overall_match_status": "Review", "extra_column": "Do not inline"},
        {"user_id": "u-2", "user_name": "Borrower Two", "mitc_form_link": "", "mitc_extraction_status": "no_link"},
    ])
    set_frame(frame)
    with app.test_client() as client:
        response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "u-1" in html and "Borrower One" in html
    assert long_url not in html
    assert "Sensitive detailed field" not in html
    assert "Do not inline" not in html
    assert "const userRows =" in html
    assert "const records =" not in html
    assert "fetch(`/api/records/${index}`" in html
    assert "fetch('/api/records/status/summary'" in html
    assert "renderForm()" in html and "fetchRecord(selectedIndex)" in html
    assert "documentRoute()" in html and "MITC_PREVIEW_PAGE = 23" in html
    assert "function selectUser(index)" in html and "ArrowDown" in html and "ArrowUp" in html
    assert "navigateVisibleUsers(1)" in html and "navigateVisibleUsers(-1)" in html
    assert "if (userRows.length) selectUser(0)" in html
    assert "const recordCache = new Map()" in html
    assert "recordCache.set(index, payload.record)" in html
    assert "requestedIndex === selectedIndex" in html
    assert "recordCache.get(selectedIndex)" in html
    assert "const formRecordIndex = selectedIndex" in html
    assert "fetch(`/api/records/${formRecordIndex}/mitc`" in html
    assert "const r = selectedRecord()" in html
    viewer_script = (app_module.BASE_DIR / "static" / "pdf-viewer.js").read_text()
    assert "loadMitcDocument()" in viewer_script and "documentRoute()" in viewer_script
    assert len(response.data) < 85000


def test_coalesced_workbook_write_persists_latest_state_once(tmp_path, monkeypatch):
    frame = pd.DataFrame([
        {"user_id": "one", "mitc_form_link": "https://fixture.test/one.pdf", "mitc_extraction_status": "success"},
        {"user_id": "two", "mitc_form_link": "https://fixture.test/two.pdf", "mitc_extraction_status": "success"},
    ])
    output = tmp_path / "coalesced.xlsx"
    set_frame(frame, output)
    monkeypatch.setattr(app_module, "WORKBOOK_WRITE_DEBOUNCE_SECONDS", 0.04)
    written = []
    finished = Event()

    def fake_write(snapshot, path):
        written.append((snapshot.copy(), Path(path)))
        finished.set()

    monkeypatch.setattr(app_module, "_write_workbook_locked", fake_write)
    app_module._schedule_workbook_write(frame, 71)
    app_module._schedule_workbook_write(frame, 71)
    app_module._schedule_workbook_write(frame, 71)
    assert finished.wait(2), "debounced workbook writer did not run"
    assert len(written) == 1
    assert written[0][1] == output
    assert list(written[0][0]["user_id"]) == ["one", "two"]
    assert app_module._PENDING_WORKBOOK_WRITE is None


def test_download_flushes_debounced_latest_state(tmp_path, monkeypatch):
    frame = pd.DataFrame([{"user_id": "latest", "mitc_extraction_status": "success"}])
    output = tmp_path / "latest.xlsx"
    # Flask send_file needs an actual file; the test writer makes a tiny marker file.
    output.write_bytes(b"existing workbook marker")
    set_frame(frame, output)
    flushes = []

    def fake_persist(df, generation):
        flushes.append((df.iloc[0]["user_id"], generation))

    monkeypatch.setattr(app_module, "_persist_current_workbook", fake_persist)
    with app.test_client() as client:
        response = client.get("/download")
    assert response.status_code == 200
    assert flushes == [("latest", 71)]


def test_large_initial_page_and_poll_scale_with_light_rows_only():
    count = 3674
    long_url = "https://s3.fixture.test/" + ("long-mitc-document-object-" * 20) + ".pdf"
    data = {
        "user_id": [f"fixture-{index}" for index in range(count)],
        "user_name": [f"Fixture User {index}" for index in range(count)],
        "mitc_form_link": [long_url] * count,
        "mitc_extraction_status": ["queued"] * count,
        "mitc_borrower_name": ["Synthetic detail not for initial HTML"] * count,
    }
    for column_index in range(47):
        data[f"extra_column_{column_index}"] = [f"synthetic-{column_index}-{index}" for index in range(count)]
    set_frame(pd.DataFrame(data))
    with app.test_client() as client:
        page = client.get("/")
        status = client.get("/api/records/status/summary")
    html = page.get_data(as_text=True)
    summary_bytes = status.data
    assert page.status_code == 200 and status.status_code == 200
    assert len(re.findall(r'<button class="user-row(?: selected)?"', html)) == count
    assert long_url not in html
    assert "Synthetic detail not for initial HTML" not in html
    assert "extra_column_0" not in html
    assert "records" not in status.get_json()
    assert len(status.get_json()["rows"]) == count
    assert long_url.encode() not in summary_bytes
    # Lightweight per-row states remain far smaller than transmitting 52-column rows with long links.
    full_estimate = count * (len(long_url) + 47 * 28 + 120)
    assert len(summary_bytes) < full_estimate // 10
