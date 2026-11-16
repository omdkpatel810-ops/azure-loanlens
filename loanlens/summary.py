"""The short summary an underwriter reads first, in English or French.

Facts come only from what the worker read and what underwriters confirmed or corrected: never from the
documents themselves, so a model writing the summary can't see anything it shouldn't. Azure OpenAI
writes it; every number in what it writes must be one of the facts' numbers, or the summary is thrown
away and a plain template summary is shown instead. The template is also what runs on a computer
without Azure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol

from loanlens.models import Application, Document

LOAN_NAMES = {
    "en": {"mortgage": "a mortgage", "car": "a car loan", "personal": "a personal loan"},
    "fr": {"mortgage": "un prêt hypothécaire", "car": "un prêt auto", "personal": "un prêt personnel"},
}


@dataclass(frozen=True)
class Summary:
    text: str
    source: str  # "model": written by Azure OpenAI, numbers checked; "template": LoanLens' own wording
    note: str = ""


def money(value: Decimal | int, lang: str) -> str:
    whole = int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{whole:,}".replace(",", " ") + " $" if lang == "fr" else f"${whole:,}"


def facts(app: Application, docs: list[Document], values: dict[str, dict[str, str | None]]) -> dict:
    """What the summary may say. `values` maps each T4's document ID to its current field values
    (corrected by an underwriter where they were)."""
    slips = []
    for doc in docs:
        v = values.get(doc.id)
        if doc.kind == "t4" and v and v.get("box14"):
            slips.append({
                "year": v.get("year"), "employer": v.get("employer_name"), "income": v.get("box14"),
                "tax": v.get("box22"), "province": v.get("province"),
            })  # fmt: skip
    years = sorted({s["year"] for s in slips if s["year"]})
    latest = years[-1] if years else None
    income = sum((Decimal(s["income"]) for s in slips if s["year"] == latest), Decimal(0)) if latest else Decimal(0)
    return {
        "applicant": app.applicant_name,
        "loan_type": app.loan_type,
        "amount": app.amount,
        "documents": len(docs),
        "t4_slips": len(slips),
        "slips": slips,
        "latest_year": latest,
        "latest_year_income": f"{income:.2f}" if latest else None,
        "employers_latest_year": len({s["employer"] for s in slips if s["year"] == latest}),
        "amount_to_income": f"{Decimal(app.amount) / income:.1f}" if income else None,
        "waiting_for_a_person": sum(d.status in ("uploaded", "reading", "needs review", "failed") for d in docs),
    }


def fingerprint(f: dict) -> str:
    return hashlib.sha256(json.dumps(f, sort_keys=True).encode()).hexdigest()[:16]


def template(f: dict, lang: str) -> str:
    loan = LOAN_NAMES[lang][f["loan_type"]]
    amount = money(f["amount"], lang)
    if lang == "fr":
        parts = [f"{f['applicant']} demande {loan} de {amount}."]
        if f["latest_year"]:
            income, n = money(Decimal(f["latest_year_income"]), lang), f["employers_latest_year"]
            ratio = f["amount_to_income"].replace(".", ",")
            parts.append(
                f"Ses feuillets T4 de {f['latest_year']} indiquent un revenu d’emploi de {income} "
                f"({n} employeur{'s' if n > 1 else ''}); le montant demandé représente {ratio} fois ce revenu."
            )
        else:
            parts.append("Aucun feuillet T4 n’a encore été lu.")
        if f["waiting_for_a_person"]:
            parts.append(f"Documents en attente de vérification : {f['waiting_for_a_person']} sur {f['documents']}.")
        return " ".join(parts)
    parts = [f"{f['applicant']} is applying for {loan} of {amount}."]
    if f["latest_year"]:
        income, n = money(Decimal(f["latest_year_income"]), lang), f["employers_latest_year"]
        parts.append(
            f"Their {f['latest_year']} T4 slips show employment income of {income} "
            f"({n} employer{'s' if n > 1 else ''}); the amount requested is {f['amount_to_income']} times that income."
        )
    else:
        parts.append("No T4 slip has been read yet.")
    if f["waiting_for_a_person"]:
        parts.append(f"Documents waiting for a person to check them: {f['waiting_for_a_person']} of {f['documents']}.")
    return " ".join(parts)


# A number standing on its own: digits inside a word, like the 4 in "T4", don't count. Only French
# uses spaces between thousands, so only French numbers may contain them.
NUMBER = {
    "en": re.compile(r"(?<![A-Za-z0-9])\d[\d,.]*\d(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d(?![A-Za-z0-9])"),
    "fr": re.compile(r"(?<![A-Za-z0-9])\d[\d,.\s\u00a0\u202f]*\d(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d(?![A-Za-z0-9])"),
}


def numbers_in(text: str, lang: str) -> list[Decimal]:
    """Every number in a piece of text, read the way that language writes numbers."""
    found = []
    for raw in NUMBER[lang].findall(text):
        s = re.sub(r"[\s\u00a0\u202f]", "", raw)
        # French: 62 000,50 and 4,2. English: 62,000.50 and 4.2.
        s = s.replace(".", "").replace(",", ".") if lang == "fr" else s.replace(",", "")
        try:
            found.append(Decimal(s))
        except InvalidOperation:
            continue
    return found


def allowed_numbers(f: dict) -> set[Decimal]:
    values: list = [f["amount"], f["documents"], f["t4_slips"], f["employers_latest_year"], f["waiting_for_a_person"]]
    values += [f["latest_year"], f["latest_year_income"], f["amount_to_income"]]
    for s in f["slips"]:
        values += [s["year"], s["income"], s["tax"]]
    allowed = set()
    for v in values:
        if v is None:
            continue
        d = Decimal(str(v))
        allowed |= {d, d.quantize(Decimal(1), rounding=ROUND_HALF_UP), d.quantize(Decimal("0.01"))}
    return allowed


def unsupported_numbers(text: str, f: dict, lang: str) -> list[Decimal]:
    allowed = allowed_numbers(f)
    return [n for n in numbers_in(text, lang) if n not in allowed]


class Summarizer(Protocol):
    def summarize(self, f: dict, lang: str) -> Summary: ...


class TemplateSummarizer:
    def summarize(self, f: dict, lang: str) -> Summary:
        return Summary(template(f, lang), "template")


PROMPT = {
    "en": "Write a summary in English, at most three sentences, for a credit union underwriter.",
    "fr": "Rédige un résumé en français, trois phrases au plus, pour un souscripteur de caisse populaire. "
    "Écris les nombres à la française (62 000 $, 4,2).",
}
RULES = (
    "Use only the facts in the JSON. Don't add numbers, percentages or judgments that aren't in it, and don't "
    "recommend a decision. Mention the amount requested, the latest year's employment income, the amount-to-income "
    "figure, and any documents still waiting for a person."
)


class OpenAISummarizer:
    """Azure OpenAI, signed in to with the dashboard's managed identity, with a numbers check."""

    def __init__(self, endpoint: str, deployment: str, *, client=None):
        if client is None:  # pragma: no cover - only in Azure
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AzureOpenAI

            token = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
            client = AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=token, api_version="2024-10-21")
        self.client, self.deployment = client, deployment

    def summarize(self, f: dict, lang: str) -> Summary:
        fallback = template(f, lang)
        try:
            reply = self.client.chat.completions.create(
                model=self.deployment,
                temperature=0,
                max_tokens=220,
                messages=[
                    {"role": "system", "content": f"{PROMPT[lang]} {RULES}"},
                    {"role": "user", "content": json.dumps(f, ensure_ascii=False)},
                ],
            )
            text = (reply.choices[0].message.content or "").strip()
        except Exception as e:  # any failure: the template still tells the underwriter what they need
            return Summary(fallback, "template", f"Azure OpenAI wasn't available ({type(e).__name__})")
        bad = unsupported_numbers(text, f, lang)
        if not text or bad:
            return Summary(
                fallback, "template", f"The model's summary had numbers that aren't in the facts ({', '.join(map(str, bad))})"
            )
        return Summary(text, "model")
