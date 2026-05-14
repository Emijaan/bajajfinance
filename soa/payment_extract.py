"""Extract 'Payment Received' rows from Bajaj GetSOAReport payloads.

Bajaj may return either structured JSON (nested objects/arrays) or embed the SOA
as a base64 PDF under a ``url`` field (often nested, e.g. ``data[0].url``). This
module tries JSON row discovery first, then decodes the PDF and scans text.

Field names are not documented in-repo; JSON discovery walks dicts and uses
fuzzy key matching for date / particulars / credit columns.
"""

from __future__ import annotations

import base64
import io
import re
from datetime import date, datetime
from typing import Any, Literal

PaymentType = Literal["online", "cash", "unknown"]

# DD-Mon-YYYY as shown on SOA (e.g. 08-Apr-2025)
_RE_DD_MON_YYYY = re.compile(
    r"\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b",
    re.IGNORECASE,
)
# Split PDF text into chunks that start with a statement date.
_RE_ROW_BOUNDARY = re.compile(r"(?=\b\d{1,2}-[A-Za-z]{3}-\d{4}\b)")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_PARTICULARS_KEYS = (
    "particulars",
    "description",
    "narration",
    "remarks",
    "particular",
    "tranparticulars",
    "transdesc",
    "details",
)

_DATE_KEYS = (
    "date",
    "txndate",
    "transactiondate",
    "trandate",
    "valuedate",
    "duedate",
    "voucherdate",
    "postingdate",
)

_CREDIT_KEYS = (
    "credit",
    "creditamt",
    "creditamount",
    "cr",
    "cramount",
    "received",
    "paid",
    "amountreceived",
)


def is_likely_pdf_base64(s: str) -> bool:
    """Match the browser-side check in ``soa/templates/soa/index.html``."""
    if not s or not isinstance(s, str) or len(s) < 24:
        return False
    t = re.sub(r"\s+", "", s)
    if t.startswith("JVBERi"):
        return True
    try:
        take = min(len(t), 48)
        chunk = t[:take]
        pad = "=" * ((4 - len(chunk) % 4) % 4)
        raw = base64.b64decode(chunk + pad, validate=False)
        return raw[:4] == b"%PDF"
    except Exception:
        return False


def extract_pdf_base64_from_tree(data: Any) -> str | None:
    """First base64 PDF string found in any ``url`` field (recursive)."""
    if data is None or not isinstance(data, (dict, list)):
        return None
    if isinstance(data, list):
        for item in data:
            found = extract_pdf_base64_from_tree(item)
            if found:
                return found
        return None
    u = data.get("url")
    if isinstance(u, str) and is_likely_pdf_base64(u):
        return u
    for v in data.values():
        found = extract_pdf_base64_from_tree(v)
        if found:
            return found
    return None


def parse_dd_mon_yyyy(s: str) -> date | None:
    m = _RE_DD_MON_YYYY.search(s.strip())
    if not m:
        return None
    d, mon, y = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        return date(y, month, d)
    except ValueError:
        return None


def classify_payment_type(particulars: str) -> PaymentType | None:
    """Return payment channel if this row is a 'Payment Received' line, else None.

    Bajaj SOA commonly uses (often across line breaks in PDF text)::

        Payment Received vide
        ONLINE payment No: …   /   CASH payment No: …

    Older exports may still say ``ONLINE vide Reference No:`` etc.; those are kept
    as fallbacks.
    """
    if not particulars or not isinstance(particulars, str):
        return None
    t = re.sub(r"\s+", " ", particulars.strip().lower())
    if "payment received" not in t and "payment recieved" not in t:
        return None

    if re.search(r"online\s+payment\s*no", t):
        return "online"
    if re.search(r"cash\s+payment\s*no", t):
        return "cash"

    if re.search(r"\bonline\b", t) and (
        "vide reference" in t
        or "reference no" in t
        or "payment received vide" in t
    ):
        return "online"
    if re.search(r"\bcash\b", t) and ("vide" in t or "payment received vide" in t):
        return "cash"
    if re.search(r"\bonline\b", t):
        return "online"
    if re.search(r"\bcash\b", t):
        return "cash"
    return "unknown"


def _norm_key(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _particulars_from_dict(d: dict[str, Any]) -> str:
    nk = {_norm_key(k): k for k in d}
    for pk in _PARTICULARS_KEYS:
        for cand, orig in nk.items():
            if pk in cand or cand in pk:
                v = d.get(orig)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if v is not None and not isinstance(v, (dict, list)):
                    return str(v).strip()
    return ""


def _date_from_dict(d: dict[str, Any]) -> date | None:
    nk = {_norm_key(k): k for k in d}
    for dk in _DATE_KEYS:
        for cand, orig in nk.items():
            if dk in cand:
                v = d.get(orig)
                if isinstance(v, str):
                    parsed = parse_dd_mon_yyyy(v)
                    if parsed:
                        return parsed
                    try:
                        return datetime.strptime(v[:10], "%Y-%m-%d").date()
                    except ValueError:
                        pass
                if isinstance(v, datetime):
                    return v.date()
                if isinstance(v, date):
                    return v
    return None


def _credit_from_dict(d: dict[str, Any]) -> float | None:
    nk = {_norm_key(k): k for k in d}
    for ck in _CREDIT_KEYS:
        for cand, orig in nk.items():
            if ck in cand:
                v = d.get(orig)
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, str):
                    try:
                        return float(v.replace(",", "").strip())
                    except ValueError:
                        continue
    return None


