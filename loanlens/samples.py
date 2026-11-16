"""Sample T4 slips for training and testing the extraction model: invented people and employers,
consistent figures, and the exact position of every value, written out as Document Intelligence labels.

    python -m loanlens.samples --out data/t4 --train 40 --test 20

Every slip carries a large SAMPLE watermark and a footer saying it isn't a real tax slip, uses invented
names and employers, and shows a Social Insurance Number starting with 0, a digit the government never
issues. The layout is LoanLens' own, laid out like a T4 (bilingual labels, numbered boxes), not a copy of
the CRA's form. About a third of the slips are rendered as tilted, noisy "phone photos", because that's
what applicants upload.

Each file ends with its true values after the end-of-file marker, which PDF readers and image viewers
ignore. That lets the local stand-in extractor (no Azure needed) and the accuracy measurement know the
right answers.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import random
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from loanlens.t4 import LIMITS

TRAILER = b"%loanlens-sample "
FIELD_TYPES = {"year": "string", "employer_name": "string", "employee_name": "string", "province": "string"}

FIRST = [
    "Amélie",
    "Jordan",
    "Priya",
    "Mathieu",
    "Chloé",
    "Daniel",
    "Aisha",
    "Ravi",
    "Sophie",
    "Kenji",
    "Marie-Ève",
    "Tyler",
    "Nadia",
    "Olivier",
    "Grace",
    "Hamza",
    "Isabelle",
    "Lucas",
    "Mei",
    "Samuel",
    "Zoé",
    "Farah",
    "Owen",
    "Léa",
]
LAST = [
    "Tremblay",
    "Okafor",
    "Sharma",
    "Gagnon",
    "Nguyen",
    "Bouchard",
    "Singh",
    "Roy",
    "Kowalski",
    "Morin",
    "Chen",
    "Côté",
    "MacDonald",
    "Patel",
    "Lavoie",
    "Haddad",
    "Fortin",
    "Wilson",
    "Lee",
    "Bergeron",
    "Ahmed",
    "Pelletier",
    "Campbell",
    "Dubé",
]
FIRM_A = [
    "Prairie",
    "Red River",
    "Northgate",
    "Lakeshore",
    "Aspen",
    "Boréal",
    "Granite",
    "Maple Leaf",
    "Riverbend",
    "Kestrel",
    "Saint-Laurent",
    "Tamarack",
]
FIRM_B = [
    "Grain",
    "Logistics",
    "Dental",
    "Software",
    "Builders",
    "Foods",
    "Outfitters",
    "Clinics",
    "Freight",
    "Bakery",
    "Analytics",
    "Hardware",
]
FIRM_C = ["Inc.", "Ltd.", "Co.", "Corp.", "Ltée", "Group", "Partners"]
STREETS = ["Main St", "Portage Ave", "Pembina Hwy", "rue Principale", "King St"]
CITIES = ["Winnipeg", "Brandon", "Toronto", "Montréal", "Calgary", "Regina"]
PROVINCE_WEIGHTS = {"MB": 30, "ON": 20, "QC": 15, "AB": 10, "BC": 10, "SK": 10, "NS": 5}
FONTS = ["Helvetica", "Times-Roman", "Courier"]
PAGE_W, PAGE_H = 612.0, 792.0  # US letter, in points


def cents(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def invent(rng: random.Random) -> dict[str, str]:
    """One person's T4 for one year, with figures that follow that year's rules."""
    year = rng.choice(sorted(LIMITS))
    lim = LIMITS[year]
    province = rng.choices(list(PROVINCE_WEIGHTS), weights=list(PROVINCE_WEIGHTS.values()))[0]
    quebec = province == "QC"
    income = cents(Decimal(rng.randint(24_000, 145_000)) + Decimal(rng.randint(0, 99)) / 100)
    pensionable = min(income, lim.ympe)
    exempt = Decimal(3500)  # the basic exemption, the same every year
    # Rates follow from each year's maximums: maximum contribution = rate x (maximum earnings - exemption).
    main_rate = (lim.qpp if quebec else lim.cpp) / (lim.ympe - exempt)
    main = cents(max(pensionable - exempt, Decimal(0)) * main_rate)
    second_ceiling = lim.ympe + lim.cpp2 / Decimal("0.04")  # second earnings ceiling, at the 4% rate
    second = cents(max(min(income, second_ceiling) - lim.ympe, Decimal(0)) * Decimal("0.04"))
    insurable = min(income, lim.insurable)
    ei = cents(insurable * (lim.ei_quebec if quebec else lim.ei) / lim.insurable)
    fields = {
        "year": str(year),
        "employer_name": f"{rng.choice(FIRM_A)} {rng.choice(FIRM_B)} {rng.choice(FIRM_C)}",
        "employee_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
        "province": province,
        "box14": f"{income:.2f}",
        "box16" if not quebec else "box17": f"{min(main, lim.qpp if quebec else lim.cpp):.2f}",
        "box18": f"{min(ei, lim.ei_quebec if quebec else lim.ei):.2f}",
        "box22": f"{cents(income * Decimal(rng.randint(11, 27)) / 100):.2f}",
        "box24": f"{insurable:.2f}",
        "box26": f"{pensionable:.2f}",
    }
    if second > 0 and not quebec:  # Quebec's second contributions go in box 17A, which LoanLens doesn't read
        fields["box16a"] = f"{min(second, lim.cpp2):.2f}"
    if rng.random() < 0.25:
        fields["box44"] = f"{cents(income * Decimal('0.012')):.2f}"
    return fields


def printed(value: str, style: int) -> str:
    """How an amount is printed on the slip: three styles real payroll software uses."""
    d = Decimal(value)
    if style == 0:
        return f"{d:.2f}"
    if style == 1:
        return f"{d:,.2f}"
    return f"{d:,.2f}".replace(",", " ").replace(".", ",")  # 52 340,00


BOXES = [  # (field, box number, English label, French label)
    ("box14", "14", "Employment income", "Revenus d'emploi"),
    ("box22", "22", "Income tax deducted", "Impôt sur le revenu retenu"),
    ("box16", "16", "Employee's CPP contributions", "Cotisations de l'employé au RPC"),
    ("box16a", "16A", "Employee's second CPP contributions", "Deuxièmes cotisations au RPC"),
    ("box17", "17", "Employee's QPP contributions", "Cotisations de l'employé au RRQ"),
    ("box18", "18", "Employee's EI premiums", "Cotisations de l'employé à l'AE"),
    ("box24", "24", "EI insurable earnings", "Gains assurables d'AE"),
    ("box26", "26", "CPP/QPP pensionable earnings", "Gains ouvrant droit à pension"),
    ("box44", "44", "Union dues", "Cotisations syndicales"),
]


@dataclass
class Placed:
    field: str
    text: str
    words: list[tuple[float, float, float, float]]  # each word's box in points: left, top, right, bottom (from the top)


def draw(fields: dict[str, str], rng: random.Random) -> tuple[bytes, list[Placed]]:
    """The slip as a PDF, and where each value landed."""
    from reportlab.lib.colors import Color
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle("SAMPLE T4 - not a real tax slip")
    font = rng.choice(FONTS)
    size = rng.choice([9.5, 10, 10.5, 11])
    style = rng.randrange(3)
    ox, oy = rng.uniform(-14, 14), rng.uniform(-14, 14)
    placed: list[Placed] = []

    def value(field: str, text: str, x: float, y_top: float, align_right: bool = False) -> None:
        width = stringWidth(text, font, size)
        left = x - width if align_right else x
        baseline = PAGE_H - y_top
        c.setFont(font, size)
        c.drawString(left, baseline, text)
        words, cursor = [], left
        for i, word in enumerate(text.split(" ")):
            w = stringWidth(word, font, size)
            words.append((cursor, y_top - size * 0.78, cursor + w, y_top + size * 0.22))
            cursor += w + (stringWidth(" ", font, size) if i < len(text.split(" ")) - 1 else 0)
        placed.append(Placed(field, text, words))

    def box(x: float, y_top: float, w: float, h: float, number: str, en: str, fr: str) -> None:
        c.setLineWidth(0.6)
        c.rect(x, PAGE_H - y_top - h, w, h)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(x + 4, PAGE_H - y_top - 10, number)
        c.setFont("Helvetica", 6.2)
        c.drawString(x + 22, PAGE_H - y_top - 9, en)
        c.drawString(x + 22, PAGE_H - y_top - 17, fr)

    left, top = 40 + ox, 60 + oy
    c.setFont("Helvetica-Bold", 13)
    c.drawString(left, PAGE_H - top, "T4 SAMPLE  ·  Statement of Remuneration Paid")
    c.setFont("Helvetica", 9)
    c.drawString(left, PAGE_H - top - 13, "État de la rémunération payée  ·  SPÉCIMEN")

    box(left, top + 28, 300, 46, "", "Employer's name / Nom de l'employeur", "")
    value("employer_name", fields["employer_name"], left + 8, top + 64)
    box(left + 310, top + 28, 90, 46, "", "Year / Année", "")
    value("year", fields["year"], left + 320, top + 64)
    box(left + 410, top + 28, 122, 46, "10", "Province of employment", "Province d'emploi")
    value("province", fields["province"], left + 432, top + 64)

    box(left, top + 84, 300, 64, "", "Employee's name and address / Nom et adresse de l'employé", "")
    value("employee_name", fields["employee_name"], left + 8, top + 118)
    c.setFont(font, size - 1)
    c.drawString(
        left + 8,
        PAGE_H - top - 134,
        f"{rng.randint(10, 999)} {rng.choice(['Main', 'Portage', 'Pembina', 'Rue Principale', 'King'])} St, "
        f"{rng.choice(['Winnipeg', 'Brandon', 'Toronto', 'Montréal', 'Calgary', 'Regina'])}",
    )
    box(left + 310, top + 84, 222, 64, "12", "Social insurance number", "Numéro d'assurance sociale")
    c.setFont(font, size)
    c.drawString(left + 320, PAGE_H - top - 128, f"0{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}")

    grid = [b for b in BOXES if b[0] in fields]
    for i, (field, number, en, fr) in enumerate(grid):
        col, row = i % 2, i // 2
        bx, by = left + col * 270, top + 162 + row * 48
        box(bx, by, 262, 42, number, en, fr)
        value(field, printed(fields[field], style), bx + 252, by + 34, align_right=True)

    c.setFont("Helvetica", 7)
    c.drawString(
        left,
        46,
        "Generated by LoanLens to train and test a model. Not issued by the Canada Revenue Agency. "
        "Invented people and employers.",
    )
    c.saveState()
    c.setFillColor(Color(0.8, 0.1, 0.1, alpha=0.18))
    c.setFont("Helvetica-Bold", 46)
    c.translate(PAGE_W / 2, PAGE_H / 2 - 40)
    c.rotate(32)
    c.drawCentredString(0, 18, "SAMPLE · NOT A REAL TAX SLIP")
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(0, -26, "SPÉCIMEN · AUCUNE VALEUR")
    c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue(), placed


Corners = list[list[tuple[float, float]]]


def photograph(
    pdf: bytes, placed: list[Placed], rng: random.Random
) -> tuple[bytes, list[list[tuple[float, float]]], tuple[int, int]]:
    """The slip as a tilted, slightly noisy JPEG, like a phone photo. Returns each word's corners in pixels."""
    import pymupdf
    from PIL import Image, ImageFilter

    dpi = rng.choice([120, 150])
    scale = dpi / 72
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    angle = rng.uniform(-2.5, 2.5)
    img = img.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(236, 233, 226))
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.2, 0.8)))
    noise = Image.effect_noise(img.size, rng.uniform(6, 14)).convert("RGB")
    img = Image.blend(img, noise, 0.06)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=rng.randint(70, 88))
    cx, cy, a = img.width / 2, img.height / 2, math.radians(angle)

    def turn(x: float, y: float) -> tuple[float, float]:  # PIL rotates counter-clockwise for positive angles
        dx, dy = x * scale - cx, y * scale - cy
        return (cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(a))

    corners = [[turn(x0, y0), turn(x1, y0), turn(x1, y1), turn(x0, y1)] for p in placed for (x0, y0, x1, y1) in p.words]
    return out.getvalue(), corners, img.size


