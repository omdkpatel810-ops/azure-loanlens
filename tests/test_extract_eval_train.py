"""Document Intelligence calls (with a stand-in client), the accuracy measurement and the training script."""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from loanlens import evaluate, samples, train
from loanlens.extract import DocumentIntelligence, Extraction, ExtractionFailed, Field, SampleReader


class Poller:
    def __init__(self, result):
        self._result = result

    def result(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeDI:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def begin_analyze_document(self, model_id, body, **kw):
        self.calls.append((model_id, body.read(), kw))
        if self.error:
            raise self.error
        return Poller(self.result)


def di_result(fields: dict[str, tuple[str | None, str | None, float]], doc_type="loanlens-t4-v1"):
    found = {k: SimpleNamespace(value_string=v, content=c, confidence=conf) for k, (v, c, conf) in fields.items()}
    return SimpleNamespace(documents=[SimpleNamespace(fields=found, doc_type=doc_type)], pages=[1, 2])


def test_reading_with_document_intelligence():
    fake = FakeDI(di_result({"box14": ("62,000.00", None, 0.96), "employer_name": (None, "Prairie Grain", 0.9)}))
    out = DocumentIntelligence("https://di", "loanlens-t4-v1", client=fake).extract(b"%PDF-1.7", "application/pdf")
    assert fake.calls[0][0] == "loanlens-t4-v1" and fake.calls[0][1] == b"%PDF-1.7"
    assert out.fields["box14"] == Field("62,000.00", 0.96)
    assert out.fields["employer_name"] == Field("Prairie Grain", 0.9)  # no typed value: the text it read
    assert out.fields["box17"] == Field(None, 0.0) and out.pages == 2 and out.meta == {"doc_type": "loanlens-t4-v1"}


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (ServiceRequestError("no route"), True),
        (HttpResponseError(message="slow down", response=SimpleNamespace(status_code=429, reason="x", headers={})), True),
        (HttpResponseError(message="down", response=SimpleNamespace(status_code=503, reason="x", headers={})), True),
        (HttpResponseError(message="bad file", response=SimpleNamespace(status_code=400, reason="x", headers={})), False),
    ],
    ids=["network", "throttled", "outage", "bad-file"],
)
def test_which_failures_are_worth_retrying(error, transient):
    with pytest.raises(ExtractionFailed) as e:
        DocumentIntelligence("https://di", "m", client=FakeDI(error=error)).extract(b"x", "application/pdf")
    assert e.value.transient is transient


def test_no_t4_found():
    empty = SimpleNamespace(documents=[], pages=[])
    with pytest.raises(ExtractionFailed, match="found no T4") as e:
        DocumentIntelligence("https://di", "m", client=FakeDI(empty)).extract(b"x", "application/pdf")
    assert e.value.transient is False


@pytest.fixture(scope="module")
def test_set(tmp_path_factory):
    folder = tmp_path_factory.mktemp("test")
    samples.write_set(folder, 6, random.Random(11), "test", photo_share=0.5)
    return folder


class Mistaken:
    """Right about everything except where told otherwise. Slips are picked by their box 14 amount."""

    model_id = "mistaken"

    def __init__(self, wrong: dict[str, dict[str, str]], unsure: frozenset = frozenset(), fail: frozenset = frozenset()):
        self.wrong, self.unsure, self.fail, self.reader = wrong, unsure, fail, SampleReader()

    def extract(self, data, content_type):
        key = samples.truth(data)["box14"]
        if key in self.fail:
            raise ExtractionFailed("unreadable", transient=False)
        fields = dict(self.reader.extract(data, content_type).fields)
        for k, v in self.wrong.get(key, {}).items():
            fields[k] = Field(v, 0.95)
        if key in self.unsure:
            fields = {k: Field(f.value, 0.5) for k, f in fields.items()}
        return Extraction(self.model_id, fields)


