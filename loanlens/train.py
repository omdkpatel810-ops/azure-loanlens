"""Train LoanLens' custom T4 model in Azure AI Document Intelligence. Run by the operator, signed in with
`az login`, from an address the storage account allows:

    python -m loanlens.samples --out data/t4
    python -m loanlens.train data/t4/train --account <storage account> \\
        --endpoint https://<name>.cognitiveservices.azure.com --model-id loanlens-t4-v1

1. Uploads the training slips, their labels and fields.json to the `training` container, in a folder
   named after the model.
2. Runs Microsoft's layout model on each slip and stores its output beside it (`<file>.ocr.json`):
   training matches the labels to the words layout found.
3. Builds a neural model from that folder. Document Intelligence reads the folder with its own managed
   identity, so no storage key or SAS link is involved.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

CONTAINER = "training"


def dataset(folder: Path) -> list[Path]:
    docs = sorted(p for p in folder.iterdir() if p.suffix in (".pdf", ".jpg", ".png"))
    missing = [d.name for d in docs if not (folder / f"{d.name}.labels.json").exists()]
    if not docs or missing or not (folder / "fields.json").exists():
        raise ValueError(f"{folder} needs documents, a labels file for each and fields.json (missing labels: {missing})")
    return docs


def upload(container, folder: Path, prefix: str) -> list[str]:
    docs = dataset(folder)
    names = []
    for path in [*docs, *[folder / f"{d.name}.labels.json" for d in docs], folder / "fields.json"]:
        container.upload_blob(f"{prefix}/{path.name}", path.read_bytes(), overwrite=True)
        names.append(path.name)
    return names


def add_layout(analyzer, container, folder: Path, prefix: str) -> int:
    count = 0
    for doc in dataset(folder):
        result = analyzer.begin_analyze_document("prebuilt-layout", io.BytesIO(doc.read_bytes())).result()
        ocr = {"status": "succeeded", "analyzeResult": result.as_dict()}
        container.upload_blob(f"{prefix}/{doc.name}.ocr.json", json.dumps(ocr).encode(), overwrite=True)
        count += 1
    return count


def build(admin, model_id: str, container_url: str, prefix: str, mode: str = "neural") -> dict:
    from azure.ai.documentintelligence.models import AzureBlobContentSource, BuildDocumentModelRequest

    request = BuildDocumentModelRequest(
        model_id=model_id,
        build_mode=mode,
        description="LoanLens: T4 slips (trained on generated samples)",
        azure_blob_source=AzureBlobContentSource(container_url=container_url, prefix=f"{prefix}/"),
    )
    model = admin.begin_build_document_model(request).result()
    doc_types = model.doc_types or {}
    confidence = {}
    for details in doc_types.values():
        confidence.update(details.field_confidence or {})
    return {"model_id": model.model_id, "fields": sorted(confidence), "field_confidence": confidence}


def main(argv: list[str] | None = None, clients=None) -> int:
    parser = argparse.ArgumentParser(description="Train the custom T4 model in Document Intelligence.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--account", required=True, help="the storage account name")
    parser.add_argument("--endpoint", required=True, help="the Document Intelligence endpoint")
    parser.add_argument("--model-id", default="loanlens-t4-v1")
    parser.add_argument("--mode", choices=["neural", "template"], default="neural")
    args = parser.parse_args(argv)

    if clients is None:  # pragma: no cover - needs Azure
        from azure.ai.documentintelligence import DocumentIntelligenceAdministrationClient, DocumentIntelligenceClient
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        credential = DefaultAzureCredential()
        container = BlobServiceClient(
            f"https://{args.account}.blob.core.windows.net", credential=credential
        ).get_container_client(CONTAINER)
        clients = (
            container,
            DocumentIntelligenceClient(args.endpoint, credential),
            DocumentIntelligenceAdministrationClient(args.endpoint, credential),
        )
    container, analyzer, admin = clients
    print(f"Uploaded {len(upload(container, args.folder, args.model_id))} files.")
    print(f"Ran layout on {add_layout(analyzer, container, args.folder, args.model_id)} slips.")
    url = f"https://{args.account}.blob.core.windows.net/{CONTAINER}"
    model = build(admin, args.model_id, url, args.model_id, args.mode)
    print(f"Built {model['model_id']} with {len(model['fields'])} fields:")
    for field, value in sorted(model["field_confidence"].items()):
        print(f"  {field}: {value:.0%} estimated accuracy")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
