"""DiscordGate 단위 테스트.

네트워크 없이 FakeDiscordWebhookClient + FakeDiscordReceiver + InMemoryReviewStore +
가짜 clock/sleep을 주입해 dry_run 자동승인, 결정 수신, 타임아웃, HTTP 에러,
redaction, store 영속, receiver=None 경우를 검증한다.
"""

from __future__ import annotations

from collections import deque

import pytest

from nutti.config import Settings
from nutti.models import ReviewDecision, ReviewRequest, Stage
from nutti.review.gates import (
    DiscordGate,
    DiscordReceiverTransientError,
    DiscordWebhookClient,
    DiscordWebhookError,
    DiscordWebhookTransientError,
    InMemoryDiscordStore,
)
from nutti.storage.reviews import InMemoryReviewStore


# ---------------------------------------------------------------------------
# Fake 클라이언트 / 수신기
# ---------------------------------------------------------------------------


class FakeDiscordWebhookClient:
    """send_card 호출을 기록하는 가짜 웹훅 클라이언트."""

    def __init__(self, *, raise_on_send: Exception | None = None):
        self.sent: list[ReviewRequest] = []
        self._raise = raise_on_send

    def send_card(self, review: ReviewRequest) -> None:
        if self._raise is not None:
            raise self._raise
        self.sent.append(review)


class FakeDiscordReceiver:
    """큐 기반으로 poll 호출 시 순서대로 결정을 반환하는 가짜 수신기.

    큐에 있는 값이 None이면 "아직 결정 없음"(대기)을 나타낸다.
    """

    def __init__(self, responses: list[ReviewDecision | None]):
        self._queue: deque[ReviewDecision | None] = deque(responses)
        self.calls: list[str] = []  # poll에 전달된 review_id 기록

    def poll(self, review_id: str) -> ReviewDecision | None:
        self.calls.append(review_id)
        if self._queue:
            return self._queue.popleft()
        return None


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

_WEBHOOK_URL = "https://discord.com/api/webhooks/123456789/TOKEN_SECRET_VALUE"


