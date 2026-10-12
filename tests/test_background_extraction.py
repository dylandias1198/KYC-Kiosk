"""Deterministic, local-only tests for the MITC background coordinator."""
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pandas as pd

import app as app_module
from app import app
from mitc_extraction import MITC_COLUMNS, empty_mitc_fields


FIXTURE = Path(__file__).parents[1] / "sample_input.xlsx"


def workbook_bytes(link: str) -> bytes:
    frame = pd.read_excel(FIXTURE, dtype=object).iloc[:1].copy()
    frame.loc[frame.index[0], "mitc_form_link"] = link
    stream = BytesIO()
    frame.to_excel(stream, index=False, engine="openpyxl")
    return stream.getvalue()


def reset_state():
    app_module.STATE.clear()
    app_module.STATE.update({"df": None, "filename": None, "path": None, "generation": 0})
    app_module._INFLIGHT_MITC.clear()


def test_upload_queues_and_returns_before_worker_finishes(monkeypatch):
    reset_state()
    started = Event()
    release = Event()

    def blocked_extraction(_url):
        started.set()
        assert release.wait(2)
        return empty_mitc_fields(status="success")

    monkeypatch.setattr(app_module, "extract_mitc_from_url", blocked_extraction)
    with app.test_client() as client:
        response = client.post(
            "/upload",
            data={"file": (BytesIO(workbook_bytes("https://fixture.test/queued.pdf")), "fixture.xlsx")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        assert started.wait(1), "background worker did not start"
        status = client.get("/api/records/status").get_json()
        assert status["rows"][0]["extraction_status"] == "extracting"
        assert status["pending"] is True
        release.set()
        future = next(iter(app_module._INFLIGHT_MITC.values()))
        future.result(timeout=2)
        # The callback updates the frame before the next status request in the
        # normal executor path; force a second read if the callback is settling.
        status = client.get("/api/records/status").get_json()
        assert status["records"][0]["mitc_extraction_status"] == "success"


def test_missing_links_do_not_call_urlopen(monkeypatch):
    reset_state()
    monkeypatch.setattr("mitc_extraction.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network call")))
    with app.test_client() as client:
        response = client.post(
            "/upload",
            data={"file": (BytesIO(FIXTURE.read_bytes()), "fixture.xlsx")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        status = client.get("/api/records/status").get_json()
        assert all(row["extraction_status"] == "no_link" for row in status["rows"])
        assert status["pending"] is False


def test_worker_exception_isolated_to_failed_row(monkeypatch):
    reset_state()

    started = Event()
    release = Event()

    def exploding_extraction(_url):
        started.set()
        assert release.wait(2)
        raise RuntimeError("synthetic worker failure")

    monkeypatch.setattr(app_module, "extract_mitc_from_url", exploding_extraction)
    with app.test_client() as client:
        response = client.post(
            "/upload",
            data={"file": (BytesIO(workbook_bytes("https://fixture.test/error.pdf")), "fixture.xlsx")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        assert started.wait(1)
        future = next(iter(app_module._INFLIGHT_MITC.values()))
        release.set()
        try:
            future.result(timeout=2)
        except RuntimeError:
            pass
        row = client.get("/api/records/status").get_json()["records"][0]
        assert row["mitc_extraction_status"] == "failed"
        assert "synthetic worker failure" in row["mitc_extraction_error"]
        assert set(MITC_COLUMNS).issubset(row)
