"""Reading a document: Azure AI Document Intelligence with LoanLens' custom T4 model, or, on a computer
without Azure, a stand-in that reads the true values LoanLens' own sample slips carry."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Protocol

from loanlens import samples
from loanlens.t4 import FIELDS


@dataclass(frozen=True)
class Field:
    value: str | None
    confidence: float


@dataclass(frozen=True)
class Extraction:
    model_id: str
    fields: dict[str, Field]
    pages: int = 1
    meta: dict = field(default_factory=dict)


class ExtractionFailed(Exception):
    """The document couldn't be read. `transient` failures (throttling, outages) are worth retrying."""

    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


class Extractor(Protocol):
    def extract(self, data: bytes, content_type: str) -> Extraction: ...


class DocumentIntelligence:
    """Calls Document Intelligence with the custom model, signed in as the worker's managed identity."""

    def __init__(self, endpoint: str, model_id: str, *, client=None, credential=None):
        if client is None:  # pragma: no cover - only in Azure
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.identity import DefaultAzureCredential

            client = DocumentIntelligenceClient(endpoint, credential or DefaultAzureCredential())
        self.client, self.model_id = client, model_id

    def extract(self, data: bytes, content_type: str) -> Extraction:
        from azure.core.exceptions import HttpResponseError, ServiceRequestError

        try:
            result = self.client.begin_analyze_document(
                self.model_id, io.BytesIO(data), content_type="application/octet-stream"
            ).result()
        except ServiceRequestError as e:
            raise ExtractionFailed(f"couldn't reach Document Intelligence: {e}", transient=True) from e
        except HttpResponseError as e:
            transient = e.status_code in (408, 429) or (e.status_code or 0) >= 500
            raise ExtractionFailed(f"Document Intelligence said {e.status_code}: {e.message}", transient=transient) from e
        documents = result.documents or []
        if not documents:
            raise ExtractionFailed("the model found no T4 in this document", transient=False)
        found = documents[0].fields or {}
        fields = {}
        for key in FIELDS:
            f = found.get(key)
            if f is None:
                fields[key] = Field(None, 0.0)
            else:
                fields[key] = Field(f.value_string if f.value_string is not None else f.content, float(f.confidence or 0.0))
        return Extraction(self.model_id, fields, pages=len(result.pages or []) or 1, meta={"doc_type": documents[0].doc_type})


class SampleReader:
    """For running LoanLens on a computer without Azure: reads LoanLens' own sample slips, and nothing else."""

    model_id = "local-sample-reader"

    def extract(self, data: bytes, content_type: str) -> Extraction:
        truth = samples.truth(data)
        if truth is None:
            raise ExtractionFailed("the local stand-in reads only LoanLens' sample slips", transient=False)
        return Extraction(self.model_id, {k: Field(truth.get(k), 0.99 if k in truth else 0.0) for k in FIELDS})
