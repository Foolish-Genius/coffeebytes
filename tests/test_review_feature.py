"""Supply-chain review on the example purchase history (fixture registries)."""
from datetime import date
from pathlib import Path

import pytest

from siliconscout.config import Config
from siliconscout.review import load_rows, review

EX = Path(__file__).resolve().parent.parent / "examples" / "purchase_history.csv"


@pytest.fixture(scope="module")
def rep():
    return review(load_rows(EX), Config.load(), today=date(2026, 11, 22))


def issues(rep, item):
    return [f["issue"] for f in rep["findings"] if f["item"] == item]


def test_expired_approval_is_high(rep):
    f = next(f for f in rep["findings"] if f["item"] == "SES-VT200")
    assert f["severity"] == "high" and "expired" in f["issue"].lower() and "2025-06-30" in f["evidence"]


def test_trader_selling_approved_model_points_to_maker(rep):
    f = next(f for f in rep["findings"] if f["item"] == "TT-AIS9")
    assert "Trackovo Telematics" in f["issue"] and "Buy direct" in f["recommendation"]


def test_unapproved_model_is_high(rep):
    assert "Model not on any applicable approval list" in issues(rep, "FG-Pro")


def test_single_source_and_price_rise(rep):
    got = issues(rep, "NVT-140X")
    assert "Single source" in got and any(i.startswith("Price up 17%") for i in got)


def test_component_channel_and_lead_time(rep):
    got = issues(rep, "FSC-MCU100A")
    assert "Not an authorized distributor of Fictiva Semiconductor" in got
    assert any(i.startswith("Long lead time: 120") for i in got)
    assert not any("Broker" not in f["supplier"] and "authorized" in f["issue"] and f["item"] == "FSC-MCU100A"
                   for f in rep["findings"])  # Distributor Alpha is authorized -> no finding


def test_sorted_by_severity_and_counts(rep):
    sev = [f["severity"] for f in rep["findings"]]
    assert sev == sorted(sev, key=["high", "medium", "low"].index)
    assert rep["counts"]["high"] >= 2 and rep["lines"] == 7 and rep["items"] == 5
