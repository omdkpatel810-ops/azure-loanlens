"""The Azure Storage code, against Azurite: the same SDK calls the app makes in Azure.

CI starts Azurite before running these; locally, start it with `npx azurite --inMemoryPersistence`.
"""

from __future__ import annotations

import json
import socket
import uuid

import pytest
from azure.storage.blob import BlobServiceClient

from loanlens import events
from loanlens.models import User
from loanlens.settings import AZURITE
from loanlens.store import AzureStore
from tests.conftest import PDF, Portal, as_person

pytestmark = pytest.mark.azurite


def azurite_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 10000)) == 0


@pytest.fixture
def azure():
    if not azurite_running():
        pytest.fail("Azurite isn't running on 127.0.0.1:10000; start it with: npx azurite --inMemoryPersistence")
    store = AzureStore(connection=AZURITE, emulate_events=True)
    store.prepare()
    store.prepare()  # a second time changes nothing
    store.queue.clear_messages()
    return store


def test_applications_are_kept_per_applicant(azure):
    amelie = User(f"user-{uuid.uuid4().hex[:6]}", "Amélie Tremblay")
    first = azure.create_application(amelie, "mortgage", 350_000)
    second = azure.create_application(amelie, "car", 25_000)
    assert {a.id for a in azure.applications_for(amelie.id)} == {first.id, second.id}
    assert azure.application(amelie.id, first.id) == first
    assert azure.application("someone-else", first.id) is None
    assert azure.applications_for("someone-else") == []


def test_documents_land_in_private_storage_with_an_event(azure):
    amelie = User(f"user-{uuid.uuid4().hex[:6]}", "Amélie Tremblay")
    app = azure.create_application(amelie, "personal", 8_000)
    doc = azure.add_document(app, "t4", "T4 2025.pdf", "application/pdf", ".pdf", PDF)
    assert azure.documents(app.id) == [doc]
    blob = azure.uploads.get_blob_client(doc.blob_name)
    assert blob.download_blob().readall() == PDF
    props = blob.get_blob_properties()
    assert props.content_settings.content_type == "application/pdf"
    assert props.metadata == {"application_id": app.id, "document_id": doc.id, "kind": "t4"}
    (message,) = azure.queue.receive_messages()
    assert events.parse(message.content) == ("uploads", doc.blob_name)
    assert json.loads(message.content)["data"]["url"].endswith(f"/uploads/{doc.blob_name}")


def test_nothing_is_public(azure):
    access = azure.uploads.get_container_access_policy()
    assert access.get("public_access") is None


def test_the_portal_end_to_end(azure, settings):
    portal = Portal(azure, settings, as_person(f"user-{uuid.uuid4().hex[:6]}"))
    app_id = portal.start(amount="120000").headers["location"].rsplit("/", 1)[-1]
    assert portal.upload(app_id).status_code == 201
    (doc,) = azure.documents(app_id)
    assert doc.status == "uploaded" and len(azure.queue.peek_messages()) == 1


class Credential:
    def get_token(self, *scopes, **kwargs):  # pragma: no cover - never called: nothing is requested
        raise AssertionError("no requests in this test")


def test_signing_in_by_account_name():
    store = AzureStore(account="stloanlens", credential=Credential())
    assert store.blob.url == "https://stloanlens.blob.core.windows.net/"
    assert store.tables.url.startswith("https://stloanlens.table.core.windows.net")


def test_one_way_to_reach_storage_is_needed():
    with pytest.raises(ValueError, match="LOANLENS_STORAGE_ACCOUNT"):
        AzureStore()


def test_blob_names_dont_collide(azure):
    user = User("u", "U")
    app = azure.create_application(user, "car", 5_000)
    a = azure.add_document(app, "t4", "same.pdf", "application/pdf", ".pdf", PDF)
    b = azure.add_document(app, "t4", "same.pdf", "application/pdf", ".pdf", PDF)
    assert a.blob_name != b.blob_name
    assert isinstance(BlobServiceClient.from_connection_string(AZURITE), BlobServiceClient)


def test_in_azure_event_grid_queues_the_event_not_the_app(azure):
    store = AzureStore(connection=AZURITE, emulate_events=False)
    app = store.create_application(User("u2", "U"), "car", 5_000)
    store.add_document(app, "pay_stub", "stub.png", "image/png", ".png", b"\x89PNG\r\n\x1a\n")
    assert azure.queue.peek_messages() == []
