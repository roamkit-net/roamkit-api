"""Deterministic coverage snapshot normalizer (purchase-time)."""

from __future__ import annotations

import json

from apps.orders.product_snapshot import normalize_coverage_snapshot


def test_merge_duplicate_countries_and_sort():
    raw = [
        {
            "code": "si",
            "name": "Slovenia",
            "networks": [{"name": "A1", "types": ["4G"]}],
        },
        {
            "code": "HR",
            "name": "Croatia",
            "networks": [{"name": "Telemach", "types": ["5G"]}],
        },
        {
            "code": "HR",
            "name": "",
            "networks": [{"name": "A1", "types": ["LTE"]}],
        },
    ]
    out = normalize_coverage_snapshot(raw)
    assert [c["country_code"] for c in out] == ["HR", "SI"]
    assert out[0]["country_name"] == "Croatia"
    assert out[0]["operators"] == ["A1", "Telemach"]
    assert out[1]["operators"] == ["A1"]


def test_operator_case_safe_dedupe_and_alphabetical_order():
    raw = [
        {
            "code": "IT",
            "name": "Italy",
            "networks": [
                {"name": "WindTre"},
                {"name": "TIM"},
                {"name": "tim"},
                {"name": "Vodafone"},
                {"name": ""},
            ],
        }
    ]
    out = normalize_coverage_snapshot(raw)
    assert out[0]["operators"] == ["TIM", "Vodafone", "WindTre"]


def test_invalid_country_codes_ignored():
    raw = [
        {"code": "USA", "name": "United States", "networks": [{"name": "X"}]},
        {"code": "", "name": "Nowhere", "networks": [{"name": "Y"}]},
        {"code": "H", "name": "Bad", "networks": [{"name": "Z"}]},
        {"code": "hr", "name": "Croatia", "networks": []},
    ]
    out = normalize_coverage_snapshot(raw)
    assert out == [
        {"country_code": "HR", "country_name": "Croatia", "operators": []},
    ]


def test_missing_country_name_survives_as_null():
    raw = [{"code": "DE", "networks": [{"name": "Telekom"}]}]
    out = normalize_coverage_snapshot(raw)
    assert out == [
        {
            "country_code": "DE",
            "country_name": None,
            "operators": ["Telekom"],
        }
    ]


def test_empty_operators_kept():
    raw = [{"code": "AT", "name": "Austria", "networks": []}]
    out = normalize_coverage_snapshot(raw)
    assert out[0]["operators"] == []


def test_never_emits_provider_raw_fields():
    raw = [
        {
            "code": "FR",
            "name": "France",
            "networks": [{"name": "Orange", "types": ["5G"], "apn": "x"}],
            "operator_id": "secret",
        }
    ]
    out = normalize_coverage_snapshot(raw)
    blob = json.dumps(out)
    assert "networks" not in blob
    assert "types" not in blob
    assert "apn" not in blob
    assert "operator_id" not in blob
    assert set(out[0].keys()) == {"country_code", "country_name", "operators"}


def test_large_snapshot_under_size_budget():
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    raw = []
    for i in range(150):
        code = alphabet[i // 26] + alphabet[i % 26]
        raw.append(
            {
                "code": code,
                "name": f"Country {code}",
                "networks": [{"name": f"Op{j}", "types": ["5G"]} for j in range(8)],
            }
        )
    out = normalize_coverage_snapshot(raw)
    assert len(out) == 150
    payload = json.dumps(
        {
            "device_external_id": "dev",
            "coverage_type": "global",
            "coverage": out,
            "checked_at": "2026-08-09T12:00:00Z",
        }
    )
    # Soft guardrail: full coverage response stays under 500 KiB for v1.
    assert len(payload.encode("utf-8")) < 500_000
