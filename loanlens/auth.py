"""Who is signed in.

In Azure, Container Apps' built-in sign-in (Easy Auth) handles the login with Microsoft Entra External ID
and passes the person to the app in X-MS-CLIENT-PRINCIPAL headers. The platform removes those headers
from incoming requests, so a browser can't forge them, and turns away anyone not signed in before the
app sees the request. Locally, `dev` mode offers a sign-in page with sample people instead.
"""

from __future__ import annotations

import base64
import binascii
import json

from fastapi import Request

from loanlens.models import User

DEV_COOKIE = "loanlens_dev_user"
DEV_PEOPLE = {
    "dev-amelie": "Amélie Tremblay",
    "dev-jordan": "Jordan Okafor",
}
UNDERWRITER = "Underwriter"  # the app role, in the staff directory, that opens the dashboard
DEV_STAFF_COOKIE = "loanlens_dev_staff"
DEV_STAFF = {  # local only: one underwriter, and one colleague without the role
    "dev-samira": ("Samira Haddad", frozenset({UNDERWRITER})),
    "dev-kim": ("Kim Nguyen", frozenset()),
}


def _claims(principal: str | None) -> list[dict]:
    """The claims in X-MS-CLIENT-PRINCIPAL: base64-encoded JSON, so names with accents survive the trip."""
    if not principal:
        return []
    try:
        claims = json.loads(base64.b64decode(principal)).get("claims", [])
    except (binascii.Error, ValueError, AttributeError):
        return []
    return [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []


def current_user(request: Request, mode: str) -> User | None:
    if mode == "easyauth":
        user_id = request.headers.get("x-ms-client-principal-id")
        if not user_id:
            return None
        claims = _claims(request.headers.get("x-ms-client-principal"))
        name = next((c.get("val") for c in claims if c.get("typ") == "name" and c.get("val")), None)
        roles = frozenset(c.get("val", "") for c in claims if c.get("typ") in ("roles", "role"))
        return User(user_id, name or request.headers.get("x-ms-client-principal-name") or "Applicant", roles)
    user_id = request.cookies.get(DEV_COOKIE, "")
    return User(user_id, DEV_PEOPLE[user_id]) if user_id in DEV_PEOPLE else None


def current_staff(request: Request, mode: str) -> User | None:
    """The signed-in staff member, from the staff directory's sign-in. Roles decide what they may do."""
    if mode == "easyauth":
        return current_user(request, mode)
    found = DEV_STAFF.get(request.cookies.get(DEV_STAFF_COOKIE, ""))
    return User(request.cookies[DEV_STAFF_COOKIE], found[0], found[1]) if found else None