def labels(document: str, placed: list[Placed], polygons: Corners, size: tuple[float, float]) -> dict:
    """Document Intelligence's labels file for one document: each field's text and its words' positions,
    as fractions of the page's width and height."""
    w, h = size
    out, i = [], 0
    for p in placed:
        boxes = []
        for _ in p.words:
            boxes.append([round(v, 6) for x, y in polygons[i] for v in (x / w, y / h)])
            i += 1
        out.append({"label": p.field, "value": [{"page": 1, "text": p.text, "boundingBoxes": boxes}]})
    return {
        "$schema": "https://schema.cognitiveservices.azure.com/formrecognizer/2021-03-01/labels.schema.json",
        "document": document,
        "labels": out,
    }


def fields_file() -> dict:
    from loanlens.t4 import FIELDS

    return {
        "$schema": "https://schema.cognitiveservices.azure.com/formrecognizer/2021-03-01/fields.schema.json",
        "fields": [{"fieldKey": k, "fieldType": "string", "fieldFormat": "not-specified"} for k in FIELDS],
        "definitions": {},
    }


def with_truth(data: bytes, fields: dict[str, str]) -> bytes:
    return data + b"\n" + TRAILER + base64.b64encode(json.dumps(fields, ensure_ascii=False).encode()) + b"\n"


