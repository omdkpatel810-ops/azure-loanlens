"""The applicant portal: sign-in, applications, uploads, and who can see what."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from loanlens import events
from loanlens.portal import create_app
from loanlens.settings import Settings
from tests.conftest import JPEG, PDF, PNG, Portal, text


def new_app(portal: Portal, **kw) -> str:
    response = portal.start(**kw)
    assert response.status_code == 303, text(response)
    return response.headers["location"].rsplit("/", 1)[-1]


def test_health_needs_no_sign_in(store, settings):
    assert TestClient(create_app(store, settings)).get("/healthz").text == "ok"


def test_signed_out_people_are_sent_to_sign_in(store, settings):
    client = TestClient(create_app(store, settings), follow_redirects=False)
    assert client.get("/").headers["location"] == "/.auth/login/aad"
    assert client.get("/applications/app-x").headers["location"] == "/.auth/login/aad"
    assert client.post("/applications", data={}).headers["location"] == "/.auth/login/aad"
    assert client.post("/applications/app-x/documents", files={"file": ("a.pdf", PDF)}).headers["location"] == "/.auth/login/aad"


def test_starting_an_application(amelie, store):
    home = amelie.client.get("/")
    assert "Signed in as <b>Amélie Tremblay</b>" in text(home) and "You haven't started an application yet." in text(home)
    app_id = new_app(amelie, loan_type="car", amount="$32,500")
    app = store.apps[app_id]
    assert (app.applicant_id, app.loan_type, app.amount, app.status) == ("user-amelie", "car", 32500, "collecting documents")
    assert "Car loan</b> · $32,500" in text(amelie.client.get("/"))


@pytest.mark.parametrize(
    ("loan_type", "amount", "message"),
    [
        ("boat", "5000", "Something went wrong with the form"),
        ("mortgage", "lots", "Enter an amount between $1,000 and $2,000,000."),
        ("mortgage", "999", "Enter an amount between"),
        ("mortgage", "2000001", "Enter an amount between"),
    ],
)
def test_bad_applications(amelie, store, loan_type, amount, message):
    response = amelie.start(loan_type, amount)
    assert response.status_code == 400 and message in text(response) and not store.apps


def test_forms_from_another_site_are_refused(amelie, store):
    amelie.token()
    response = amelie.client.post("/applications", data={"loan_type": "car", "amount": "5000", "csrf": "forged"})
    assert response.status_code == 400 and not store.apps
    app_id = new_app(amelie)
    assert amelie.upload(app_id, csrf="forged").status_code == 400 and not store.docs


def test_at_most_five_open_applications(amelie):
    for _ in range(5):
        new_app(amelie)
    response = amelie.start()
    assert response.status_code == 409 and "You already have 5 applications open." in text(response)


def test_uploading_a_t4(amelie, store):
    app_id = new_app(amelie)
    response = amelie.upload(app_id, name="../../T4 2025 <final>.pdf")
    assert response.status_code == 201 and "Thanks, we have T4 2025 final.pdf." in text(response)
    (doc,) = store.docs.values()
    assert (doc.kind, doc.content_type, doc.status, doc.size) == ("t4", "application/pdf", "uploaded", len(PDF))
    assert doc.blob_name == f"{app_id}/{doc.id}.pdf" and store.blobs[doc.blob_name] == PDF
    # Event Grid's job, done by the memory store: one BlobCreated event the worker can read.
    assert events.parse(store.queue[0]) == ("uploads", doc.blob_name)
    assert 'T4 2025 final.pdf</td><td>T4 slip</td><td class="status">Received' in text(
        amelie.client.get(f"/applications/{app_id}")
    )


@pytest.mark.parametrize(("data", "ext"), [(PNG, ".png"), (JPEG, ".jpg")], ids=["png", "jpeg"])
def test_photos_of_documents_are_fine(amelie, store, data, ext):
    app_id = new_app(amelie)
    assert amelie.upload(app_id, data, name="photo", kind="pay_stub").status_code == 201
    assert next(iter(store.docs.values())).blob_name.endswith(ext)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "That file is empty."),
        (b"MZ\x90\x00 not a document", "Only PDF, JPEG and PNG files can be uploaded."),
        (b"%PDF-1.7 " + b"x" * (1024 * 1024), "That file is over 1 MB."),
        (b"%PDF-1.7\n<< /Encrypt 5 0 R >>", "That PDF is password-protected."),
    ],
    ids=["empty", "program", "too-big", "locked"],
)
def test_uploads_that_are_refused(amelie, store, data, message):
    app_id = new_app(amelie)
    response = amelie.upload(app_id, data, name="evil.pdf")
    assert response.status_code == 400 and message in text(response)
    assert not store.docs and not store.blobs and not store.queue


def test_the_document_kind_must_be_one_we_know(amelie, store):
    app_id = new_app(amelie)
    assert amelie.upload(app_id, kind="passport").status_code == 400 and not store.docs


def test_at_most_twenty_documents(amelie):
    app_id = new_app(amelie)
    for _ in range(20):
        assert amelie.upload(app_id).status_code == 201
    response = amelie.upload(app_id)
    assert response.status_code == 409 and "already has 20 documents" in text(response)


def test_applicants_see_only_their_own(amelie, jordan, store):
    app_id = new_app(amelie)
    assert jordan.client.get(f"/applications/{app_id}").status_code == 404
    assert jordan.upload(app_id, csrf=jordan.token()).status_code == 404
    assert app_id not in text(jordan.client.get("/"))
    assert not store.docs


def test_french(amelie):
    page = amelie.client.get("/?lang=fr")
    assert '<html lang="fr">' in text(page) and "Vos demandes" in text(page)
    assert page.cookies.get("loanlens_lang") == "fr"
    app_id = new_app(amelie)
    assert "Prêt hypothécaire · 350\u00a0000\u00a0$" in text(amelie.client.get(f"/applications/{app_id}"))  # remembered
    assert "English" in text(amelie.client.get("/"))


def test_french_from_the_browser(store, settings):
    portal = Portal(store, settings, {"X-MS-CLIENT-PRINCIPAL-ID": "u", "Accept-Language": "fr-CA,fr;q=0.9"})
    assert "Commencer une demande" in text(portal.client.get("/"))


def test_every_page_has_security_headers(amelie):
    headers = amelie.client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-store"
    cookie = next(c for c in headers.get_list("set-cookie") if c.startswith("loanlens_csrf="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie


def test_unknown_pages(amelie):
    response = amelie.client.get("/nope")
    assert response.status_code == 404 and "We couldn't find that page." in text(response)


def test_the_name_shown(store, settings):
    portal = Portal(store, settings, {"X-MS-CLIENT-PRINCIPAL-ID": "u", "X-MS-CLIENT-PRINCIPAL-NAME": "sam@example.com"})
    assert "Signed in as <b>sam@example.com</b>" in text(portal.client.get("/"))  # no name claim: the sign-in name
    portal = Portal(store, settings, {"X-MS-CLIENT-PRINCIPAL-ID": "u"})
    assert "Signed in as <b>Applicant</b>" in text(portal.client.get("/"))


def test_local_sign_in(store):
    portal = Portal(store, Settings(auth="dev", secure_cookies=False))
    assert portal.client.get("/").headers["location"] == "/dev/sign-in"
    page = portal.client.get("/dev/sign-in")
    assert "Amélie Tremblay" in text(page) and "Local development only" in text(page)
    token = portal.token("/dev/sign-in")
    assert portal.client.post("/dev/sign-in", data={"person": "dev-nobody", "csrf": token}).headers["location"] == "/dev/sign-in"
    assert portal.client.post("/dev/sign-in", data={"person": "dev-jordan", "csrf": token}).headers["location"] == "/"
    assert "Signed in as <b>Jordan Okafor</b>" in text(portal.client.get("/"))
    assert 'href="/dev/sign-out"' in text(portal.client.get("/"))
    portal.client.get("/dev/sign-out")
    assert portal.client.get("/").headers["location"] == "/dev/sign-in"


def test_local_sign_in_doesnt_exist_in_azure(amelie):
    assert amelie.client.get("/dev/sign-in").status_code == 404
    assert amelie.client.post("/dev/sign-in", data={"person": "dev-jordan"}).status_code in (404, 405)


def test_queued_events_are_json(amelie, store):
    app_id = new_app(amelie)
    amelie.upload(app_id)
    event = json.loads(store.queue[0])
    assert event["eventType"] == "Microsoft.Storage.BlobCreated" and event["data"]["contentType"] == "application/pdf"
