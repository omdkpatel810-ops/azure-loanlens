"""The sample T4 generator: honest about being samples, consistent figures, and labels that point at the values."""

from __future__ import annotations

import io
import json
import random

import pymupdf
import pytest

from loanlens import samples, t4


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("t4")
    assert samples.main(["--out", str(out), "--train", "8", "--test", "4", "--seed", "7"]) == 0
    return out


def test_two_sets_with_labels_and_truth(dataset):
    for name, count in (("train", 8), ("test", 4)):
        folder = dataset / name
        docs = sorted(p for p in folder.iterdir() if p.suffix in (".pdf", ".jpg"))
        assert len(docs) == count
        assert all((folder / f"{d.name}.labels.json").exists() for d in docs)
        assert len((folder / "truth.jsonl").read_text().splitlines()) == count
        fields = json.loads((folder / "fields.json").read_text())
        assert [f["fieldKey"] for f in fields["fields"]] == list(t4.FIELDS)


def test_the_same_seed_makes_the_same_slips(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    samples.write_set(a, 3, random.Random(1), "x", photo_share=0)
    samples.write_set(b, 3, random.Random(1), "x", photo_share=0)
    assert (a / "truth.jsonl").read_text() == (b / "truth.jsonl").read_text()


def test_every_slip_says_it_is_a_sample(dataset):
    pdf = next((dataset / "train").glob("*.pdf"))
    text = pymupdf.open(pdf).load_page(0).get_text()
    assert "SAMPLE · NOT A REAL TAX SLIP" in text and "Not issued by the Canada Revenue Agency" in text
    assert "SPÉCIMEN" in text


def test_social_insurance_numbers_start_with_zero_and_are_never_labelled(dataset):
    pdf = next((dataset / "train").glob("*.pdf"))
    text = pymupdf.open(pdf).load_page(0).get_text()
    sin = next(line for line in text.splitlines() if line.count(" ") == 2 and line.replace(" ", "").isdigit())
    assert sin.startswith("0")  # never issued by the government
    labels = json.loads(pdf.with_name(f"{pdf.name}.labels.json").read_text())
    assert "sin" not in {label["label"] for label in labels["labels"]}


def test_figures_follow_each_years_rules():
    rng = random.Random(3)
    for _ in range(300):
        fields = samples.invent(rng)
        assert t4.check({k: t4.normalize(k, v) for k, v in fields.items()}) == []
        assert ("box17" in fields) == (fields["province"] == "QC") and ("box16" in fields) != ("box17" in fields)


def test_labels_point_at_the_printed_values(dataset):
    for pdf in (dataset / "train").glob("*.pdf"):
        page = pymupdf.open(pdf).load_page(0)
        # (x0, y0, x1, y1, word, ...), leaving out the big diagonal watermark's letters
        printed = [w for w in page.get_text("words") if w[3] - w[1] < 20]
        labels = json.loads(pdf.with_name(f"{pdf.name}.labels.json").read_text())
        for label in labels["labels"]:
            (value,) = label["value"]
            found = []
            for box in value["boundingBoxes"]:
                x0, y0 = box[0] * samples.PAGE_W, box[1] * samples.PAGE_H
                x1, y1 = box[4] * samples.PAGE_W, box[5] * samples.PAGE_H
                found += [w[4] for w in printed if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1]
            assert " ".join(found) == value["text"], (pdf.name, label["label"])


def test_photos_have_labels_inside_the_image(dataset):
    photos = list((dataset / "train").glob("*.jpg")) + list((dataset / "test").glob("*.jpg"))
    assert photos, "with this seed some slips are photos"
    for photo in photos:
        labels = json.loads(photo.with_name(f"{photo.name}.labels.json").read_text())
        coords = [v for label in labels["labels"] for box in label["value"][0]["boundingBoxes"] for v in box]
        assert all(0 <= v <= 1 for v in coords)
        from PIL import Image

        assert Image.open(io.BytesIO(photo.read_bytes())).format == "JPEG"


def test_amount_styles():
    assert [samples.printed("52340.5", s) for s in range(3)] == ["52340.50", "52,340.50", "52 340,50"]


def test_each_file_carries_its_truth(dataset):
    rows = [json.loads(line) for line in (dataset / "test" / "truth.jsonl").read_text().splitlines()]
    for row in rows:
        data = (dataset / "test" / row["document"]).read_bytes()
        assert samples.truth(data) == {k: v for k, v in row.items() if k != "document"}
    assert samples.truth(b"%PDF-1.7 nothing here") is None
    assert samples.truth(b"x\n%loanlens-sample bm90IGpzb24=\n") is None  # "not json"
    assert samples.truth(b"x\n%loanlens-sample WzFd\n") is None  # [1]
