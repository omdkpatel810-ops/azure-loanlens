"""File checks, upload events, settings and sign-in roles."""

from __future__ import annotations

import base64
import json

import pytest
from starlette.requests import Request

from loanlens import auth, events, files
from loanlens.settings import AZURITE, Settings
from tests.conftest import JPEG, PDF, PNG, principal


@pytest.mark.parametrize(("data", "expected"), [(PDF, "application/pdf"), (PNG, "image/png"), (JPEG, "image/jpeg")])
def test_types_come_from_the_first_bytes(data, expected):
    assert files.check(data, 10_000)[0] == expected


def test_a_renamed_program_is_still_a_program():
    with pytest.raises(files.Rejected) as e:
        files.check(b"MZ\x90\x00", 10_000)
    assert e.value.reason == "wrong_type"


@pytest.mark.parametrize(
    ("name", "clean"),
    [
        ("C:\\Users\\me\\T4.pdf", "T4.pdf"),
        ("../../etc/passwd", "passwd"),
        ("a\x00b\x1fc.pdf", "abc.pdf"),
        ("...", "document"),
        (None, "document"),
        ("x" * 100 + ".pdf", "x" * 80),
        ("relevé de paie.pdf", "relevé de paie.pdf"),
    ],
)
def test_filenames(name, clean):
    assert files.clean_filename(name) == clean


def test_events_round_trip():
    event = events.blob_created("https://st.blob.core.windows.net/", "app-1/doc-1.pdf", "application/pdf", 10)
    assert event["data"]["url"] == "https://st.blob.core.windows.net/uploads/app-1/doc-1.pdf"
    text = json.dumps(event)
    assert events.parse(text) == ("uploads", "app-1/doc-1.pdf")
    assert events.parse(base64.b64encode(text.encode()).decode()) == ("uploads", "app-1/doc-1.pdf")
    assert events.parse(json.dumps([event])) == ("uploads", "app-1/doc-1.pdf")


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("not json", "not an Event Grid event"),
        (json.dumps({"eventType": "Microsoft.Storage.BlobDeleted"}), "not a BlobCreated event"),
        (json.dumps({"eventType": "Microsoft.Storage.BlobCreated", "subject": "/x"}), "unexpected subject"),
    ],
)
def test_events_that_arent_uploads(message, error):
    with pytest.raises(ValueError, match=error):
        events.parse(message)


def test_settings():
    local = Settings.from_env({"LOANLENS_AUTH": "dev"})
    assert (local.storage_connection, local.emulate_events, local.secure_cookies) == (AZURITE, True, False)
    azure = Settings.from_env({"LOANLENS_STORAGE_ACCOUNT": "stloanlens", "LOANLENS_MAX_UPLOAD_MB": "5"})
    assert (azure.auth, azure.storage_account, azure.storage_connection, azure.emulate_events, azure.max_upload_mb) == (
        "easyauth", "stloanlens", "", False, 5,
    )  # fmt: skip
    with pytest.raises(ValueError, match="easyauth or dev"):
        Settings.from_env({"LOANLENS_AUTH": "none"})
    assert Settings.from_env().auth in ("easyauth", "dev")


def request(headers: dict[str, str]) -> Request:
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_roles_come_from_the_principal():
    user = auth.current_user(
        request({"X-MS-CLIENT-PRINCIPAL-ID": "u", "X-MS-CLIENT-PRINCIPAL": principal("Underwriter")}), "easyauth"
    )
    assert user.roles == {"Underwriter"}
    for broken in ("%%%", base64.b64encode(b"[1]").decode(), base64.b64encode(b'{"claims": 5}').decode()):
        assert (
            auth.current_user(request({"X-MS-CLIENT-PRINCIPAL-ID": "u", "X-MS-CLIENT-PRINCIPAL": broken}), "easyauth").roles
            == set()
        )
    assert auth.current_user(request({}), "easyauth") is None
    # Headers mean nothing in local mode: there, only the local sign-in cookie counts.
    assert auth.current_user(request({"X-MS-CLIENT-PRINCIPAL-ID": "u"}), "dev") is None


def test_the_memory_store_tracks_status():
    from loanlens.models import User
    from loanlens.store import MemoryStore

    store = MemoryStore()
    app = store.create_application(User("u", "U"), "car", 5_000)
    doc = store.add_document(app, "t4", "a.pdf", "application/pdf", ".pdf", PDF)
    assert store.set_status(doc, "read").status == "read" and store.documents(app.id)[0].status == "read"
