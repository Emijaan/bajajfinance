"""HTTP views for the public SOA lookup page."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import quote

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from .batch_job_worker import start_batch_report_job_thread
from .client import (
    BajajClient,
    BajajError,
    BajajLoginError,
    BajajParallelSessionError,
)
from .excel_io import read_loan_numbers_from_xlsx
from .models import BatchReportJob

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

    fname = (upload.name or "loans.xlsx").strip() or "loans.xlsx"
    if not fname.lower().endswith(".xlsx"):
        fname += ".xlsx"
    total_cap = min(
        len(loans), int(getattr(settings, "SOA_BATCH_MAX_LOANS", 20000))
    )
    up = SimpleUploadedFile(
        fname,
        raw,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    job = BatchReportJob.objects.create(
        date_from=date_from,
        date_to=date_to,
        total_loans=total_cap,
        status=BatchReportJob.Status.PENDING,
        input_file=up,
    )
    job.refresh_from_db()

    start_batch_report_job_thread(job.pk)
    target = (
        reverse("batch_job_status", kwargs={"job_id": job.pk})
        + "?access="
        + quote(job.access_token, safe="")
    )
    return redirect(target)


def _batch_job_for_request(request: HttpRequest, job_id) -> BatchReportJob:
    job = get_object_or_404(BatchReportJob, pk=job_id)
    if request.GET.get("access") != job.access_token:
        raise Http404("Job not found.")
    return job


@require_http_methods(["GET"])
def batch_job_status(request: HttpRequest, job_id) -> HttpResponse:
    job = _batch_job_for_request(request, job_id)
    return render(
        request,
        "soa/batch_job_status.html",
        {
            "job": job,
        },
    )


@require_http_methods(["GET"])
def batch_job_status_json(request: HttpRequest, job_id) -> JsonResponse:
    job = _batch_job_for_request(request, job_id)
    return JsonResponse(
        {
            "status": job.status,
            "total_loans": job.total_loans,
            "processed_loans": job.processed_loans,
            "payment_count": job.payment_count,
            "error": (job.error_message[:500] if job.error_message else ""),
        }
    )


@require_http_methods(["GET"])
def batch_job_download(request: HttpRequest, job_id) -> HttpResponse:
    job = _batch_job_for_request(request, job_id)
    if job.status != BatchReportJob.Status.DONE:
        return HttpResponse(
            "Report is not ready yet. Refresh the status page.",
            status=409,
            content_type="text/plain; charset=utf-8",
        )
    if not job.result_file:
        raise Http404("No result file.")

    data = job.result_file.read()
    stamp = job.updated_at.strftime("%Y%m%d-%H%M") if job.updated_at else "export"
    fname = f"soa-payments-{stamp}.xlsx"
    resp = HttpResponse(
        data,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    resp["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp["Content-Length"] = str(len(data))
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
