"""Unit tests for the pure helpers in app.py — no database required.

Run with:  pytest -q
"""
import hashlib
from datetime import date
from decimal import Decimal

import pytest

import app as crm


# ── parse_money ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("$6,000.00", Decimal("6000.00")),
    ("6,000", Decimal("6000")),
    ("6000.50", Decimal("6000.50")),
    ("$ 1,234.56", Decimal("1234.56")),
    ("€1,000.50", Decimal("1000.50")),
    ("BDT 12,345", Decimal("12345")),
    ("", Decimal("0")),
    (None, Decimal("0")),
    (0, Decimal("0")),
    (42.5, Decimal("42.5")),
    ("not-a-number", Decimal("0")),
])
def test_parse_money(raw, expected):
    assert crm.parse_money(raw) == expected


def test_parse_money_accepts_decimal():
    assert crm.parse_money(Decimal("7.25")) == Decimal("7.25")


def test_parse_money_never_returns_float():
    assert isinstance(crm.parse_money("$6,000.00"), Decimal)


# ── parse_date ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("2026-08-03", date(2026, 8, 3)),
    ("03/08/2026", date(2026, 8, 3)),
    ("08/03/2026", date(2026, 3, 8)),
    ("03-08-2026", date(2026, 8, 3)),
    ("", None),
    (None, None),
    ("garbage", None),
])
def test_parse_date(raw, expected):
    assert crm.parse_date(raw) == expected


def test_parse_date_accepts_datetime():
    assert crm.parse_date(date(2026, 1, 1)) == date(2026, 1, 1)


# ── _safe_next (open-redirect guard) ───────────────────────────────────────

@pytest.mark.parametrize("target,expected", [
    ("/leads", "/leads"),
    ("/dashboard?x=1", "/dashboard?x=1"),
    ("https://evil.example.com", None),
    ("//evil.example.com", None),
    ("", None),
    (None, None),
])
def test_safe_next(target, expected):
    assert crm._safe_next(target) == expected


# ── password hashing / legacy scheme ───────────────────────────────────────

def test_verify_password_werkzeug_roundtrip():
    stored = crm.hash_password("correct horse battery staple")
    assert crm.verify_password("correct horse battery staple", stored)
    assert not crm.verify_password("wrong", stored)


def test_verify_password_legacy_sha256():
    salt = "a" * 32
    digest = hashlib.sha256((salt + "secret").encode()).hexdigest()
    stored = f"{salt}:{digest}"
    assert crm._is_legacy_hash(stored)
    assert crm.verify_password("secret", stored)
    assert not crm.verify_password("not-secret", stored)


def test_is_legacy_hash_detects_scheme():
    assert not crm._is_legacy_hash(crm.hash_password("secret"))
    assert not crm._is_legacy_hash("")
    assert not crm._is_legacy_hash(None)


# ── money filter ───────────────────────────────────────────────────────────

def test_money_filter():
    assert crm.money_filter(0) == "$0"
    assert crm.money_filter(Decimal("1000")) == "$1,000"
    assert crm.money_filter("1500") == "$1,500"
    assert crm.money_filter(None) == "$0"
    assert crm.money_filter("oops") == "$0"


# ── _normalize_database_url ────────────────────────────────────────────────

def test_normalize_url_appends_sslmode():
    url = "postgresql://postgres.ref:pass@aws-0-region.pooler.supabase.com:6543/postgres"
    out = crm._normalize_database_url(url)
    assert out.endswith("?sslmode=require")


def test_normalize_url_pins_sslmode_and_preserves_other_params():
    # sslmode is always forced to require (the pooler only accepts SSL);
    # any other query params survive.
    url = "postgresql://u:pw@host:6543/db?application_name=crm&sslmode=disable"
    out = crm._normalize_database_url(url)
    assert "application_name=crm" in out
    assert "sslmode=require" in out
    assert "sslmode=disable" not in out


def test_normalize_url_repairs_mangled_sslmode():
    # The exact pattern that broke the Render deploy:
    #   invalid dsn: extra key/value separator "=" in URI query parameter: "sslmode"
    url = "postgresql://u:pw@aws-0-region.pooler.supabase.com:6543/postgres?sslmode=require="
    out = crm._normalize_database_url(url)
    assert out.endswith("?sslmode=require")
    assert "==" not in out


def test_normalize_url_strips_whitespace():
    url = "  postgresql://u:pw@host:5432/db?sslmode=require  "
    out = crm._normalize_database_url(url)
    assert not out.startswith(" ")
    assert not out.endswith(" ")


def test_normalize_url_rejects_multi_at():
    # Port glued into the host — the case the guard is meant to catch.
    with pytest.raises(RuntimeError):
        crm._normalize_database_url(
            "postgresql://u:pass@6543@aws-0-region.pooler.supabase.com:5432/db"
        )


def test_normalize_url_rejects_empty():
    with pytest.raises(RuntimeError):
        crm._normalize_database_url("")
