"""Korea Investment & Securities Open API: authentication and one guarded REST call.

Quotes only. The app key and secret come from the environment and go nowhere
but KIS's own headers; no account number is read, and nothing here can place
an order.

**Credentials are reused, not re-issued.** KIS allows one access token a
minute and asks that a day's token be reused. The issued token and the
WebSocket approval key are stored (`kis_credential`) and handed out until
they lapse. Issuing is serialised across processes by an advisory lock, and
refused outright if the last issue was under a minute ago — the provider's
rule, kept by us rather than discovered from its refusal.

**Every REST call is reserved before it is sent**, on the `kis_rest` quota,
and paced by a token bucket. KIS answers an over-rate call with `EGW00201`;
that means our pacing and theirs disagree, so it is a loud failure
(`RateLimitedError`), not a quiet retry.

**Nothing secret is logged or raised.** Errors carry KIS's own code and
message, never a header, a token or a request body.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.collectors.base import (
    RateLimitedError,
    TokenBucket,
    UpstreamUnavailableError,
    as_object,
)
from app.collectors.quota import QuotaGuard
from app.config import get_settings
from app.core.clock import utc_now
from app.db import session_scope
from app.repositories import kis_repo

logger = logging.getLogger(__name__)

REST_BASE = {
    "real": "https://openapi.koreainvestment.com:9443",
    "mock": "https://openapivts.koreainvestment.com:29443",
}
WS_URL = {
    "real": "ws://ops.koreainvestment.com:21000",
    "mock": "ws://ops.koreainvestment.com:31000",
}

REST_GROUP = "kis_rest"
TOKEN_GROUP = "kis_token"
ACCESS_TOKEN = "ACCESS_TOKEN"
APPROVAL_KEY = "APPROVAL_KEY"

# KIS's rule is one token a minute; a little more than that, so a clock a
# second off does not decide it.
MIN_ISSUE_GAP = timedelta(seconds=65)
# A credential this close to its expiry is not handed out.
EXPIRY_MARGIN = timedelta(minutes=10)
# KIS does not state how long an approval key lasts. Treated as a day, like
# the token, and re-issued if a connection is refused.
APPROVAL_LIFETIME = timedelta(hours=24)

RATE_LIMITED = "EGW00201"
TOKEN_EXPIRED = "EGW00123"


class KisError(UpstreamUnavailableError):
    """KIS answered with a failure code. Carries its code and message, nothing else."""

    def __init__(self, code: str | None, message: str | None) -> None:
        super().__init__(f"KIS {code or '?'}: {message or 'no message'}")
        self.code = code


class KisClient:
    """Authenticated, reserved, paced calls to the KIS REST API."""

    def __init__(
        self,
        *,
        guard: QuotaGuard | None = None,
        scope: Callable[[], AbstractContextManager[Session]] = session_scope,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self._key = settings.kis_app_key
        self._secret = settings.kis_app_secret
        self.env = settings.kis_env
        # The mock server allows far less; the official sample paces it at 2/s.
        rate = settings.kis_rate if self.env == "real" else min(settings.kis_rate, 1.0)
        # Capacity one: no burst at start-up. A full bucket of `rate` would let
        # the first second carry twice the rate.
        self._bucket = TokenBucket(rate, capacity=1.0)
        self._guard = guard if guard is not None else QuotaGuard()
        self._scope = scope
        self._http = httpx.Client(base_url=REST_BASE[self.env], timeout=30, transport=transport)
        # The token in hand and when it stops being handed out. Held in memory
        # to spare a database read per call, and dropped at the same margin
        # the store uses, so a long-running process moves to the next token
        # without waiting for KIS to refuse the old one.
        self._token: str | None = None
        self._token_until: datetime | None = None

    def is_configured(self) -> bool:
        return bool(self._key and self._secret)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KisClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- credentials ------------------------------------------------------

    def _credential(
        self, kind: str, issue: Callable[[], tuple[str, timedelta]]
    ) -> tuple[str, datetime]:
        """A usable credential of this kind and when it lapses, issuing one if none is."""
        with self._scope() as session:
            kis_repo.lock_issue(session, kind)
            now = kis_repo.db_now(session)
            found = kis_repo.usable(session, env=self.env, kind=kind, until=now + EXPIRY_MARGIN)
            if found is not None:
                return found.value, found.expires_at
            last = kis_repo.last_issued(session, env=self.env, kind=kind)
            if last is not None and now - last < MIN_ISSUE_GAP:
                raise KisError(
                    None,
                    f"{kind} was issued {int((now - last).total_seconds())}s ago; "
                    "KIS allows one a minute, so not asking again yet",
                )
            self._guard.reserve(TOKEN_GROUP, kind.lower())
            value, lifetime = issue()
            expires_at = now + lifetime
            kis_repo.save(session, env=self.env, kind=kind, value=value, expires_at=expires_at)
            logger.info("KIS %s issued for %s, valid %s", kind, self.env, lifetime)
            return value, expires_at

    def access_token(self) -> str:
        if (
            self._token is None
            or self._token_until is None
            or utc_now() >= self._token_until - EXPIRY_MARGIN
        ):
            self._token, self._token_until = self._credential(ACCESS_TOKEN, self._issue_token)
        return self._token

    def approval_key(self) -> str:
        return self._credential(APPROVAL_KEY, self._issue_approval)[0]

    def _issue_token(self) -> tuple[str, timedelta]:
        body = self._post_json(
            "/oauth2/tokenP",
            {"grant_type": "client_credentials", "appkey": self._key, "appsecret": self._secret},
        )
        token = body.get("access_token")
        try:
            lifetime = timedelta(seconds=int(body.get("expires_in", 0)))
        except (TypeError, ValueError):
            lifetime = timedelta(0)
        if not isinstance(token, str) or not token or lifetime <= timedelta(0):
            raise KisError(
                str(body.get("error_code") or ""),
                str(body.get("error_description") or "no token issued"),
            )
        return token, lifetime

    def _issue_approval(self) -> tuple[str, timedelta]:
        body = self._post_json(
            "/oauth2/Approval",
            {"grant_type": "client_credentials", "appkey": self._key, "secretkey": self._secret},
        )
        key = body.get("approval_key")
        if not isinstance(key, str) or not key:
            raise KisError(str(body.get("error_code") or ""), "no approval key issued")
        return key, APPROVAL_LIFETIME

    def _post_json(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"KIS {path} failed: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise UpstreamUnavailableError(
                f"KIS {path} returned HTTP {response.status_code}, not JSON"
            ) from None
        return as_object(body, source=f"KIS {path}")

    # --- REST -------------------------------------------------------------

    def get(
        self, path: str, *, tr_id: str, params: Mapping[str, str], tr_cont: str = ""
    ) -> tuple[dict[str, Any], str]:
        """One quotation call, reserved before it is sent. Returns the body and KIS's `tr_cont`."""
        for attempt in (1, 2):
            token = self.access_token()
            self._guard.reserve(REST_GROUP, tr_id)
            self._bucket.acquire()
            try:
                response = self._http.get(
                    path,
                    params=dict(params),
                    headers={
                        "content-type": "application/json; charset=utf-8",
                        "authorization": f"Bearer {token}",
                        "appkey": self._key,
                        "appsecret": self._secret,
                        "tr_id": tr_id,
                        "custtype": "P",
                        "tr_cont": tr_cont,
                    },
                )
            except httpx.HTTPError as exc:
                raise UpstreamUnavailableError(
                    f"KIS {tr_id} failed: {type(exc).__name__}"
                ) from None
            try:
                body = as_object(response.json(), source=f"KIS {tr_id}")
            except ValueError:
                raise UpstreamUnavailableError(
                    f"KIS {tr_id} returned HTTP {response.status_code}, not JSON"
                ) from None
            code = body.get("msg_cd")
            if response.status_code == 429 or code == RATE_LIMITED:
                raise RateLimitedError(
                    f"KIS refused {tr_id} as over its rate ({code}), but our pacing allowed it. "
                    "The pacing is wrong; check it before calling again"
                )
            if code == TOKEN_EXPIRED and attempt == 1:
                # Stored as valid but KIS says otherwise: drop our copy and ask once more.
                self._token = None
                self._expire_stored_token(token)
                continue
            if body.get("rt_cd") != "0":
                raise KisError(str(code or ""), str(body.get("msg1") or ""))
            return body, response.headers.get("tr_cont", "")
        raise KisError(TOKEN_EXPIRED, "token refused twice")

    def _expire_stored_token(self, refused: str) -> None:
        """Mark the token KIS refused as lapsed — that one, not whichever is newest."""
        with self._scope() as session:
            kis_repo.expire(session, env=self.env, kind=ACCESS_TOKEN, value=refused)
