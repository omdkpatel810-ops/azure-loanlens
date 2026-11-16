"""T4 fields: reading amounts the way slips print them, and the checks that catch a misread."""

from __future__ import annotations

from decimal import Decimal

import pytest

from loanlens import t4

GOOD = {
    "year": "2025", "employer_name": "PRAIRIE GRAIN INC.", "employee_name": "AMÉLIE TREMBLAY", "province": "MB",
    "box14": "62000.00", "box16": "3480.65", "box18": "1016.80", "box22": "9300.00", "box24": "62000.00", "box26": "62000.00",
}  # fmt: skip


@pytest.mark.parametrize(
    ("printed", "value"),
    [
        ("52340.00", "52340.00"),
        ("52,340.00", "52340.00"),
        ("52 340,00", "52340.00"),
        ("52 340,00", "52340.00"),
        ("52340 00", "52340.00"),
        ("$1,049.12", "1049.12"),
        ("188", "188.00"),
        ("abc", None),
        ("-5.00", None),
        ("", None),
        (None, None),
    ],
)
def test_amounts(printed, value):
    assert t4.normalize("box14", printed) == value


def test_other_fields():
    assert t4.normalize("employer_name", "  Prairie   Grain inc. ") == "PRAIRIE GRAIN INC."
    assert t4.normalize("province", "Q.C.") == "QC"
    assert t4.normalize("province", "XX") is None
    assert t4.normalize("year", "Year / Année 2025") == "2025"
    assert t4.normalize("year", "last year") is None
    assert t4.amount("   ") is None and t4.amount(None) is None


def test_a_good_slip_has_no_issues():
    assert t4.check(GOOD) == []


@pytest.mark.parametrize(
    ("change", "issue"),
    [
        ({"box14": None}, "Box 14: employment income is missing"),
        ({"box14": "0.00", "box24": "0.00", "box26": "0.00"}, "should be more than zero"),
        ({"box22": "70000.00"}, "Box 22: income tax deducted is more than the income in box 14"),
        ({"box16": "4100.00"}, "Box 16: employee's CPP contributions is more than the 2025 maximum of $4,034.10"),
        ({"box16a": "400.00"}, "Box 16A: employee's second CPP contributions is more than the 2025 maximum of $396.00"),
        ({"box18": "1200.00"}, "Box 18: employee's EI premiums is more than the 2025 maximum of $1,077.48"),
        ({"box24": "66000.00", "box14": "90000.00"}, "Box 24: EI insurable earnings is more than the 2025 maximum of $65,700.00"),
        ({"box26": "75000.00", "box14": "90000.00"}, "is more than the 2025 maximum of $71,300.00"),
        ({"box26": "63000.00"}, "Box 26: CPP/QPP pensionable earnings is more than the employment income in box 14"),
        ({"box17": "100.00"}, "Box 17 has QPP contributions, but the province isn't Quebec"),
        ({"year": "2019"}, "Tax year 2019 is outside the years LoanLens has limits for (2024 to 2025)"),
    ],
)
def test_what_gets_caught(change, issue):
    issues = t4.check({**GOOD, **change})
    assert any(issue in i for i in issues), issues


def test_quebec_slips():
    quebec = {**GOOD, "province": "QC", "box16": None, "box17": "3740.80", "box18": "812.20"}
    assert t4.check(quebec) == []
    assert "but Quebec employees pay QPP" in t4.check({**quebec, "box16": "100.00"})[0]
    assert "the 2025 maximum of $860.67" in t4.check({**quebec, "box18": "900.00"})[0]


def test_limits_are_consistent():
    for lim in t4.LIMITS.values():
        # Maximum contributions are the rates times the maximum earnings above the $3,500 exemption.
        assert float(lim.cpp / (lim.ympe - 3500)) == pytest.approx(0.0595)
        assert float(lim.qpp / (lim.ympe - 3500)) == pytest.approx(0.064)
        assert lim.ei < lim.insurable * Decimal("0.02") and lim.ei_quebec < lim.ei


@pytest.mark.parametrize(
    ("on_slip", "applicant", "same"),
    [
        ("AMELIE TREMBLAY", "Amélie Tremblay", True),
        ("AMÉLIE TREMBLAY", "Amélie Tremblay-Roy", True),
        ("J. OKAFOR", "Jordan Okafor", True),
        ("JORDAN A OKAFOR", "Jordan Okafor", True),
        ("MATHIEU PELLETIER", "Jordan Okafor", False),
        ("TREMBLAY", "Amélie Roy", False),
        (None, "Jordan Okafor", True),
        ("JORDAN OKAFOR", "", True),
    ],
)
def test_same_person(on_slip, applicant, same):
    assert t4.same_person(on_slip, applicant) is same
