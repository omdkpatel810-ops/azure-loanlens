"""The uploads queue, as the worker sees it: take messages, delete them when done, and set aside the
ones that keep failing (the poison queue) so they stop blocking the rest."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from loanlens import events


@dataclass(frozen=True)
class Message:
    id: str
    pop_receipt: str
    content: str
    dequeue_count: int


class Queue(Protocol):
    def receive(self, count: int, visibility_seconds: int) -> list[Message]: ...
    def delete(self, message: Message) -> None: ...
    def poison(self, message: Message, reason: str) -> None: ...


class MemoryQueue:
    """For tests: messages taken but not deleted come back when `expire()` is called, as they would
    after their visibility timeout in Azure."""

    def __init__(self, pending: list[str]):
        self.pending = pending  # shared with MemoryStore.queue, where the portal puts upload events
        self.ids: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.in_flight: dict[str, str] = {}
        self.poisoned: list[dict] = []

    def receive(self, count: int, visibility_seconds: int) -> list[Message]:
        taken = []
        while self.pending and len(taken) < count:
            content = self.pending.pop(0)
            message_id = self.ids.setdefault(content, f"msg-{len(self.ids) + 1}")
            self.counts[message_id] = self.counts.get(message_id, 0) + 1
            self.in_flight[message_id] = content
            taken.append(Message(message_id, f"receipt-{self.counts[message_id]}", content, self.counts[message_id]))
        return taken

    def delete(self, message: Message) -> None:
        del self.in_flight[message.id]

    def poison(self, message: Message, reason: str) -> None:
        self.poisoned.append({"reason": reason, "message": message.content})
        self.delete(message)

    def expire(self) -> None:
        self.pending.extend(self.in_flight.values())
        self.in_flight.clear()


class AzureQueue:
    def __init__(self, service):
        self.queue = service.get_queue_client(events.QUEUE)
        self.poison_queue = service.get_queue_client(events.POISON_QUEUE)

    def receive(self, count: int, visibility_seconds: int) -> list[Message]:
        got = self.queue.receive_messages(messages_per_page=count, max_messages=count, visibility_timeout=visibility_seconds)
        return [Message(m.id, m.pop_receipt, m.content, m.dequeue_count) for m in got]

    def delete(self, message: Message) -> None:
        self.queue.delete_message(message.id, message.pop_receipt)

    def poison(self, message: Message, reason: str) -> None:
        self.poison_queue.send_message(json.dumps({"reason": reason, "message": message.content}))
        self.delete(message)