def truth(data: bytes) -> dict[str, str] | None:
    """The true values a LoanLens sample carries after its end-of-file marker, or None for any other file."""
    found = re.search(rb"%loanlens-sample ([A-Za-z0-9+/=]+)\s*$", data[-8192:])
    if not found:
        return None
    try:
        value = json.loads(base64.b64decode(found.group(1)))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def make(rng: random.Random, name: str, photo: bool) -> tuple[str, bytes, dict, dict[str, str]]:
    """One sample: its file name, bytes, labels and true values."""
    fields = invent(rng)
    pdf, placed = draw(fields, rng)
    if photo:
        image, polygons, size = photograph(pdf, placed, rng)
        filename = f"{name}.jpg"
        return filename, with_truth(image, fields), labels(filename, placed, polygons, size), fields
    polygons = [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)] for p in placed for (x0, y0, x1, y1) in p.words]
    filename = f"{name}.pdf"
    return filename, with_truth(pdf, fields), labels(filename, placed, polygons, (PAGE_W, PAGE_H)), fields


def write_set(folder: Path, count: int, rng: random.Random, prefix: str, photo_share: float = 0.35) -> list[dict]:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fields.json").write_text(json.dumps(fields_file(), indent=2) + "\n")
    rows = []
    for i in range(1, count + 1):
        filename, data, label, fields = make(rng, f"{prefix}-{i:03d}", rng.random() < photo_share)
        (folder / filename).write_bytes(data)
        (folder / f"{filename}.labels.json").write_text(json.dumps(label, indent=2, ensure_ascii=False) + "\n")
        rows.append({"document": filename, **fields})
    (folder / "truth.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate sample T4 slips with Document Intelligence labels.")
    parser.add_argument("--out", type=Path, default=Path("data/t4"))
    parser.add_argument("--train", type=int, default=40)
    parser.add_argument("--test", type=int, default=20)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)
    train = write_set(args.out / "train", args.train, rng, "train")
    test = write_set(args.out / "test", args.test, rng, "test")
    print(f"Wrote {len(train)} training and {len(test)} test slips to {args.out}/ (seed {args.seed}).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
