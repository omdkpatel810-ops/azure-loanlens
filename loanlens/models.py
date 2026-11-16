"""What LoanLens stores: applications, and the documents uploaded to them."""

from __future__ import annotations

from dataclasses import dataclass

LOAN_TYPES = ("mortgage", "car", "personal")
DOCUMENT_KINDS = ("t4", "pay_stub", "other")
MIN_AMOUNT, MAX_AMOUNT = 1_000, 2_000_000
MAX_OPEN_APPLICATIONS = 5
MAX_DOCUMENTS = 20


@dataclass(frozen=True)
class User:
    id: str
    name: str
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Application:
    id: str
    applicant_id: str
    applicant_name: str
    loan_type: str
    amount: int
    status: str  # collecting documents, in review, decided
    created_at: str


@dataclass(frozen=True)
class Document:
    id: str
    application_id: str
    kind: str
    filename: str
    content_type: str
    size: int
    blob_name: str
    status: str  # uploaded, reading, read, needs review, failed
    uploaded_at: str
