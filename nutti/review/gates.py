"""검수 게이트 추상화.

파이프라인은 ReviewGate 인터페이스에만 의존한다. 실제 채널(텔레그램/디스코드)이나
자동 승인(테스트)은 구현체를 갈아끼우면 된다.
"""

from __future__ import annotations

from typing import Protocol

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import ReviewDecision, ReviewRequest

log = get_logger(__name__)


class ReviewGate(Protocol):
    """검수 요청을 보내고 결정(승인/거절/수정)을 반환한다."""

    def request(self, review: ReviewRequest) -> ReviewDecision: ...


class AutoApproveGate:
    """dry_run/테스트용: 항상 승인. 무인 실행 파일럿에도 사용 가능."""

    def request(self, review: ReviewRequest) -> ReviewDecision:
        log.info("auto_approve", stage=review.stage.value, title=review.title)
        return ReviewDecision.APPROVED


class TelegramGate:
    """텔레그램 인라인 버튼 검수(검수①·②).

    실제 구현은 봇이 메시지를 보내고 콜백(버튼 탭)을 기다리는 비동기 흐름이지만,
    여기서는 인터페이스만 고정한다. dry_run이면 자동 승인으로 폴백한다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def request(self, review: ReviewRequest) -> ReviewDecision:
        if self.settings.dry_run or not self.settings.telegram_bot_token:
            log.info("telegram.dry_run_approve", stage=review.stage.value)
            return ReviewDecision.APPROVED
        # TODO: sendMessage(inline_keyboard=[승인/수정/거절]) → 콜백 폴링/웹훅 대기
        raise NotImplementedError("텔레그램 검수 봇 미구현")


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
