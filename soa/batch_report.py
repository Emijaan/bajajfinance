"""Orchestrate batch SOA fetch + payment extraction for a date window."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from django.conf import settings

from .client import BajajClient, BajajError, BajajLoginError, BajajParallelSessionError
from .payment_extract import extract_payment_rows

logger = logging.getLogger(__name__)


def _delay_seconds() -> float:
    return float(getattr(settings, "SOA_BATCH_DELAY_SECONDS", 0.35))


def _max_loans() -> int:
    return int(getattr(settings, "SOA_BATCH_MAX_LOANS", 500))


def _max_upload_bytes() -> int:
    return int(getattr(settings, "SOA_BATCH_MAX_UPLOAD_BYTES", 2 * 1024 * 1024))


def build_payment_report(
    loan_nos: list[str],
    date_from: date,
    date_to: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (payment_rows_for_sheet, loan_status_rows_for_sheet)."""
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to.")

    capped = loan_nos[: _max_loans()]
    if len(loan_nos) > len(capped):
        logger.warning(
            "Batch truncated from %s to %s loans", len(loan_nos), len(capped)
        )

    client = BajajClient.instance()
    payment_out: list[dict[str, Any]] = []
    status_out: list[dict[str, Any]] = []

    for i, loan in enumerate(capped):
        if i:
            time.sleep(_delay_seconds())
        try:
            data = client.fetch_soa(loan)
        except BajajParallelSessionError as exc:
            status_out.append(
                {
                    "loan_no": loan,
                    "status": "session_conflict",
                    "detail": str(exc),
                    "payments_found": 0,
                }
            )
            continue
        except BajajLoginError as exc:
            status_out.append(
                {
                    "loan_no": loan,
                    "status": "login_error",
                    "detail": str(exc),
                    "payments_found": 0,
                }
            )
            continue
        except BajajError as exc:
            status_out.append(
                {
                    "loan_no": loan,
                    "status": "upstream_error",
                    "detail": str(exc),
                    "payments_found": 0,
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error fetching SOA for %s", loan)
            status_out.append(
                {
                    "loan_no": loan,
                    "status": "error",
                    "detail": str(exc),
                    "payments_found": 0,
                }
            )
            continue

        rows = extract_payment_rows(data)
        in_window = 0
        for r in rows:
            dt = r.get("date")
            if not isinstance(dt, date):
                continue
            if dt < date_from or dt > date_to:
                continue
            in_window += 1
            payment_out.append(
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

        status_out.append(
            {
                "loan_no": loan,
                "status": "ok",
                "detail": "",
                "payments_found": in_window,
            }
        )

    return payment_out, status_out
