"""HTTP views for the public SOA lookup page."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from io import BytesIO

from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from .batch_report import build_payment_report
from .client import (
    BajajClient,
    BajajError,
    BajajLoginError,
    BajajParallelSessionError,
)
from .excel_io import read_loan_numbers_from_xlsx, write_payment_report_xlsx

logger = logging.getLogger(__name__)

# AgreementNo seen in the wild looks like "P6D9PRR66028313" — uppercase
# alphanumerics. Allow 5–40 chars to be safe.
_AGREEMENT_RE = re.compile(r"^[A-Z0-9]{5,40}$")


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    return render(request, "soa/index.html")


@require_GET
def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_soa(request: HttpRequest) -> JsonResponse:
    agreement = _extract_agreement(request)
    if agreement is None:
        return JsonResponse(
            {"error": "Missing 'agreement' parameter."},
            status=400,
        )

    agreement = agreement.strip().upper()
    if not _AGREEMENT_RE.match(agreement):
        return JsonResponse(
            {
                "error": (
                    "Invalid agreement number format. Expected 5-40 "
                    "uppercase alphanumeric characters."
                )
            },
            status=400,
        )

    force_remote = _wants_remote_logout_retry(request)
    try:
        client = BajajClient.instance()
        if force_remote:
            data = client.fetch_soa_after_remote_logout(agreement)
        else:
            data = client.fetch_soa(agreement)
    except BajajParallelSessionError as exc:
        logger.info("Bajaj parallel session / conflict: %s", exc)
        return JsonResponse(
            {
                "agreement": agreement,
                "code": "SESSION_CONFLICT",
                "error": (
                    "The Bajaj portal reports a conflicting or duplicate session "
                    "for this account."
                ),
                "detail": str(exc),
            },
            status=409,
        )
    except BajajLoginError as exc:
        logger.exception("Bajaj login failed")
        return JsonResponse(
            {"error": "Upstream login failed.", "detail": str(exc)},
            status=502,
        )
    except BajajError as exc:
        logger.exception("Bajaj SOA fetch failed")
        return JsonResponse(
            {"error": "Upstream SOA fetch failed.", "detail": str(exc)},
            status=502,
        )

    return JsonResponse(
        {"agreement": agreement, "data": data},
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_protect
@require_http_methods(["GET", "POST"])
def payment_report(request: HttpRequest) -> HttpResponse:
    """Upload Excel (loan numbers), date range → download payment report xlsx."""
    if request.method == "GET":
        return render(request, "soa/payment_report.html")

    err: str | None = None
    upload = request.FILES.get("file")
    if not upload:
        err = "Please choose an Excel file (.xlsx)."
    elif upload.size > int(settings.SOA_BATCH_MAX_UPLOAD_BYTES):
        err = (
            f"File too large (max {settings.SOA_BATCH_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."
        )
    else:
        name = (upload.name or "").lower()
        if not name.endswith(".xlsx"):
            err = "Only .xlsx files are supported."

    df_raw = request.POST.get("date_from", "").strip()
    dt_raw = request.POST.get("date_to", "").strip()
    date_from: date | None = None
    date_to: date | None = None
    try:
        if df_raw:
            date_from = datetime.strptime(df_raw, "%Y-%m-%d").date()
        if dt_raw:
            date_to = datetime.strptime(dt_raw, "%Y-%m-%d").date()
    except ValueError:
        if err is None:
            err = "Invalid date format. Use the date pickers (YYYY-MM-DD)."

    if err is None and (date_from is None or date_to is None):
        err = "Both date from and date to are required."

    if err:
        return render(
            request,
            "soa/payment_report.html",
            {"error": err, "date_from": df_raw, "date_to": dt_raw},
            status=400,
        )

    assert date_from is not None and date_to is not None

    try:
        raw = upload.read()
        loans = read_loan_numbers_from_xlsx(raw)
    except ValueError as exc:
        return render(
            request,
            "soa/payment_report.html",
            {"error": str(exc), "date_from": df_raw, "date_to": dt_raw},
            status=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read upload")
        return render(
            request,
            "soa/payment_report.html",
            {
                "error": f"Could not read Excel: {exc}",
                "date_from": df_raw,
                "date_to": dt_raw,
            },
            status=400,
        )

    if not loans:
        return render(
            request,
            "soa/payment_report.html",
            {
                "error": "No loan numbers found under the detected header row.",
                "date_from": df_raw,
                "date_to": dt_raw,
            },
            status=400,
        )

    try:
        payments, statuses = build_payment_report(loans, date_from, date_to)
    except ValueError as exc:
        return render(
            request,
            "soa/payment_report.html",
            {"error": str(exc), "date_from": df_raw, "date_to": dt_raw},
            status=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Batch report failed")
        return render(
            request,
            "soa/payment_report.html",
            {
                "error": str(exc),
                "date_from": df_raw,
                "date_to": dt_raw,
            },
            status=502,
        )

    xlsx = write_payment_report_xlsx(payments, statuses)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    resp = FileResponse(
        BytesIO(xlsx),
        as_attachment=True,
        filename=f"soa-payments-{stamp}.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    return resp


def _wants_remote_logout_retry(request: HttpRequest) -> bool:
    if request.method != "POST":
        return False
    ctype = request.content_type or ""
    if "application/json" not in ctype.lower():
        return False
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    flag = payload.get("remote_logout_and_retry")
    return flag is True or flag == "true" or flag == 1


def _extract_agreement(request: HttpRequest) -> str | None:
    if request.method == "GET":
        return request.GET.get("agreement")

    # POST: support both JSON and form-encoded bodies.
    ctype = request.content_type or ""
    if "application/json" in ctype.lower():
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return None
        value = payload.get("agreement") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None
    return request.POST.get("agreement")
