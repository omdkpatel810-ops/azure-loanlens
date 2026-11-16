from __future__ import annotations

import base64
import html
import json
import re

import pytest
from fastapi.testclient import TestClient

from loanlens.portal import create_app
from loanlens.settings import Settings
from loanlens.store import MemoryStore

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def text(response) -> str:
    """A page's text as a person reads it, with HTML entities like &#39; turned back into characters."""
    return html.unescape(response.text)


def principal(*roles: str, name: str | None = None) -> str:
    claims = [{"typ": "roles", "val": r} for r in roles] + ([{"typ": "name", "val": name}] if name else [])
    return base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()


def as_person(user_id: str, name: str = "Amélie Tremblay") -> dict[str, str]:
    """The headers Container Apps' built-in sign-in adds for a signed-in person."""
    email = f"{user_id.removeprefix('user-')}@example.com"
    return {
        "X-MS-CLIENT-PRINCIPAL-ID": user_id,
        "X-MS-CLIENT-PRINCIPAL-NAME": email,
        "X-MS-CLIENT-PRINCIPAL": principal(name=name),
    }


class Portal:
    """A test browser for the portal: keeps cookies, and reads the form token off each page."""

    def __init__(self, store, settings, headers=None):
        self.store = store
        app = create_app(store, settings)
        self.client = TestClient(app, base_url="https://loanlens.test", headers=headers or {}, follow_redirects=False)

    def token(self, path: str = "/") -> str:
        page = self.client.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def start(self, loan_type="mortgage", amount="350000"):
        return self.client.post("/applications", data={"loan_type": loan_type, "amount": amount, "csrf": self.token()})

    def upload(self, app_id, data=PDF, name="t4-2025.pdf", kind="t4", csrf=None):
        path = f"/applications/{app_id}"
        token = self.token(path) if csrf is None else csrf
        return self.client.post(
            f"{path}/documents", data={"kind": kind, "csrf": token}, files={"file": (name, data, "application/pdf")}
        )


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def settings():
    return Settings(auth="easyauth", max_upload_mb=1)


@pytest.fixture
def amelie(store, settings):
    return Portal(store, settings, as_person("user-amelie"))


@pytest.fixture
def jordan(store, settings):
    return Portal(store, settings, as_person("user-jordan", "Jordan Okafor"))
