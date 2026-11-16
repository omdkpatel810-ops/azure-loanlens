"""Upload events. In Azure, Event Grid writes a BlobCreated event to the uploads queue for every new
document; locally the portal writes the same event itself, so the worker can't tell the difference."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime

CONTAINER = "uploads"
QUEUE = "uploads"
POISON_QUEUE = "uploads-poison"


def blob_created(account_url: str, blob_name: str, content_type: str, size: int) -> dict:
    """A BlobCreated event, in the Event Grid schema."""
    return {
        "id": str(uuid.uuid4()),
        "topic": "local",
        "subject": f"/blobServices/default/containers/{CONTAINER}/blobs/{blob_name}",
        "eventType": "Microsoft.Storage.BlobCreated",
        "eventTime": datetime.now(UTC).isoformat(),
        "dataVersion": "",
        "data": {
            "api": "PutBlob",
            "contentType": content_type,
            "contentLength": size,
            "blobType": "BlockBlob",
            "url": f"{account_url.rstrip('/')}/{CONTAINER}/{blob_name}",
        },
    }


def parse(message: str) -> tuple[str, str]:
    """The container and blob name a queued event is about. Accepts the event as JSON or base64-encoded JSON."""
    try:
        event = json.loads(message)
    except json.JSONDecodeError:
        try:
            event = json.loads(base64.b64decode(message, validate=True))
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError("not an Event Grid event") from e
    if isinstance(event, list) and len(event) == 1:
        event = event[0]
    if not isinstance(event, dict) or event.get("eventType") != "Microsoft.Storage.BlobCreated":
        raise ValueError("not a BlobCreated event")
    prefix = "/blobServices/default/containers/"
    subject = str(event.get("subject", ""))
    if not subject.startswith(prefix) or "/blobs/" not in subject:
        raise ValueError(f"unexpected subject: {subject!r}")
    container, blob = subject.removeprefix(prefix).split("/blobs/", 1)
    return container, blob
