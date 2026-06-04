"""TelegramClient 단위 테스트 — ok:false 탐지(#5) + 토큰 스크럽(#4).

httpx.Client를 주입해 네트워크 없이 검증한다.
"""

from __future__ import annotations

import httpx
import pytest

from nutti.integrations.telegram import TelegramClient, TelegramError
from nutti.models import ReviewRequest, Stage


class _FakeResp:
    def __init__(self, json_data: dict):
        self._json = json_data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._json


class _OkHttp:
    """항상 주어진 JSON을 반환하는 가짜 httpx 클라이언트."""

    def __init__(self, json_data: dict):
        self._json = json_data
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        return _FakeResp(self._json)


class _RaisingHttp:
    def __init__(self, exc: Exception):
        self._exc = exc

    def post(self, url, json=None):
        raise self._exc


def _review() -> ReviewRequest:
    return ReviewRequest(stage=Stage.SCRIPT, title="t", preview="p")


def test_send_review_returns_message_id():
    http = _OkHttp({"ok": True, "result": {"message_id": 77}})
    client = TelegramClient("tok", http=http)
    assert client.send_review("123", _review()) == 77


def test_ok_false_raises_telegram_error():
    # 텔레그램은 논리 오류 시 HTTP 200 + ok:false를 반환 → TelegramError로 표면화.
    http = _OkHttp({"ok": False, "description": "chat not found"})
    client = TelegramClient("tok", http=http)
    with pytest.raises(TelegramError) as exc:
        client.send_review("123", _review())
    assert "chat not found" in str(exc.value)


def test_http_error_scrubs_token():
    token = "123456:SECRETTOKEN"
    exc = httpx.RequestError(f"fail for https://api.telegram.org/bot{token}/sendMessage")
    client = TelegramClient(token, http=_RaisingHttp(exc))
    with pytest.raises(TelegramError) as ei:
        client.send_review("123", _review())
    msg = str(ei.value)
    assert token not in msg   # 토큰 노출 안 됨
    assert "***" in msg       # 가려짐
