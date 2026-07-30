"""검수 게이트 추상화.

파이프라인은 ReviewGate 인터페이스에만 의존한다. 실제 채널(텔레그램)이나
자동 승인(테스트)은 구현체를 갈아끼우면 된다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from nutti.config import Settings
from nutti.integrations.telegram import _BUTTONS, TelegramClient, TelegramTransientError
from nutti.logging import get_logger
from nutti.models import ReviewDecision, ReviewRequest
from nutti.storage.reviews import JsonFileReviewStore, ReviewStore

log = get_logger(__name__)


def _callback_origin_chat(cb: dict) -> str:
    """콜백이 속한 chat.id를 안전하게 추출한다.

    inline-mode 콜백은 message가 JSON null(None)이라 .get 체이닝이 깨질 수 있어
    각 단계를 `or {}`로 방어한다.
    """
    message = cb.get("message") or {}
    chat = message.get("chat") or {}
    return str(chat.get("id", ""))


def _is_review_echo(text: str, review: ReviewRequest | None) -> bool:
    """수신 텍스트가 검수 카드 본문(제목/미리보기)의 되돌이인지 판정한다.

    카드 본문은 telegram.send_review가 `f"{title}\\n\\n{preview}"`로 만든다 — 제목으로
    시작하거나 미리보기와 같으면 사람이 쓴 수정 대본일 수 없다. 복사·전달·에코 등
    경로와 무관하게 내용만 보고 막는다(hard-rule-over-prompt).
    """
    if review is None:
        return False
    body = text.strip()
    if not body:
        return False
    title = (review.title or "").strip()
    preview = (review.preview or "").strip()
    return bool(title and body.startswith(title)) or bool(preview and body == preview)


class ReviewGate(Protocol):
    """검수 요청을 보내고 결정(승인/거절/수정)을 반환한다."""

    def request(self, review: ReviewRequest) -> ReviewDecision: ...


class AutoApproveGate:
    """dry_run/테스트용: 항상 승인. 무인 실행 파일럿에도 사용 가능."""

    def request(self, review: ReviewRequest) -> ReviewDecision:
        log.info("auto_approve", stage=review.stage.value, title=review.title)
        return ReviewDecision.APPROVED


def _decision_from_callback(data: str, prefix: str) -> ReviewDecision:
    """콜백 데이터(`nutti:{id}:{value}`)에서 ReviewDecision을 파싱한다."""
    value = data[len(prefix):]
    try:
        return ReviewDecision(value)
    except ValueError:
        # 알 수 없는 값은 보수적으로 거절 처리.
        return ReviewDecision.REJECTED


class TelegramGate:
    """텔레그램 인라인 버튼 검수(검수①·②).

    설계: 여기서 '비동기'는 사람이 비동기로 버튼을 탭하는 것을 뜻한다. 파이프라인은
    승인 전까지 블로킹돼야 하므로 request()는 동기 시그니처를 유지하되, 내부에서
    getUpdates를 롱폴링하며 일치하는 콜백을 기다린다. 검수 상태는 store에 영속화해
    프로세스 재시작에도 살아남는다. client/store/clock/sleep을 주입하면 네트워크
    없이 테스트할 수 있다.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: TelegramClient | None = None,
        store: ReviewStore | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.settings = settings
        self._client = client
        self._store = store
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], None] = sleep or time.sleep

    def request(self, review: ReviewRequest) -> ReviewDecision:
        if self.settings.dry_run or not self.settings.telegram_bot_token:
            log.info("telegram.dry_run_approve", stage=review.stage.value)
            return ReviewDecision.APPROVED

        # 토큰만 있고 검수 채팅이 없으면 불투명한 API 크래시 대신 명확히 실패(설정 오류).
        if not self.settings.telegram_chat_id:
            raise ValueError(
                "TELEGRAM_CHAT_ID가 비어 있습니다 — 봇 토큰만으로는 검수를 진행할 수 없습니다."
            )

        client = self._client or TelegramClient(self.settings.telegram_bot_token)
        store = self._store or JsonFileReviewStore(self.settings.review_store_path)
        chat_id = self.settings.telegram_chat_id

        # 1) 인라인 버튼 메시지 전송 + PENDING 상태 영속화
        #    media_path가 있으면 영상 파일을 sendVideo로 전송, 없으면 텍스트 메시지.
        if review.media_path:
            inline_keyboard = [
                [
                    {"text": label, "callback_data": f"nutti:{review.id}:{value}"}
                    for label, value in _BUTTONS
                ]
            ]
            caption = f"{review.title}\n\n{review.preview}"
            message_id = client.send_video(
                chat_id,
                review.media_path,
                caption=caption,
                reply_markup={"inline_keyboard": inline_keyboard},
            )
        else:
            message_id = client.send_review(chat_id, review)
        review.message_id = message_id
        review.decision = ReviewDecision.PENDING
        store.save(review)
        log.info("telegram.sent", stage=review.stage.value, message_id=message_id)

        # 2) 콜백 롱폴링: 일치하는 버튼 탭이 오거나 타임아웃까지 대기.
        #    서버사이드 long-poll(timeout)을 사용해 getUpdates 호출 횟수를 줄인다.
        prefix = f"nutti:{review.id}:"
        offset: int | None = None
        start = self._clock()
        while True:
            remaining = self.settings.review_timeout_sec - (self._clock() - start)
            if remaining <= 0:
                store.update_decision(review.id, ReviewDecision.REJECTED, note="timeout")
                log.warning("telegram.timeout", stage=review.stage.value)
                return ReviewDecision.REJECTED

            long_poll = max(0, min(50, int(remaining)))  # 텔레그램 권장 long-poll 상한
            try:
                updates = client.get_updates(offset=offset, timeout=long_poll)
            except TelegramTransientError as exc:
                # 일시적 오류만 재시도(전체 대기는 바깥 타임아웃이 제한). 영구 오류
                # (잘못된 토큰 등 TelegramError)는 전파해 1시간 헛돌지 않고 빠르게 실패.
                log.warning("telegram.poll_transient", stage=review.stage.value, error=str(exc))
                self._sleep(self.settings.review_poll_interval_sec)
                continue

            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
                cb = update.get("callback_query")
                if not cb or not str(cb.get("data", "")).startswith(prefix):
                    continue
                # 인가 확인: 설정된 검수 채팅에서 온 콜백만 인정(아무나 승인 차단).
                if not self._is_authorized(cb, chat_id):
                    log.warning(
                        "telegram.unauthorized_callback",
                        stage=review.stage.value,
                        from_chat=_callback_origin_chat(cb) or None,
                    )
                    try:
                        client.answer_callback(cb.get("id", ""))
                    except Exception:
                        pass
                    continue
                decision = _decision_from_callback(cb["data"], prefix)
                # 사람이 이미 결정했으므로 UI 호출 전에 먼저 영속화(분실 방지).
                store.update_decision(review.id, decision)
                try:
                    client.answer_callback(cb.get("id", ""))
                    # 미디어 메시지(영상 등)는 editMessageCaption, 텍스트는 editMessageText.
                    if review.media_path:
                        client.edit_caption(chat_id, message_id, f"검수 완료: {decision.value}")
                    else:
                        client.edit_message(chat_id, message_id, f"검수 완료: {decision.value}")
                except Exception:  # UI 갱신은 best-effort
                    log.warning("telegram.ui_update_failed", review_id=review.id)
                log.info(
                    "telegram.decision", stage=review.stage.value, decision=decision.value
                )

                # REVISE: 수정 안내 메시지를 보내고 텍스트 입력 대기.
                # 이미 소비한 시간을 전달해 총 대기가 review_timeout_sec를 넘지 않도록 한다.
                if decision == ReviewDecision.REVISE:
                    elapsed = self._clock() - start
                    revised = self._wait_for_text_input(
                        client, chat_id, offset, review=review, elapsed_sec=elapsed
                    )
                    if revised is not None:
                        review.revised_content = revised
                        log.info("telegram.revised_content_received", stage=review.stage.value)
                # REJECTED: 반려 사유를 한 줄 받아 다음 런 대본·주제 프롬프트에 넣는다
                # (2026-07-29 PO 선택 — 대본 4연속 반려에도 사유가 코드로 안 돌아와
                # 리드가 추측으로 프롬프트를 고치는 낭비가 있었다). 사유 입력은 선택이라
                # 짧은 별도 타임아웃(reject_reason_timeout_sec)만 기다리고 넘어간다.
                elif decision == ReviewDecision.REJECTED:
                    reason = self._wait_for_text_input(
                        client,
                        chat_id,
                        offset,
                        review=review,
                        timeout_sec=self.settings.reject_reason_timeout_sec,
                        prompt_text=(
                            "✏️ 반려 사유를 한 줄로 적어주세요 — 다음 대본 생성에 "
                            "그대로 반영됩니다. (안 적으면 잠시 후 그냥 넘어갑니다)"
                        ),
                    )
                    if reason:
                        review.note = reason
                        store.update_decision(review.id, decision, note=reason)
                        log.info("telegram.reject_reason_received", stage=review.stage.value)

                return decision

            self._sleep(self.settings.review_poll_interval_sec)

    def _wait_for_text_input(
        self,
        client: TelegramClient,
        chat_id: str,
        offset: int | None,
        *,
        review: ReviewRequest | None = None,
        elapsed_sec: float = 0.0,
        timeout_sec: float | None = None,
        prompt_text: str = "✏️ 수정할 대본 내용을 입력해 주세요.",
    ) -> str | None:
        """안내 메시지를 보내고 사용자의 일반 텍스트 메시지를 수신 대기한다.

        elapsed_sec: 콜백 폴링에서 이미 소비한 시간(초). 남은 시간 = 타임아웃 - elapsed_sec.
        timeout_sec: 이 대기의 상한(기본 review_timeout_sec). 반려 사유처럼 입력이
        선택인 경우 짧게 줘서 안 적으면 곧장 넘어가게 한다.
        타임아웃 내에 인가된 채팅에서 텍스트 메시지가 오면 반환하고,
        타임아웃이 지나면 None을 반환한다.

        `review`를 주면 **검수 카드 자신의 텍스트를 수정 대본으로 받아들이지 않는다.**
        실측 사고(2026-07-29 run 805e16ea): 수신된 "수정 대본"이 카드 본문
        (`title\\n\\npreview`)과 바이트 단위로 일치했고, 그게 그대로 script.body가 되어
        "대본 검수(클립별) [대본 검수 — 3개 클립·약 24초]"가 영상 자막·훅 오버레이로
        구워졌다($1.14 소모 후 PO 반려). 봇이 보낸 메시지(from.is_bot)와 카드 제목으로
        시작하는 텍스트는 건너뛰고 진짜 입력을 계속 기다린다.
        """
        try:
            client.send_message(chat_id, prompt_text)
        except Exception:  # 안내 메시지 실패는 best-effort
            log.warning("telegram.revise_prompt_failed")

        limit = (
            float(timeout_sec)
            if timeout_sec is not None
            else float(self.settings.review_timeout_sec)
        )
        start = self._clock() - elapsed_sec  # elapsed만큼 앞당겨 총 타임아웃 내에서 소진
        current_offset = offset
        while True:
            remaining = limit - (self._clock() - start)
            if remaining <= 0:
                log.warning("telegram.revise_text_timeout")
                return None

            long_poll = max(0, min(50, int(remaining)))
            try:
                updates = client.get_updates(offset=current_offset, timeout=long_poll)
            except TelegramTransientError as exc:
                log.warning("telegram.revise_poll_transient", error=str(exc))
                self._sleep(self.settings.review_poll_interval_sec)
                continue

            for update in updates:
                current_offset = int(update.get("update_id", 0)) + 1
                msg = update.get("message") or {}
                text = msg.get("text", "")
                if not text:
                    continue
                # 인가 확인: 설정된 검수 채팅에서 온 메시지만 수락.
                msg_chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not msg_chat_id or msg_chat_id != str(chat_id):
                    continue
                # 에코 차단: 봇 메시지·검수 카드 본문은 수정 대본이 아니다(위 docstring 사고).
                if (msg.get("from") or {}).get("is_bot") or _is_review_echo(text, review):
                    log.warning(
                        "telegram.revise_echo_ignored",
                        stage=review.stage.value if review else "",
                    )
                    continue
                # 수정 내용 접수 완료 표시(best-effort).
                try:
                    client.edit_message(
                        chat_id, msg.get("message_id", 0), "✅ 수정 내용 접수 완료"
                    )
                except Exception:
                    pass
                return text

            self._sleep(self.settings.review_poll_interval_sec)

    @staticmethod
    def _is_authorized(cb: dict, chat_id: str) -> bool:
        """콜백이 설정된 검수 채팅(chat_id)에서 왔는지 확인한다.

        message.chat.id만 신뢰한다 — 텔레그램 서버가 봇이 메시지를 보낸 채팅으로
        설정하는 값이라 위조 불가능하고, 설정된 chat_id와 정의상 일치한다(1:1 DM이면
        chat.id가 곧 사용자 id). from.id(탭한 사용자)는 봇이 속한 다른 채팅에서도
        일치할 수 있어 인증 기준으로 쓰면 우회가 생기므로 사용하지 않는다.
        message가 없는 inline-mode 콜백은 chat을 알 수 없어 미인가로 처리한다.
        """
        if not chat_id:
            return False
        origin_chat = _callback_origin_chat(cb)
        return bool(origin_chat) and origin_chat == str(chat_id)
