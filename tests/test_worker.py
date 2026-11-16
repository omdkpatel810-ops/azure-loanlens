"""The worker: from an upload event to a recorded reading, and every way that can go wrong."""

from __future__ import annotations

import json
import random

import pytest

from loanlens import events, samples
from loanlens.extract import Extraction, ExtractionFailed, Field, SampleReader
from loanlens.models import User
from loanlens.queues import MemoryQueue
from loanlens.store import MemoryStore
from loanlens.worker import MAX_ATTEMPTS, Worker


def sample_slip(seed=1, **change) -> tuple[bytes, dict]:
    rng = random.Random(seed)
    fields = {**samples.invent(rng), **change}
    pdf, _ = samples.draw(fields, rng)
    return samples.with_truth(pdf, fields), fields


class Fixed:
    """An extractor that returns what it's told to, or raises."""

    model_id = "test-model"

    def __init__(self, fields=None, confidence=0.97, error=None):
        self.fields, self.confidence, self.error, self.calls = fields or {}, confidence, error, 0

    def extract(self, data, content_type):
        self.calls += 1
        if self.error:
            raise self.error
        return Extraction(self.model_id, {k: Field(v, self.confidence) for k, v in self.fields.items()}, pages=1)


@pytest.fixture
def setup():
    store = MemoryStore()
    queue = MemoryQueue(store.queue)
    app = store.create_application(User("u", "U"), "mortgage", 300_000)
    return store, queue, app


def upload(store, app, data=None, kind="t4"):
    data = data if data is not None else sample_slip()[0]
    return store.add_document(app, kind, "slip.pdf", "application/pdf", ".pdf", data)


def test_a_clean_t4_is_read(setup):
    store, queue, app = setup
    data, fields = sample_slip()
    doc = upload(store, app, data)
    clock = iter([10.0, 11.5])
    worker = Worker(store, queue, SampleReader(), clock=lambda: next(clock))
    assert worker.run_once() == ["read"]
    assert store.docs[doc.id].status == "read" and not queue.in_flight and not queue.pending
    reading = store.reading(app.id, doc.id)
    assert reading["fields"]["box14"]["value"] == fields["box14"] and reading["fields"]["box14"]["confidence"] == 0.99
    assert reading["issues"] == [] and reading["model_id"] == "local-sample-reader" and reading["seconds"] == 1.5


def test_a_misread_goes_to_a_person(setup):
    store, queue, app = setup
    _, fields = sample_slip()
    doc = upload(store, app)
    worker = Worker(store, queue, Fixed({**fields, "box22": str(float(fields["box14"]) * 2)}))
    assert worker.run_once() == ["needs review"]
    assert any("income tax deducted is more than the income" in i for i in store.reading(app.id, doc.id)["issues"])


def test_an_unsure_model_goes_to_a_person(setup):
    store, queue, app = setup
    _, fields = sample_slip()
    doc = upload(store, app)
    assert Worker(store, queue, Fixed(fields, confidence=0.55)).run_once() == ["needs review"]
    issues = store.reading(app.id, doc.id)["issues"]
    assert "Box 14: employment income: the model is only 55% sure" in issues
    # The threshold is a setting.
    store2 = MemoryStore()
    q2 = MemoryQueue(store2.queue)
    upload(store2, store2.create_application(User("u", "U"), "car", 9_000))
    assert Worker(store2, q2, Fixed(fields, confidence=0.55), confidence=0.5).run_once() == ["read"]


def test_other_documents_go_to_a_person(setup):
    store, queue, app = setup
    doc = upload(store, app, b"%PDF-1.7 a pay stub", kind="pay_stub")
    extractor = Fixed()
    assert Worker(store, queue, extractor).run_once() == ["needs review"]
    assert extractor.calls == 0 and "Only T4 slips are read automatically" in store.reading(app.id, doc.id)["issues"][0]


def test_throttling_is_retried_then_gives_up(setup):
    store, queue, app = setup
    doc = upload(store, app)
    worker = Worker(store, queue, Fixed(error=ExtractionFailed("429 too many requests", transient=True)))
    for attempt in range(1, MAX_ATTEMPTS):
        assert worker.run_once() == ["retry later"], attempt
        assert store.docs[doc.id].status == "uploaded" and queue.in_flight  # still on the queue
        queue.expire()
    assert worker.run_once() == ["failed"]
    assert store.docs[doc.id].status == "failed" and queue.poisoned[0]["reason"] == "429 too many requests"


def test_an_unreadable_document_fails_at_once(setup):
    store, queue, app = setup
    doc = upload(store, app, b"%PDF-1.7 not a sample")
    assert Worker(store, queue, SampleReader()).run_once() == ["failed"]
    assert store.docs[doc.id].status == "failed"
    assert "reads only LoanLens' sample slips" in store.reading(app.id, doc.id)["issues"][0]
    assert len(queue.poisoned) == 1


def test_the_same_event_twice_is_handled_once(setup):
    store, queue, app = setup
    upload(store, app)
    store.queue.append(json.dumps(json.loads(store.queue[0]), indent=1))  # a second delivery: a new message, the same event
    extractor = SampleReader()
    assert Worker(store, queue, extractor).run_once() == ["read", "already done"]


def test_events_for_files_without_a_record_are_set_aside(setup):
    store, queue, app = setup
    store.queue.append(json.dumps(events.blob_created("x", f"{app.id}/doc-unknown.pdf", "application/pdf", 5)))
    assert Worker(store, queue, Fixed()).run_once() == ["poisoned"]
    assert "wasn't uploaded through the portal" in queue.poisoned[0]["reason"]


def test_messages_that_arent_events(setup):
    store, queue, _ = setup
    store.queue.append("hello")
    other = events.blob_created("x", "a/b.pdf", "application/pdf", 1)
    other["subject"] = "/blobServices/default/containers/training/blobs/a/b.pdf"
    store.queue.append(json.dumps(other))
    assert Worker(store, queue, Fixed()).run_once() == ["poisoned", "ignored"]
    assert not queue.in_flight


def test_an_unexpected_error_leaves_the_message_for_later(setup, caplog):
    store, queue, app = setup
    upload(store, app)
    worker = Worker(store, queue, Fixed(error=RuntimeError("bug")))
    assert worker.run_once() == ["error"]
    assert queue.in_flight and "unexpected error" in caplog.text


def test_the_loop_waits_when_the_queue_is_empty(setup):
    store, queue, app = setup
    slept = []
    worker = Worker(store, queue, SampleReader())
    upload(store, app)
    worker.run(idle_seconds=7, sleep=slept.append, rounds=3)
    assert slept == [7, 7] and store.readings
