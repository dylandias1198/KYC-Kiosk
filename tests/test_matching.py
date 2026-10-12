from datetime import datetime
import matching
from matching import (
    ADDRESS_MATCH_THRESHOLD, NAME_MATCH_THRESHOLD, match_address, match_dob, match_gender, match_name,
    normalize_gender, normalize_name,
)

def test_name_honorific_and_threshold():
    assert normalize_name("Dr. Anita Sharma") == "anita sharma"
    status, score = match_name("Dr. Anita Sharma", "Anita Sharma")
    assert status == "Match" and score >= NAME_MATCH_THRESHOLD

def test_name_and_address_threshold_boundaries(monkeypatch):
    assert NAME_MATCH_THRESHOLD == 60
    assert ADDRESS_MATCH_THRESHOLD == 60

    monkeypatch.setattr(matching, "_score_name", lambda left, right: 60.0)
    assert match_name("left", "right") == ("Match", 60.0)
    monkeypatch.setattr(matching, "_score_name", lambda left, right: 59.99)
    assert match_name("left", "right") == ("Not a Match", 59.99)

    monkeypatch.setattr(matching, "_score_address", lambda left, right: 60.0)
    assert match_address("left", "right", "permanent") == ("Match", 60.0)
    monkeypatch.setattr(matching, "_score_address", lambda left, right: 59.99)
    assert match_address("left", "right", "permanent") == ("Not a Match", 59.99)

def test_dob_supported_formats_and_invalid():
    assert match_dob("15-Jan-1990", "1990-01-15")[0] == "Match"
    assert match_dob(datetime(1995, 7, 11), "11/07/1995")[0] == "Match"
    assert match_dob("Tue Nov 01 18:30:00 GMT 2005", "2005-11-01") == ("Match", 100.0)
    assert match_dob("Tuesday November 01 18:30:00 GMT+05:30 2005", "01/11/2005") == ("Match", 100.0)
    assert match_dob("Tue Nov 01 18:30:00 PST 2005", "2005-11-01")[0] == "Match"
    assert match_dob("Tue Nov 01 18:30:00 GMT+0530 2005", "2005-11-02")[0] == "Not a Match"
    assert match_dob("31-Feb-1992", "31-Feb-1992")[0] == "Invalid user dob"

def test_address_pin_mismatch_is_not_match():
    status, score = match_address("12 MG Rd, Pune 411001", "12 Mahatma Gandhi Road, Pune 400001", "permanent")
    assert status == "Not a Match" and score < ADDRESS_MATCH_THRESHOLD

def test_missing_statuses():
    assert match_name("", "A")[0] == "User name not present"
    assert match_dob("", "2020-01-01")[0] == "User dob not present"
    assert match_address("", "A", "current")[0] == "User current address not present"
    assert match_address("", "", "permanent")[0] == "Both not present"


def test_gender_aliases_and_canonical_unknowns():
    assert normalize_gender(" Female ") == "FEMALE"
    assert normalize_gender("fem") == "FEMALE"
    assert normalize_gender("non-binary") == "OTHER"
    assert normalize_gender("  self-described  ") == "SELF DESCRIBED"
    assert match_gender("Female", "F")[0:2] == ("Match", 100.0)
    assert match_gender("Male", "female")[0:2] == ("Not a Match", 0.0)


def test_gender_missing_statuses():
    assert match_gender("", "F") == ("User gender not present", None)
    assert match_gender("M", "") == ("KYC gender not present", None)
    assert match_gender("", "") == ("Both not present", None)


def test_gender_enrichment_and_overall_logic():
    from matching import enrich_row

    row = {
        "user_name": "A", "kyc_name": "A",
        "user_dob": "2000-01-01", "kyc_dob": "2000-01-01",
        "user_gender": "Female", "kyc_gender": "F",
        "user_perm_address": "1 Main Road", "kyc_permanent_address": "1 Main Road",
        "user_curr_address": "1 Main Road", "kyc_current_address": "1 Main Road",
    }
    enriched = enrich_row(row)
    assert enriched["gender_match"] == "Match"
    assert enriched["gender_match_score"] == 100.0
    assert enriched["user_gender_normalized"] == "FEMALE"
    assert enriched["kyc_gender_normalized"] == "FEMALE"
    assert enriched["overall_match_status"] == "Match"
