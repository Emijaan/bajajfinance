"""HTTP client that maintains a hot session against the Bajaj DMS portal.

The portal expects its login payload to be JSON-stringified, RSA-encrypted
with a public key embedded in its frontend bundle (node-forge, PKCS#1 v1.5
padding), then base64-encoded. After login it issues a short-lived JWT
(`PortalSession` cookie, ~15 min). The agency report endpoints additionally
expect an ``Estag`` header that is set during the `/agency` handshake.

The single ``BajajClient`` instance per process is exposed via
``BajajClient.instance()`` and is safe for concurrent use thanks to an
internal reentrant lock.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from django.conf import settings

logger = logging.getLogger(__name__)


# Public RSA key extracted from the Bajaj login bundle
# (`login_main.<hash>.js`, field `publicKey`).
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqaq+Dy/P3yhjzUVEZ6Am
rxLZecN1yxwWjnIvOzq7MVkn8i7t81B8wnnGZ+Z3O58JAAbRYePpdU/TopjxfVNL
ER3jvB+vz+jzJF45tj664Od9b+Xr/1t+u/RuiHsll6TCK3GPPSM7DL90sX7MFpaq
WhoYBizDrBlfxAHaW3++yl2l63JzD7K5MbfTvBOkl6FSGfbqFvEScfTXqEmx0D5a
wv8c7qQzQ3BEaK2xYqO1GKtyf3eLNAGDReJuXCewAGpTWaqZFC8dRgUb5/fkCqDS
0u936nSYnJxFrXVFaU8b/CGITYRTnBsUbe9x5sBn0qUIx2hyIcQ+uhtyvBslzw0M
iwIDAQAB
-----END PUBLIC KEY-----
"""

BASE_URL = "https://dmsoneportal.bajajfinserv.in"

# Re-login this many seconds before the JWT actually expires so that
# in-flight requests on a near-expired token don't 401.
RENEW_BUFFER_SECONDS = 30

# Modern Chrome on Windows. Bajaj sniffs UA in places.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


class BajajError(RuntimeError):
    """Generic failure talking to the Bajaj DMS portal."""


class BajajLoginError(BajajError):
    """Login (RSA / credentials / COC) failed."""


