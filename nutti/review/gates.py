"""검수 게이트 추상화.

파이프라인은 ReviewGate 인터페이스에만 의존한다. 실제 채널(텔레그램/디스코드)이나
자동 승인(테스트)은 구현체를 갈아끼우면 된다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from nutti.config import Settings
from nutti.integrations.telegram import TelegramClient
from nutti.logging import get_logger
from nutti.models import ReviewDecision, ReviewRequest
from nutti.storage.reviews import JsonFileReviewStore, ReviewStore

log = get_logger(__name__)


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

        client = self._client or TelegramClient(self.settings.telegram_bot_token)
        store = self._store or JsonFileReviewStore(self.settings.review_store_path)
        chat_id = self.settings.telegram_chat_id

        # 1) 인라인 버튼 메시지 전송 + PENDING 상태 영속화
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
            for update in client.get_updates(offset=offset, timeout=long_poll):
                offset = int(update.get("update_id", 0)) + 1
                cb = update.get("callback_query")
                if not cb or not str(cb.get("data", "")).startswith(prefix):
                    continue
                decision = _decision_from_callback(cb["data"], prefix)
                # 사람이 이미 결정했으므로 UI 호출 전에 먼저 영속화(분실 방지).
                store.update_decision(review.id, decision)
                try:
                    client.answer_callback(cb.get("id", ""))
                    client.edit_message(chat_id, message_id, f"검수 완료: {decision.value}")
                except Exception:  # UI 갱신은 best-effort
                    log.warning("telegram.ui_update_failed", review_id=review.id)
                log.info(
                    "telegram.decision", stage=review.stage.value, decision=decision.value
                )
                return decision

            self._sleep(self.settings.review_poll_interval_sec)


class DiscordGate:
    """디스코드 검수/아카이브(검수③ + 리포트 보관)."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def request(self, review: ReviewRequest) -> ReviewDecision:
        if self.settings.dry_run or not self.settings.discord_webhook_url:
            log.info("discord.dry_run_approve", stage=review.stage.value)
            return ReviewDecision.APPROVED
        # TODO: 웹훅으로 메타데이터 전송 → 답장 기반 수정 요청 수신
        raise NotImplementedError("디스코드 검수 미구현")
