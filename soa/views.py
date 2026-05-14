"""HTTP views for the public SOA lookup page."""

from __future__ import annotations

import json
import logging
import re

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .client import BajajClient, BajajError, BajajLoginError

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

    try:
        data = BajajClient.instance().fetch_soa(agreement)
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
