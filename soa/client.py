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
import time
from datetime import datetime, timezone
from typing import Any

import jwt
import requests
from requests.utils import dict_from_cookiejar
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

# Fallback JWT renew lead (seconds) when ``BAJAJ_SESSION_RENEW_BUFFER_SECONDS`` is unset.
# Each ``fetch_soa`` runs ``_ensure_session``; use ~3 minutes so long batches renew
# before the ~15-minute portal cookie expires.
RENEW_BUFFER_SECONDS = 180

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


class BajajParallelSessionError(BajajError):
    """Portal indicates another / conflicting session; user may force remote logout."""


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
        self._portal_session_id: str | None = None

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
        """Return the parsed SOA JSON for a given agreement number.

        Refreshes the portal session before JWT expiry on every call, re-logs in
        on HTTP 401/403, and retries transient transport errors so long batch jobs
        can run past the default ~15-minute cookie lifetime.

        Each call uses a short-lived ``requests.Session`` cloned from the shared
        cookie jar so multiple batch worker threads can post concurrently without
        sharing one mutable session object.
        """
        agreement_no = agreement_no.strip().upper()
        if not agreement_no:
            raise BajajError("agreement_no is required")

        max_rounds = int(getattr(settings, "BAJAJ_SOA_FETCH_MAX_RETRIES", 8))
        last_err: BaseException | None = None
        for n in range(max_rounds):
            worker = requests.Session()
            try:
                with self._lock:
                    self._ensure_session()
                    snap = dict_from_cookiejar(self._session.cookies)
                    hdrs = list(self._session.headers.items())
                worker.headers.update(hdrs)
                worker.cookies.update(snap)
                data, resp = self._call_soa(agreement_no, http=worker)
                with self._lock:
                    self._session.cookies.update(resp.cookies)
                return data
            except BajajParallelSessionError:
                raise
            except BajajLoginError:
                raise
            except _SessionExpired as exc:
                last_err = exc
                logger.info(
                    "Bajaj SOA session rejected (HTTP %s); re-login and retry "
                    "(%s/%s) for %s.",
                    getattr(exc, "status", "?"),
                    n + 1,
                    max_rounds,
                    agreement_no,
                )
                with self._lock:
                    self._login()
            except requests.RequestException as exc:
                last_err = exc
                if n >= max_rounds - 1:
                    raise BajajError(
                        f"SOA transport failed after {max_rounds} attempts: {exc}"
                    ) from exc
                wait = min(30.0, 2.0 * (2**n))
                logger.warning(
                    "Bajaj SOA transport error for %s (%s); sleeping %.1fs then retry.",
                    agreement_no,
                    exc,
                    wait,
                )
                time.sleep(wait)
            finally:
                worker.close()
        raise BajajError(
            f"SOA failed after {max_rounds} session renewals (last error: {last_err!r})."
        ) from last_err

    def fetch_soa_after_remote_logout(self, agreement_no: str) -> Any:
        """End the current portal session server-side, log in again, then fetch SOA.

        Mirrors the DMS flow: ``POST /common/api/Auth/UpdateLogoutInfo`` with the
        active ``PortalSession`` JWT's ``SessionId``, ``GET /login``, then a fresh
        RSA login and SOA request.
        """
        agreement_no = agreement_no.strip().upper()
        if not agreement_no:
            raise BajajError("agreement_no is required")

        max_rounds = int(getattr(settings, "BAJAJ_SOA_FETCH_MAX_RETRIES", 8))
        with self._lock:
            self._ensure_session()
            self._remote_logout_current_portal_session()
            self._login()
            last_err: BaseException | None = None
            for n in range(max_rounds):
                try:
                    data, _resp = self._call_soa(agreement_no, http=self._session)
                    return data
                except BajajParallelSessionError:
                    raise
                except BajajLoginError:
                    raise
                except _SessionExpired as exc:
                    last_err = exc
                    logger.info(
                        "SOA after remote-logout: session rejected; re-login (%s/%s).",
                        n + 1,
                        max_rounds,
                    )
                    self._login()
                except requests.RequestException as exc:
                    last_err = exc
                    if n >= max_rounds - 1:
                        raise BajajError(
                            f"SOA transport failed after {max_rounds} attempts: {exc}"
                        ) from exc
                    time.sleep(min(30.0, 2.0 * (2**n)))
            raise BajajError(
                f"SOA failed after {max_rounds} retries (last: {last_err!r})."
            ) from last_err

    # ------------------------------------------------------------------ #
    # Login / session management
    # ------------------------------------------------------------------ #

    def _ensure_session(self) -> None:
        now = datetime.now(timezone.utc)
        buffer = float(
            getattr(
                settings,
                "BAJAJ_SESSION_RENEW_BUFFER_SECONDS",
                RENEW_BUFFER_SECONDS,
            )
        )
        if (
            self._expires_at is None
            or (self._expires_at - now).total_seconds() < buffer
        ):
            self._login()

    def _login(self) -> None:
        # Drop any stale cookies before re-auth so we start clean.
        self._session.cookies.clear()
        self._expires_at = None
        self._user_id = None
        self._portal_session_id = None

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
        self._portal_session_id = self._session_id_from_claims(claims)
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

    def _remote_logout_current_portal_session(self) -> None:
        """Notify Bajaj that this ``SessionID`` is logging out, then open ``/login``."""
        portal_cookie = self._session.cookies.get("PortalSession")
        if not portal_cookie:
            logger.info("UpdateLogoutInfo skipped: no PortalSession cookie")
            return

        claims = jwt.decode(
            portal_cookie,
            options={"verify_signature": False, "verify_exp": False},
        )
        session_id = self._session_id_from_claims(claims)
        if not session_id:
            logger.warning("UpdateLogoutInfo skipped: no SessionId in PortalSession")
            return

        body_obj = {"Username": self._username, "SessionID": session_id}
        body_bytes = json.dumps(
            body_obj, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        estag = self._compute_estag(body_bytes)
        url = f"{BASE_URL}/common/api/Auth/UpdateLogoutInfo"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/agency/home",
            "Sourceuri": f"{BASE_URL}/agency/home",
            "X-Requested-With": "XMLHttpRequest",
            "Show-Spinner": "true",
            "Estag": estag,
        }
        try:
            resp = self._session.post(
                url, data=body_bytes, headers=headers, timeout=30
            )
            logger.info("UpdateLogoutInfo -> HTTP %s", resp.status_code)
        except requests.RequestException as exc:
            logger.warning("UpdateLogoutInfo request failed: %s", exc)

        try:
            self._session.get(
                f"{BASE_URL}/login",
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Referer": f"{BASE_URL}/agency/home",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.warning("GET /login after logout failed (non-fatal): %s", exc)

    @staticmethod
    def _session_id_from_claims(claims: dict[str, Any]) -> str | None:
        sid = (
            claims.get("SessionId")
            or claims.get("SessionID")
            or claims.get("sessionId")
        )
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        return None

    # ------------------------------------------------------------------ #
    # SOA call
    # ------------------------------------------------------------------ #

    def _call_soa(
        self,
        agreement_no: str,
        http: requests.Session | None = None,
    ) -> tuple[Any, requests.Response]:
        """POST GetSOAReport; return parsed payload and the raw response.

        ``http`` defaults to ``self._session``. For concurrent batch fetches,
        pass a short-lived session that carries a snapshot of auth cookies.
        """
        sess = http if http is not None else self._session
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
        resp = sess.post(url, data=body_bytes, headers=headers, timeout=60)

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
            data = resp.json()
        else:
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError:
                data = {"raw": resp.text}

        self._raise_if_parallel_session_payload(data)
        return data, resp

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

    @staticmethod
    def _collect_message_strings(data: Any, out: list[str], depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(data, dict):
            for key in (
                "Message",
                "message",
                "ErrorMessage",
                "errorMessage",
                "Description",
                "description",
                "Msg",
                "msg",
                "error",
                "Error",
            ):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v)
            for v in data.values():
                if isinstance(v, (dict, list)):
                    BajajClient._collect_message_strings(v, out, depth + 1)
        elif isinstance(data, list):
            for item in data[:20]:
                BajajClient._collect_message_strings(item, out, depth + 1)

    def _raise_if_parallel_session_payload(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        sc = data.get("StatusCode") or data.get("statusCode")
        if sc in (409, 440, 441):
            texts: list[str] = []
            self._collect_message_strings(data, texts)
            raise BajajParallelSessionError(
                texts[0] if texts else "Portal returned a session conflict status."
            )

        texts = []
        self._collect_message_strings(data, texts)
        blob = " ".join(texts).lower()
        triggers = (
            "already logged in",
            "logged in from",
            "another location",
            "other machine",
            "other session",
            "logged in another",
            "duplicate login",
            "another user",
            "concurrent",
            "duplicate session",
            "active session",
            "please logout",
            "log out and",
            "multiple login",
            "session conflict",
            "invalid session",
        )
        if blob and any(t in blob for t in triggers):
            raise BajajParallelSessionError(texts[0] if texts else "Session conflict.")


class _SessionExpired(Exception):
    """Internal marker: we should drop the session and re-login once."""

    def __init__(self, status: int) -> None:
        super().__init__(f"session expired (HTTP {status})")
        self.status = status
