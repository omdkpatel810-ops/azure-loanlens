"""The burst test: many applicants upload at once. How fast does every T4 get read?

    python -m loanlens.burst --applications 100 --yes          # in Azure, signed in as the operator
    python -m loanlens.burst --applications 30 --yes --local   # against Azurite, with a local worker running

It starts N applications, each with a generated sample T4, through the same storage code the portal uses,
so every upload raises the same event and goes through the same queue, worker and model. Then it watches
until every document is read and reports throughput, the time from upload to read (median and 95th
percentile) and anything that didn't come out clean. Test applicants are named "Burst Test n" and have
IDs starting with "burst-", so they're easy to find and remove.

Portal pages themselves are load tested separately, with Locust (loadtest/locustfile.py).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loanlens import samples
from loanlens.models import Document, User
from loanlens.store import Store
from loanlens.worker import DONE

ROOT = Path(__file__).resolve().parent.parent


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))  # nearest rank
    return round(ordered[rank], 2)


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def run(
    store: Store,
    count: int,
    *,
    timeout_minutes: float = 30,
    poll_seconds: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    seed: int = 7,
) -> dict:
    rng = random.Random(seed)
    run_id = uuid.uuid4().hex[:6]
    started = clock()
    docs: list[Document] = []
    for i in range(1, count + 1):
        name = f"Burst Test {i}"
        app = store.create_application(User(f"burst-{run_id}-{i}", name), "car", 25_000)
        fields = {**samples.invent(rng), "employee_name": name}
        slip = samples.with_truth(samples.draw(fields, rng)[0], fields)
        docs.append(store.add_document(app, "t4", f"burst-{i}.pdf", "application/pdf", ".pdf", slip))
    uploaded = clock() - started

    pending = {d.id: d for d in docs}
    finished: dict[str, Document] = {}
    deadline = clock() + timeout_minutes * 60
    while pending and clock() < deadline:
        sleep(poll_seconds)
        for doc_id, doc in list(pending.items()):
            current = store.document(doc.application_id, doc_id)
            if current and current.status in DONE:
                finished[doc_id] = current
                del pending[doc_id]

    waits, last_read = [], None
    for doc in finished.values():
        reading = store.reading(doc.application_id, doc.id) or {}
        if reading.get("read_at"):
            read_at = _time(reading["read_at"])
            waits.append(max(0.0, (read_at - _time(doc.uploaded_at)).total_seconds()))
            last_read = max(last_read or read_at, read_at)
    first_upload = min(_time(d.uploaded_at) for d in docs) if docs else None
    span = (last_read - first_upload).total_seconds() if last_read and first_upload else None
    return {
        "run": run_id,
        "applications": count,
        "upload_seconds": round(uploaded, 1),
        "outcomes": dict(Counter(d.status for d in finished.values())) | ({"not done in time": len(pending)} if pending else {}),
        "seconds_to_read": {
            "median": percentile(waits, 50),
            "p95": percentile(waits, 95),
            "max": round(max(waits), 2) if waits else None,
        },
        "per_minute": round(len(waits) / span * 60, 1) if span else None,
    }


def to_markdown(result: dict) -> str:
    waits, outcomes = result["seconds_to_read"], result["outcomes"]
    head = f"**Burst of {result['applications']} applications: "
    head += f"{result['per_minute']:g} T4s read a minute; " if result["per_minute"] else ""
    head += (
        f"median {waits['median']:g} s and 95th percentile {waits['p95']:g} s from upload to read.**"
        if waits["median"] is not None
        else "nothing read.**"
    )
    lines = [head, "", "| Outcome | Documents |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(outcomes.items())]
    return (
        "\n".join(lines)
        + f"\n\nUploading took {result['upload_seconds']:g} s. Test applicants' IDs start with burst-{result['run']}-.\n"
    )


def main(argv: list[str] | None = None, store: Store | None = None, **kw) -> int:
    parser = argparse.ArgumentParser(description="Upload many applications at once and time the pipeline.")
    parser.add_argument("--applications", type=int, default=50)
    parser.add_argument("--timeout-minutes", type=float, default=30)
    parser.add_argument("--local", action="store_true", help="use Azurite instead of Azure")
    parser.add_argument("--yes", action="store_true", help="really upload")
    args = parser.parse_args(argv)
    if not args.yes:
        print(
            f"Would start {args.applications} test applications, each with a sample T4, and time how long the worker takes. "
            "Run again with --yes."
        )
        return 0
    if store is None:  # pragma: no cover - needs Azure or Azurite
        from loanlens.settings import AZURITE, Settings
        from loanlens.store import AzureStore

        settings = Settings.from_env()
        store = (
            AzureStore(connection=AZURITE, emulate_events=True) if args.local else AzureStore(account=settings.storage_account)
        )
    result = run(store, args.applications, timeout_minutes=args.timeout_minutes, **kw)
    print(to_markdown(result))
    out = ROOT / "results" / f"burst-{datetime.now(UTC):%Y-%m-%dT%H%M}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Saved {out}")
    return 0 if not result["outcomes"].get("not done in time") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
