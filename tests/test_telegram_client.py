"""TelegramClient 단위 테스트 — ok:false 탐지(#5) + 토큰 스크럽(#4).

httpx.Client를 주입해 네트워크 없이 검증한다.
"""

from __future__ import annotations

import httpx
import pytest

from nutti.integrations.telegram import (
    TelegramClient,
    TelegramError,
    TelegramTransientError,
)
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


class _StatusErrorResp:
    """raise_for_status가 토큰이 박힌 URL을 담은 HTTPStatusError를 던지는 응답."""

    def __init__(self, status: int, token: str):
        self._status = status
        self._token = token

    def raise_for_status(self) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        req = httpx.Request("POST", url)
        resp = httpx.Response(self._status, request=req)
        raise httpx.HTTPStatusError(
            f"Client error '{self._status}' for url '{url}'", request=req, response=resp
        )

    def json(self) -> dict:
        return {}


class _StatusErrorHttp:
    def __init__(self, status: int, token: str):
        self._status = status
        self._token = token

    def post(self, url, json=None):
        return _StatusErrorResp(self._status, self._token)


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


def test_http_status_error_scrubs_token():
    # raise_for_status가 던지는 HTTPStatusError(4xx)에도 토큰이 박힌 URL이 들어감.
    token = "999999:STATUSSECRET"
    client = TelegramClient(token, http=_StatusErrorHttp(401, token))
    with pytest.raises(TelegramError) as ei:
        client.send_review("123", _review())
    msg = str(ei.value)
    assert token not in msg
    assert "***" in msg
    assert not isinstance(ei.value, TelegramTransientError)  # 401은 영구(재시도 안 함)


def test_http_429_is_transient():
    # 레이트리밋은 일시적 오류로 분류되어 호출자가 재시도할 수 있어야 한다.
    client = TelegramClient("tok", http=_StatusErrorHttp(429, "tok"))
    with pytest.raises(TelegramTransientError):
        client.get_updates()


def test_http_500_is_transient():
    # 5xx 서버 오류도 일시적으로 분류.
    client = TelegramClient("tok", http=_StatusErrorHttp(503, "tok"))
    with pytest.raises(TelegramTransientError):
        client.get_updates()


def test_transport_error_is_transient():
    # 네트워크/연결 오류(TransportError 계열)는 일시적으로 분류.
    client = TelegramClient("tok", http=_RaisingHttp(httpx.ConnectError("연결 실패")))
    with pytest.raises(TelegramTransientError):
        client.get_updates()


def test_answer_callback_ok_false_raises():
    # send_review 외 다른 메서드도 _call을 거치므로 ok:false 시 표면화돼야 한다.
    http = _OkHttp({"ok": False, "description": "query is too old"})
    client = TelegramClient("tok", http=http)
    with pytest.raises(TelegramError):
        client.answer_callback("cbq")


# ---------------------------------------------------------------------------
# send_video — multipart 업로드 실경로(파일 I/O + data/files 구성)
# ---------------------------------------------------------------------------


class _MultipartResp:
    def __init__(self, json_data: dict):
        self._json = json_data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._json


class _MultipartHttp:
    """sendVideo의 multipart POST(data=, files=)를 기록하는 가짜 httpx 클라이언트."""

    def __init__(self, json_data: dict):
        self._json = json_data
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, data=None, files=None):
        self.calls.append((url, data or {}, files or {}))
        return _MultipartResp(self._json)


def test_send_video_reads_file_and_posts_multipart(tmp_path):
    """실제 파일을 열어 바이트를 읽고, data(chat_id/caption/reply_markup)+files로 POST한다."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"MP4_BYTES_HERE")
    http = _MultipartHttp({"ok": True, "result": {"message_id": 555}})
    client = TelegramClient("tok", http=http)

    mid = client.send_video(
        "chat_9",
        str(video),
        caption="cap",
        reply_markup={"inline_keyboard": []},
    )

    assert mid == 555
    url, data, files = http.calls[0]
    assert url.endswith("/sendVideo")
    assert data["chat_id"] == "chat_9"
    assert data["caption"] == "cap"
    assert "reply_markup" in data  # JSON 문자열로 직렬화돼 실림
    # 실제 파일 바이트가 multipart files에 실렸는지 확인
    _name, content, mime = files["video"]
    assert content == b"MP4_BYTES_HERE"
    assert mime == "video/mp4"


def test_send_video_omits_optional_fields(tmp_path):
    """caption/reply_markup이 없으면 data에 넣지 않는다(빈 caption 미전송)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"X")
    http = _MultipartHttp({"ok": True, "result": {"message_id": 1}})
    client = TelegramClient("tok", http=http)

    client.send_video("chat_9", str(video))

    _url, data, _files = http.calls[0]
    assert "caption" not in data
    assert "reply_markup" not in data


def test_send_video_file_not_found_raises():
    """존재하지 않는 파일 경로면 TelegramError(영상 파일 읽기 실패)."""
    http = _MultipartHttp({"ok": True, "result": {"message_id": 1}})
    client = TelegramClient("tok", http=http)
    with pytest.raises(TelegramError, match="영상 파일 읽기 실패"):
        client.send_video("chat_9", "no_such_dir/missing.mp4")


def test_send_video_ok_false_raises(tmp_path):
    """sendVideo가 ok:false면 TelegramError로 표면화한다."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"X")
    http = _MultipartHttp({"ok": False, "description": "bad request"})
    client = TelegramClient("tok", http=http)
    with pytest.raises(TelegramError, match="sendVideo"):
        client.send_video("chat_9", str(video))
