"""What one loan application costs to run.

    python -m loanlens.costs --export costs.csv --month 2027-01              # from Guardrails' cost export
    python -m loanlens.costs --export costs.csv --month 2027-01 --di-per-1000-pages 10 --openai-per-million-tokens 0.6

Two numbers:
- the actual cost: everything billed to LoanLens' resource group that month (from the FOCUS-format cost
  export Guardrails already writes for every team), divided by the applications started that month;
- the cost of one more application: the pages Document Intelligence read and the tokens Azure OpenAI used,
  times the prices you give. Fixed costs (the registry, the idle environment) don't grow with volume, so this
  is what each application adds at scale.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from loanlens.store import Store

RESOURCE_GROUP = "rg-loanlens-prod"


@dataclass(frozen=True)
class Usage:
    applications: int
    documents: int
    pages: int  # read by Document Intelligence
    tokens: int  # used writing summaries


def usage(store: Store, month: str) -> Usage:
    apps = [a for a in store.all_applications() if a.created_at.startswith(month)]
    documents = pages = tokens = 0
    for app in apps:
        for doc in store.documents(app.id):
            documents += 1
            reading = store.reading(app.id, doc.id) or {}
            pages += int(reading.get("pages") or 0) if "model_id" in reading else 0
        for lang in ("en", "fr"):
            tokens += int((store.summary_record(app.id, lang) or {}).get("tokens") or 0)
    return Usage(len(apps), documents, pages, tokens)


def billed(export: Path, month: str, resource_group: str = RESOURCE_GROUP) -> dict[str, Decimal]:
    """Cost by service for one resource group and month, from a FOCUS-format cost export."""
    by_service: dict[str, Decimal] = defaultdict(Decimal)
    with export.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("ChargePeriodStart", "").startswith(month):
                continue
            group = row.get("x_ResourceGroupName") or row.get("ResourceGroupName") or ""
            if not group and "/resourcegroups/" in (row.get("ResourceId") or "").lower():
                group = row["ResourceId"].lower().split("/resourcegroups/")[1].split("/")[0]
            if group.lower() != resource_group.lower():
                continue
            by_service[row.get("ServiceName") or "Other"] += Decimal(row.get("BilledCost") or "0")
    return dict(by_service)


def report(use: Usage, by_service: dict[str, Decimal], month: str, di_price=None, openai_price=None) -> str:
    total = sum(by_service.values(), Decimal(0))
    lines = [
        f"**LoanLens, {month}: {use.applications} applications, {use.documents} documents, {use.pages} pages read, "
        f"{use.tokens:,} summary tokens.**",
        "",
    ]
    if use.applications:
        lines.append(f"Actual cost: ${total:,.2f} billed, **${total / use.applications:,.2f} per application**.")
    else:
        lines.append(f"Actual cost: ${total:,.2f} billed, and no applications started, so no cost per application.")
    if by_service:
        lines += ["", "| Service | Billed |", "|---|---|"]
        lines += [f"| {name} | ${cost:,.2f} |" for name, cost in sorted(by_service.items(), key=lambda kv: -kv[1])]
    if di_price is not None and openai_price is not None and use.applications:
        marginal = (
            Decimal(use.pages) * Decimal(str(di_price)) / 1000 + Decimal(use.tokens) * Decimal(str(openai_price)) / 1_000_000
        )
        lines += ["", f"Cost of one more application (pages and tokens only): **${marginal / use.applications:,.4f}**."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, store: Store | None = None) -> int:
    parser = argparse.ArgumentParser(description="What one loan application costs.")
    parser.add_argument("--export", type=Path, required=True, help="a FOCUS-format cost export (CSV)")
    parser.add_argument("--month", required=True, help="YYYY-MM")
    parser.add_argument("--resource-group", default=RESOURCE_GROUP)
    parser.add_argument("--di-per-1000-pages", type=float, help="Document Intelligence's price for custom extraction")
    parser.add_argument("--openai-per-million-tokens", type=float, help="the summary model's blended price")
    args = parser.parse_args(argv)
    if store is None:  # pragma: no cover - needs Azure
        from loanlens.settings import Settings
        from loanlens.store import AzureStore

        settings = Settings.from_env()
        store = AzureStore(account=settings.storage_account, connection=settings.storage_connection)
    print(
        report(
            usage(store, args.month),
            billed(args.export, args.month, args.resource_group),
            args.month,
            args.di_per_1000_pages,
            args.openai_per_million_tokens,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
