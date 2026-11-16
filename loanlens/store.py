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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Store(Protocol):
    def create_application(self, applicant: User, loan_type: str, amount: int) -> Application: ...
    def applications_for(self, applicant_id: str) -> list[Application]: ...
    def application(self, applicant_id: str, application_id: str) -> Application | None: ...
    def add_document(self, app: Application, kind: str, filename: str, content_type: str, ext: str, data: bytes) -> Document: ...
    def documents(self, application_id: str) -> list[Document]: ...


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

    def set_status(self, doc: Document, status: str) -> Document:
        self.docs[doc.id] = replace(doc, status=status)
        return self.docs[doc.id]


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
        self.queue = self.queues.get_queue_client(events.QUEUE)

    def prepare(self) -> None:
        """Create the container, tables and queues if they don't exist. Terraform creates them in Azure;
        this is for Azurite."""
        from azure.core.exceptions import ResourceExistsError

        for create in (
            self.uploads.create_container,
            self.apps.create_table,
            self.docs.create_table,
            self.queue.create_queue,
            self.queues.get_queue_client(events.POISON_QUEUE).create_queue,
        ):
            with contextlib.suppress(ResourceExistsError):
                create()

    @staticmethod
    def _app(e: dict) -> Application:
        return Application(
            e["RowKey"], e["PartitionKey"], e["applicant_name"], e["loan_type"], int(e["amount"]), e["status"], e["created_at"]
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
