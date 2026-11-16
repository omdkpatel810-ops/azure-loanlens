"""Summaries: the facts they may use, the template in both languages, and the numbers check on the model."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from loanlens import summary
from loanlens.models import Application, Document

APP = Application("app-1", "u", "Amélie Tremblay", "mortgage", 350_000, "collecting documents", "2026-11-16")
DOCS = [
    Document("d1", "app-1", "t4", "a.pdf", "application/pdf", 1, "x", "read", "t1"),
    Document("d2", "app-1", "t4", "b.pdf", "application/pdf", 1, "y", "needs review", "t2"),
    Document("d3", "app-1", "t4", "c.pdf", "application/pdf", 1, "z", "read", "t3"),
    Document("d4", "app-1", "pay_stub", "d.pdf", "application/pdf", 1, "w", "needs review", "t4"),
]
VALUES = {
    "d1": {"year": "2025", "employer_name": "PRAIRIE GRAIN INC.", "box14": "42000.00", "box22": "6100.00", "province": "MB"},
    "d2": {"year": "2025", "employer_name": "ASPEN FOODS LTD.", "box14": "20000.00", "box22": "2500.00", "province": "MB"},
    "d3": {"year": "2024", "employer_name": "PRAIRIE GRAIN INC.", "box14": "39000.00", "box22": "5600.00", "province": "MB"},
}
FACTS = summary.facts(APP, DOCS, VALUES)


def test_facts_use_the_latest_year_and_every_employer_in_it():
    assert FACTS["latest_year"] == "2025" and FACTS["latest_year_income"] == "62000.00"
    assert FACTS["employers_latest_year"] == 2 and FACTS["t4_slips"] == 3 and FACTS["amount_to_income"] == "5.6"
    assert FACTS["waiting_for_a_person"] == 2 and FACTS["documents"] == 4


def test_no_t4_yet():
    f = summary.facts(APP, DOCS[3:], {})
    assert f["latest_year"] is None and f["amount_to_income"] is None
    assert summary.template(f, "en").endswith(
        "No T4 slip has been read yet. Documents waiting for a person to check them: 1 of 1."
    )
    assert "Aucun feuillet T4 n’a encore été lu." in summary.template(f, "fr")


def test_the_template_in_both_languages():
    en = summary.template(FACTS, "en")
    assert en == (
        "Amélie Tremblay is applying for a mortgage of $350,000. Their 2025 T4 slips show employment income of $62,000 "
        "(2 employers); the amount requested is 5.6 times that income. Documents waiting for a person to check them: 2 of 4."
    )
    fr = summary.template(FACTS, "fr")
    assert "demande un prêt hypothécaire de 350 000 $" in fr and "62 000 $ (2 employeurs)" in fr and "5,6 fois" in fr
    one = summary.facts(APP, DOCS[2:3], VALUES)
    assert "(1 employer)" in summary.template(one, "en") and "(1 employeur)" in summary.template(one, "fr")
    assert summary.template({**one, "waiting_for_a_person": 0}, "fr").endswith("ce revenu.")


@pytest.mark.parametrize(
    ("text", "lang", "bad"),
    [
        ("Income was $62,000 in 2025; the loan is 5.6 times that.", "en", []),
        ("Income was $64,000.", "en", [Decimal("64000")]),
        ("Revenu de 62 000 $ en 2025, soit 5,6 fois.", "fr", []),
        ("Revenu de 62 500 $.", "fr", [Decimal("62500")]),
        ("Two T4 slips and a T4A were provided.", "en", []),  # digits inside words aren't numbers
        ("A ratio of 1.2.3 makes no sense.", "en", []),  # not a number at all
        ("Tax deducted was $6,100.00 on the first slip.", "en", []),
    ],
)
def test_the_numbers_check(text, lang, bad):
    assert summary.unsupported_numbers(text, FACTS, lang) == bad


class Chat:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


def test_the_model_writes_it_when_its_numbers_check_out():
    client = Chat("Amélie Tremblay asks for $350,000, 5.6 times her 2025 income of $62,000; 2 documents still need a look.")
    out = summary.OpenAISummarizer("https://oai", "summaries", client=client).summarize(FACTS, "en")
    assert out.source == "model" and out.text.startswith("Amélie Tremblay asks")
    call = client.calls[0]
    assert call["model"] == "summaries" and call["temperature"] == 0
    assert "Use only the facts in the JSON" in call["messages"][0]["content"]
    assert '"latest_year_income": "62000.00"' in call["messages"][1]["content"]


def test_french_asks_for_french():
    client = Chat("Amélie Tremblay demande 350 000 $, soit 5,6 fois son revenu de 62 000 $ en 2025.")
    out = summary.OpenAISummarizer("https://oai", "summaries", client=client).summarize(FACTS, "fr")
    assert out.source == "model" and "Rédige un résumé en français" in client.calls[0]["messages"][0]["content"]


def test_a_made_up_number_means_the_template():
    client = Chat("Amélie earns $85,000 and should be approved.")
    out = summary.OpenAISummarizer("https://oai", "summaries", client=client).summarize(FACTS, "en")
    assert out.source == "template" and out.text == summary.template(FACTS, "en")
    assert "numbers that aren't in the facts (85000)" in out.note


def test_an_empty_reply_or_an_outage_means_the_template():
    assert summary.OpenAISummarizer("e", "d", client=Chat("")).summarize(FACTS, "en").source == "template"
    out = summary.OpenAISummarizer("e", "d", client=Chat(error=TimeoutError())).summarize(FACTS, "fr")
    assert out.source == "template" and out.text == summary.template(FACTS, "fr") and "TimeoutError" in out.note


def test_the_template_summarizer_and_fingerprints():
    assert summary.TemplateSummarizer().summarize(FACTS, "en").source == "template"
    assert summary.fingerprint(FACTS) == summary.fingerprint(dict(FACTS))
    assert summary.fingerprint(FACTS) != summary.fingerprint({**FACTS, "amount": 1})


def test_numbers_check_without_any_t4():
    f = summary.facts(APP, DOCS[3:], {})
    assert summary.unsupported_numbers(summary.template(f, "en"), f, "en") == []
