"""The Canada East failover drill: Canada Central's storage is gone. How much is lost, and how long until
applicants can upload again?

    python -m loanlens.failover status
    python -m loanlens.failover drill --yes                  # planned failover to Canada East, timed
    python -m loanlens.failover drill --yes --failback       # and back to Canada Central afterwards

The documents account keeps a second copy in Canada East (read-access geo-redundant storage). The drill:

1. records the last time Canada East caught up (`lastSyncTime`): anything written after it is what an
   unplanned failover would lose (the recovery point);
2. checks the Canada East copy can already be read, before anything changes;
3. fails the account over to Canada East, writing a small test file every 15 seconds the whole time;
4. measures the recovery time: from the start until writes work again with Canada East as the primary.

The account's addresses don't change in a failover (DNS moves), so the portal and worker keep working
without a redeploy. What this drill doesn't cover: the apps themselves still run in Canada Central. Losing
all of Canada Central would also need a standby Container Apps environment in Canada East.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loanlens.azcli import Shell, az, run_shell

ROOT = Path(__file__).resolve().parent.parent
PROBE_CONTAINER = "uploads"
PROBE_PREFIX = "failover-probe/"


def status(shell: Shell, account: str, resource_group: str) -> dict:
    query = (
        "{primary: primaryLocation, secondary: secondaryLocation, sku: sku.name, "
        "lastSync: geoReplicationStats.lastSyncTime, canFailover: geoReplicationStats.canFailover}"
    )
    return json.loads(
        az(
            shell,
            [
                "storage",
                "account",
                "show",
                "-n",
                account,
                "-g",
                resource_group,
                "--expand",
                "geoReplicationStats",
                "--query",
                query,
            ],
        )
    )


def probe(shell: Shell, account: str) -> bool:
    """Write a tiny file to the account, signed in as the operator. True if it worked."""
    name = f"{PROBE_PREFIX}{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4]}.txt"
    code, _ = shell(
        [
            "az",
            "storage",
            "blob",
            "upload",
            "--account-name",
            account,
            "--container-name",
            PROBE_CONTAINER,
            "--name",
            name,
            "--data",
            "failover drill",
            "--auth-mode",
            "login",
            "--overwrite",
            "--only-show-errors",
        ],
        None,
    )
    return code == 0


def read_secondary(shell: Shell, account: str) -> bool:
    """Whether the Canada East copy answers a read, before any failover."""
    code, _ = shell(
        [
            "az",
            "storage",
            "container",
            "list",
            "--blob-endpoint",
            f"https://{account}-secondary.blob.core.windows.net",
            "--auth-mode",
            "login",
            "--num-results",
            "1",
            "--only-show-errors",
        ],
        None,
    )
    return code == 0


def _seconds(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds(), 1)


def drill(
    shell: Shell,
    account: str,
    resource_group: str,
    *,
    kind: str = "Planned",
    poll_seconds: float = 15,
    timeout_minutes: float = 90,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    before = status(shell, account, resource_group)
    if not before.get("canFailover"):
        raise RuntimeError(f"{account} can't fail over right now (canFailover is false); try again later")
    started = now()
    last_sync = datetime.fromisoformat(before["lastSync"]) if before.get("lastSync") else None
    readable = read_secondary(shell, account)
    az(
        shell,
        ["storage", "account", "failover", "-n", account, "-g", resource_group, "--failover-type", kind, "--yes", "--no-wait"],
    )
    probes, recovered, after = [], None, before
    deadline = started.timestamp() + timeout_minutes * 60
    while now().timestamp() < deadline:
        sleep(poll_seconds)
        ok = probe(shell, account)
        probes.append({"at": now().isoformat(timespec="seconds"), "ok": ok})
        after = status(shell, account, resource_group)
        if ok and after["primary"] == before["secondary"]:
            recovered = now()
            break
    return {
        "account": account,
        "kind": kind,
        "from": before["primary"],
        "to": before["secondary"],
        "started_at": started.isoformat(timespec="seconds"),
        "secondary_readable_before": readable,
        "recovery_point_seconds": _seconds(last_sync, started) if last_sync else None,
        "recovery_time_seconds": _seconds(started, recovered) if recovered else None,
        "failed_writes": sum(not p["ok"] for p in probes),
        "writes": len(probes),
        "primary_now": after["primary"],
        "sku_now": after.get("sku"),
    }


def to_markdown(result: dict) -> str:
    if result["recovery_time_seconds"] is None:
        head = f"**Failover from {result['from']} to {result['to']} didn't finish in time.**"
    else:
        head = (
            f"**Failover from {result['from']} to {result['to']}: writes worked again after "
            f"{result['recovery_time_seconds'] / 60:.1f} minutes.**"
        )
    rpo = result["recovery_point_seconds"]
    lines = [
        head,
        "",
        f"- Recovery point: Canada East was {rpo:g} seconds behind when the drill started."
        if rpo is not None
        else "- Recovery point: unknown.",
        f"- Before the failover, the Canada East copy {'could' if result['secondary_readable_before'] else 'could not'} be read.",
        f"- {result['failed_writes']} of {result['writes']} test writes failed during the switch.",
        f"- The account's primary is now {result['primary_now']} ({result['sku_now']}).",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, shell: Shell = run_shell, **kw) -> int:
    parser = argparse.ArgumentParser(description="The Canada East failover drill for LoanLens' document storage.")
    parser.add_argument("command", choices=["status", "drill"])
    parser.add_argument("--account", required=True)
    parser.add_argument("--resource-group", default="rg-loanlens-prod")
    parser.add_argument(
        "--type",
        choices=["Planned", "Unplanned"],
        default="Planned",
        help="Planned keeps both copies. Unplanned is what a real outage needs; it leaves one copy until "
        "geo-redundancy is turned back on",
    )
    parser.add_argument("--failback", action="store_true", help="fail back to the original region afterwards")
    parser.add_argument("--yes", action="store_true", help="really fail over")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(shell, args.account, args.resource_group), indent=2))
        return 0
    if not args.yes:
        print(
            f"Would fail {args.account} over to its secondary region ({args.type}), writing a test file every 15 seconds. "
            "Run again with --yes."
        )
        return 0
    results = [drill(shell, args.account, args.resource_group, kind=args.type, **kw)]
    print(to_markdown(results[0]))
    if args.failback and results[0]["recovery_time_seconds"] is not None:
        results.append(drill(shell, args.account, args.resource_group, kind="Planned", **kw))
        print(to_markdown(results[1]))
    out = ROOT / "results" / f"failover-{datetime.now(UTC):%Y-%m-%dT%H%M}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Saved {out}")
    return 0 if all(r["recovery_time_seconds"] is not None for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
