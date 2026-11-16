"""How well the model reads T4s: run it over the held-out test slips and compare with the truth.

    python -m loanlens.evaluate data/t4/test --endpoint https://<name>.cognitiveservices.azure.com --model loanlens-t4-v1
    python -m loanlens.evaluate data/t4/test --local      # the stand-in: always right, so it only checks the plumbing

The numbers that matter most for a lending team:
- field accuracy: of every value on the test slips, how many the model read exactly right;
- silent errors: slips with a wrong value that the worker would NOT have sent to a person. These are the
  dangerous ones; everything flagged gets a second look anyway.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from loanlens import t4
from loanlens.extract import ExtractionFailed, Extractor, SampleReader

ROOT = Path(__file__).resolve().parent.parent


def evaluate(folder: Path, extractor: Extractor, confidence: float = 0.8, clock=time.monotonic) -> dict:
    rows = [json.loads(line) for line in (folder / "truth.jsonl").read_text().splitlines() if line.strip()]
    right = dict.fromkeys(t4.FIELDS, 0)
    seen = dict.fromkeys(t4.FIELDS, 0)
    docs, seconds = [], []
    for row in rows:
        path = folder / row["document"]
        truth = {k: t4.normalize(k, row.get(k)) for k in t4.FIELDS}
        started = clock()
        try:
            extraction = extractor.extract(path.read_bytes(), "image/jpeg" if path.suffix == ".jpg" else "application/pdf")
        except ExtractionFailed as e:
            docs.append(
                {
                    "document": row["document"],
                    "photo": path.suffix == ".jpg",
                    "wrong": ["(not read)"],
                    "flagged": True,
                    "error": str(e),
                }
            )
            for k in t4.FIELDS:
                seen[k] += truth[k] is not None
            continue
        seconds.append(clock() - started)
        got = {k: t4.normalize(k, extraction.fields[k].value) if k in extraction.fields else None for k in t4.FIELDS}
        wrong = []
        for k in t4.FIELDS:
            if truth[k] is None and got[k] is None:
                continue
            seen[k] += 1
            if truth[k] == got[k]:
                right[k] += 1
            else:
                wrong.append(k)
        unsure = [k for k, f in extraction.fields.items() if got.get(k) is not None and f.confidence < confidence]
        flagged = bool(t4.check(got) or unsure)
        docs.append({"document": row["document"], "photo": path.suffix == ".jpg", "wrong": wrong, "flagged": flagged})

    def share(part: int, whole: int) -> float:
        return round(100 * part / whole, 1) if whole else 0.0

    total_seen, total_right = sum(seen.values()), sum(right.values())
    by_kind = {}
    for kind, is_photo in (("pdf", False), ("photo", True)):
        group = [d for d in docs if d["photo"] == is_photo]
        by_kind[kind] = {"documents": len(group), "all_right": share(sum(not d["wrong"] for d in group), len(group))}
    return {
        "model": getattr(extractor, "model_id", "unknown"),
        "documents": len(docs),
        "field_accuracy": share(total_right, total_seen),
        "fields": {k: share(right[k], seen[k]) for k in t4.FIELDS if seen[k]},
        "documents_all_right": share(sum(not d["wrong"] for d in docs), len(docs)),
        "flagged_for_a_person": share(sum(d["flagged"] for d in docs), len(docs)),
        "silent_errors": [d["document"] for d in docs if d["wrong"] and not d["flagged"]],
        "by_kind": by_kind,
        "seconds_per_document": round(sum(seconds) / len(seconds), 2) if seconds else None,
        "documents_detail": docs,
    }


def to_markdown(result: dict) -> str:
    lines = [
        f"**{result['model']}: {result['field_accuracy']:g}% of fields read exactly right** on {result['documents']} test slips; "
        f"{result['documents_all_right']:g}% of slips entirely right; {result['flagged_for_a_person']:g}% sent to a person; "
        f"{len(result['silent_errors'])} silent errors.",
        "",
        "| Field | Accuracy |",
        "|---|---|",
    ]
    lines += [f"| {t4.FIELDS[k]} | {v:g}% |" for k, v in result["fields"].items()]
    kinds = result["by_kind"]
    lines += ["", f"Slips entirely right: {kinds['pdf']['all_right']:g}% of {kinds['pdf']['documents']} PDFs, "
              f"{kinds['photo']['all_right']:g}% of {kinds['photo']['documents']} photos."]  # fmt: skip
    if result["silent_errors"]:
        lines.append(f"Silent errors: {', '.join(result['silent_errors'])}.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, extractor: Extractor | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure how well the T4 model reads the test slips.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--local", action="store_true", help="use the local stand-in instead of Azure")
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--confidence", type=float, default=0.8)
    parser.add_argument("--save", type=Path, help="where to write the full results as JSON")
    args = parser.parse_args(argv)
    if extractor is None:
        if args.local:
            extractor = SampleReader()
        elif args.endpoint and args.model:  # pragma: no cover - needs Azure
            from loanlens.extract import DocumentIntelligence

            extractor = DocumentIntelligence(args.endpoint, args.model)
        else:
            parser.error("give --endpoint and --model, or --local")
    result = evaluate(args.folder, extractor, args.confidence)
    print(to_markdown(result))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Saved {args.save}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