def _live_settings(**kwargs) -> Settings:
    """dry_run=False + webhook_url 존재 → 실제 경로를 타게 한다."""
    defaults = dict(
        NUTTI_DRY_RUN=False,
        DISCORD_WEBHOOK_URL=_WEBHOOK_URL,
        NUTTI_REVIEW_TIMEOUT_SEC=10,
        NUTTI_REVIEW_POLL_INTERVAL_SEC=0,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _review() -> ReviewRequest:
    return ReviewRequest(stage=Stage.SCRIPT, title="대본 검수", preview="미리보기 내용")


def _gate(
    client=None,
    receiver=None,
    store=None,
    clock=None,
    sleep=None,
    settings=None,
) -> DiscordGate:
    return DiscordGate(
        settings or _live_settings(),
        client=client or FakeDiscordWebhookClient(),
        receiver=receiver,
        store=store or InMemoryReviewStore(),
        clock=clock,
        sleep=sleep or (lambda _s: None),
    )


# ---------------------------------------------------------------------------
# dry_run / webhook_url 없음 → 자동 승인
# ---------------------------------------------------------------------------


def test_dry_run_auto_approves():
    """dry_run=True이면 네트워크 호출 없이 자동 승인해야 한다."""
    gate = DiscordGate(Settings(NUTTI_DRY_RUN=True))
    assert gate.request(_review()) == ReviewDecision.APPROVED


def test_no_webhook_url_auto_approves():
    """discord_webhook_url이 비어있으면 자동 승인해야 한다."""
    settings = Settings(NUTTI_DRY_RUN=False, DISCORD_WEBHOOK_URL="")
    gate = DiscordGate(settings)
    assert gate.request(_review()) == ReviewDecision.APPROVED


# ---------------------------------------------------------------------------
# 결정 수신 케이스
# ---------------------------------------------------------------------------


def test_approved_decision():
    """fake receiver가 APPROVED를 반환하면 APPROVED를 반환해야 한다."""
    review = _review()
    receiver = FakeDiscordReceiver([ReviewDecision.APPROVED])
    store = InMemoryReviewStore()
    decision = _gate(receiver=receiver, store=store).request(review)

    assert decision == ReviewDecision.APPROVED
    # store에 최종 결정이 영속화되어야 한다.
    assert store.get(review.id).decision == ReviewDecision.APPROVED


def test_rejected_decision():
    """fake receiver가 REJECTED를 반환하면 REJECTED를 반환해야 한다."""
    review = _review()
    receiver = FakeDiscordReceiver([ReviewDecision.REJECTED])
    assert _gate(receiver=receiver).request(review) == ReviewDecision.REJECTED


def test_revise_decision():
    """fake receiver가 REVISE를 반환하면 REVISE를 반환해야 한다."""
    review = _review()
    receiver = FakeDiscordReceiver([ReviewDecision.REVISE])
    assert _gate(receiver=receiver).request(review) == ReviewDecision.REVISE


def test_decision_after_none_responses():
    """poll이 먼저 None을 여러 번 반환한 뒤 결정을 반환해도 올바르게 처리해야 한다."""
    review = _review()
    receiver = FakeDiscordReceiver([None, None, ReviewDecision.APPROVED])
    decision = _gate(receiver=receiver).request(review)
    assert decision == ReviewDecision.APPROVED
    # poll이 3회 호출되어야 한다(None, None, APPROVED).
    assert len(receiver.calls) == 3


# ---------------------------------------------------------------------------
# 타임아웃
# ---------------------------------------------------------------------------


def test_timeout_returns_rejected():
    """타임아웃 발생 시 REJECTED + store에 note='timeout'이 기록되어야 한다.

    clock load-bearing 검증: send_card 직후 clock이 즉시 타임아웃 값으로 점프하면
    poll()이 한 번도 호출되지 않고 타임아웃이 발생해야 한다.
    counter 기반이었다면 빈 큐를 소진한 뒤에야 종료됐을 것이다.
    """
    review = _review()
    # tick 0: send_card 이후 start = 0.0
    # tick 1: while 루프 첫 remaining 계산 → 9999.0 - 0.0 = 이미 초과 → 즉시 타임아웃
    ticks = iter([0.0, 9999.0])
    store = InMemoryReviewStore()
    receiver = FakeDiscordReceiver([])  # 결정 없음
    gate = _gate(receiver=receiver, store=store, clock=lambda: next(ticks))
    decision = gate.request(review)

    assert decision == ReviewDecision.REJECTED
    saved = store.get(review.id)
    assert saved is not None
    assert saved.decision == ReviewDecision.REJECTED
    assert saved.note == "timeout"
    # clock 기반이면 poll이 한 번도 호출되지 않는다(첫 remaining 계산에서 즉시 타임아웃).
    assert len(receiver.calls) == 0, "clock 기반 타임아웃이면 poll()이 호출되지 않아야 합니다"


# ---------------------------------------------------------------------------
# HTTP 에러 전파
# ---------------------------------------------------------------------------


def test_http_error_on_send_propagates():
    """send_card에서 DiscordWebhookError가 발생하면 그대로 전파되어야 한다."""
    review = _review()
    exc = DiscordWebhookError("discord_error code=50035 message=Invalid Form Body")
    client = FakeDiscordWebhookClient(raise_on_send=exc)
    receiver = FakeDiscordReceiver([ReviewDecision.APPROVED])

    with pytest.raises(DiscordWebhookError):
        _gate(client=client, receiver=receiver).request(review)


def test_transient_http_error_on_send_propagates():
    """send_card에서 DiscordWebhookTransientError도 전파되어야 한다."""
    review = _review()
    exc = DiscordWebhookTransientError("rate_limited retry_after=1.0")
    client = FakeDiscordWebhookClient(raise_on_send=exc)
    receiver = FakeDiscordReceiver([ReviewDecision.APPROVED])

    with pytest.raises(DiscordWebhookTransientError):
        _gate(client=client, receiver=receiver).request(review)


# ---------------------------------------------------------------------------
# send_card 에러 시 토큰 비노출 (send_card 경로에 _scrub_webhook_url 없음 — 에러 메시지가
# 상태코드+code 정수만 포함하도록 설계됐으므로 직접 단언)
# ---------------------------------------------------------------------------


def test_discord_webhook_client_send_card_4xx_does_not_leak_token():
    """DiscordWebhookClient.send_card가 4xx 에러 발생 시 토큰을 메시지에 포함하지 않는다."""

    class _FakeResp:
        status_code = 400
        is_success = False

        def json(self):
            return {"code": 50035, "message": f"bad request for {_WEBHOOK_URL}"}

    class _FakeHttp:
        def post(self, url, **kwargs):
            return _FakeResp()

    client = DiscordWebhookClient(_WEBHOOK_URL, http=_FakeHttp())
    with pytest.raises(DiscordWebhookError) as exc_info:
        client.send_card(_review())

    err_msg = str(exc_info.value)
    assert "TOKEN_SECRET_VALUE" not in err_msg


# ---------------------------------------------------------------------------
# store PENDING → 결정 영속
# ---------------------------------------------------------------------------


def test_store_persists_pending_then_decision():
    """send_card 직후 PENDING이 저장되고, 결정 수신 후 최종 결정으로 업데이트되어야 한다."""
    review = _review()
    store = InMemoryReviewStore()
    client = FakeDiscordWebhookClient()

    # send_card 후 PENDING 확인을 위해 receiver가 폴링 전에 한 번 None을 반환.
    pending_checked = []

    class _CapturingReceiver:
        def poll(self, review_id: str) -> ReviewDecision | None:
            saved = store.get(review_id)
            if not pending_checked:
                # 첫 poll 시점에 PENDING이어야 함.
                if saved is not None:
                    pending_checked.append(saved.decision)
                return None
            return ReviewDecision.APPROVED

    gate = _gate(client=client, receiver=_CapturingReceiver(), store=store)
    decision = gate.request(review)

    assert decision == ReviewDecision.APPROVED
    assert pending_checked == [ReviewDecision.PENDING]
    assert store.get(review.id).decision == ReviewDecision.APPROVED


# ---------------------------------------------------------------------------
# receiver=None → ValueError (실 경로 + receiver 미주입)
# ---------------------------------------------------------------------------


def test_receiver_none_raises_value_error():
    """실 경로(dry_run=False, webhook_url 존재)에서 receiver가 None이면 ValueError가 발생해야 한다."""
    # receiver를 주입하지 않고 실 경로를 타면 ValueError.
    gate = DiscordGate(
        _live_settings(),
        client=FakeDiscordWebhookClient(),
        receiver=None,  # 명시적으로 None
        store=InMemoryReviewStore(),
    )
    with pytest.raises(ValueError, match="DISCORD_DECISION_RECEIVER"):
        gate.request(_review())


# ---------------------------------------------------------------------------
# InMemoryDiscordStore
# ---------------------------------------------------------------------------


def test_in_memory_discord_store_set_and_poll():
    """InMemoryDiscordStore가 set_decision/poll을 올바르게 동작해야 한다."""
    store = InMemoryDiscordStore()
    assert store.poll("unknown_id") is None

    store.set_decision("review_abc", ReviewDecision.APPROVED)
    assert store.poll("review_abc") == ReviewDecision.APPROVED
    assert store.poll("other_id") is None


# ---------------------------------------------------------------------------
# send_card가 실제로 호출되는지 확인
# ---------------------------------------------------------------------------


def test_send_card_called_with_review():
    """request()가 send_card를 올바른 ReviewRequest로 호출해야 한다."""
    review = _review()
    client = FakeDiscordWebhookClient()
    receiver = FakeDiscordReceiver([ReviewDecision.APPROVED])
    _gate(client=client, receiver=receiver).request(review)

    assert len(client.sent) == 1
    assert client.sent[0].id == review.id


# ---------------------------------------------------------------------------
# receiver.poll() 일시적 오류 복원
# ---------------------------------------------------------------------------


class _TransientThenDecisionReceiver:
    """첫 N번 poll()에서 DiscordReceiverTransientError를 던진 뒤 결정을 반환하는 fake 수신기."""

    def __init__(self, transient_count: int, final_decision: ReviewDecision):
        self._remaining = transient_count
        self._decision = final_decision
        self.calls: list[str] = []

    def poll(self, review_id: str) -> ReviewDecision | None:
        self.calls.append(review_id)
        if self._remaining > 0:
            self._remaining -= 1
            raise DiscordReceiverTransientError("임시 네트워크 오류")
        return self._decision


class _PermanentErrorReceiver:
    """poll() 호출 시 항상 일반 RuntimeError를 던지는 fake 수신기(영구 오류 시뮬레이션)."""

    def poll(self, review_id: str) -> ReviewDecision | None:
        raise RuntimeError("영구 오류 — 재시도 불가")


def test_receiver_transient_error_retries_and_succeeds():
    """poll()이 DiscordReceiverTransientError를 던지면 재시도 후 결정을 반환해야 한다.

    이 테스트가 없으면 try/except 제거 시 예외가 전파되어 store가 PENDING으로 남게 된다.
    """
    review = _review()
    store = InMemoryReviewStore()
    receiver = _TransientThenDecisionReceiver(
        transient_count=2, final_decision=ReviewDecision.APPROVED
    )
    gate = _gate(receiver=receiver, store=store)
    decision = gate.request(review)

    assert decision == ReviewDecision.APPROVED
    # 총 3번 poll 호출(2번 일시적 오류 + 1번 성공)
    assert len(receiver.calls) == 3
    # store에 최종 결정이 영속화되어야 한다
    assert store.get(review.id).decision == ReviewDecision.APPROVED


def test_receiver_permanent_error_propagates_and_store_stays_pending():
    """poll()이 RuntimeError(영구 오류)를 던지면 예외가 전파되고 store는 PENDING으로 남는다.

    DiscordReceiverTransientError가 아닌 예외는 재시도하지 않고 즉시 전파해야 한다.
    """
    review = _review()
    store = InMemoryReviewStore()
    receiver = _PermanentErrorReceiver()
    gate = _gate(receiver=receiver, store=store)

    with pytest.raises(RuntimeError, match="영구 오류"):
        gate.request(review)

    # store는 PENDING 상태로 남아야 한다(결정 수신 실패)
    saved = store.get(review.id)
    assert saved is not None
    assert saved.decision == ReviewDecision.PENDING


# ---------------------------------------------------------------------------
# DiscordWebhookClient 커넥션 풀 누수 방지 — close() 호출 회귀 방지
# ---------------------------------------------------------------------------


def test_self_created_client_is_closed_on_success():
    """DiscordGate가 직접 생성한 클라이언트는 정상 종료 시 close()가 호출되어야 한다.

    self._client=None이면 DiscordGate.request()가 DiscordWebhookClient를 직접 생성하고
    try/finally 안에서 close()를 호출해야 한다. 이 테스트가 없으면 try/finally 블록을
    삭제해도 ruff/테스트가 통과해 버려 httpx 커넥션 풀 누수가 재발할 수 있다.
    """

    class _TrackingClient:
        """send_card 기록 + close() 호출 횟수를 추적하는 가짜 클라이언트."""

        def __init__(self):
            self.sent: list[ReviewRequest] = []
            self.close_count = 0

        def send_card(self, review: ReviewRequest) -> None:
            self.sent.append(review)

        def close(self) -> None:
            self.close_count += 1

    tracking = _TrackingClient()

    # DiscordGate에 client를 주입하지 않고, DiscordWebhookClient 생성자를 monkeypatch로
    # _TrackingClient를 반환하도록 교체한다.
    import nutti.review.gates as _gates_mod

    _orig = _gates_mod.DiscordWebhookClient

    def _fake_constructor(webhook_url: str):
        return tracking

    _gates_mod.DiscordWebhookClient = _fake_constructor  # type: ignore[assignment]
    try:
        gate = DiscordGate(
            _live_settings(),
            # client를 주입하지 않음 → _own_client=True → close()가 호출되어야 함
            receiver=FakeDiscordReceiver([ReviewDecision.APPROVED]),
            store=InMemoryReviewStore(),
            sleep=lambda _s: None,
        )
        decision = gate.request(_review())
    finally:
        _gates_mod.DiscordWebhookClient = _orig  # type: ignore[assignment]

    assert decision == ReviewDecision.APPROVED
    assert len(tracking.sent) == 1
    # close()가 정확히 1번 호출되어야 한다(커넥션 풀 누수 방지 계약).
    assert tracking.close_count == 1, "DiscordGate가 직접 생성한 클라이언트를 close()하지 않았습니다"


def test_self_created_client_is_closed_on_error():
    """send_card에서 예외가 발생해도 직접 생성한 클라이언트의 close()가 호출되어야 한다.

    try/finally가 없으면 예외 전파 시 close()가 누락되어 커넥션 풀이 누수된다.
    """

    class _ErrorOnSendClient:
        def __init__(self):
            self.close_count = 0

        def send_card(self, review: ReviewRequest) -> None:
            raise DiscordWebhookError("전송 실패")

        def close(self) -> None:
            self.close_count += 1

    tracking = _ErrorOnSendClient()

    import nutti.review.gates as _gates_mod

    _orig = _gates_mod.DiscordWebhookClient

    def _fake_constructor(webhook_url: str):
        return tracking

    _gates_mod.DiscordWebhookClient = _fake_constructor  # type: ignore[assignment]
    try:
        gate = DiscordGate(
            _live_settings(),
            receiver=FakeDiscordReceiver([ReviewDecision.APPROVED]),
            store=InMemoryReviewStore(),
            sleep=lambda _s: None,
        )
        with pytest.raises(DiscordWebhookError):
            gate.request(_review())
    finally:
        _gates_mod.DiscordWebhookClient = _orig  # type: ignore[assignment]

    # 예외 전파 후에도 close()가 호출되어야 한다.
    assert tracking.close_count == 1, "예외 발생 시에도 close()가 호출되어야 합니다"


def test_injected_client_is_not_closed():
    """주입된 클라이언트(self._client is not None)는 DiscordGate가 close()하지 않는다.

    호출자가 수명을 관리하는 주입 클라이언트를 DiscordGate가 임의로 닫으면 안 된다.
    """

    class _TrackingInjectedClient:
        def __init__(self):
            self.sent: list[ReviewRequest] = []
            self.close_count = 0

        def send_card(self, review: ReviewRequest) -> None:
            self.sent.append(review)

        def close(self) -> None:
            self.close_count += 1

    injected = _TrackingInjectedClient()
    gate = DiscordGate(
        _live_settings(),
        client=injected,  # 주입 → _own_client=False
        receiver=FakeDiscordReceiver([ReviewDecision.APPROVED]),
        store=InMemoryReviewStore(),
        sleep=lambda _s: None,
    )
    gate.request(_review())

    assert injected.close_count == 0, "주입된 클라이언트는 DiscordGate가 close()해서는 안 됩니다"


# ---------------------------------------------------------------------------
# 항목 3: webhook_url 공개 속성 제거 — masked_url 프로퍼티 회귀 방지
# ---------------------------------------------------------------------------


def test_webhook_client_masked_url_hides_token():
    """DiscordWebhookClient.masked_url이 토큰 부분을 *** 로 마스킹해야 한다.

    항목 3: public webhook_url 속성이 전체 토큰을 노출하면 로그/에러에서 새어나간다.
    이 테스트가 없으면 _webhook_url → webhook_url 되돌리기가 테스트 통과 상태로 재발할 수 있다.
    """
    client = DiscordWebhookClient(_WEBHOOK_URL)
    masked = client.masked_url

    assert "TOKEN_SECRET_VALUE" not in masked
    assert "***" in masked
    # webhook ID(숫자)는 노출되어도 무방하다.
    assert "123456789" in masked


def test_webhook_client_has_no_public_webhook_url_attribute():
    """DiscordWebhookClient에 공개 webhook_url 속성이 없어야 한다.

    항목 3: 전체 토큰이 포함된 URL을 공개 속성으로 노출하면 __repr__, 로깅 라이브러리,
    pydantic 직렬화 등 예상치 못한 경로로 토큰이 새어나갈 수 있다.
    """
    client = DiscordWebhookClient(_WEBHOOK_URL)
    # 공개 속성(이름이 _로 시작하지 않음)으로 webhook_url이 없어야 한다.
    assert not hasattr(client, "webhook_url"), (
        "DiscordWebhookClient에 공개 webhook_url 속성이 있으면 토큰이 노출됩니다"
    )


# ---------------------------------------------------------------------------
# 항목 8: DiscordWebhookClient.close() 후 재생성 누수 방지
# ---------------------------------------------------------------------------


def test_webhook_client_close_prevents_second_send_from_creating_new_client():
    """close() 후 _http가 None으로 초기화되어야 한다.

    항목 8: close()가 self._http = None 을 하지 않으면, close() 후 두 번째 send_card
    호출 시 _client 프로퍼티가 새 httpx.Client를 생성해 누수가 발생한다.
    이 테스트는 close() 후 _http=None 여부를 직접 확인해 회귀를 막는다.
    """
    created_clients: list = []

    class _TrackingHttp:
        """생성 횟수를 추적하는 가짜 httpx.Client."""

        def __init__(self):
            created_clients.append(self)
            self.closed = False

        def post(self, url, **kwargs):
            raise AssertionError("이 테스트에서 post를 호출해서는 안 됩니다")

        def close(self):
            self.closed = True

    # 가짜 http를 주입한 후 close() 호출
    fake_http = _TrackingHttp()
    client = DiscordWebhookClient(_WEBHOOK_URL, http=fake_http)
    client.close()

    # close() 후 _http는 None이어야 한다.
    assert client._http is None, "close() 후 _http가 None이 아닙니다 — 재생성 누수 발생 가능"
    assert fake_http.closed, "close()가 기존 httpx 클라이언트를 닫지 않았습니다"


# ---------------------------------------------------------------------------
# 항목 10: send_card TransportError → DiscordWebhookTransientError 변환
# ---------------------------------------------------------------------------


def test_send_card_transport_error_raises_transient():
    """send_card에서 httpx.TransportError 발생 시 DiscordWebhookTransientError로 변환해야 한다.

    항목 10: 이 테스트가 없으면 try/except 제거 시 원본 TransportError(토큰 포함)가
    그대로 전파되어도 테스트가 통과해버린다.
    """
    import httpx

    class _TransportErrorHttp:
        def post(self, url, **kwargs):
            raise httpx.TransportError("연결 실패", request=httpx.Request("POST", url))

    client = DiscordWebhookClient(_WEBHOOK_URL, http=_TransportErrorHttp())

    with pytest.raises(DiscordWebhookTransientError):
        client.send_card(_review())


def test_send_card_transport_error_does_not_leak_token():
    """send_card TransportError 시 예외 메시지에 웹훅 토큰이 포함되지 않아야 한다.

    항목 10: TransportError 원본 메시지에 URL(토큰 포함)이 들어있어도
    래핑된 DiscordWebhookTransientError 메시지에는 나오지 않아야 한다.
    """
    import httpx

    class _TransportErrorHttp:
        def post(self, url, **kwargs):
            # 원본 TransportError 메시지에 URL(토큰 포함)을 넣어 누출 여부 검증
            raise httpx.TransportError(
                f"failed to connect to {_WEBHOOK_URL}",
                request=httpx.Request("POST", url),
            )

    client = DiscordWebhookClient(_WEBHOOK_URL, http=_TransportErrorHttp())

    with pytest.raises(DiscordWebhookTransientError) as exc_info:
        client.send_card(_review())

    assert "TOKEN_SECRET_VALUE" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 항목 11: send_card 429 rate-limit(retry_after 파싱) + 5xx 분류
# ---------------------------------------------------------------------------


class _FakeRespHttp:
    """고정 응답을 반환하는 가짜 HTTP 클라이언트."""

    def __init__(self, status_code: int, body: dict | None = None, is_success: bool | None = None):
        self._status_code = status_code
        self._body = body or {}
        # is_success를 명시하지 않으면 2xx 여부로 자동 결정
        self._is_success = is_success if is_success is not None else (200 <= status_code < 300)

    def post(self, url, **kwargs):
        return _FakeRespObj(self._status_code, self._body, self._is_success)


class _FakeRespObj:
    def __init__(self, status_code: int, body: dict, is_success: bool):
        self.status_code = status_code
        self._body = body
        self.is_success = is_success

    def json(self):
        return self._body


def test_send_card_429_raises_transient_with_retry_after():
    """send_card가 HTTP 429 응답 시 retry_after를 파싱해 DiscordWebhookTransientError를 올린다.

    항목 11: 429 분기가 없으면 rate-limit 상황에서 영구 오류로 처리되어 재시도가 차단된다.
    """
    client = DiscordWebhookClient(
        _WEBHOOK_URL,
        http=_FakeRespHttp(429, {"retry_after": 2.5}, is_success=False),
    )

    with pytest.raises(DiscordWebhookTransientError) as exc_info:
        client.send_card(_review())

    assert "retry_after=2.5" in str(exc_info.value)


def test_send_card_429_default_retry_after_on_bad_body():
    """429 응답 body를 파싱할 수 없을 때 retry_after=1.0으로 폴백해야 한다.

    항목 11: body 파싱 실패 시에도 DiscordWebhookTransientError가 발생해야 한다.
    """

    class _BadJsonResp:
        status_code = 429
        is_success = False

        def json(self):
            raise ValueError("파싱 불가")

    class _BadJsonHttp:
        def post(self, url, **kwargs):
            return _BadJsonResp()

    client = DiscordWebhookClient(_WEBHOOK_URL, http=_BadJsonHttp())

    with pytest.raises(DiscordWebhookTransientError) as exc_info:
        client.send_card(_review())

    assert "retry_after=1.0" in str(exc_info.value)


def test_send_card_5xx_raises_transient():
    """send_card가 HTTP 5xx 응답 시 DiscordWebhookTransientError를 올린다.

    항목 11: 5xx 분기가 없으면 서버 오류가 영구 오류로 처리되어 재시도가 차단된다.
    """
    for status in (500, 502, 503):
        client = DiscordWebhookClient(
            _WEBHOOK_URL,
            http=_FakeRespHttp(status, is_success=False),
        )
        with pytest.raises(DiscordWebhookTransientError):
            client.send_card(_review())


def test_send_card_4xx_raises_permanent_error():
    """send_card가 HTTP 4xx(429 제외) 응답 시 DiscordWebhookError(영구)를 올린다.

    항목 11: 4xx와 5xx의 분류가 올바른지 확인한다.
    """
    client = DiscordWebhookClient(
        _WEBHOOK_URL,
        http=_FakeRespHttp(400, {"code": 50035, "message": "Invalid Form Body"}, is_success=False),
    )

    with pytest.raises(DiscordWebhookError) as exc_info:
        client.send_card(_review())

    # 영구 오류(DiscordWebhookTransientError가 아닌 베이스 클래스)인지 확인
    assert type(exc_info.value) is DiscordWebhookError


def test_send_card_4xx_does_not_leak_token():
    """send_card 4xx 에러 메시지에 웹훅 토큰이 포함되지 않아야 한다.

    항목 11 + 항목 3: 에러 detail에 응답 body message(임의 문자열)가 들어가지 않아야 한다.
    """
    client = DiscordWebhookClient(
        _WEBHOOK_URL,
        http=_FakeRespHttp(
            400,
            {"code": 50035, "message": f"bad request including {_WEBHOOK_URL}"},
            is_success=False,
        ),
    )

    with pytest.raises(DiscordWebhookError) as exc_info:
        client.send_card(_review())

    assert "TOKEN_SECRET_VALUE" not in str(exc_info.value)
