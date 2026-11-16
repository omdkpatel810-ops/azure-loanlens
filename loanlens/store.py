"""Where applications and documents live.

`AzureStore` uses Azure Storage: documents in a private blob container, records in tables. In Azure it
signs in with the app's managed identity (no keys exist: shared-key access is off on the account);
locally it talks to Azurite. `MemoryStore` keeps everything in memory, for tests.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Protocol

from loanlens import events
from loanlens.models import Application, Document, User

APPLICATIONS = "applications"
DOCUMENTS = "documents"
READINGS = "readings"  # what the worker read from each document
REVIEWS = "reviews"  # what an underwriter confirmed or corrected, per document
AUDIT = "audit"  # who did what, per application
SUMMARIES = "summaries"  # summaries already written, so the model isn't asked twice for the same facts


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Store(Protocol):
    def create_application(self, applicant: User, loan_type: str, amount: int) -> Application: ...
    def applications_for(self, applicant_id: str) -> list[Application]: ...
    def application(self, applicant_id: str, application_id: str) -> Application | None: ...
    def add_document(self, app: Application, kind: str, filename: str, content_type: str, ext: str, data: bytes) -> Document: ...
    def documents(self, application_id: str) -> list[Document]: ...
    def document(self, application_id: str, document_id: str) -> Document | None: ...
    def set_status(self, doc: Document, status: str) -> Document: ...
    def download(self, blob_name: str) -> bytes: ...
    def save_reading(self, doc: Document, reading: dict) -> None: ...
    def reading(self, application_id: str, document_id: str) -> dict | None: ...
    def all_applications(self) -> list[Application]: ...
    def find_application(self, application_id: str) -> Application | None: ...
    def set_application_status(self, app: Application, status: str, note: str = "") -> Application: ...
    def save_review(self, doc: Document, review: dict) -> None: ...
    def review(self, application_id: str, document_id: str) -> dict | None: ...
    def add_audit(self, application_id: str, actor: str, action: str, detail: str = "") -> None: ...
    def audit(self, application_id: str) -> list[dict]: ...
    def cached_summary(self, application_id: str, lang: str, fingerprint: str) -> dict | None: ...
    def save_summary(self, application_id: str, lang: str, fingerprint: str, summary: dict) -> None: ...
    def summary_record(self, application_id: str, lang: str) -> dict | None: ...


def new_application(applicant: User, loan_type: str, amount: int) -> Application:
    return Application(_id("app"), applicant.id, applicant.name, loan_type, amount, "collecting documents", _now())


def new_document(app: Application, kind: str, filename: str, content_type: str, ext: str, size: int) -> Document:
    doc_id = _id("doc")
    return Document(doc_id, app.id, kind, filename, content_type, size, f"{app.id}/{doc_id}{ext}", "uploaded", _now())


class MemoryStore:
    def __init__(self):
        self.apps: dict[str, Application] = {}
        self.docs: dict[str, Document] = {}
        self.blobs: dict[str, bytes] = {}
        self.queue: list[str] = []  # what Event Grid would have queued
        self.readings: dict[str, dict] = {}
        self.reviews: dict[str, dict] = {}
        self.audit_log: list[dict] = []
        self.summaries: dict[tuple[str, str], dict] = {}

    def create_application(self, applicant: User, loan_type: str, amount: int) -> Application:
        app = new_application(applicant, loan_type, amount)
        self.apps[app.id] = app
        return app

    def applications_for(self, applicant_id: str) -> list[Application]:
        return sorted((a for a in self.apps.values() if a.applicant_id == applicant_id), key=lambda a: a.created_at, reverse=True)

    def application(self, applicant_id: str, application_id: str) -> Application | None:
        app = self.apps.get(application_id)
        return app if app and app.applicant_id == applicant_id else None

    def add_document(self, app: Application, kind: str, filename: str, content_type: str, ext: str, data: bytes) -> Document:
        doc = new_document(app, kind, filename, content_type, ext, len(data))
        self.blobs[doc.blob_name] = data
        self.docs[doc.id] = doc
        self.queue.append(json.dumps(events.blob_created("memory://", doc.blob_name, content_type, len(data))))
        return doc

    def documents(self, application_id: str) -> list[Document]:
        return sorted((d for d in self.docs.values() if d.application_id == application_id), key=lambda d: d.uploaded_at)

    def document(self, application_id: str, document_id: str) -> Document | None:
        doc = self.docs.get(document_id)
        return doc if doc and doc.application_id == application_id else None

    def set_status(self, doc: Document, status: str) -> Document:
        self.docs[doc.id] = replace(doc, status=status)
        return self.docs[doc.id]

    def download(self, blob_name: str) -> bytes:
        return self.blobs[blob_name]

    def save_reading(self, doc: Document, reading: dict) -> None:
        self.readings[doc.id] = json.loads(json.dumps(reading))

    def reading(self, application_id: str, document_id: str) -> dict | None:
        return self.readings.get(document_id) if self.document(application_id, document_id) else None

    def all_applications(self) -> list[Application]:
        return sorted(self.apps.values(), key=lambda a: a.created_at)

    def find_application(self, application_id: str) -> Application | None:
        return self.apps.get(application_id)

    def set_application_status(self, app: Application, status: str, note: str = "") -> Application:
        self.apps[app.id] = replace(app, status=status, note=note)
        return self.apps[app.id]

    def save_review(self, doc: Document, review: dict) -> None:
        self.reviews[doc.id] = json.loads(json.dumps(review))

    def review(self, application_id: str, document_id: str) -> dict | None:
        return self.reviews.get(document_id) if self.document(application_id, document_id) else None

    def add_audit(self, application_id: str, actor: str, action: str, detail: str = "") -> None:
        self.audit_log.append(
            {"application_id": application_id, "at": _now(), "actor": actor, "action": action, "detail": detail}
        )

    def audit(self, application_id: str) -> list[dict]:
        return [e for e in self.audit_log if e["application_id"] == application_id]

    def cached_summary(self, application_id: str, lang: str, fingerprint: str) -> dict | None:
        found = self.summaries.get((application_id, lang))
        return found if found and found["fingerprint"] == fingerprint else None

    def save_summary(self, application_id: str, lang: str, fingerprint: str, summary: dict) -> None:
        self.summaries[(application_id, lang)] = {**summary, "fingerprint": fingerprint}

    def summary_record(self, application_id: str, lang: str) -> dict | None:
        return self.summaries.get((application_id, lang))


class AzureStore:
    def __init__(self, *, account: str = "", connection: str = "", emulate_events: bool = False, credential=None):
        from azure.data.tables import TableServiceClient
        from azure.storage.blob import BlobServiceClient
        from azure.storage.queue import QueueServiceClient

        if connection:
            self.blob = BlobServiceClient.from_connection_string(connection)
            self.queues = QueueServiceClient.from_connection_string(connection)
            self.tables = TableServiceClient.from_connection_string(connection)
        elif account:
            if credential is None:  # pragma: no cover - only in Azure
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential()
            self.blob = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential)
            self.queues = QueueServiceClient(f"https://{account}.queue.core.windows.net", credential=credential)
            self.tables = TableServiceClient(f"https://{account}.table.core.windows.net", credential=credential)
        else:
            raise ValueError("set LOANLENS_STORAGE_ACCOUNT, or LOANLENS_STORAGE_CONNECTION for Azurite")
        self.emulate_events = emulate_events
        self.uploads = self.blob.get_container_client(events.CONTAINER)
        self.apps = self.tables.get_table_client(APPLICATIONS)
        self.docs = self.tables.get_table_client(DOCUMENTS)
        self.read_table = self.tables.get_table_client(READINGS)
        self.review_table = self.tables.get_table_client(REVIEWS)
        self.audit_table = self.tables.get_table_client(AUDIT)
        self.summary_table = self.tables.get_table_client(SUMMARIES)
        self.queue = self.queues.get_queue_client(events.QUEUE)

    def prepare(self) -> None:
        """Create the container, tables and queues if they don't exist. Terraform creates them in Azure;
        this is for Azurite."""
        from azure.core.exceptions import ResourceExistsError

        for create in (
            self.uploads.create_container,
            self.apps.create_table,
            self.docs.create_table,
            self.read_table.create_table,
            self.review_table.create_table,
            self.audit_table.create_table,
            self.summary_table.create_table,
            self.queue.create_queue,
            self.queues.get_queue_client(events.POISON_QUEUE).create_queue,
        ):
            with contextlib.suppress(ResourceExistsError):
                create()

    @staticmethod
    def _app(e: dict) -> Application:
        return Application(
            e["RowKey"],
            e["PartitionKey"],
            e["applicant_name"],
            e["loan_type"],
            int(e["amount"]),
            e["status"],
            e["created_at"],
            e.get("note", ""),
        )

    @staticmethod
    def _doc(e: dict) -> Document:
        return Document(
            e["RowKey"],
            e["PartitionKey"],
            e["kind"],
            e["filename"],
            e["content_type"],
            int(e["size"]),
            e["blob_name"],
            e["status"],
            e["uploaded_at"],
        )

    def create_application(self, applicant: User, loan_type: str, amount: int) -> Application:
        app = new_application(applicant, loan_type, amount)
        row = asdict(app)
        self.apps.create_entity({"PartitionKey": row.pop("applicant_id"), "RowKey": row.pop("id"), **row})
        return app

    def applications_for(self, applicant_id: str) -> list[Application]:
        rows = self.apps.query_entities("PartitionKey eq @p", parameters={"p": applicant_id})
        return sorted((self._app(r) for r in rows), key=lambda a: a.created_at, reverse=True)

    def application(self, applicant_id: str, application_id: str) -> Application | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._app(self.apps.get_entity(applicant_id, application_id))
        except ResourceNotFoundError:
            return None

    def add_document(self, app: Application, kind: str, filename: str, content_type: str, ext: str, data: bytes) -> Document:
        from azure.storage.blob import ContentSettings

        doc = new_document(app, kind, filename, content_type, ext, len(data))
        # The record first: the worker may pick the upload event up the moment the blob lands.
        row = asdict(doc)
        self.docs.create_entity({"PartitionKey": row.pop("application_id"), "RowKey": row.pop("id"), **row})
        self.uploads.upload_blob(
            doc.blob_name,
            data,
            overwrite=False,
            content_settings=ContentSettings(content_type=content_type),
            metadata={"application_id": app.id, "document_id": doc.id, "kind": kind},
        )
        if self.emulate_events:
            event = events.blob_created(self.blob.url, doc.blob_name, content_type, len(data))
            self.queue.send_message(json.dumps(event))
        return doc

    def documents(self, application_id: str) -> list[Document]:
        rows = self.docs.query_entities("PartitionKey eq @p", parameters={"p": application_id})
        return sorted((self._doc(r) for r in rows), key=lambda d: d.uploaded_at)

    def document(self, application_id: str, document_id: str) -> Document | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._doc(self.docs.get_entity(application_id, document_id))
        except ResourceNotFoundError:
            return None

    def set_status(self, doc: Document, status: str) -> Document:
        from azure.data.tables import UpdateMode

        self.docs.update_entity({"PartitionKey": doc.application_id, "RowKey": doc.id, "status": status}, mode=UpdateMode.MERGE)
        return replace(doc, status=status)

    def download(self, blob_name: str) -> bytes:
        return self.uploads.download_blob(blob_name).readall()

    def save_reading(self, doc: Document, reading: dict) -> None:
        self.read_table.upsert_entity({"PartitionKey": doc.application_id, "RowKey": doc.id, "reading": json.dumps(reading)})

    def reading(self, application_id: str, document_id: str) -> dict | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(self.read_table.get_entity(application_id, document_id)["reading"])
        except ResourceNotFoundError:
            return None

    def all_applications(self) -> list[Application]:
        return sorted((self._app(r) for r in self.apps.list_entities()), key=lambda a: a.created_at)

    def find_application(self, application_id: str) -> Application | None:
        rows = list(self.apps.query_entities("RowKey eq @id", parameters={"id": application_id}))
        return self._app(rows[0]) if rows else None

    def set_application_status(self, app: Application, status: str, note: str = "") -> Application:
        from azure.data.tables import UpdateMode

        row = {"PartitionKey": app.applicant_id, "RowKey": app.id, "status": status, "note": note}
        self.apps.update_entity(row, mode=UpdateMode.MERGE)
        return replace(app, status=status, note=note)

    def save_review(self, doc: Document, review: dict) -> None:
        self.review_table.upsert_entity({"PartitionKey": doc.application_id, "RowKey": doc.id, "review": json.dumps(review)})

    def review(self, application_id: str, document_id: str) -> dict | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(self.review_table.get_entity(application_id, document_id)["review"])
        except ResourceNotFoundError:
            return None

    def add_audit(self, application_id: str, actor: str, action: str, detail: str = "") -> None:
        at = _now()
        row = {
            "PartitionKey": application_id,
            "RowKey": f"{at}-{uuid.uuid4().hex[:6]}",
            "at": at,
            "actor": actor,
            "action": action,
        }
        self.audit_table.create_entity({**row, "detail": detail})

    def audit(self, application_id: str) -> list[dict]:
        rows = self.audit_table.query_entities("PartitionKey eq @p", parameters={"p": application_id})
        entries = [{"application_id": application_id, **{k: r[k] for k in ("at", "actor", "action", "detail")}} for r in rows]
        return sorted(entries, key=lambda e: e["at"])

    def cached_summary(self, application_id: str, lang: str, fingerprint: str) -> dict | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            row = self.summary_table.get_entity(application_id, lang)
        except ResourceNotFoundError:
            return None
        return json.loads(row["summary"]) | {"fingerprint": row["fingerprint"]} if row["fingerprint"] == fingerprint else None

    def save_summary(self, application_id: str, lang: str, fingerprint: str, summary: dict) -> None:
        row = {"PartitionKey": application_id, "RowKey": lang, "fingerprint": fingerprint, "summary": json.dumps(summary)}
        self.summary_table.upsert_entity(row)

    def summary_record(self, application_id: str, lang: str) -> dict | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            row = self.summary_table.get_entity(application_id, lang)
        except ResourceNotFoundError:
            return None
        return json.loads(row["summary"]) | {"fingerprint": row["fingerprint"]}