class BajajClient:
    """Session-managing HTTP client for the Bajaj DMS Agency portal."""

    _singleton: "BajajClient | None" = None
    _singleton_lock = threading.Lock()

    def __init__(self, username: str, password: str) -> None:
        if not username or not password:
            raise BajajLoginError(
                "BAJAJ_USERNAME and BAJAJ_PASSWORD must both be set in the "
                "environment / .env before the client can be used."
            )
        self._username = username.upper()
        self._password = password
        self._public_key: RSAPublicKey = serialization.load_pem_public_key(
            PUBLIC_KEY_PEM
        )  # type: ignore[assignment]

        self._session = requests.Session()
        self._session.headers.update(_BROWSER_HEADERS)

        self._lock = threading.RLock()
        self._expires_at: datetime | None = None
        self._user_id: str | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @classmethod
    def instance(cls) -> "BajajClient":
        """Return the process-wide singleton, creating it on first call."""
        if cls._singleton is None:
            with cls._singleton_lock:
                if cls._singleton is None:
                    cls._singleton = cls(
                        username=settings.BAJAJ_USERNAME,
                        password=settings.BAJAJ_PASSWORD,
                    )
        return cls._singleton

    def fetch_soa(self, agreement_no: str) -> Any:
        """Return the parsed SOA JSON for a given agreement number."""
        agreement_no = agreement_no.strip().upper()
        if not agreement_no:
            raise BajajError("agreement_no is required")

        with self._lock:
            self._ensure_session()
            try:
                return self._call_soa(agreement_no)
            except _SessionExpired:
                logger.info("Bajaj session rejected on SOA call; re-login.")
                self._login()
                return self._call_soa(agreement_no)

    # ------------------------------------------------------------------ #
    # Login / session management
    # ------------------------------------------------------------------ #

    def _ensure_session(self) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._expires_at is None
            or (self._expires_at - now).total_seconds() < RENEW_BUFFER_SECONDS
        ):
            self._login()

    def _login(self) -> None:
        # Drop any stale cookies before re-auth so we start clean.
        self._session.cookies.clear()
        self._expires_at = None
        self._user_id = None

        # Field shape & values mirror the Bajaj login page exactly. The
        # frontend's first ("Get") call to /api/Authorize/Login is:
        #   loginService.login(user, pass, location.origin+location.pathname,
        #                      e.appId /* undefined */, "Get", "")
        # JSON.stringify drops undefined-valued keys, so `App` is OMITTED.
        plaintext_payload = {
            "Username": self._username,
            "Password": self._password,
            "sourceURI": f"{BASE_URL}/login/",
            "IN_Flag": "Get",
            "portalSelected": "",
            # node-forge's `JSON.stringify(new Date())` yields ISO-8601 with ms.
            "TimeOut": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
        login_key = self._encrypt_login_key(plaintext_payload)

        url = f"{BASE_URL}/login/api/Authorize/Login"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/login/",
        }
        body = {"cloudProvider": None, "LoginKey": login_key}

        logger.debug("POST %s (LoginKey=%d chars)", url, len(login_key))
        resp = self._session.post(url, json=body, headers=headers, timeout=30)
        if not resp.ok:
            logger.error(
                "Bajaj login HTTP %s; response headers=%s body=%r",
                resp.status_code,
                dict(resp.headers),
                resp.text[:1000],
            )
        self._raise_for_login_error(resp)

        portal_session = self._session.cookies.get("PortalSession")
        if not portal_session:
            raise BajajLoginError(
                "Login response did not set the PortalSession cookie. "
                f"Status={resp.status_code}, body={resp.text[:500]!r}"
            )

        claims = jwt.decode(
            portal_session,
            options={"verify_signature": False, "verify_exp": False},
        )
        exp = claims.get("exp")
        if not exp:
            raise BajajLoginError("PortalSession JWT has no 'exp' claim")
        self._expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
        self._user_id = claims.get("Userid") or claims.get("unique_name")
        logger.info(
            "Bajaj login OK as %s; session valid until %s",
            self._user_id,
            self._expires_at.isoformat(),
        )

        # Best-effort COC acceptance check (mirrors what the real frontend
        # does right after login). Failures here are non-fatal.
        if self._user_id:
            try:
                self._session.get(
                    f"{BASE_URL}/login/api/COCAcceptance/GetCOCCheckAccepted",
                    params={
                        "userid": self._user_id,
                        "cocAcceptanceSource": "DMS_WEB",
                    },
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": f"{BASE_URL}/login/",
                    },
                    timeout=20,
                )
            except requests.RequestException as exc:
                logger.warning("COC check failed (non-fatal): %s", exc)

        # Prime the agency portal and capture any Estag/Etag the server
        # hands back. The real frontend visits /agency/home then calls
        # /agency/api/Auth/home, so we do the same.
        self._prime_agency_session()

    def _prime_agency_session(self) -> None:
        """Replay the navigation the real Angular app does post-login.

        The agency portal HTTP interceptor expects every authenticated call
        to carry ``Sourceuri`` + ``Estag`` + ``X-Requested-With`` headers
        (see ``soa.client._compute_estag``). Hitting ``/agency/home`` and
        then ``/agency/api/Auth/home`` mirrors the real flow and helps the
        server bind whatever per-session state it tracks; the per-request
        Estag is computed on each individual call, not stored.
        """
        try:
            self._session.get(
                f"{BASE_URL}/agency/home",
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Referer": f"{BASE_URL}/login/",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=20,
            )
            home_resp = self._session.get(
                f"{BASE_URL}/agency/api/Auth/home",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Sourceuri": f"{BASE_URL}/agency/home",
                    "Referer": f"{BASE_URL}/agency/home",
                    "Show-Spinner": "true",
                    "X-Requested-With": "XMLHttpRequest",
                    # GET with no body -> empty Estag (btoa("") === "")
                    "Estag": "",
                },
                timeout=20,
            )
            logger.debug(
                "Primed /agency/api/Auth/home -> %s", home_resp.status_code
            )
        except requests.RequestException as exc:
            logger.warning("Agency priming failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------ #
    # SOA call
    # ------------------------------------------------------------------ #

    def _call_soa(self, agreement_no: str) -> Any:
        url = f"{BASE_URL}/agency/api/ReportAgency/GetSOAReport"
        # Hand-serialize so the bytes we sign with MD5 are byte-identical
        # to the bytes we send in the body. Angular's JSON.stringify emits
        # no extra whitespace, so we match (separators=(",", ":")).
        body_obj = {"flag": "Agency", "AgreementNo": agreement_no}
        body_bytes = json.dumps(
            body_obj, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        estag = self._compute_estag(body_bytes)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/agency/others/agency-soa",
            "Sourceuri": f"{BASE_URL}/agency/others/agency-soa",
            "X-Requested-With": "XMLHttpRequest",
            "Show-Spinner": "true",
            "Estag": estag,
        }
        logger.debug("POST %s body=%s estag=%s", url, body_bytes, estag)
        resp = self._session.post(
            url, data=body_bytes, headers=headers, timeout=60
        )

        if resp.status_code in (401, 403):
            raise _SessionExpired(resp.status_code)
        if not resp.ok:
            logger.error(
                "Bajaj SOA HTTP %s headers=%s body=%r",
                resp.status_code,
                dict(resp.headers),
                resp.text[:1000],
            )
            raise BajajError(
                f"SOA call failed: HTTP {resp.status_code} "
                f"body={resp.text[:500]!r}"
            )

        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype.lower():
            return resp.json()
        # Some Bajaj endpoints return text/plain wrapping JSON.
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError:
            return {"raw": resp.text}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _encrypt_login_key(self, payload: dict[str, Any]) -> str:
        plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ciphertext = self._public_key.encrypt(plaintext, padding.PKCS1v15())
        return base64.b64encode(ciphertext).decode("ascii")

    @staticmethod
    def _compute_estag(body_bytes: bytes) -> str:
        """Mirror the agency JS: ``btoa(md5(body) lowercase-hex)``.

        Empty body -> empty estag (matches ``btoa(Ei || "")`` in the
        frontend interceptor, used for sessionless GETs).
        """
        if not body_bytes:
            return ""
        hex_md5 = hashlib.md5(body_bytes).hexdigest().lower()
        return base64.b64encode(hex_md5.encode("ascii")).decode("ascii")

    @staticmethod
    def _raise_for_login_error(resp: requests.Response) -> None:
        if resp.ok:
            return
        raise BajajLoginError(
            f"Login HTTP {resp.status_code}: {resp.text[:500]!r}"
        )


class _SessionExpired(Exception):
    """Internal marker: we should drop the session and re-login once."""

    def __init__(self, status: int) -> None:
        super().__init__(f"session expired (HTTP {status})")
        self.status = status
