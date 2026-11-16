"""The underwriter dashboard: every application, what the worker read, a summary in English or French,
and the person's decision. Nothing is verified without an underwriter.

    LOANLENS_AUTH=dev uvicorn --factory loanlens.dashboard:from_env --port 8001    # locally, with Azurite

In Azure it signs people in with the credit union's own staff directory (not the applicants' tenant), and
only staff given the Underwriter app role get past the sign-in at all. The role is checked again here.

For each T4 the underwriter confirms the values, correcting any the model got wrong. Corrections are
kept with who made them, which also measures the model on real documents: the share of values
underwriters kept as read.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from loanlens import auth, i18n, summary, t4
from loanlens.models import Application, Document, User
from loanlens.portal import CSRF_COOKIE, SECURITY_HEADERS
from loanlens.settings import Settings
from loanlens.store import Store

HERE = Path(__file__).resolve().parent
NEEDS_A_PERSON = ("uploaded", "reading", "needs review", "failed", "read")
MAX_NOTE = 500


def current_values(reading: dict | None, review: dict | None) -> dict[str, str | None]:
    """A T4's values as they stand: the underwriter's where they've confirmed it, the model's otherwise."""
    if review and "values" in review:
        return dict(review["values"])
    fields = (reading or {}).get("fields") or {}
    return {k: (fields.get(k) or {}).get("value") for k in t4.FIELDS}


def accuracy(store: Store, apps: list[Application]) -> tuple[int, int]:
    """How many values the model read, across every T4 an underwriter has confirmed, and how many they kept."""
    kept = total = 0
    for app in apps:
        for doc in store.documents(app.id):
            review, reading = store.review(app.id, doc.id), store.reading(app.id, doc.id)
            if doc.kind != "t4" or not review or not reading or "fields" not in reading:
                continue
            read = {k: f["value"] for k, f in reading["fields"].items() if f.get("value") is not None}
            total += len(read)
            kept += sum(review["values"].get(k) == v for k, v in read.items())
    return kept, total


def create_dashboard(store: Store, settings: Settings, summarizer: summary.Summarizer) -> FastAPI:
    app = FastAPI(title="LoanLens dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def page(request: Request, name: str, user: User | None, status: int = 200, **context) -> Response:
        lang = i18n.language(request)
        token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        s = i18n.STAFF[lang]
        response = templates.TemplateResponse(
            request,
            f"staff/{name}",
            {
                "t": i18n.TEXT[lang],
                "s": s,
                "lang": lang,
                "user": user,
                "csrf": token,
                "money": lambda amount: i18n.money(int(amount), lang),
                "field_name": lambda k: i18n.FIELD_NAMES.get(lang, {}).get(k, t4.FIELDS[k]),
                "sign_out": "/dev/sign-out" if settings.auth == "dev" else "/.auth/logout",
                "switch_url": f"{request.url.path}?{urlencode({'lang': s['other_lang']})}",
                **context,
            },
            status_code=status,
        )
        cookie = {"httponly": True, "secure": settings.secure_cookies, "samesite": "lax"}
        response.set_cookie(CSRF_COOKIE, token, **cookie)
        if request.query_params.get("lang") in i18n.TEXT:
            response.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, **cookie)
        return response

    def underwriter(request: Request) -> tuple[User | None, Response | None]:
        user = auth.current_staff(request, settings.auth)
        if user is None:
            return None, RedirectResponse("/dev/sign-in" if settings.auth == "dev" else "/.auth/login/aad", status_code=303)
        if auth.UNDERWRITER not in user.roles:
            return None, page(request, "forbidden.html", user, 403)
        return user, None

    def form_ok(request: Request, token: str) -> bool:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        return bool(cookie) and secrets.compare_digest(cookie, token)

    def details(found: Application, lang: str) -> dict:
        docs = store.documents(found.id)
        rows = []
        values = {}
        for doc in docs:
            reading, review = store.reading(found.id, doc.id), store.review(found.id, doc.id)
            if doc.kind == "t4" and reading and "fields" in reading:
                values[doc.id] = current_values(reading, review)
            rows.append({"doc": doc, "reading": reading, "review": review, "current": values.get(doc.id)})
        f = summary.facts(found, docs, values)
        fp = summary.fingerprint(f)
        cached = store.cached_summary(found.id, lang, fp)
        if cached is None:
            written = summarizer.summarize(f, lang)
            cached = {"text": written.text, "source": written.source, "note": written.note, "tokens": written.tokens}
            store.save_summary(found.id, lang, fp, cached)
        return {"application": found, "rows": rows, "summary": cached, "history": store.audit(found.id)}

    def show(request: Request, user: User, application_id: str, *, error=None, message=None, status=200) -> Response:
        found = store.find_application(application_id)
        if not found:
            return page(request, "missing.html", user, 404)
        return page(
            request, "review.html", user, status, error=error, message=message, fields=t4.FIELDS, max_note=MAX_NOTE,
            **details(found, i18n.language(request)),
        )  # fmt: skip

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request):
        user, refused = underwriter(request)
        if refused:
            return refused
        apps = store.all_applications()
        rows = []
        for a in apps:
            docs = store.documents(a.id)
            rows.append({"app": a, "documents": len(docs), "attention": sum(d.status in NEEDS_A_PERSON for d in docs)})
        kept, total = accuracy(store, apps)
        return page(
            request, "queue.html", user, rows=rows, kept=kept, total=total, pct=round(100 * kept / total) if total else None
        )

    @app.get("/applications/{application_id}", response_class=HTMLResponse)
    def review_page(request: Request, application_id: str, saved: str = ""):
        user, refused = underwriter(request)
        if refused:
            return refused
        return show(request, user, application_id, message="saved" if saved else None)

    def find(application_id: str, document_id: str) -> tuple[Application | None, Document | None]:
        found = store.find_application(application_id)
        return found, store.document(application_id, document_id) if found else None

    @app.get("/applications/{application_id}/documents/{document_id}/file")
    def file(request: Request, application_id: str, document_id: str):
        user, refused = underwriter(request)
        if refused:
            return refused
        found, doc = find(application_id, document_id)
        if not doc:
            return page(request, "missing.html", user, 404)
        store.add_audit(found.id, user.name, "viewed a document", doc.filename)
        safe = "".join(c if c.isascii() and (c.isalnum() or c in "._- ") else "_" for c in doc.filename)
        # Photos open in the browser, sandboxed; PDFs download and open in the underwriter's PDF reader,
        # so nothing an applicant uploaded ever runs as part of the dashboard.
        disposition = "inline" if doc.content_type.startswith("image/") else "attachment"
        return Response(
            store.download(doc.blob_name),
            media_type=doc.content_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{safe}"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.post("/applications/{application_id}/documents/{document_id}/review")
    async def confirm(request: Request, application_id: str, document_id: str):
        user, refused = underwriter(request)
        if refused:
            return refused
        found, doc = find(application_id, document_id)
        if not doc:
            return page(request, "missing.html", user, 404)
        form = await request.form()
        if not form_ok(request, str(form.get("csrf", ""))):
            return show(request, user, application_id, error=i18n.STAFF[i18n.language(request)]["bad_form"], status=400)
        record = {"reviewer": user.name, "reviewer_id": user.id, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        detail = ""
        if doc.kind == "t4":
            before = current_values(store.reading(found.id, doc.id), None)
            values, changes = {}, {}
            for key in t4.FIELDS:
                typed = str(form.get(f"field__{key}", "")).strip()
                value = t4.normalize(key, typed) if typed else None
                if typed and value is None:
                    s = i18n.STAFF[i18n.language(request)]
                    name = i18n.FIELD_NAMES.get(i18n.language(request), {}).get(key, t4.FIELDS[key])
                    return show(request, user, application_id, error=s["bad_value"].format(field=name, value=typed), status=400)
                values[key] = value
                if value != before.get(key):
                    changes[key] = {"from": before.get(key), "to": value}
            record |= {"values": values, "changes": changes}
            detail = f"{doc.filename}: {len(changes)} value(s) corrected" + (f" ({', '.join(changes)})" if changes else "")
        else:
            detail = doc.filename
        store.save_review(doc, record)
        store.set_status(doc, "confirmed")
        store.add_audit(found.id, user.name, "checked a document", detail)
        return RedirectResponse(f"/applications/{found.id}?saved=1", status_code=303)

    @app.post("/applications/{application_id}/decision")
    async def decide(request: Request, application_id: str):
        user, refused = underwriter(request)
        if refused:
            return refused
        found = store.find_application(application_id)
        if not found:
            return page(request, "missing.html", user, 404)
        form = await request.form()
        s = i18n.STAFF[i18n.language(request)]
        decision, note = str(form.get("decision", "")), str(form.get("note", "")).strip()
        if not form_ok(request, str(form.get("csrf", ""))) or decision not in ("verified", "more documents needed"):
            return show(request, user, application_id, error=s["bad_form"], status=400)
        docs = store.documents(found.id)
        if decision == "verified" and (not docs or any(d.status != "confirmed" for d in docs)):
            return show(request, user, application_id, error=s["all_checked_first"], status=409)
        if decision == "more documents needed" and not 0 < len(note) <= MAX_NOTE:
            return show(request, user, application_id, error=s["note_needed"], status=400)
        store.set_application_status(found, decision, note if decision == "more documents needed" else "")
        store.add_audit(found.id, user.name, f"marked the application: {decision}", note)
        return RedirectResponse(f"/applications/{found.id}?saved=1", status_code=303)

    @app.exception_handler(404)
    async def missing(request: Request, exc) -> Response:
        return page(request, "missing.html", auth.current_staff(request, settings.auth), 404)

    if settings.auth == "dev":

        @app.get("/dev/sign-in", response_class=HTMLResponse)
        def dev_sign_in(request: Request):
            return page(request, "dev_sign_in.html", None, people={k: v[0] for k, v in auth.DEV_STAFF.items()})

        @app.post("/dev/sign-in")
        async def dev_choose(request: Request):
            form = await request.form()
            person = str(form.get("person", ""))
            if person not in auth.DEV_STAFF or not form_ok(request, str(form.get("csrf", ""))):
                return RedirectResponse("/dev/sign-in", status_code=303)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(auth.DEV_STAFF_COOKIE, person, httponly=True, samesite="lax")
            return response

        @app.get("/dev/sign-out")
        def dev_sign_out():
            response = RedirectResponse("/dev/sign-in", status_code=303)
            response.delete_cookie(auth.DEV_STAFF_COOKIE)
            return response

    return app


def from_env() -> FastAPI:  # pragma: no cover - wiring for the real server
    import os

    from loanlens import telemetry
    from loanlens.store import AzureStore

    telemetry.setup("dashboard")
    settings = Settings.from_env()
    store = AzureStore(account=settings.storage_account, connection=settings.storage_connection)
    if settings.storage_connection:
        store.prepare()
    endpoint, deployment = os.environ.get("LOANLENS_OPENAI_ENDPOINT"), os.environ.get("LOANLENS_OPENAI_DEPLOYMENT")
    writer = summary.OpenAISummarizer(endpoint, deployment) if endpoint and deployment else summary.TemplateSummarizer()
    return create_dashboard(store, settings, writer)
