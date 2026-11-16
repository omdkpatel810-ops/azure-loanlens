"""The underwriter dashboard: who gets in, what they see, corrections, decisions and the audit trail."""

from __future__ import annotations

import base64
import json
import random
import re

import pytest
from fastapi.testclient import TestClient

from loanlens import samples
from loanlens.dashboard import create_dashboard
from loanlens.extract import SampleReader
from loanlens.models import User
from loanlens.queues import MemoryQueue
from loanlens.settings import Settings
from loanlens.store import MemoryStore
from loanlens.summary import Summary, TemplateSummarizer
from loanlens.worker import Worker
from tests.conftest import PNG, principal, text


def staff(user_id="staff-1", name="Samira Haddad", *roles):
    return {"X-MS-CLIENT-PRINCIPAL-ID": user_id, "X-MS-CLIENT-PRINCIPAL": principal(*roles, name=name)}


class Desk:
    def __init__(self, store, headers, summarizer=None, settings=None):
        app = create_dashboard(store, settings or Settings(auth="easyauth"), summarizer or TemplateSummarizer())
        self.client = TestClient(app, base_url="https://dashboard.test", headers=headers, follow_redirects=False)

    def token(self, path="/"):
        return re.search(r'name="csrf" value="([^"]+)"', self.client.get(path).text).group(1)

    def post(self, path, data, page):
        return self.client.post(path, data={**data, "csrf": self.token(page)})


@pytest.fixture
def lending():
    """An application with a sample T4 the worker has read, and a pay stub."""
    store = MemoryStore()
    app = store.create_application(User("applicant-1", "Amélie Tremblay"), "mortgage", 350_000)
    rng = random.Random(3)
    fields = {**samples.invent(rng), "province": "MB", "year": "2025", "employee_name": "Amelie Tremblay"}
    fields.pop("box17", None)
    t4 = store.add_document(
        app, "t4", "T4 2025.pdf", "application/pdf", ".pdf", samples.with_truth(samples.draw(fields, rng)[0], fields)
    )
    stub = store.add_document(app, "pay_stub", "stub.png", "image/png", ".png", PNG)
    Worker(store, MemoryQueue(store.queue), SampleReader()).run_once()
    return store, app, t4, stub, fields


@pytest.fixture
def samira(lending):
    return Desk(lending[0], staff("staff-1", "Samira Haddad", "Underwriter"))


def test_only_underwriters_get_in(lending):
    store = lending[0]
    assert (
        TestClient(create_dashboard(store, Settings(), TemplateSummarizer()), follow_redirects=False).get("/").headers["location"]
        == "/.auth/login/aad"
    )
    kim = Desk(store, staff("staff-2", "Kim Nguyen"))
    response = kim.client.get("/")
    assert response.status_code == 403 and "you don't have the Underwriter role" in text(response)
    app_id = lending[1].id
    assert kim.client.get(f"/applications/{app_id}").status_code == 403
    assert kim.client.get(f"/applications/{app_id}/documents/{lending[2].id}/file").status_code == 403
    assert kim.client.post(f"/applications/{app_id}/decision", data={"decision": "verified"}).status_code == 403
    assert kim.client.post(f"/applications/{app_id}/documents/{lending[2].id}/review", data={}).status_code == 403


def test_the_queue(samira, lending):
    page = text(samira.client.get("/"))
    assert "Amélie Tremblay" in page and "Mortgage · $350,000" in page
    assert '<span class="badge">' in page  # the pay stub needs a person
    assert "No T4 has been checked by an underwriter yet." in page


def test_the_review_page(samira, lending):
    store, app, _, _, fields = lending
    page = text(samira.client.get(f"/applications/{app.id}"))
    assert "Amélie Tremblay is applying for a mortgage of $350,000." in page
    assert "LoanLens' standard wording" in page
    assert f'name="field__box14" value="{fields["box14"]}"' in page
    assert "99%" in page and "Nothing looked wrong." in page
    assert "Only T4 slips are read automatically" in page
    assert store.cached_summary(app.id, "en", next(iter(store.summaries.values()))["fingerprint"])


