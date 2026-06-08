"""검수 게이트 추상화.

파이프라인은 ReviewGate 인터페이스에만 의존한다. 실제 채널(텔레그램/디스코드)이나
자동 승인(테스트)은 구현체를 갈아끼우면 된다.
"""

from __future__ import annotations

import re
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
                        client, chat_id, offset, elapsed_sec=elapsed
                    )
                    if revised is not None:
                        review.revised_content = revised
                        log.info("telegram.revised_content_received", stage=review.stage.value)

                return decision

            self._sleep(self.settings.review_poll_interval_sec)

    def _wait_for_text_input(
        self,
        client: TelegramClient,
        chat_id: str,
        offset: int | None,
        *,
        elapsed_sec: float = 0.0,
    ) -> str | None:
        """수정 안내 메시지를 보내고 사용자의 일반 텍스트 메시지를 수신 대기한다.

        elapsed_sec: 콜백 폴링에서 이미 소비한 시간(초). 남은 시간 = review_timeout_sec - elapsed_sec.
        타임아웃 내에 인가된 채팅에서 텍스트 메시지가 오면 반환하고,
        타임아웃이 지나면 None을 반환한다.
        """
        try:
            client.send_message(chat_id, "✏️ 수정할 대본 내용을 입력해 주세요.")
        except Exception:  # 안내 메시지 실패는 best-effort
            log.warning("telegram.revise_prompt_failed")

        start = self._clock() - elapsed_sec  # elapsed만큼 앞당겨 총 타임아웃 내에서 소진
        current_offset = offset
        while True:
            remaining = self.settings.review_timeout_sec - (self._clock() - start)
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


# ---------------------------------------------------------------------------
# 디스코드 게이트 (검수③)
# ---------------------------------------------------------------------------


class DiscordWebhookError(RuntimeError):
    """디스코드 웹훅 호출 실패(영구). 웹훅 URL 토큰은 메시지에서 가려진다."""


class DiscordWebhookTransientError(DiscordWebhookError):
    """일시적 실패(네트워크/429/5xx) — 호출자가 재시도해도 되는 경우."""


class DiscordReceiverTransientError(RuntimeError):
    """DiscordDecisionReceiver.poll() 일시적 실패(네트워크/타임아웃 등).

    poll() 구현체가 이 예외를 던지면 DiscordGate 폴링 루프가 재시도한다.
    영구 오류는 이 클래스가 아닌 RuntimeError(또는 그 서브클래스)를 사용해
    전파시켜 빠르게 실패하도록 한다.
    """



