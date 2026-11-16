"""Milestone 4's tools: the burst test, the failover drill, the cost per application and the load test report."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from loanlens import burst, costs, failover, telemetry
from loanlens.extract import SampleReader
from loanlens.models import User
from loanlens.queues import MemoryQueue
from loanlens.store import MemoryStore
from loanlens.worker import Worker

ROOT = Path(__file__).resolve().parent.parent


# --- Burst test ------------------------------------------------------------------------------------------


def test_percentiles():
    assert burst.percentile([], 50) is None
    assert burst.percentile([5.0], 95) == 5.0
    values = [float(v) for v in range(1, 101)]
    assert (burst.percentile(values, 50), burst.percentile(values, 95)) == (50.0, 95.0)


def test_a_burst_end_to_end():
    store = MemoryStore()
    worker = Worker(store, MemoryQueue(store.queue), SampleReader())
    ticks = iter(range(1000))
    result = burst.run(store, 6, sleep=lambda s: worker.run_once(), clock=lambda: next(ticks), poll_seconds=1)
    assert result["applications"] == 6 and result["outcomes"] == {"read": 6}
    assert (
        result["seconds_to_read"]["median"] is not None
        and result["seconds_to_read"]["p95"] >= result["seconds_to_read"]["median"]
    )
    names = {a.applicant_name for a in store.all_applications()}
    assert names == {f"Burst Test {i}" for i in range(1, 7)}
    assert all(a.applicant_id.startswith(f"burst-{result['run']}-") for a in store.all_applications())
    text = burst.to_markdown(result)
    assert text.startswith("**Burst of 6 applications: ") and "| read | 6 |" in text


def test_a_burst_that_doesnt_finish():
    store = MemoryStore()
    ticks = iter(range(0, 100_000, 60))
    result = burst.run(store, 2, sleep=lambda s: None, clock=lambda: next(ticks), timeout_minutes=2)
    assert result["outcomes"] == {"not done in time": 2} and result["per_minute"] is None
    assert "nothing read" in burst.to_markdown(result)


def test_the_burst_command_line(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(burst, "ROOT", tmp_path)
    assert burst.main(["--applications", "3"]) == 0
    assert "Run again with --yes" in capsys.readouterr().out
    store = MemoryStore()
    worker = Worker(store, MemoryQueue(store.queue), SampleReader())
    assert burst.main(["--applications", "3", "--yes"], store=store, sleep=lambda s: worker.run_once(), poll_seconds=0) == 0
    assert "Saved" in capsys.readouterr().out and list((tmp_path / "results").glob("burst-*.json"))
    stuck = MemoryStore()
    assert burst.main(["--applications", "1", "--yes", "--timeout-minutes", "0"], store=stuck) == 1


# --- Failover drill --------------------------------------------------------------------------------------


class FakeAzure:
    """A storage account that fails over after a few status checks, with writes failing in between."""

    def __init__(self, can=True, switch_after=3, last_sync="2027-01-20T14:58:30+00:00"):
        self.primary, self.secondary = "canadacentral", "canadaeast"
        self.can, self.switch_after, self.last_sync = can, switch_after, last_sync
        self.failing_over = False
        self.checks = 0
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        text = " ".join(args)
        if "account show" in text:
            if self.failing_over:
                self.checks += 1
                if self.checks >= self.switch_after:
                    self.primary, self.secondary, self.failing_over = self.secondary, self.primary, False
            info = {
                "primary": self.primary,
                "secondary": self.secondary,
                "sku": "Standard_RAGRS",
                "lastSync": self.last_sync,
                "canFailover": self.can,
            }
            return 0, json.dumps(info)
        if "account failover" in text:
            self.failing_over = True
            return 0, "{}"
        if "blob upload" in text:
            return (1, "ERROR: unavailable") if self.failing_over else (0, "{}")
        if "container list" in text:
            return 0, "[]"
        return 1, "unexpected"


class Clock:
    def __init__(self):
        self.t = datetime(2027, 1, 20, 15, 0, tzinfo=UTC)

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def test_the_failover_drill():
    az, clock = FakeAzure(), Clock()
    result = failover.drill(az, "stloanlens", "rg-loanlens-prod", now=clock.now, sleep=clock.sleep)
    assert (result["from"], result["to"], result["primary_now"]) == ("canadacentral", "canadaeast", "canadaeast")
    assert result["recovery_point_seconds"] == 90.0  # 14:58:30 to 15:00:00
    assert result["recovery_time_seconds"] == 60.0  # writes fail for three 15-second checks, then work
    assert (result["failed_writes"], result["writes"]) == (3, 4)
    assert result["secondary_readable_before"] is True
    failover_call = next(c for c in az.calls if "failover" in c)
    assert failover_call[failover_call.index("--failover-type") + 1] == "Planned" and "--no-wait" in failover_call
    secondary = next(c for c in az.calls if "container" in c)
    assert "https://stloanlens-secondary.blob.core.windows.net" in secondary
    text = failover.to_markdown(result)
    assert text.startswith("**Failover from canadacentral to canadaeast: writes worked again after 1.0 minutes.**")
    assert "Canada East was 90 seconds behind" in text and "3 of 4 test writes failed" in text


def test_a_failover_that_doesnt_finish():
    az, clock = FakeAzure(switch_after=10_000, last_sync=None), Clock()
    result = failover.drill(az, "st", "rg", now=clock.now, sleep=clock.sleep, timeout_minutes=1)
    assert result["recovery_time_seconds"] is None and result["recovery_point_seconds"] is None
    text = failover.to_markdown(result)
    assert "didn't finish in time" in text and "Recovery point: unknown." in text


def test_no_failover_when_azure_says_no():
    with pytest.raises(RuntimeError, match="can't fail over right now"):
        failover.drill(FakeAzure(can=False), "st", "rg")


def test_an_unreadable_secondary_is_reported():
    az, clock = FakeAzure(), Clock()
    original = az.__call__

    def shell(args, stdin=None):
        return (1, "denied") if "container" in args else original(args, stdin)

    result = failover.drill(shell, "st", "rg", now=clock.now, sleep=clock.sleep)
    assert result["secondary_readable_before"] is False and "could not be read" in failover.to_markdown(result)


def test_the_failover_command_line(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(failover, "ROOT", tmp_path)
    assert failover.main(["status", "--account", "st"], shell=FakeAzure()) == 0
    assert '"canFailover": true' in capsys.readouterr().out
    assert failover.main(["drill", "--account", "st"], shell=FakeAzure()) == 0
    assert "Run again with --yes" in capsys.readouterr().out
    clock = Clock()
    assert (
        failover.main(["drill", "--account", "st", "--yes", "--failback"], shell=FakeAzure(), now=clock.now, sleep=clock.sleep)
        == 0
    )
    saved = json.loads(next((tmp_path / "results").glob("failover-*.json")).read_text())
    assert [r["to"] for r in saved] == ["canadaeast", "canadacentral"]  # there and back
    clock = Clock()
    stuck = FakeAzure(switch_after=10_000)
    assert failover.main(["drill", "--account", "st", "--yes", "--failback"], shell=stuck, now=clock.now, sleep=clock.sleep) == 1


def test_az_failures_are_reported():
    from loanlens.azcli import az

    with pytest.raises(RuntimeError, match="az storage account show failed: nope"):
        az(lambda a, s: (1, "nope"), ["storage", "account", "show"])


# --- Cost per application --------------------------------------------------------------------------------

EXPORT = """ChargePeriodStart,ServiceName,BilledCost,x_ResourceGroupName,ResourceId
2027-01-01T00:00:00Z,Azure AI Document Intelligence,4.10,rg-loanlens-prod,/subscriptions/s/resourceGroups/rg-loanlens-prod/providers/x
2027-01-01T00:00:00Z,Container Registry,5.00,rg-loanlens-prod,
2027-01-15T00:00:00Z,Azure OpenAI,0.25,,/subscriptions/s/resourceGroups/RG-LOANLENS-PROD/providers/Microsoft.CognitiveServices/accounts/oai
2027-01-15T00:00:00Z,Storage,1.00,rg-guardrails-platform,
2027-02-01T00:00:00Z,Storage,9.99,rg-loanlens-prod,
2027-01-20T00:00:00Z,,0.65,rg-loanlens-prod,
"""


@pytest.fixture
def january(tmp_path):
    store = MemoryStore()
    worker = Worker(store, MemoryQueue(store.queue), SampleReader())
    import random

    from loanlens import samples

    rng = random.Random(1)
    for i in range(4):
        app = store.create_application(User(f"u{i}", "U"), "car", 10_000)
        store.apps[app.id] = app.__class__(**{**app.__dict__, "created_at": f"2027-01-0{i + 1}T10:00:00.000+00:00"})
        fields = samples.invent(rng)
        store.add_document(
            app, "t4", "t4.pdf", "application/pdf", ".pdf", samples.with_truth(samples.draw(fields, rng)[0], fields)
        )
        store.add_document(app, "pay_stub", "stub.pdf", "application/pdf", ".pdf", b"%PDF-1.7")
        store.save_summary(app.id, "en", "fp", {"text": "x", "source": "model", "tokens": 500})
    old = store.create_application(User("old", "O"), "car", 10_000)
    store.apps[old.id] = old.__class__(**{**old.__dict__, "created_at": "2026-12-30T10:00:00.000+00:00"})
    worker.run_once()
    path = tmp_path / "costs.csv"
    path.write_text(EXPORT)
    return store, path


def test_usage_for_a_month(january):
    store, _ = january
    use = costs.usage(store, "2027-01")
    assert use == costs.Usage(applications=4, documents=8, pages=4, tokens=2000)  # the stand-in reads 1 page per T4


def test_billed_by_service(january):
    _, path = january
    assert costs.billed(path, "2027-01") == {
        "Azure AI Document Intelligence": Decimal("4.10"),
        "Container Registry": Decimal("5.00"),
        "Azure OpenAI": Decimal("0.25"),  # found by its resource ID
        "Other": Decimal("0.65"),
    }


def test_the_cost_report(january, capsys):
    store, path = january
    args = ["--export", str(path), "--month", "2027-01", "--di-per-1000-pages", "10", "--openai-per-million-tokens", "0.6"]
    assert costs.main(args, store=store) == 0
    text = capsys.readouterr().out
    assert "4 applications, 8 documents, 4 pages read, 2,000 summary tokens" in text
    assert "$10.00 billed, **$2.50 per application**" in text
    assert "| Container Registry | $5.00 |" in text
    assert "**$0.0103**" in text  # (4 pages x $10/1000 + 2000 tokens x $0.6/1M) / 4


def test_a_month_with_no_applications(january):
    store, path = january
    text = costs.report(costs.usage(store, "2027-03"), costs.billed(path, "2027-03"), "2027-03")
    assert "no applications started, so no cost per application" in text and "| Service |" not in text


# --- Load test report and telemetry ----------------------------------------------------------------------

LOCUST = """Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%
GET,/applications/[id],500,0,10,12,3,80,2000,10.0,0.0,10,12,14,16,20,38,50,60,80,80,80
POST,/applications/[id]/documents,100,{fail},18,20,8,90,2000,2.0,0.0,18,20,22,24,26,27,40,50,90,90,90
,Aggregated,600,{fail},12,14,3,90,2000,12.0,0.0,12,14,16,18,30,62,70,80,90,90,90
"""


def test_the_load_test_report(tmp_path, capsys, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("report", ROOT / "loadtest" / "report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    good, bad = tmp_path / "good.csv", tmp_path / "bad.csv"
    good.write_text(LOCUST.format(fail=0))
    bad.write_text(LOCUST.format(fail=3))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert report.main([str(good)]) == 0
    assert "**600 requests, 0 failed, 12.0 a second; median 12 ms, 95th percentile 62 ms.**" in capsys.readouterr().out
    assert "| POST /applications/[id]/documents | 100 | 0 | 18 | 27 |" in summary.read_text()
    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    assert report.main([str(bad)]) == 1


def test_telemetry_is_off_without_application_insights():
    assert telemetry.setup("portal", {}) is False


def test_a_burst_where_reading_fails():
    from loanlens.extract import ExtractionFailed

    class Broken:
        model_id = "broken"

        def extract(self, data, content_type):
            raise ExtractionFailed("bad file", transient=False)

    store = MemoryStore()
    worker = Worker(store, MemoryQueue(store.queue), Broken())
    result = burst.run(store, 2, sleep=lambda s: worker.run_once(), clock=iter(range(100)).__next__, poll_seconds=1)
    assert result["outcomes"] == {"failed": 2} and result["seconds_to_read"]["median"] is None