def test_the_stand_in_is_always_right(test_set):
    result = evaluate.evaluate(test_set, SampleReader())
    assert result["field_accuracy"] == 100.0 and result["documents_all_right"] == 100.0
    assert result["silent_errors"] == [] and result["flagged_for_a_person"] == 0.0
    assert result["by_kind"]["pdf"]["documents"] + result["by_kind"]["photo"]["documents"] == 6


def test_errors_are_counted_and_silent_ones_named(test_set):
    rows = [json.loads(line) for line in (test_set / "truth.jsonl").read_text().splitlines()]
    a, b, c, d = (r["box14"] for r in rows[:4])
    extractor = Mistaken(
        wrong={
            a: {"employer_name": "SOMEONE ELSE INC."},  # wrong, and nothing would catch it
            b: {"box22": "999999.00"},  # wrong, and the checks catch it
        },
        unsure=frozenset({c}),
        fail=frozenset({d}),
    )
    result = evaluate.evaluate(test_set, extractor)
    assert result["silent_errors"] == [rows[0]["document"]]
    assert result["documents_all_right"] == pytest.approx(round(100 * 3 / 6, 1))
    assert result["flagged_for_a_person"] == pytest.approx(round(100 * 3 / 6, 1))
    assert result["fields"]["employer_name"] < 100 and result["fields"]["box22"] < 100
    text = evaluate.to_markdown(result)
    assert text.startswith("**mistaken: ") and f"Silent errors: {rows[0]['document']}." in text
    assert "| Box 14: employment income | " in text


def test_the_command_line(test_set, tmp_path, capsys):
    out = tmp_path / "accuracy.json"
    assert evaluate.main([str(test_set), "--local", "--save", str(out)]) == 0
    assert "100% of fields read exactly right" in capsys.readouterr().out
    assert json.loads(out.read_text())["documents"] == 6
    assert evaluate.main([str(test_set)], extractor=SampleReader()) == 0
    with pytest.raises(SystemExit):
        evaluate.main([str(test_set)])
    assert evaluate.evaluate(test_set, SampleReader(), clock=iter(range(100)).__next__)["seconds_per_document"] == 1


class FakeContainer:
    def __init__(self):
        self.blobs = {}

    def upload_blob(self, name, data, overwrite=False):
        self.blobs[name] = data


class FakeLayout:
    def begin_analyze_document(self, model_id, body, **kw):
        assert model_id == "prebuilt-layout"
        return Poller(SimpleNamespace(as_dict=lambda: {"pages": [{"words": []}]}))


class FakeAdmin:
    def __init__(self):
        self.requests = []

    def begin_build_document_model(self, request):
        self.requests.append(request)
        types = {"loanlens-t4-v1": SimpleNamespace(field_confidence={"box14": 0.98, "year": 0.99})}
        return Poller(SimpleNamespace(model_id=request.model_id, doc_types=types))


def test_training(test_set, capsys):
    container, admin = FakeContainer(), FakeAdmin()
    code = train.main(
        [str(test_set), "--account", "stloanlens", "--endpoint", "https://di", "--mode", "template"],
        clients=(container, FakeLayout(), admin),
    )
    assert code == 0
    docs = [p.name for p in test_set.iterdir() if p.suffix in (".pdf", ".jpg")]
    assert {f"loanlens-t4-v1/{d}" for d in docs} | {f"loanlens-t4-v1/{d}.ocr.json" for d in docs} <= set(container.blobs)
    assert "loanlens-t4-v1/fields.json" in container.blobs
    assert json.loads(container.blobs[f"loanlens-t4-v1/{docs[0]}.ocr.json"])["status"] == "succeeded"
    (request,) = admin.requests
    assert request.build_mode == "template" and request.azure_blob_source.prefix == "loanlens-t4-v1/"
    assert request.azure_blob_source.container_url == "https://stloanlens.blob.core.windows.net/training"
    assert "box14: 98% estimated accuracy" in capsys.readouterr().out


def test_training_needs_labels(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    with pytest.raises(ValueError, match=r"missing labels: \['a.pdf'\]"):
        train.dataset(tmp_path)
