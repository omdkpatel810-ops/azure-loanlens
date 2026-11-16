"""The worker: reads each uploaded document and records what it found.

    python -m loanlens.worker          # in Azure, or locally with LOANLENS_AUTH=dev and Azurite

For every upload event on the queue it finds the document's record, and then:
- for a T4, asks the extractor for its fields, normalizes them, and checks them against the year's legal
  maximums, each other (`t4.check`), and the applicant's name. Any problem, or any field the model is unsure of, sends the
  document to a person ("needs review"); otherwise it's "read". An underwriter reviews every
  application either way (milestone 3); "read" only means nothing looked wrong.
- for anything else (pay stubs, other documents), marks it for a person to read.

Failures are handled by kind. Throttling or an outage leaves the message on the queue to come back
later. A document that can't be read, an event for a file LoanLens has no record of, or a message that
has failed five times goes to the poison queue, so it stops blocking the others.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime

from loanlens import events, t4
from loanlens.extract import ExtractionFailed, Extractor
from loanlens.queues import Message, Queue
from loanlens.store import Store

log = logging.getLogger("loanlens.worker")
MAX_ATTEMPTS = 5
VISIBILITY_SECONDS = 300
DONE = ("read", "needs review", "failed")


class Worker:
    def __init__(self, store: Store, queue: Queue, extractor: Extractor, *, confidence: float = 0.8, clock=time.monotonic):
        self.store, self.queue, self.extractor = store, queue, extractor
        self.confidence, self.clock = confidence, clock

    def handle(self, message: Message) -> str:
        """Process one message. Returns what happened, for logs and tests."""
        try:
            container, blob = events.parse(message.content)
        except ValueError as e:
            self._set_aside(message, f"not an upload event: {e}")
            return "poisoned"
        if container != events.CONTAINER or "/" not in blob:
            self.queue.delete(message)
            return "ignored"
        application_id, filename = blob.split("/", 1)
        doc = self.store.document(application_id, filename.rsplit(".", 1)[0])
        if doc is None:
            self._set_aside(message, f"no record for {blob}; it wasn't uploaded through the portal")
            return "poisoned"
        if doc.status in DONE:  # Event Grid delivers at least once, so the same event can come twice
            self.queue.delete(message)
            return "already done"
        doc = self.store.set_status(doc, "reading")

        if doc.kind != "t4":
            self.store.save_reading(
                doc, {"kind": doc.kind, "issues": ["Only T4 slips are read automatically; a person reads this one"]}
            )
            self.store.set_status(doc, "needs review")
            self.queue.delete(message)
            return "needs review"

        started = self.clock()
        try:
            extraction = self.extractor.extract(self.store.download(doc.blob_name), doc.content_type)
        except ExtractionFailed as e:
            if e.transient and message.dequeue_count < MAX_ATTEMPTS:
                self.store.set_status(doc, "uploaded")  # back in line; the message reappears after its timeout
                log.warning("will retry %s: %s", blob, e)
                return "retry later"
            self.store.save_reading(doc, {"kind": "t4", "issues": [f"Couldn't be read: {e}"], "error": str(e)})
            self.store.set_status(doc, "failed")
            self._set_aside(message, str(e))
            return "failed"

        values = {k: t4.normalize(k, f.value) for k, f in extraction.fields.items()}
        unsure = [
            f"{t4.FIELDS[k]}: the model is only {f.confidence:.0%} sure"
            for k, f in extraction.fields.items()
            if values.get(k) is not None and f.confidence < self.confidence
        ]
        issues = t4.check(values) + unsure
        app = self.store.find_application(application_id)
        if app and not t4.same_person(values.get("employee_name"), app.applicant_name):
            issues.append(
                f"The employee on this slip ({values['employee_name']}) doesn't look like the applicant ({app.applicant_name})"
            )
        status = "needs review" if issues else "read"
        self.store.save_reading(
            doc,
            {
                "kind": "t4",
                "model_id": extraction.model_id,
                "fields": {
                    k: {"value": values[k], "printed": f.value, "confidence": round(f.confidence, 3)}
                    for k, f in extraction.fields.items()
                },
                "issues": issues,
                "pages": extraction.pages,
                "seconds": round(self.clock() - started, 2),
                "read_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            },
        )
        self.store.set_status(doc, status)
        self.queue.delete(message)
        return status

    def _set_aside(self, message: Message, reason: str) -> None:
        log.warning("set aside a message: %s", reason)  # Azure Monitor alerts on these
        self.queue.poison(message, reason)

    def run_once(self, batch: int = 16) -> list[str]:
        outcomes = []
        for message in self.queue.receive(batch, VISIBILITY_SECONDS):
            try:
                outcomes.append(self.handle(message))
            except Exception:  # keep going; the message comes back after its timeout
                log.exception("unexpected error on message %s", message.id)
                outcomes.append("error")
        return outcomes

    def run(self, idle_seconds: float = 5, sleep: Callable[[float], None] = time.sleep, rounds: int | None = None) -> None:
        done = 0
        while rounds is None or done < rounds:
            outcomes = self.run_once()
            if outcomes:
                log.info("handled %d: %s", len(outcomes), ", ".join(outcomes))
            else:
                sleep(idle_seconds)
            done += 1


def from_env(env: dict[str, str] | None = None) -> Worker:  # pragma: no cover - wiring for the real process
    import os

    from loanlens.extract import DocumentIntelligence, SampleReader
    from loanlens.queues import AzureQueue
    from loanlens.settings import Settings
    from loanlens.store import AzureStore

    env = dict(os.environ if env is None else env)
    settings = Settings.from_env(env)
    store = AzureStore(account=settings.storage_account, connection=settings.storage_connection, emulate_events=False)
    if settings.storage_connection:
        store.prepare()
    if env.get("LOANLENS_EXTRACTOR", "sample" if settings.auth == "dev" else "document-intelligence") == "sample":
        extractor = SampleReader()
    else:
        extractor = DocumentIntelligence(env["LOANLENS_DI_ENDPOINT"], env["LOANLENS_DI_MODEL"])
    return Worker(store, AzureQueue(store.queues), extractor, confidence=float(env.get("LOANLENS_CONFIDENCE", "0.8")))


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("azure").setLevel(logging.WARNING)  # the SDK logs every request at INFO
    from loanlens import telemetry

    telemetry.setup("worker")
    from_env().run()
