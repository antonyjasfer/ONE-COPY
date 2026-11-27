"""Reproducible behavioral traffic simulator for One Copy end-to-end HTTP verification.

Drives isolated httpx.Client cookie jars per simulated visitor against the deployed
Vercel proxy (or local dev server) using relative /api/* endpoints and an explicit
Origin header. Defaults to dry-run; pass --execute to send live HTTP traffic.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import random
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

DEFAULT_BASE_URL = "https://web-two-sigma-w7mbp23lzt.vercel.app"
TRANSIENT_STATUSES = {502, 503, 504}


@dataclass
class SessionOutcome:
    idx: int
    arm: str  # "control" | "check-status-first" | "error"
    outcome: str  # "primary_1_copy" | "duplicate_2_plus" | "abandoned" | "closed_early" | "error"
    failure_mode: str = "unrevealed"
    final_entry_count: int | None = None
    error: str | None = None


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _arm_label(backend_variant: str) -> str:
    if backend_variant in ("check_status", "check-status-first"):
        return "check-status-first"
    return "control"


def _post_with_retry(
    client: httpx.Client,
    path: str,
    json_body: dict[str, Any] | None = None,
    *,
    max_attempts: int = 3,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.post(path, json=json_body)
            if resp.status_code in TRANSIENT_STATUSES and attempt < max_attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            return resp
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"POST {path} failed after {max_attempts} attempts: {last_exc}")


def _get_with_retry(
    client: httpx.Client,
    path: str,
    *,
    max_attempts: int = 3,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.get(path)
            if resp.status_code in TRANSIENT_STATUSES and attempt < max_attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            return resp
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"GET {path} failed after {max_attempts} attempts: {last_exc}")


def simulate_dry_run_session(idx: int, seed: int) -> SessionOutcome:
    """Deterministic projection of the behavioral model without network calls."""
    rng = random.Random(seed * 1_000_003 + idx)
    arm = "control" if rng.random() < 0.5 else "check-status-first"
    failure_mode = "reply_lost" if rng.random() < 0.5 else "request_lost"

    if arm == "control":
        if rng.random() < 0.88:
            entries = 2 if failure_mode == "reply_lost" else 1
            return SessionOutcome(
                idx=idx,
                arm=arm,
                outcome="duplicate_2_plus" if entries >= 2 else "primary_1_copy",
                failure_mode=failure_mode,
                final_entry_count=entries,
            )
        return SessionOutcome(idx=idx, arm=arm, outcome="abandoned")

    # check-status-first
    if rng.random() < 0.90:
        if failure_mode == "reply_lost":
            if rng.random() < 0.95:
                return SessionOutcome(
                    idx=idx,
                    arm=arm,
                    outcome="primary_1_copy",
                    failure_mode=failure_mode,
                    final_entry_count=1,
                )
            return SessionOutcome(idx=idx, arm=arm, outcome="closed_early")
        # found == false
        if rng.random() < 0.82:
            return SessionOutcome(
                idx=idx,
                arm=arm,
                outcome="primary_1_copy",
                failure_mode=failure_mode,
                final_entry_count=1,
            )
        return SessionOutcome(idx=idx, arm=arm, outcome="abandoned")

    return SessionOutcome(idx=idx, arm=arm, outcome="abandoned")


def simulate_live_session(
    idx: int,
    seed: int,
    base_url: str,
    origin: str,
    timeout: float,
) -> SessionOutcome:
    rng = random.Random(seed * 1_000_003 + idx)
    headers = {"Origin": origin}

    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        try:
            s_resp = _post_with_retry(client, "/api/session")
            if s_resp.status_code != 200:
                return SessionOutcome(
                    idx=idx,
                    arm="error",
                    outcome="error",
                    error=f"POST /api/session -> {s_resp.status_code}: {s_resp.text[:160]}",
                )

            t_resp = _post_with_retry(
                client,
                "/api/trials",
                {"title": f"Sim entry #{idx:04d}"},
            )
            if t_resp.status_code != 201:
                return SessionOutcome(
                    idx=idx,
                    arm="error",
                    outcome="error",
                    error=f"POST /api/trials -> {t_resp.status_code}: {t_resp.text[:160]}",
                )
            trial_id = t_resp.json()["trialId"]

            init_action = str(uuid4())
            init_resp = _post_with_retry(
                client,
                f"/api/trials/{trial_id}/submit",
                {"actionId": init_action, "intent": "initial"},
            )
            if init_resp.status_code != 202:
                return SessionOutcome(
                    idx=idx,
                    arm="error",
                    outcome="error",
                    error=f"POST submit(initial) -> {init_resp.status_code}: {init_resp.text[:160]}",
                )

            arm = _arm_label(str(init_resp.json().get("variant", "retry")))

            if arm == "control":
                if rng.random() >= 0.88:
                    return SessionOutcome(idx=idx, arm=arm, outcome="abandoned")

                retry_resp = _post_with_retry(
                    client,
                    f"/api/trials/{trial_id}/submit",
                    {"actionId": str(uuid4()), "intent": "retry"},
                )
                if retry_resp.status_code != 200:
                    return SessionOutcome(
                        idx=idx,
                        arm=arm,
                        outcome="error",
                        error=f"POST submit(retry) -> {retry_resp.status_code}: {retry_resp.text[:160]}",
                    )
                code = retry_resp.json()["receiptCode"]
                return _fetch_and_display(client, idx, arm, code)

            # check-status-first
            if rng.random() >= 0.90:
                return SessionOutcome(idx=idx, arm=arm, outcome="abandoned")

            cs_resp = _post_with_retry(
                client,
                f"/api/trials/{trial_id}/check-status",
                {"actionId": str(uuid4())},
            )
            if cs_resp.status_code != 200:
                return SessionOutcome(
                    idx=idx,
                    arm=arm,
                    outcome="error",
                    error=f"POST check-status -> {cs_resp.status_code}: {cs_resp.text[:160]}",
                )
            cs_body = cs_resp.json()

            if cs_body.get("found") is True:
                if rng.random() >= 0.95:
                    return SessionOutcome(idx=idx, arm=arm, outcome="closed_early")
                code = cs_body["receiptCode"]
                return _fetch_and_display(client, idx, arm, code)

            # found == False -> 82% retry + displayed, 18% abandon due to extra step
            if rng.random() >= 0.82:
                return SessionOutcome(idx=idx, arm=arm, outcome="abandoned")

            retry_resp = _post_with_retry(
                client,
                f"/api/trials/{trial_id}/submit",
                {"actionId": str(uuid4()), "intent": "retry"},
            )
            if retry_resp.status_code != 200:
                return SessionOutcome(
                    idx=idx,
                    arm=arm,
                    outcome="error",
                    error=f"POST submit(retry) -> {retry_resp.status_code}: {retry_resp.text[:160]}",
                )
            code = retry_resp.json()["receiptCode"]
            return _fetch_and_display(client, idx, arm, code)
        except Exception as exc:
            return SessionOutcome(
                idx=idx,
                arm="error",
                outcome="error",
                error=str(exc),
            )


def _fetch_and_display(
    client: httpx.Client,
    idx: int,
    arm: str,
    receipt_code: str,
) -> SessionOutcome:
    rec_resp = _get_with_retry(client, f"/api/receipts/{receipt_code}")
    if rec_resp.status_code != 200:
        return SessionOutcome(
            idx=idx,
            arm=arm,
            outcome="error",
            error=f"GET /api/receipts/{receipt_code} -> {rec_resp.status_code}",
        )

    disp_resp = _post_with_retry(
        client,
        f"/api/receipts/{receipt_code}/displayed",
        {"actionId": str(uuid4())},
    )
    if disp_resp.status_code != 200:
        return SessionOutcome(
            idx=idx,
            arm=arm,
            outcome="error",
            error=f"POST /api/receipts/{receipt_code}/displayed -> {disp_resp.status_code}",
        )

    body = disp_resp.json()
    final_count = int(body.get("finalEntryCount", 0))
    failure_mode = str(body.get("failureMode", "unrevealed"))
    primary_converted = bool(body.get("primaryConverted", False))

    if final_count >= 2:
        outcome = "duplicate_2_plus"
    elif primary_converted and final_count == 1:
        outcome = "primary_1_copy"
    else:
        outcome = "abandoned"

    return SessionOutcome(
        idx=idx,
        arm=arm,
        outcome=outcome,
        failure_mode=failure_mode,
        final_entry_count=final_count,
    )


def print_summary_table(outcomes: list[SessionOutcome], *, is_dry_run: bool) -> None:
    by_arm: dict[str, dict[str, int]] = {
        "control": defaultdict(int),
        "check-status-first": defaultdict(int),
    }
    errors: list[SessionOutcome] = []

    for item in outcomes:
        if item.outcome == "error" or item.arm == "error":
            errors.append(item)
            continue
        bucket = by_arm.setdefault(item.arm, defaultdict(int))
        bucket["exposed"] += 1
        bucket[item.outcome] += 1
        bucket[f"mode:{item.failure_mode}"] += 1

    mode_label = "DRY-RUN PROJECTION (no HTTP requests sent)" if is_dry_run else "LIVE SIMULATED RESULTS"
    print(f"\n=== {mode_label} ===")
    header = (
        f"{'Arm':<20} | {'Exposed':>7} | {'Primary(1)':>10} | {'Dup(2+)':>8} | "
        f"{'Abandoned':>9} | {'EarlyClose':>10} | {'Primary%':>8} | {'Dup%':>7}"
    )
    print(header)
    print("-" * len(header))

    for arm in ("control", "check-status-first"):
        b = by_arm[arm]
        exp = b["exposed"]
        prim = b["primary_1_copy"]
        dup = b["duplicate_2_plus"]
        ab = b["abandoned"]
        early = b["closed_early"]
        prim_pct = f"{(prim / exp * 100):.1f}%" if exp else "0.0%"
        dup_pct = f"{(dup / exp * 100):.1f}%" if exp else "0.0%"
        print(
            f"{arm:<20} | {exp:>7} | {prim:>10} | {dup:>8} | "
            f"{ab:>9} | {early:>10} | {prim_pct:>8} | {dup_pct:>7}"
        )

    print("\nFailure mode breakdown (revealed terminal sessions):")
    for arm in ("control", "check-status-first"):
        b = by_arm[arm]
        print(
            f"  {arm:<20}: reply_lost={b['mode:reply_lost']}, "
            f"request_lost={b['mode:request_lost']}, unrevealed={b['mode:unrevealed']}"
        )

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for err in errors[:10]:
            print(f"  session #{err.idx}: {err.error}")


def fetch_and_print_ground_truth(base_url: str, origin: str, timeout: float) -> None:
    with httpx.Client(
        base_url=base_url,
        headers={"Origin": origin},
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        resp = _get_with_retry(client, "/api/stats/summary")
        if resp.status_code != 200:
            print(f"\nFailed to fetch GET /api/stats/summary: {resp.status_code} {resp.text[:200]}")
            return
        data = resp.json()

    print("\n=== POSTGRES GROUND TRUTH (GET /api/stats/summary) ===")
    print(f"Experiment        : {data.get('experiment')}")
    print(f"Cohorts           : {data.get('cohorts')}")
    print(f"Live Total Exposed: {data.get('liveTotalExposed')}")
    print(f"Primary Rate      : {data.get('primaryRate', 0.0) * 100:.2f}%")
    print(f"Duplicate Rate    : {data.get('duplicateRate', 0.0) * 100:.2f}%")
    print(f"Abandonment Rate  : {data.get('abandonmentRate', 0.0) * 100:.2f}%")
    rec = data.get("recoveryTimeSec") or {}
    print(f"Recovery Time (s) : p50={rec.get('p50')}, p90={rec.get('p90')}")

    rows = data.get("byArmAndFailureMode") or []
    if rows:
        print("\nBy Arm & Failure Mode:")
        hdr = (
            f"{'Variant':<14} | {'FailureMode':<12} | {'Exposed':>7} | {'Primary':>7} | "
            f"{'Dup':>5} | {'Aband':>5} | {'PrimRate':>8} | {'DupRate':>7}"
        )
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            print(
                f"{str(r['variant']):<14} | {str(r['failureMode']):<12} | "
                f"{r['totalExposed']:>7} | {r['primaryCount']:>7} | "
                f"{r['duplicateCount']:>5} | {r['abandonmentCount']:>5} | "
                f"{r['primaryRate'] * 100:>7.1f}% | {r['duplicateRate'] * 100:>6.1f}%"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reproducible behavioral traffic simulator for One Copy."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Target app base URL")
    parser.add_argument(
        "--origin",
        default=None,
        help="Origin header value (defaults to scheme://host of --base-url)",
    )
    parser.add_argument("--sessions", type=int, default=300, help="Number of simulated sessions")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help="Worker threads when --execute is set (default: 6)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="HTTP request timeout in seconds",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually send HTTP requests (defaults to dry-run without this flag)",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    origin = (args.origin or _origin_from_url(base_url)).rstrip("/")

    print(
        f"Configuration: base_url={base_url}, origin={origin}, "
        f"sessions={args.sessions}, seed={args.seed}, execute={args.execute}"
    )

    if not args.execute:
        outcomes = [simulate_dry_run_session(i, args.seed) for i in range(args.sessions)]
        print_summary_table(outcomes, is_dry_run=True)
        print("\nDry-run only: no HTTP traffic sent. Re-run with --execute to run live.")
        return

    outcomes: list[SessionOutcome] = []
    workers = max(1, min(args.concurrency, args.sessions))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                simulate_live_session,
                i,
                args.seed,
                base_url,
                origin,
                args.timeout,
            )
            for i in range(args.sessions)
        ]
        for fut in as_completed(futures):
            outcomes.append(fut.result())

    outcomes.sort(key=lambda o: o.idx)
    print_summary_table(outcomes, is_dry_run=False)
    fetch_and_print_ground_truth(base_url, origin, args.timeout)


if __name__ == "__main__":
    main()
