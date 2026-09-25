"""The KIS client against a scripted server: credentials reused, calls reserved, secrets kept out.

The HTTP side is an `httpx.MockTransport`; the credential store is the real
table, under an environment name (`test`) no real credential uses, and every
row under it is removed afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import RateLimitedError
from app.collectors.kis import ACCESS_TOKEN, APPROVAL_KEY, KisClient, KisError
from app.config import get_settings
from app.models import Base
from app.models.kis import KisCredential

pytestmark = pytest.mark.integration

KEY = "test-app-key-0000"
SECRET = "test-app-secret-9999"
ENV = "test"


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    settings = get_settings()
    monkeypatch.setattr(settings, "kis_app_key", KEY)
    monkeypatch.setattr(settings, "kis_app_secret", SECRET)
    monkeypatch.setattr(settings, "kis_env", "real")
    made = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    try:
        yield made
    finally:
        with made() as s:
            s.execute(text("DELETE FROM kis_credential WHERE env = :e"), {"e": ENV})
            s.commit()


class Guard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def reserve(self, group: str, endpoint: str, **_: Any) -> None:
        self.events.append(f"reserve:{group}")


class Server:
    """Answers token, approval and quotation calls from a script."""

    def __init__(self, events: list[str], quotes: list[tuple[int, dict[str, Any]]] | None = None):
        self.events = events
        self.quotes = list(quotes or [])
        self.tokens = 0
        self.issued: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            self.tokens += 1
            self.events.append("send:token")
            token = f"token-{self.tokens}-secretvalue"
            self.issued.append(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 86400})
        if request.url.path == "/oauth2/Approval":
            self.events.append("send:approval")
            body = request.read().decode()
            assert '"secretkey"' in body
            return httpx.Response(200, json={"approval_key": "approval-secretvalue"})
        self.events.append("send:quote")
        assert request.headers["authorization"].startswith("Bearer token-")
        assert request.headers["custtype"] == "P"
        status, body = self.quotes.pop(0) if self.quotes else (200, {"rt_cd": "0", "output2": []})
        return httpx.Response(status, json=body)


def client(factory: sessionmaker[Session], server: Server, events: list[str]) -> KisClient:
    def scope() -> Any:
        from contextlib import contextmanager

        @contextmanager
        def run() -> Iterator[Session]:
            with factory() as s:
                try:
                    yield s
                    s.commit()
                except Exception:
                    s.rollback()
                    raise

        return run()

    c = KisClient(guard=Guard(events), scope=scope, transport=httpx.MockTransport(server))  # type: ignore[arg-type]
    c.env = ENV
    return c


def stored(factory: sessionmaker[Session], kind: str) -> list[KisCredential]:
    with factory() as s:
        return list(
            s.execute(
                select(KisCredential).where(KisCredential.env == ENV, KisCredential.kind == kind)
            ).scalars()
        )


def quote(c: KisClient) -> dict[str, Any]:
    body, _ = c.get("/uapi/x", tr_id="FHKST03010230", params={"FID_INPUT_ISCD": "005930"})
    return body


class TestCredentials:
    def test_a_token_is_issued_once_and_reused_across_calls_and_clients(
        self, factory: sessionmaker[Session]
    ) -> None:
        events: list[str] = []
        server = Server(events)
        quote(client(factory, server, events))
        quote(client(factory, server, events))
        assert server.tokens == 1
        (row,) = stored(factory, ACCESS_TOKEN)
        # Counted from before the request, so it lapses a little early, never late.
        lifetime = row.expires_at - row.issued_at
        assert timedelta(seconds=86400 - 60) <= lifetime <= timedelta(seconds=86400)

    def test_issuing_again_within_a_minute_is_refused_before_asking(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            s.execute(
                text(
                    "INSERT INTO kis_credential (env, kind, value, expires_at) "
                    "VALUES (:e, :k, 'expired', clock_timestamp())"
                ),
                {"e": ENV, "k": ACCESS_TOKEN},
            )
            s.commit()
        events: list[str] = []
        server = Server(events)
        with pytest.raises(KisError, match="one a minute"):
            quote(client(factory, server, events))
        assert server.tokens == 0

    def test_the_approval_key_is_issued_with_the_secret_key_field_and_reused(
        self, factory: sessionmaker[Session]
    ) -> None:
        events: list[str] = []
        server = Server(events)
        c = client(factory, server, events)
        assert c.approval_key() == c.approval_key()
        assert events.count("send:approval") == 1
        assert len(stored(factory, APPROVAL_KEY)) == 1


class TestCalls:
    def test_every_call_is_reserved_before_it_is_sent(self, factory: sessionmaker[Session]) -> None:
        events: list[str] = []
        c = client(factory, Server(events), events)
        quote(c)
        quote(c)
        assert events == [
            "reserve:kis_token",
            "send:token",
            "reserve:kis_rest",
            "send:quote",
            "reserve:kis_rest",
            "send:quote",
        ]

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (200, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
            (429, {}),
        ],
    )
    def test_a_refusal_as_over_rate_is_loud(
        self, factory: sessionmaker[Session], status: int, body: dict[str, Any]
    ) -> None:
        events: list[str] = []
        c = client(factory, Server(events, [(status, body)]), events)
        with pytest.raises(RateLimitedError):
            quote(c)

    def test_a_failure_carries_the_code_and_no_secret(
        self, factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        events: list[str] = []
        server = Server(events, [(200, {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 종목"})])
        c = client(factory, server, events)
        with pytest.raises(KisError) as raised:
            quote(c)
        assert raised.value.code == "OPSQ0002"
        text_seen = str(raised.value) + caplog.text
        for secret in (KEY, SECRET, *server.issued):
            assert secret not in text_seen

    def test_a_token_the_server_calls_expired_is_dropped_and_issued_again(
        self, factory: sessionmaker[Session]
    ) -> None:
        # A stored token, issued long enough ago that a new one may be asked for.
        with factory() as s:
            s.execute(
                text(
                    "INSERT INTO kis_credential (env, kind, value, issued_at, expires_at) "
                    "VALUES (:e, :k, 'token-0-old', clock_timestamp() - interval '2 hours', "
                    "clock_timestamp() + interval '10 hours')"
                ),
                {"e": ENV, "k": ACCESS_TOKEN},
            )
            s.commit()
        events: list[str] = []
        expired = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}
        server = Server(events, [(200, expired), (200, {"rt_cd": "0", "output2": [1]})])
        c = client(factory, server, events)
        assert quote(c)["output2"] == [1]
        assert server.tokens == 1


def test_the_mock_environment_uses_the_mock_host(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "kis_env", "mock")
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        return httpx.Response(200, json={"access_token": "t", "expires_in": 60})

    c = KisClient(guard=Guard([]), transport=httpx.MockTransport(record))  # type: ignore[arg-type]
    c._http.post("/oauth2/tokenP", json={})
    assert seen == ["openapivts.koreainvestment.com"]


def insert(factory: sessionmaker[Session], value: str, *, issued: str, expires: str) -> None:
    with factory() as s:
        s.execute(
            text(
                "INSERT INTO kis_credential (env, kind, value, issued_at, expires_at) VALUES "
                f"(:e, :k, :v, clock_timestamp() - interval '{issued}', "
                f"clock_timestamp() + interval '{expires}')"
            ),
            {"e": ENV, "k": ACCESS_TOKEN, "v": value},
        )
        s.commit()


class TestLongRunning:
    def test_a_token_near_its_expiry_is_let_go_without_waiting_for_a_refusal(
        self, factory: sessionmaker[Session]
    ) -> None:
        insert(factory, "token-0-ageing", issued="23 hours 55 minutes", expires="5 minutes")
        events: list[str] = []
        server = Server(events)
        c = client(factory, server, events)
        quote(c)
        # The stored one was inside the margin, so a new one was issued at once.
        assert server.tokens == 1
        assert c.access_token() == server.issued[0]

    def test_only_the_refused_token_is_marked_lapsed(self, factory: sessionmaker[Session]) -> None:
        insert(factory, "token-A-ours", issued="3 hours", expires="10 hours")
        events: list[str] = []
        expired = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}
        server = Server(events, [(200, expired), (200, {"rt_cd": "0", "output2": []})])
        c = client(factory, server, events)
        assert c.access_token() == "token-A-ours"
        # Another process has since stored a newer one.
        insert(factory, "token-B-theirs", issued="1 hour", expires="20 hours")
        quote(c)
        with factory() as s:
            live = {
                v
                for (v,) in s.execute(
                    text(
                        "SELECT value FROM kis_credential WHERE env = :e AND expires_at > clock_timestamp()"
                    ),
                    {"e": ENV},
                )
            }
        assert live == {"token-B-theirs"}
        assert server.tokens == 0

    def test_the_token_held_in_memory_is_dropped_at_the_same_margin(
        self, factory: sessionmaker[Session]
    ) -> None:
        from app.core.clock import utc_now

        events: list[str] = []
        server = Server(events)
        c = client(factory, server, events)
        quote(c)
        # Hours later, as a long-running process would see it: the token in
        # hand is about to lapse, and the store says the same.
        with factory() as s:
            s.execute(
                text(
                    "UPDATE kis_credential SET issued_at = clock_timestamp() - interval '23 hours', "
                    "expires_at = clock_timestamp() + interval '5 minutes' WHERE env = :e"
                ),
                {"e": ENV},
            )
            s.commit()
        c._token_until = utc_now() + timedelta(minutes=5)
        quote(c)
        assert server.tokens == 2
