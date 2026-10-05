"""Tokenised company links for the public hiring-request form.

The token is signed (itsdangerous) and carries the company name, so a link cannot be edited to
impersonate another company. Run `python -m app.core.links "Company name"` to print a link.
"""
from __future__ import annotations

import sys

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

_serializer = URLSafeTimedSerializer(settings.app_secret_key, salt="company-form")
MAX_AGE_SECONDS = 400 * 24 * 3600


def make_company_token(company: str) -> str:
    return _serializer.dumps({"c": company.strip()[:200]})


def read_company_token(token: str) -> str | None:
    try:
        data = _serializer.loads(token, max_age=MAX_AGE_SECONDS)
    except BadSignature:
        return None
    company = data.get("c") if isinstance(data, dict) else None
    return company if isinstance(company, str) and company.strip() else None


if __name__ == "__main__":
    name = " ".join(sys.argv[1:]).strip()
    if not name:
        raise SystemExit('Usage: python -m app.core.links "Company name"')
    print(f"/apply/{make_company_token(name)}")
