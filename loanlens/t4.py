"""T4 slips: the fields LoanLens reads, and the checks that catch a misread.

A T4 reports one year's pay from one employer. Several boxes have legal maximums set each year
(CPP and EI contributions, insurable and pensionable earnings), so a figure above its maximum is a
misread, a typo, or a document that needs a closer look. LoanLens never reads or stores the Social
Insurance Number printed on the slip: lending doesn't need it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Field key -> what it is. Box numbers are the CRA's.
FIELDS = {
    "year": "Tax year",
    "employer_name": "Employer",
    "employee_name": "Employee",
    "province": "Box 10: province of employment",
    "box14": "Box 14: employment income",
    "box16": "Box 16: employee's CPP contributions",
    "box16a": "Box 16A: employee's second CPP contributions",
    "box17": "Box 17: employee's QPP contributions",
    "box18": "Box 18: employee's EI premiums",
    "box22": "Box 22: income tax deducted",
    "box24": "Box 24: EI insurable earnings",
    "box26": "Box 26: CPP/QPP pensionable earnings",
    "box44": "Box 44: union dues",
}
AMOUNTS = ("box14", "box16", "box16a", "box17", "box18", "box22", "box24", "box26", "box44")
REQUIRED = ("year", "employer_name", "employee_name", "province", "box14", "box22")
PROVINCES = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT", "ZZ")


@dataclass(frozen=True)
class Limits:
    ympe: Decimal  # maximum pensionable earnings (box 26)
    cpp: Decimal  # maximum employee CPP contribution (box 16)
    cpp2: Decimal  # maximum second CPP contribution (box 16A)
    qpp: Decimal  # maximum employee QPP contribution (box 17)
    insurable: Decimal  # maximum insurable earnings (box 24)
    ei: Decimal  # maximum EI premium outside Quebec (box 18)
    ei_quebec: Decimal  # maximum EI premium in Quebec, where the rate is lower


# From the CRA's and Revenu Québec's published rates for each year.
LIMITS = {
    2024: Limits(
        ympe=Decimal("68500"), cpp=Decimal("3867.50"), cpp2=Decimal("188.00"), qpp=Decimal("4160.00"),
        insurable=Decimal("63200"), ei=Decimal("1049.12"), ei_quebec=Decimal("834.24"),
    ),
    2025: Limits(
        ympe=Decimal("71300"), cpp=Decimal("4034.10"), cpp2=Decimal("396.00"), qpp=Decimal("4339.20"),
        insurable=Decimal("65700"), ei=Decimal("1077.48"), ei_quebec=Decimal("860.67"),
    ),
}  # fmt: skip


def amount(text: str | None) -> Decimal | None:
    """A dollar amount as printed: 52340.00, 52,340.00, 52 340,00 or 52340 00 (dollars and cents boxes)."""
    if text is None:
        return None
    s = re.sub(r"[$\s\u00a0\u202f]", " ", str(text)).strip()  # dollar signs and every kind of space
    if not s:
        return None
    if re.fullmatch(r"\d[\d ]*\s\d{2}", s) and not re.search(r"[.,]", s):  # "52340 00": separate cents box
        s = s[: s.rfind(" ")].replace(" ", "") + "." + s[-2:]
    elif re.search(r",\d{2}$", s):  # French decimal comma
        s = s.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "").replace(" ", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if value >= 0 else None


def normalize(key: str, text: str | None) -> str | None:
    """A field's value in one canonical form, so two readings of the same slip compare equal."""
    if text is None or not str(text).strip():
        return None
    if key in AMOUNTS:
        value = amount(text)
        return None if value is None else f"{value:.2f}"
    if key == "year":
        found = re.search(r"\b(20\d\d)\b", str(text))
        return found.group(1) if found else None
    if key == "province":
        code = re.sub(r"[^A-Za-z]", "", str(text)).upper()
        return code if code in PROVINCES else None
    return " ".join(str(text).split()).upper()


def check(fields: dict[str, str | None]) -> list[str]:
    """Problems with a set of normalized T4 fields. An empty list means nothing looks wrong."""
    issues = [f"{FIELDS[k]} is missing" for k in REQUIRED if not fields.get(k)]
    money = {k: Decimal(fields[k]) for k in AMOUNTS if fields.get(k)}
    year = int(fields["year"]) if fields.get("year") else None
    if year is not None and year not in LIMITS:
        issues.append(f"Tax year {year} is outside the years LoanLens has limits for ({min(LIMITS)} to {max(LIMITS)})")
    income = money.get("box14")
    if income is not None and income <= 0:
        issues.append("Box 14: employment income should be more than zero")
    if income is not None and money.get("box22", Decimal(0)) > income:
        issues.append("Box 22: income tax deducted is more than the income in box 14")
    quebec = fields.get("province") == "QC"
    if quebec and (money.get("box16") or money.get("box16a")):
        issues.append("Boxes 16 and 16A are for CPP contributions, but Quebec employees pay QPP (box 17)")
    if not quebec and money.get("box17"):
        issues.append("Box 17 has QPP contributions, but the province isn't Quebec")
    if year in LIMITS:
        lim = LIMITS[year]
        maximums = {
            "box16": lim.cpp,
            "box16a": lim.cpp2,
            "box17": lim.qpp,
            "box18": lim.ei_quebec if quebec else lim.ei,
            "box24": lim.insurable,
            "box26": lim.ympe,
        }
        for key, maximum in maximums.items():
            if money.get(key, Decimal(0)) > maximum:
                issues.append(f"{FIELDS[key]} is more than the {year} maximum of ${maximum:,.2f}")
    if income is not None:
        for key in ("box24", "box26"):
            if money.get(key, Decimal(0)) > max(income, Decimal(0)) + Decimal("0.01"):
                issues.append(f"{FIELDS[key]} is more than the employment income in box 14")
    return issues
