"""Deterministic KYC-vs-user matching helpers used by the kiosk."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio, token_sort_ratio

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "mdm"}

# Minimum scores required for a present value to be considered a match.
NAME_MATCH_THRESHOLD = 60
ADDRESS_MATCH_THRESHOLD = 60
ADDRESS_ABBREVIATIONS = {
    "rd": "road", "st": "street", "str": "street", "ln": "lane", "rd.": "road",
    "ave": "avenue", "av": "avenue", "blvd": "boulevard", "nagar": "nagar",
    "apt": "apartment", "flat": "flat", "fl": "floor", "hno": "house number",
    "hse": "house", "no": "number", "dist": "district", "po": "post office",
    "ps": "police station", "marg": "road", "opp": "opposite", "near": "near",
}


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if bool(math.isnan(value)):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in {"", "nan", "nat", "none", "null", "n/a", "na"}


def _base_normalize(value: Any) -> str:
    if is_missing(value):
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(value: Any) -> str:
    text = _base_normalize(value)
    tokens = text.split()
    while tokens and tokens[0] in HONORIFICS:
        tokens.pop(0)
    return " ".join(tokens)


def normalize_address(value: Any) -> str:
    text = _base_normalize(value)
    tokens = []
    for token in text.split():
        tokens.append(ADDRESS_ABBREVIATIONS.get(token, token))
    return " ".join(tokens)


GENDER_ALIASES = {
    "f": "FEMALE",
    "female": "FEMALE",
    "fem": "FEMALE",
    "woman": "FEMALE",
    "women": "FEMALE",
    "m": "MALE",
    "male": "MALE",
    "man": "MALE",
    "men": "MALE",
    "o": "OTHER",
    "other": "OTHER",
    "others": "OTHER",
    "non binary": "OTHER",
    "nonbinary": "OTHER",
    "third gender": "OTHER",
    "transgender": "OTHER",
    "t": "OTHER",
}


def normalize_gender(value: Any) -> str:
    """Return a stable canonical gender label without guessing unknown values."""
    normalized = _base_normalize(value)
    if not normalized:
        return ""
    return GENDER_ALIASES.get(normalized, normalized.upper())


def match_gender(user_value: Any, kyc_value: Any) -> tuple[str, float | None]:
    user_missing, kyc_missing = is_missing(user_value), is_missing(kyc_value)
    if user_missing and kyc_missing:
        return "Both not present", None
    if user_missing:
        return "User gender not present", None
    if kyc_missing:
        return "KYC gender not present", None
    user_gender = normalize_gender(user_value)
    kyc_gender = normalize_gender(kyc_value)
    is_match = user_gender == kyc_gender
    return ("Match" if is_match else "Not a Match"), 100.0 if is_match else 0.0


def _score_name(left: str, right: str) -> float:
    if not left or not right:
        return math.nan
    scores = [token_set_ratio(left, right), token_sort_ratio(left, right), ratio(left, right)]
    return round(max(scores), 2)


def _score_address(left: str, right: str) -> float:
    if not left or not right:
        return math.nan
    score = max(token_set_ratio(left, right), token_sort_ratio(left, right), ratio(left, right))
    left_pin = set(re.findall(r"(?<!\d)\d{6}(?!\d)", left))
    right_pin = set(re.findall(r"(?<!\d)\d{6}(?!\d)", right))
    if left_pin and right_pin and left_pin != right_pin:
        score = min(score, 35.0)
    # A matching house number is a useful positive signal without overriding text similarity.
    left_house = re.findall(r"\b(?:house number|house|number)\s*(\d+[a-z]?)\b|\b(\d+[a-z]?)\b", left)
    right_house = re.findall(r"\b(?:house number|house|number)\s*(\d+[a-z]?)\b|\b(\d+[a-z]?)\b", right)
    left_nums = {part for pair in left_house for part in pair if part}
    right_nums = {part for pair in right_house for part in pair if part}
    if left_nums and right_nums and left_nums & right_nums:
        score = min(100.0, score + 5.0)
    return round(score, 2)


def match_name(user_value: Any, kyc_value: Any) -> tuple[str, float | None]:
    user_missing, kyc_missing = is_missing(user_value), is_missing(kyc_value)
    if user_missing and kyc_missing:
        return "Both not present", None
    if user_missing:
        return "User name not present", None
    if kyc_missing:
        return "KYC name not present", None
    score = _score_name(normalize_name(user_value), normalize_name(kyc_value))
    return ("Match" if score >= NAME_MATCH_THRESHOLD else "Not a Match"), score


def _to_date(value: Any) -> date | None:
    if is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial dates, including the usual 1900 date system.
        if 1 <= float(value) <= 100000:
            from openpyxl.utils.datetime import from_excel
            converted = from_excel(value)
            return converted.date() if isinstance(converted, datetime) else converted
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    # ISO datetimes are accepted after the explicit date formats.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    # Spreadsheet exports sometimes stringify dates using Java/English date
    # formatting, e.g. "Tue Nov 01 18:30:00 GMT 2005". Treat the displayed
    # calendar date as authoritative; do not shift it by the timezone offset.
    weekday_date = re.fullmatch(
        r"\s*(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\s+"
        r"([A-Za-z]{3,9})\s+(\d{1,2})\s+"
        r"\d{1,2}:\d{2}:\d{2}\s+.+?\s+(\d{4})\s*",
        text,
        re.IGNORECASE,
    )
    if weekday_date:
        month, day_value, year = weekday_date.groups()
        for month_format in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(f"{month} {day_value} {year}", month_format).date()
            except ValueError:
                pass
    return None


def match_dob(user_value: Any, kyc_value: Any) -> tuple[str, float | None]:
    user_missing, kyc_missing = is_missing(user_value), is_missing(kyc_value)
    if user_missing and kyc_missing:
        return "Both not present", None
    if user_missing:
        return "User dob not present", None
    if kyc_missing:
        return "KYC dob not present", None
    user_date, kyc_date = _to_date(user_value), _to_date(kyc_value)
    if user_date is None:
        return "Invalid user dob", None
    if kyc_date is None:
        return "Invalid KYC dob", None
    return ("Match" if user_date == kyc_date else "Not a Match"), 100.0 if user_date == kyc_date else 0.0


def match_address(user_value: Any, kyc_value: Any, label: str) -> tuple[str, float | None]:
    user_missing, kyc_missing = is_missing(user_value), is_missing(kyc_value)
    if user_missing and kyc_missing:
        return "Both not present", None
    if user_missing:
        return f"User {label} address not present", None
    if kyc_missing:
        return f"KYC {label} address not present", None
    score = _score_address(normalize_address(user_value), normalize_address(kyc_value))
    return ("Match" if score >= ADDRESS_MATCH_THRESHOLD else "Not a Match"), score


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    name_status, name_score = match_name(row.get("user_name"), row.get("kyc_name"))
    dob_status, dob_score = match_dob(row.get("user_dob"), row.get("kyc_dob"))
    perm_status, perm_score = match_address(row.get("user_perm_address"), row.get("kyc_permanent_address"), "permanent")
    curr_status, curr_score = match_address(row.get("user_curr_address"), row.get("kyc_current_address"), "current")
    gender_status, gender_score = match_gender(row.get("user_gender"), row.get("kyc_gender"))
    out.update({
        "name_match_status": name_status,
        "name_match_score": name_score,
        "dob_match_status": dob_status,
        "dob_match_score": dob_score,
        "permanent_address_match_status": perm_status,
        "permanent_address_match_score": perm_score,
        "current_address_match_status": curr_status,
        "current_address_match_score": curr_score,
        "user_gender_normalized": normalize_gender(row.get("user_gender")),
        "kyc_gender_normalized": normalize_gender(row.get("kyc_gender")),
        "gender_match": gender_status,
        "gender_match_score": gender_score,
    })
    statuses = [name_status, dob_status, perm_status, curr_status, gender_status]
    out["overall_match_status"] = "Match" if all(item == "Match" for item in statuses) else "Review"
    return out
