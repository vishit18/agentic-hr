"""Deterministic normalisation. One right answer, so this is code, not a model.

Emails lower-cased and validated, phones to E.164, dates parsed, years of experience computed
from dates (never taken from what the model or the CV claims), certificates and skills mapped
to canonical names through a lookup table.
"""
from __future__ import annotations

import re
from datetime import date

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from app.config import settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalise_email(value: str) -> str | None:
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def find_emails(text: str) -> list[str]:
    seen: list[str] = []
    for match in EMAIL_RE.findall(text):
        email = normalise_email(match.rstrip(".,;:"))
        if email and email not in seen:
            seen.append(email)
    return seen


def normalise_phone(value: str, region: str | None = None) -> str | None:
    try:
        number = phonenumbers.parse(value.strip(), region or settings.default_phone_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


# Region tried first, then others in this order, for numbers written without a country code.
# Assumption: many candidates live outside Bahrain; a number must be valid in the region to be accepted.
PHONE_REGIONS = ["IN", "PK", "EG", "SA", "AE", "GB", "US", "PH", "JO", "LB", "BD", "LK", "NG", "KE", "OM", "KW", "QA"]


def find_phones(text: str, region: str | None = None) -> list[str]:
    first = region or settings.default_phone_region
    found: dict[tuple[int, int], str] = {}
    for reg in [first, *[r for r in PHONE_REGIONS if r != first]]:
        for match in phonenumbers.PhoneNumberMatcher(text, reg, phonenumbers.Leniency.VALID):
            found.setdefault(
                (match.start, match.end),
                phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164),
            )
    seen: list[str] = []
    for _, e164 in sorted(found.items()):
        if e164 not in seen:
            seen.append(e164)
    return seen


# ---------- dates and years of experience ----------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_PRESENT = {"present", "current", "now", "ongoing", "till date", "to date", "today", "date"}


def parse_date(value: str | None, *, end: bool = False, today: date | None = None) -> date | None:
    """Parse "Jan 2018", "01/2018", "2018-03", "2018" or "Present". Years alone: Jan (start) or Dec (end)."""
    if not value:
        return None
    today = today or date.today()
    text = value.strip().lower().replace(".", "")
    if text in _PRESENT:
        return today
    m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", text)
    if not m:
        return None
    year = int(m.group(1))
    month: int | None = None
    name = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", text)
    if name:
        month = _MONTHS[name.group(1)]
    else:
        num = re.search(r"\b(0?[1-9]|1[0-2])\s*[/\-]\s*(?:19|20)\d\d\b", text) or re.search(
            r"\b(?:19|20)\d\d\s*[/\-]\s*(0?[1-9]|1[0-2])\b", text
        )
        if num:
            month = int(num.group(1))
    if month is None:
        month = 12 if end else 1
    result = date(year, month, 1)
    return min(result, today)


def years_of_experience(ranges: list[tuple[date, date]]) -> float:
    """Total years across date ranges, with overlapping jobs counted once."""
    spans = sorted((s, e) for s, e in ranges if e >= s)
    if not spans:
        return 0.0
    merged: list[list[date]] = [[spans[0][0], spans[0][1]]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    days = sum((e - s).days for s, e in merged)
    return round(days / 365.25, 1)


# ---------- canonical names ----------

CERT_ALIASES: dict[str, list[str]] = {
    "CFA": ["cfa", "chartered financial analyst"],
    "CPA": ["cpa", "certified public accountant"],
    "ACCA": ["acca", "association of chartered certified accountants", "chartered certified accountant"],
    "CMA": ["cma", "certified management accountant"],
    "CIA": ["cia", "certified internal auditor"],
    "CISA": ["cisa", "certified information systems auditor"],
    "CISSP": ["cissp", "certified information systems security professional"],
    "FRM": ["frm", "financial risk manager"],
    "PMP": ["pmp", "project management professional"],
    "PRINCE2": ["prince2", "prince 2"],
    "CIPD": ["cipd", "chartered institute of personnel and development"],
    "SHRM-CP": ["shrm-cp", "shrm cp"],
    "SHRM-SCP": ["shrm-scp", "shrm scp"],
    "CAMS": ["cams", "certified anti-money laundering specialist"],
    "AMM": ["amm", "advanced manufacturing management"],
    "Six Sigma": ["six sigma", "lean six sigma", "six sigma green belt", "six sigma black belt"],
    "ITIL": ["itil"],
    "AWS Certified": ["aws certified", "aws certified solutions architect"],
    "CCNA": ["ccna"],
    "MCSE": ["mcse"],
}
SKILL_ALIASES: dict[str, list[str]] = {
    "microsoft excel": ["excel", "ms excel", "microsoft excel", "advanced excel"],
    "microsoft word": ["ms word", "microsoft word"],
    "microsoft powerpoint": ["powerpoint", "ms powerpoint", "microsoft powerpoint"],
    "power bi": ["power bi", "powerbi"],
    "sql": ["sql", "t-sql", "mysql", "postgresql", "postgres"],
    "python": ["python", "python3"],
    "javascript": ["javascript", "js", "java script"],
    "financial reporting": ["financial reporting", "financial statements"],
    "ifrs": ["ifrs", "international financial reporting standards"],
    "project management": ["project management", "project mgmt"],
}

_CERT_LOOKUP = {a: canon for canon, aliases in CERT_ALIASES.items() for a in aliases}
_SKILL_LOOKUP = {a: canon for canon, aliases in SKILL_ALIASES.items() for a in aliases}


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[()\[\].,]", " ", value.lower())).strip()


def canon_cert(value: str) -> str:
    key = _key(value)
    if key in _CERT_LOOKUP:
        return _CERT_LOOKUP[key]
    # "Chartered Financial Analyst (CFA) Level II" -> still CFA when the alias is a whole word inside
    for alias, canon in _CERT_LOOKUP.items():
        if len(alias) > 3 and re.search(rf"\b{re.escape(alias)}\b", key):
            return canon
    for alias, canon in _CERT_LOOKUP.items():
        if len(alias) <= 5 and re.search(rf"\b{re.escape(alias)}\b", key):
            return canon
    return value.strip()


def canon_skill(value: str) -> str:
    key = _key(value)
    return _SKILL_LOOKUP.get(key, key)


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        k = item.lower()
        if item and k not in seen:
            seen.add(k)
            out.append(item)
    return out