def test_confirming_with_a_correction(samira, lending):
    store, app, t4, _, fields = lending
    path = f"/applications/{app.id}"
    form = {f"field__{k}": v for k, v in fields.items()} | {"field__employer_name": "Prairie Grain Inc."}
    response = samira.post(f"{path}/documents/{t4.id}/review", form, path)
    assert response.status_code == 303 and response.headers["location"] == f"{path}?saved=1"
    review = store.review(app.id, t4.id)
    assert review["reviewer"] == "Samira Haddad" and review["values"]["employer_name"] == "PRAIRIE GRAIN INC."
    assert review["changes"] == {"employer_name": {"from": fields["employer_name"].upper(), "to": "PRAIRIE GRAIN INC."}}
    assert store.docs[t4.id].status == "confirmed"
    page = text(samira.client.get(f"{path}?saved=1"))
    assert "Saved." in page and "Checked by Samira Haddad" in page and "1 value(s) corrected" in page
    assert "Samira Haddad</b> checked a document: T4 2025.pdf: 1 value(s) corrected (employer_name)" in page
    # The model kept every value but one.
    n = sum(v is not None for v in store.reading(app.id, t4.id)["fields"].values() if v["value"])
    assert f"underwriters kept {round(100 * (n - 1) / n)}% of the values the model read ({n - 1} of {n})" in text(
        samira.client.get("/")
    )


def test_a_value_that_isnt_one_is_refused(samira, lending):
    store, app, t4, *_ = lending
    path = f"/applications/{app.id}"
    response = samira.post(f"{path}/documents/{t4.id}/review", {"field__box14": "lots"}, path)
    assert response.status_code == 400 and "Box 14: employment income: “lots” isn't a valid value." in text(response)
    assert store.review(app.id, t4.id) is None


def test_clearing_a_field(samira, lending):
    store, app, t4, _, fields = lending
    path = f"/applications/{app.id}"
    form = {f"field__{k}": v for k, v in fields.items()} | {"field__box44": ""}
    samira.post(f"{path}/documents/{t4.id}/review", form, path)
    assert store.review(app.id, t4.id)["values"]["box44"] is None


def test_other_documents_are_just_checked(samira, lending):
    store, app, _, stub, _ = lending
    path = f"/applications/{app.id}"
    assert samira.post(f"{path}/documents/{stub.id}/review", {}, path).status_code == 303
    assert store.docs[stub.id].status == "confirmed" and "values" not in store.review(app.id, stub.id)


def test_verifying_needs_every_document_checked(samira, lending):
    store, app, t4, stub, fields = lending
    path = f"/applications/{app.id}"
    response = samira.post(f"{path}/decision", {"decision": "verified"}, path)
    assert response.status_code == 409 and "Check every document before verifying" in text(response)
    samira.post(f"{path}/documents/{t4.id}/review", {f"field__{k}": v for k, v in fields.items()}, path)
    samira.post(f"{path}/documents/{stub.id}/review", {}, path)
    assert samira.post(f"{path}/decision", {"decision": "verified", "note": "ignored"}, path).status_code == 303
    assert store.apps[app.id].status == "verified" and store.apps[app.id].note == ""
    assert store.audit(app.id)[-1]["action"] == "marked the application: verified"


def test_asking_for_more_documents(samira, lending):
    store, app, *_ = lending
    path = f"/applications/{app.id}"
    response = samira.post(f"{path}/decision", {"decision": "more documents needed", "note": ""}, path)
    assert response.status_code == 400 and "Write a note" in text(response)
    assert samira.post(f"{path}/decision", {"decision": "more documents needed", "note": "x" * 501}, path).status_code == 400
    assert (
        samira.post(
            f"{path}/decision", {"decision": "more documents needed", "note": "Please add your 2024 T4."}, path
        ).status_code
        == 303
    )
    assert (store.apps[app.id].status, store.apps[app.id].note) == ("more documents needed", "Please add your 2024 T4.")


def test_the_applicant_sees_the_note(lending, samira):
    from tests.conftest import Portal, as_person

    store, app, *_ = lending
    path = f"/applications/{app.id}"
    samira.post(f"{path}/decision", {"decision": "more documents needed", "note": "Please add your 2024 T4."}, path)
    amelie = Portal(store, Settings(auth="easyauth"), as_person("applicant-1"))
    page = text(amelie.client.get(path))
    assert "We need more documents" in page and "A note from our lending team" in page and "Please add your 2024 T4." in page


