"""Orchestrate batch SOA fetch + payment extraction for a date window."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from datetime import date
from typing import Any

from django.conf import settings
from django.db import close_old_connections

from .client import BajajClient, BajajError, BajajLoginError, BajajParallelSessionError
from .payment_extract import extract_payment_rows

logger = logging.getLogger(__name__)

# (processed_loans, total_loans_cap, payment_rows_collected_so_far)
ProgressCallback = Callable[[int, int, int], None]


def _delay_seconds() -> float:
    return float(getattr(settings, "SOA_BATCH_DELAY_SECONDS", 0.35))


def _max_loans() -> int:
    return int(getattr(settings, "SOA_BATCH_MAX_LOANS", 20000))


def _max_upload_bytes() -> int:
    return int(getattr(settings, "SOA_BATCH_MAX_UPLOAD_BYTES", 10 * 1024 * 1024))


def _fetch_workers() -> int:
    w = int(getattr(settings, "SOA_BATCH_FETCH_WORKERS", 10))
    return max(1, min(32, w))


def _per_task_delay(workers: int) -> float:
    """Spread load when using a pool (sequential mode keeps full delay)."""
    d = _delay_seconds()
    if d <= 0:
        return 0.0
    if workers <= 1:
        return d
    return d / float(workers)


def _process_single_loan(
    idx: int,
    loan: str,
    *,
    date_from: date,
    date_to: date,
    workers: int,
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    """Fetch SOA for one loan and return (index, status_row, payment_rows)."""
    delay = _per_task_delay(workers)
    if delay > 0:
        time.sleep(delay)

    client = BajajClient.instance()
    try:
        data = client.fetch_soa(loan)
    except BajajParallelSessionError as exc:
        return (
            idx,
            {
                "loan_no": loan,
                "status": "session_conflict",
                "detail": str(exc),
                "payments_found": 0,
            },
            [],
        )
    except BajajLoginError as exc:
        return (
            idx,
            {
                "loan_no": loan,
                "status": "login_error",
                "detail": str(exc),
                "payments_found": 0,
            },
            [],
        )
    except BajajError as exc:
        return (
            idx,
            {
                "loan_no": loan,
                "status": "upstream_error",
                "detail": str(exc),
                "payments_found": 0,
            },
            [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error fetching SOA for %s", loan)
        return (
            idx,
            {
                "loan_no": loan,
                "status": "error",
                "detail": str(exc),
                "payments_found": 0,
            },
            [],
        )

    rows = extract_payment_rows(data)
    payment_rows: list[dict[str, Any]] = []
    in_window = 0
    for r in rows:
        dt = r.get("date")
        if not isinstance(dt, date):
            continue
        if dt < date_from or dt > date_to:
            continue
        in_window += 1
        payment_rows.append(
            {
                "loan_no": loan,
                "date": dt,
                "amount": r.get("amount"),
                "payment_type": r.get("payment_type"),
                "reference": r.get("reference", ""),
                "source": r.get("source"),
                "particulars": r.get("particulars", ""),
            }
        )

    status_row = {
        "loan_no": loan,
        "status": "ok",
        "detail": "",
        "payments_found": in_window,
    }
    return idx, status_row, payment_rows


def build_payment_report(
    loan_nos: list[str],
    date_from: date,
    date_to: date,
    *,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (payment_rows_for_sheet, loan_status_rows_for_sheet).

    SOA fetches run in a thread pool (``SOA_BATCH_FETCH_WORKERS``) so many
    agreements can be in flight at once; ``BajajClient`` uses per-request HTTP
    sessions so portal calls do not serialize on a single lock.

    ``on_progress(processed, total_cap, payment_count)`` is invoked as loans
    finish (completion order may differ from input order).
    """
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to.")

    capped = loan_nos[: _max_loans()]
    if len(loan_nos) > len(capped):
        logger.warning(
            "Batch truncated from %s to %s loans", len(loan_nos), len(capped)
        )

    total_cap = len(capped)
    if total_cap == 0:
        return [], []

    workers = _fetch_workers()
    progress_lock = threading.Lock()
    done_count = 0
    payment_total = 0

    def bump_progress(pay_delta: int) -> None:
        nonlocal done_count, payment_total
        if not on_progress:
            return
        with progress_lock:
            done_count += 1
            payment_total += pay_delta
            close_old_connections()
            try:
                on_progress(done_count, total_cap, payment_total)
            finally:
                close_old_connections()

    status_slot: list[dict[str, Any] | None] = [None] * total_cap
    pay_parts: list[list[dict[str, Any]]] = [[] for _ in range(total_cap)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _process_single_loan,
                i,
                loan,
                date_from=date_from,
                date_to=date_to,
                workers=workers,
            )
            for i, loan in enumerate(capped)
        ]
        for fut in as_completed(futures):
            idx, status_row, pay_rows = fut.result()
            status_slot[idx] = status_row
            pay_parts[idx] = pay_rows
            pay_delta = len(pay_rows) if status_row.get("status") == "ok" else 0
            bump_progress(pay_delta)

    status_out = [s for s in status_slot if s is not None]
    payment_out: list[dict[str, Any]] = []
    for chunk in pay_parts:
        payment_out.extend(chunk)

    return payment_out, status_out