class DiscordWebhookClient:
    """Discord 웹훅 POST 래퍼. httpx는 실제 경로에서만 lazy import한다.

    `http`로 httpx.Client를 주입하면 네트워크 없이 테스트할 수 있다.

    보안: webhook_url에는 토큰이 포함되므로 공개 속성으로 노출하지 않는다.
    마스킹된 URL이 필요하면 `masked_url` 프로퍼티를 사용한다.
    """

    def __init__(self, webhook_url: str, *, http=None):
        # 웹훅 URL(토큰 포함)을 비공개로 저장 — 공개 속성 노출 금지.
        self._webhook_url = webhook_url
        self._http = http

    @property
    def masked_url(self) -> str:
        """웹훅 URL에서 토큰(마지막 경로 세그먼트)을 *** 로 마스킹해 반환한다.

        로그/에러 메시지에서 토큰이 노출되지 않도록 이 프로퍼티를 사용한다.
        """
        return re.sub(r"(/webhooks/\d+/)[^/?#]+", r"\1***", self._webhook_url)

    @property
    def _client(self):
        """주입된 클라이언트가 없으면 httpx.Client를 지연 생성한다."""
        if self._http is None:
            import httpx  # lazy import — dry_run 경로에서는 불필요

            self._http = httpx.Client(timeout=30.0)
        return self._http

    def send_card(self, review: ReviewRequest) -> None:
        """검수 카드를 디스코드 웹훅으로 전송한다(embed 형식).

        HTTP 4xx(429 제외)/5xx 에러는 DiscordWebhookError/DiscordWebhookTransientError로
        변환하며, 웹훅 URL 토큰은 예외 메시지에서 가려진다.
        """
        import httpx  # lazy import — TransportError/TooManyRedirects 참조용

        payload = {
            "embeds": [
                {
                    "title": f"[검수 요청] {review.title}",
                    "description": review.preview[:2000],  # embed description 최대 4096자, 여유있게 제한
                    "fields": [
                        {"name": "단계", "value": review.stage.value, "inline": True},
                        {"name": "검수 ID", "value": review.id, "inline": True},
                    ],
                    "color": 0x5865F2,  # Discord 브랜드 색상
                }
            ]
        }
        try:
            resp = self._client.post(self._webhook_url, json=payload)
        except (httpx.TransportError, httpx.TooManyRedirects) as exc:
            # 전송 계층 오류 — 예외 메시지에 원본 URL(토큰 포함)이 담길 수 있으므로
            # type(exc).__name__ 만 사용하고 URL/바디는 포함하지 않는다.
            raise DiscordWebhookTransientError(
                f"discord_transport_error type={type(exc).__name__}"
            ) from None

        if resp.status_code == 429:
            # 429 응답 바디의 retry_after(float, 초 단위) 파싱.
            try:
                body = resp.json()
                retry_after = float(body.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            raise DiscordWebhookTransientError(f"rate_limited retry_after={retry_after}")
        if 500 <= resp.status_code < 600:
            raise DiscordWebhookTransientError(
                f"discord_server_error status={resp.status_code}"
            )
        if not resp.is_success:
            # 응답 body의 message 필드는 임의 문자열(URL/토큰 포함 가능)이므로
            # 상태 코드와 Discord 에러 코드(정수)만 노출한다.
            try:
                body = resp.json()
                detail = f"status={resp.status_code} code={body.get('code')}"
            except Exception:
                detail = f"status={resp.status_code}"
            raise DiscordWebhookError(f"discord_error {detail}")
        # 204 No Content 또는 200/201 — 성공.

    def close(self) -> None:
        """httpx 클라이언트를 닫는다(명시적 정리가 필요할 때).

        close() 후 self._http를 None으로 초기화해 두 번째 send_card 호출 시
        _client 프로퍼티가 새 httpx.Client를 재생성하지 않도록 한다.
        """
        if self._http is not None:
            self._http.close()
            self._http = None  # 항목 8: close 후 재생성 누수 방지

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class DiscordDecisionReceiver(Protocol):
    """디스코드 인바운드 결정 수신 인터페이스.

    실제 구현은 Discord Bot Gateway/인터랙션 또는 외부 저장소 폴링이 필요하다.
    현재 DiscordGate는 이 Protocol을 주입받아 사용하므로, 향후 실 구현 시
    이 Protocol을 구현한 클래스를 만들어 주입하면 된다.
    """

    def poll(self, review_id: str) -> ReviewDecision | None:
        """검수 ID에 대한 결정을 조회한다. 아직 결정 없으면 None 반환."""
        ...


class InMemoryDiscordStore:
    """메모리 기반 디스코드 결정 저장소(테스트/fake 수신 채널용).

    테스트에서 set_decision으로 결정을 미리 심어두면, poll이 해당 결정을 반환한다.
    """

    def __init__(self) -> None:
        self._decisions: dict[str, ReviewDecision] = {}

    def set_decision(self, review_id: str, decision: ReviewDecision) -> None:
        """결정을 저장한다(테스트에서 fake 수신 시뮬레이션용)."""
        self._decisions[review_id] = decision

    def poll(self, review_id: str) -> ReviewDecision | None:
        """저장된 결정이 있으면 반환하고, 없으면 None을 반환한다."""
        return self._decisions.get(review_id)


class DiscordGate:
    """디스코드 검수/아카이브(검수③ + 리포트 보관).

    설계: 웹훅으로 검수 카드를 전송하고, DiscordDecisionReceiver Protocol을 통해
    인바운드 결정을 폴링 방식으로 기다린다. 실 수신 구현(Bot Gateway 등)은
    TODO(live) — receiver=None이면 ValueError로 fast-fail한다.
    client/receiver/store/clock/sleep을 주입하면 네트워크 없이 테스트할 수 있다.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: DiscordWebhookClient | None = None,
        receiver: DiscordDecisionReceiver | None = None,
        store: ReviewStore | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.settings = settings
        self._client = client
        self._receiver = receiver
        self._store = store
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], None] = sleep or time.sleep

    def request(self, review: ReviewRequest) -> ReviewDecision:
        """검수 카드를 전송하고 결정이 수신될 때까지 폴링한다.

        dry_run=True 또는 discord_webhook_url이 비어있으면 자동 승인한다.
        실 경로에서 receiver가 None이면 ValueError로 즉시 실패한다
        (TODO(live): Discord Bot Gateway 또는 외부 저장소 기반 수신 구현 필요).
        """
        if self.settings.dry_run or not self.settings.discord_webhook_url:
            log.info("discord.dry_run_approve", stage=review.stage.value)
            return ReviewDecision.APPROVED

        # receiver가 없으면 결정을 수신할 방법이 없으므로 명확히 실패.
        # TODO(live): Discord Bot Gateway/인터랙션 기반 DiscordDecisionReceiver 구현 필요.
        if self._receiver is None:
            raise ValueError(
                "DISCORD_DECISION_RECEIVER가 설정되지 않았습니다 — "
                "Discord Bot Gateway 또는 외부 저장소 기반 수신 구현이 필요합니다. TODO(live)"
            )

        # self._client가 None이면 직접 생성하고, 종료 시 반드시 close()를 호출해
        # httpx 커넥션 풀이 누수되지 않도록 한다. 주입된 클라이언트는 호출자가 책임진다.
        _own_client = self._client is None
        client = self._client or DiscordWebhookClient(self.settings.discord_webhook_url)
        store = self._store or JsonFileReviewStore(self.settings.review_store_path)

        try:
            # 1) 웹훅으로 검수 카드 전송 + PENDING 상태 영속화.
            #    DiscordWebhookError(영구)/DiscordWebhookTransientError(일시적)는 전파(fast-fail).
            client.send_card(review)
            review.decision = ReviewDecision.PENDING
            store.save(review)
            log.info("discord.sent", stage=review.stage.value, review_id=review.id)

            # 2) 결정 폴링: receiver.poll()이 결정을 반환하거나 타임아웃까지 대기.
            #    일시적 오류(DiscordReceiverTransientError)는 재시도, 전체 대기는 타임아웃이 제한.
            start = self._clock()
            while True:
                remaining = self.settings.review_timeout_sec - (self._clock() - start)
                if remaining <= 0:
                    store.update_decision(review.id, ReviewDecision.REJECTED, note="timeout")
                    log.warning("discord.timeout", stage=review.stage.value)
                    return ReviewDecision.REJECTED

                try:
                    decision = self._receiver.poll(review.id)
                except DiscordReceiverTransientError as exc:
                    # 일시적 오류만 재시도(전체 대기는 바깥 타임아웃이 제한). 영구 오류는 전파.
                    log.warning(
                        "discord.poll_transient", stage=review.stage.value, error=str(exc)
                    )
                    self._sleep(self.settings.review_poll_interval_sec)
                    continue
                if decision is not None:
                    store.update_decision(review.id, decision)
                    log.info(
                        "discord.decision", stage=review.stage.value, decision=decision.value
                    )
                    return decision

                self._sleep(self.settings.review_poll_interval_sec)
        finally:
            if _own_client:
                client.close()