def test_forms_from_elsewhere_and_bad_decisions(samira, lending):
    store, app, t4, *_ = lending
    path = f"/applications/{app.id}"
    samira.token(path)
    assert samira.client.post(f"{path}/decision", data={"decision": "verified", "csrf": "forged"}).status_code == 400
    assert samira.client.post(f"{path}/documents/{t4.id}/review", data={"csrf": "forged"}).status_code == 400
    assert samira.post(f"{path}/decision", {"decision": "approve the loan"}, path).status_code == 400
    assert store.review(app.id, t4.id) is None and store.apps[app.id].status == "collecting documents"


def test_viewing_documents(samira, lending):
    store, app, t4, stub, _ = lending
    base = f"/applications/{app.id}/documents"
    photo = samira.client.get(f"{base}/{stub.id}/file")
    assert photo.content == PNG and photo.headers["content-type"] == "image/png"
    assert photo.headers["content-disposition"] == 'inline; filename="stub.png"'
    assert photo.headers["content-security-policy"] == "sandbox; default-src 'none'"
    pdf = samira.client.get(f"{base}/{t4.id}/file")
    assert pdf.headers["content-disposition"] == 'attachment; filename="T4 2025.pdf"'
    assert [e["action"] for e in store.audit(app.id)] == ["viewed a document", "viewed a document"]


def test_unknown_things(samira, lending):
    app = lending[1]
    for path in ("/applications/app-nope", f"/applications/{app.id}/documents/doc-nope/file", "/nowhere"):
        response = samira.client.get(path)
        assert response.status_code == 404 and "We couldn't find that page." in text(response), path
    page = f"/applications/{app.id}"
    assert samira.post(f"{page}/documents/doc-nope/review", {}, page).status_code == 404
    assert samira.post("/applications/app-nope/decision", {"decision": "verified"}, page).status_code == 404


def test_in_french(samira, lending):
    store, app, *_ = lending
    page = text(samira.client.get(f"/applications/{app.id}?lang=fr"))
    assert "Amélie Tremblay demande un prêt hypothécaire de 350 000 $." in page
    assert "Case 14 : revenus d’emploi" in page and "Confirmer ces valeurs" in page
    assert ("app-" in next(iter(store.summaries))[0]) and len(store.summaries) == 1  # one per language, made when needed


class Counting:
    def __init__(self):
        self.calls = 0

    def summarize(self, facts, lang):
        self.calls += 1
        return Summary(f"model summary {self.calls}", "model")


def test_summaries_are_written_once_until_the_facts_change(lending):
    store, app, t4, _, fields = lending
    writer = Counting()
    desk = Desk(store, staff("s", "Samira Haddad", "Underwriter"), summarizer=writer)
    path = f"/applications/{app.id}"
    for _ in range(3):
        page = text(desk.client.get(path))
    assert writer.calls == 1 and "Written by Azure OpenAI" in page
    desk.post(f"{path}/documents/{t4.id}/review", {f"field__{k}": v for k, v in fields.items()} | {"field__box14": "1.00"}, path)
    desk.client.get(path)
    assert writer.calls == 2  # a correction changed the facts, so the summary was written again


def test_local_sign_in(lending):
    desk = Desk(lending[0], {}, settings=Settings(auth="dev", secure_cookies=False))
    assert desk.client.get("/").headers["location"] == "/dev/sign-in"
    token = desk.token("/dev/sign-in")
    assert desk.client.post("/dev/sign-in", data={"person": "dev-nobody", "csrf": token}).headers["location"] == "/dev/sign-in"
    desk.client.post("/dev/sign-in", data={"person": "dev-kim", "csrf": token})
    assert desk.client.get("/").status_code == 403
    desk.client.post("/dev/sign-in", data={"person": "dev-samira", "csrf": token})
    assert "Signed in as <b>Samira Haddad</b>" in text(desk.client.get("/"))
    desk.client.get("/dev/sign-out")
    assert desk.client.get("/").headers["location"] == "/dev/sign-in"


def test_health_and_headers(samira):
    assert samira.client.get("/healthz").text == "ok"
    assert samira.client.get("/").headers["x-content-type-options"] == "nosniff"


def test_roles_come_only_from_the_platform(lending):
    """A role claim must be inside the signed principal header the platform adds, not anywhere a browser can set."""
    forged = base64.b64encode(json.dumps({"claims": [{"typ": "name", "val": "x"}]}).encode()).decode()
    desk = Desk(lending[0], {"X-MS-CLIENT-PRINCIPAL-ID": "u", "X-MS-CLIENT-PRINCIPAL": forged, "X-Roles": "Underwriter"})
    assert desk.client.get("/").status_code == 403
