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


class TelegramError(Exception):
    """텔레그램 Bot API 호출 실패(영구). 토큰은 메시지에서 가려진다."""


class TelegramTransientError(TelegramError):
    """일시적 실패(네트워크/타임아웃/429/5xx) — 호출자가 재시도해도 되는 경우."""


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

    def _scrub(self, text: str) -> str:
        """에러 메시지에서 봇 토큰을 가린다(로그·예외로의 유출 방지)."""
        return text.replace(self.token, "***") if self.token else text

    def _call(self, method: str, payload: dict) -> dict:
        """Bot API 호출. 오류를 토큰을 가린 채 변환한다.

        일시적 오류(네트워크/타임아웃/429/5xx)는 TelegramTransientError로, 영구 오류
        (그 외 4xx 인증·설정 오류, ok:false)는 TelegramError로 구분해 호출자가 재시도
        여부를 판단할 수 있게 한다. 토큰이 박힌 URL이 예외 메시지에 노출되지 않도록 스크럽.
        """
        try:
            resp = self.http.post(f"{self.base_url}/{method}", json=payload)
            resp.raise_for_status()
        except httpx.TransportError as exc:  # 네트워크/타임아웃 등 전송 계층 오류
            raise TelegramTransientError(self._scrub(str(exc))) from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            msg = self._scrub(str(exc))
            if status == 429 or 500 <= status < 600:  # 레이트리밋/서버 오류 = 일시적
                raise TelegramTransientError(msg) from None
            raise TelegramError(msg) from None  # 그 밖 4xx = 영구(잘못된 토큰 등)
        except httpx.HTTPError as exc:
            raise TelegramError(self._scrub(str(exc))) from None
        data = resp.json()
        # 텔레그램은 논리 오류 시 HTTP 200 + {"ok": false, "description": ...}를 반환.
        if not data.get("ok", False):
            raise TelegramError(f"Telegram API 오류({method}): {data.get('description')}")
        return data

    def send_review(self, chat_id: str, review: ReviewRequest) -> int:
        """검수 메시지를 인라인 버튼과 함께 보내고 message_id를 반환한다."""
        text = f"{review.title}\n\n{review.preview}"
        # callback_data는 텔레그램 제한상 64바이트 이내여야 함(현재 prefix+id+value ≈ 27B).
        inline_keyboard = [
            [
                {"text": label, "callback_data": f"nutti:{review.id}:{value}"}
                for label, value in _BUTTONS
            ]
        ]
        data = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": inline_keyboard},
            },
        )
        return int(data.get("result", {}).get("message_id", 0))

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """getUpdates 결과 리스트를 반환한다(콜백 폴링용)."""
        payload: dict = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        data = self._call("getUpdates", payload)
        return data.get("result", []) or []

    def answer_callback(self, callback_query_id: str) -> None:
        """버튼 탭에 대한 콜백 확인(로딩 표시 제거)."""
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        """검수 결과를 반영해 기존 메시지를 수정한다(버튼 제거 효과)."""
        self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )
