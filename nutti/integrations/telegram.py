"""텔레그램 Bot API 얇은 래퍼.

검수 게이트(TelegramGate)가 인라인 버튼 메시지를 보내고 콜백(버튼 탭)을
롱폴링으로 수신하기 위한 최소 메서드만 제공한다. HTTP 클라이언트를 주입할 수
있어 테스트는 네트워크 없이 동작한다.
"""

from __future__ import annotations

import httpx

from nutti.logging import get_logger
from nutti.models import ReviewDecision, ReviewRequest

log = get_logger(__name__)

# 콜백 데이터에 실어보낼 결정값(승인/거절/수정).
_BUTTONS: list[tuple[str, str]] = [
    ("승인", ReviewDecision.APPROVED.value),
    ("거절", ReviewDecision.REJECTED.value),
    ("수정", ReviewDecision.REVISE.value),
]


class TelegramClient:
    """텔레그램 Bot API 래퍼. `http`로 httpx.Client를 주입할 수 있다."""

    def __init__(self, token: str, *, http: httpx.Client | None = None):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._http = http

    @property
    def http(self) -> httpx.Client:
        """주입된 클라이언트가 없으면 지연 생성한다."""
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(60.0))
        return self._http

    def send_review(self, chat_id: str, review: ReviewRequest) -> int:
        """검수 메시지를 인라인 버튼과 함께 보내고 message_id를 반환한다."""
        text = f"{review.title}\n\n{review.preview}"
        inline_keyboard = [
            [
                {
                    "text": label,
                    "callback_data": f"nutti:{review.id}:{value}",
                }
                for label, value in _BUTTONS
            ]
        ]
        resp = self.http.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": inline_keyboard},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return int(data.get("result", {}).get("message_id", 0))

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """getUpdates 결과 리스트를 반환한다(콜백 폴링용)."""
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = self.http.get(f"{self.base_url}/getUpdates", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", []) or []

    def answer_callback(self, callback_query_id: str) -> None:
        """버튼 탭에 대한 콜백 확인(로딩 표시 제거)."""
        resp = self.http.post(
            f"{self.base_url}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id},
        )
        resp.raise_for_status()

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        """검수 결과를 반영해 기존 메시지를 수정한다(버튼 제거 효과)."""
        resp = self.http.post(
            f"{self.base_url}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
        )
        resp.raise_for_status()
