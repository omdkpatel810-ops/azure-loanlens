"""The applicant portal: start an application and upload documents to it.

    LOANLENS_AUTH=dev uvicorn --factory loanlens.portal:from_env    # locally, with Azurite running

Every page needs a signed-in applicant, who sees only their own applications. Uploads are checked
(type from their first bytes, size, password protection) before they're stored, and every form carries
a token that must match a cookie, so another site can't submit forms on an applicant's behalf.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from loanlens import auth, files, i18n
from loanlens.models import (
    DOCUMENT_KINDS,
    LOAN_TYPES,
    MAX_AMOUNT,
    MAX_DOCUMENTS,
    MAX_OPEN_APPLICATIONS,
    MIN_AMOUNT,
    User,
)
from loanlens.settings import Settings
from loanlens.store import AzureStore, Store

HERE = Path(__file__).resolve().parent
CSRF_COOKIE = "loanlens_csrf"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def create_app(store: Store, settings: Settings) -> FastAPI:
    app = FastAPI(title="LoanLens portal", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    max_bytes = settings.max_upload_mb * 1024 * 1024

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def page(request: Request, name: str, user: User | None, status: int = 200, **context) -> Response:
        lang = i18n.language(request)
        token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        t = i18n.TEXT[lang]
        response = templates.TemplateResponse(
            request,
            name,
            {
                "t": t,
                "lang": lang,
                "user": user,
                "csrf": token,
                "money": lambda amount: i18n.money(amount, lang),
                "sign_out": "/dev/sign-out" if settings.auth == "dev" else "/.auth/logout",
                "switch_url": f"{request.url.path}?{urlencode({'lang': t['other_lang']})}",
                **context,
            },
            status_code=status,
        )
        cookie = {"httponly": True, "secure": settings.secure_cookies, "samesite": "lax"}
        response.set_cookie(CSRF_COOKIE, token, **cookie)
        if request.query_params.get("lang") in i18n.TEXT:
            response.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, **cookie)
        return response

    def signed_in(request: Request) -> User | None:
        return auth.current_user(request, settings.auth)

    def to_sign_in() -> Response:
        return RedirectResponse("/dev/sign-in" if settings.auth == "dev" else "/.auth/login/aad", status_code=303)

    def form_ok(request: Request, token: str) -> bool:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        return bool(cookie) and secrets.compare_digest(cookie, token)

    def home(request: Request, user: User, error: str | None = None, status: int = 200) -> Response:
        return page(
            request, "home.html", user, status, applications=store.applications_for(user.id), loan_types=LOAN_TYPES, error=error
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        user = signed_in(request)
        return home(request, user) if user else to_sign_in()

    @app.post("/applications")
    def create(request: Request, loan_type: str = Form(""), amount: str = Form(""), csrf: str = Form("")):
        user = signed_in(request)
        if not user:
            return to_sign_in()
        if not form_ok(request, csrf) or loan_type not in LOAN_TYPES:
            return home(request, user, "bad_form", 400)
        try:
            value = int(amount.replace(",", "").replace(" ", "").replace("$", ""))
        except ValueError:
            value = 0
        if not MIN_AMOUNT <= value <= MAX_AMOUNT:
            return home(request, user, "bad_amount", 400)
        if sum(a.status != "verified" for a in store.applications_for(user.id)) >= MAX_OPEN_APPLICATIONS:
            return home(request, user, "too_many_applications", 409)
        new = store.create_application(user, loan_type, value)
        return RedirectResponse(f"/applications/{new.id}", status_code=303)

    def show(request: Request, user: User, application_id: str, *, message=None, error=None, status=200) -> Response:
        found = store.application(user.id, application_id)
        if not found:
            return page(request, "missing.html", user, 404)
        return page(
            request,
            "application.html",
            user,
            status,
            application=found,
            documents=store.documents(found.id),
            kinds=DOCUMENT_KINDS,
            max_mb=settings.max_upload_mb,
            message=message,
            error=error,
        )

    @app.get("/applications/{application_id}", response_class=HTMLResponse)
    def application(request: Request, application_id: str, uploaded: str = ""):
        user = signed_in(request)
        if not user:
            return to_sign_in()
        just_added = store.document(application_id, uploaded) if uploaded and store.application(user.id, application_id) else None
        return show(request, user, application_id, message=just_added.filename if just_added else None)

    @app.post("/applications/{application_id}/documents")
    async def upload(request: Request, application_id: str, file: UploadFile, kind: str = Form(""), csrf: str = Form("")):
        user = signed_in(request)
        if not user:
            return to_sign_in()
        found = store.application(user.id, application_id)
        if not found:
            return page(request, "missing.html", user, 404)
        if not form_ok(request, csrf) or kind not in DOCUMENT_KINDS:
            return show(request, user, application_id, error="bad_form", status=400)
        if len(store.documents(found.id)) >= MAX_DOCUMENTS:
            return show(request, user, application_id, error="too_many_documents", status=409)
        data = await file.read(max_bytes + 1)  # never read more than the limit allows
        try:
            content_type, ext = files.check(data, max_bytes)
        except files.Rejected as rejected:
            return show(request, user, application_id, error=rejected.reason, status=400)
        doc = store.add_document(found, kind, files.clean_filename(file.filename), content_type, ext, data)
        # Redirect after a successful upload, so reloading the page can't upload the file twice.
        return RedirectResponse(f"/applications/{found.id}?uploaded={doc.id}", status_code=303)

    @app.exception_handler(404)
    async def missing(request: Request, exc) -> Response:
        return page(request, "missing.html", signed_in(request), 404)

    if settings.auth == "dev":

        @app.get("/dev/sign-in", response_class=HTMLResponse)
        def dev_sign_in(request: Request):
            return page(request, "dev_sign_in.html", None, people=auth.DEV_PEOPLE)

        @app.post("/dev/sign-in")
        def dev_choose(request: Request, person: str = Form(""), csrf: str = Form("")):
            if person not in auth.DEV_PEOPLE or not form_ok(request, csrf):
                return RedirectResponse("/dev/sign-in", status_code=303)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(auth.DEV_COOKIE, person, httponly=True, samesite="lax")
            return response

        @app.get("/dev/sign-out")
        def dev_sign_out():
            response = RedirectResponse("/dev/sign-in", status_code=303)
            response.delete_cookie(auth.DEV_COOKIE)
            return response

    return app


def from_env() -> FastAPI:  # pragma: no cover - wiring for the real server
    settings = Settings.from_env()
    store = AzureStore(
        account=settings.storage_account, connection=settings.storage_connection, emulate_events=settings.emulate_events
    )
    if settings.storage_connection:
        store.prepare()
    return create_app(store, settings)