def _iter_dicts(obj: Any, depth: int = 0) -> Any:
    if depth > 18:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_dicts(item, depth + 1)


def extract_payment_rows_from_json(soa_data: Any) -> list[dict[str, Any]]:
    """Find dict rows anywhere in the tree that look like SOA lines with payments."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for d in _iter_dicts(soa_data):
        parts = _particulars_from_dict(d)
        if not parts:
            continue
        ptype = classify_payment_type(parts)
        if ptype is None:
            continue
        dt = _date_from_dict(d)
        if dt is None:
            dt = parse_dd_mon_yyyy(parts)
        amt = _credit_from_dict(d)
        key = (parts[:220], str(dt or ""), ptype)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "date": dt,
                "particulars": parts,
                "payment_type": ptype,
                "amount": amt,
                "source": "json",
            }
        )
    return out


def _pdf_to_text(pdf_bytes: bytes) -> str:
    import pdfplumber

    chunks: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                chunks.append(t)
    return "\n".join(chunks)


def _truncate_segment_before_total(segment: str) -> str:
    """Drop statement totals so we do not pick up aggregate debit/credit columns."""
    m = re.search(r"\bTotal\b", segment, re.I)
    if m:
        return segment[: m.start()].rstrip()
    return segment


def _heuristic_amount_from_segment(segment: str) -> float | None:
    """Infer credit for this row; avoid max() across totals (e.g. 37,613.00)."""
    body = _truncate_segment_before_total(segment)
    if not body:
        body = segment

    # Bajaj layout: transaction line often looks like ``- 0.00 8,400.00 0.00 ...``
    # (Credit (₹) is typically the first non-zero amount after a leading 0.00 debit.)
    m = re.search(r"(?:^|[\s\n])-\s+0\.00\s+([\d,]+\.\d{2})\b", body, re.MULTILINE)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    for line in body.splitlines():
        line = line.strip()
        if not line or re.search(r"\bTotal\b", line, re.I):
            continue
        nums = re.findall(r"\b(\d{1,3}(?:,\d{3})*\.\d{2})\b", line)
        if len(nums) < 2:
            continue
        try:
            floats = [float(x.replace(",", "")) for x in nums]
        except ValueError:
            continue
        if floats[0] == 0 and floats[1] > 0:
            return floats[1]

    nums = re.findall(r"\b(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\b", body)
    vals: list[float] = []
    for n in nums:
        try:
            vals.append(float(n.replace(",", "")))
        except ValueError:
            continue
    if not vals:
        return None
    positives = [x for x in vals if x > 0]
    if not positives:
        return None
    return min(positives)


def extract_payment_rows_from_pdf_b64(b64: str) -> list[dict[str, Any]]:
    """Parse SOA PDF text for 'Payment Received' blocks (multi-line rows)."""
    clean = re.sub(r"\s+", "", b64)
    try:
        pad = "=" * ((4 - len(clean) % 4) % 4)
        pdf_bytes = base64.b64decode(clean + pad)
    except Exception:
        return []
    if not pdf_bytes.startswith(b"%PDF"):
        return []

    text = _pdf_to_text(pdf_bytes)
    if not text:
        return []

    normalized = re.sub(r"[ \t]+", " ", text)
    normalized = re.sub(r"\n{2,}", "\n", normalized)

    pieces: list[str] = []
    for p in _RE_ROW_BOUNDARY.split(normalized):
        p = p.strip()
        if p and _RE_DD_MON_YYYY.search(p[:24]):
            pieces.append(p)

    if not pieces:
        for m in _RE_DD_MON_YYYY.finditer(normalized):
            chunk = normalized[m.start() : m.start() + 700].strip()
            if "payment received" in chunk.lower():
                pieces.append(chunk)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in pieces:
        ptype = classify_payment_type(chunk)
        if ptype is None:
            continue
        dm = _RE_DD_MON_YYYY.search(chunk)
        if not dm:
            continue
        dt = parse_dd_mon_yyyy(dm.group(0))
        if not dt:
            continue
        amt = _heuristic_amount_from_segment(chunk)
        sk = (str(dt), chunk[:200])
        if sk in seen:
            continue
        seen.add(sk)
        ref = ""
        rm = re.search(r"(?:Reference\s*No:?\s*)([A-Za-z0-9/\s-]+)", chunk, re.I)
        if rm:
            ref = re.sub(r"\s+", " ", rm.group(1).strip())[:80]
        out.append(
            {
                "date": dt,
                "particulars": chunk[:500],
                "payment_type": ptype,
                "amount": amt,
                "reference": ref,
                "source": "pdf",
            }
        )
    return out


def extract_payment_rows(soa_data: Any) -> list[dict[str, Any]]:
    """Prefer JSON hits; if none, scan embedded PDF (base64 under ``url``)."""
    js = extract_payment_rows_from_json(soa_data)
    if js:
        return js
    b64 = extract_pdf_base64_from_tree(soa_data)
    if b64:
        return extract_payment_rows_from_pdf_b64(b64)
    return []
