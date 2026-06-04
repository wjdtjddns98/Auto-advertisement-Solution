"""TelegramGate 단위 테스트.

네트워크 없이 FakeTelegramClient + InMemoryReviewStore + 가짜 clock/sleep을 주입해
콜백→결정 매핑, 타임아웃, dry_run 자동승인, JSON 영속을 검증한다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.models import ReviewDecision, ReviewRequest, Stage
from nutti.review.gates import TelegramGate
from nutti.storage.reviews import InMemoryReviewStore, JsonFileReviewStore


class FakeTelegramClient:
    """getUpdates가 스크립트된 큐를 순서대로 반환하는 가짜 클라이언트."""

    def __init__(self, update_batches: list[list[dict]]):
        self._batches = list(update_batches)
        self.sent: list[ReviewRequest] = []
        self.answered: list[str] = []
        self.edited: list[tuple[int, str]] = []
        self.next_message_id = 4242

    def send_review(self, chat_id: str, review: ReviewRequest) -> int:
        self.sent.append(review)
        return self.next_message_id

    def get_updates(self, offset=None, timeout: int = 0) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def answer_callback(self, callback_query_id: str) -> None:
        self.answered.append(callback_query_id)

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self.edited.append((message_id, text))


def _live_settings() -> Settings:
    # dry_run=False + 토큰 존재 → 실제 폴링 경로를 타게 한다(클라이언트는 주입).
    return Settings(
        NUTTI_DRY_RUN=False,
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="123",
        NUTTI_REVIEW_TIMEOUT_SEC=10,
        NUTTI_REVIEW_POLL_INTERVAL_SEC=0,
    )


def _callback_update(review_id: str, value: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {"id": "cbq1", "data": f"nutti:{review_id}:{value}"},
    }


def _review() -> ReviewRequest:
    return ReviewRequest(stage=Stage.SCRIPT, title="대본 검수", preview="미리보기")


def _gate(client, store=None, clock=None):
    return TelegramGate(
        _live_settings(),
        client=client,
        store=store or InMemoryReviewStore(),
        clock=clock,
        sleep=lambda _s: None,
    )


def test_approve_decision():
    review = _review()
    client = FakeTelegramClient([[_callback_update(review.id, "approved")]])
    store = InMemoryReviewStore()
    decision = _gate(client, store).request(review)

    assert decision == ReviewDecision.APPROVED
    assert store.get(review.id).decision == ReviewDecision.APPROVED
    assert client.edited and client.edited[0][0] == client.next_message_id
    assert client.answered == ["cbq1"]


def test_reject_decision():
    review = _review()
    client = FakeTelegramClient([[_callback_update(review.id, "rejected")]])
    assert _gate(client).request(review) == ReviewDecision.REJECTED


def test_revise_decision():
    review = _review()
    client = FakeTelegramClient([[_callback_update(review.id, "revise")]])
    assert _gate(client).request(review) == ReviewDecision.REVISE


def test_ignores_unrelated_callback_then_approves():
    review = _review()
    other = _callback_update("deadbeef", "approved", update_id=1)
    mine = _callback_update(review.id, "approved", update_id=2)
    client = FakeTelegramClient([[other], [mine]])
    assert _gate(client).request(review) == ReviewDecision.APPROVED


def test_timeout_returns_rejected():
    review = _review()
    client = FakeTelegramClient([[]])  # 콜백 영원히 없음
    # 첫 호출 0초 → 이후 큰 값으로 점프해 타임아웃 발생.
    ticks = iter([0.0, 9999.0, 9999.0])
    store = InMemoryReviewStore()
    gate = _gate(client, store, clock=lambda: next(ticks))
    decision = gate.request(review)

    assert decision == ReviewDecision.REJECTED
    saved = store.get(review.id)
    assert saved.decision == ReviewDecision.REJECTED
    assert saved.note == "timeout"


def test_dry_run_auto_approves_without_client():
    review = _review()
    gate = TelegramGate(Settings(NUTTI_DRY_RUN=True))
    assert gate.request(review) == ReviewDecision.APPROVED


def test_json_store_roundtrip(tmp_path):
    path = tmp_path / "reviews.json"
    review = _review()
    store = JsonFileReviewStore(path)
    store.save(review)
    store.update_decision(review.id, ReviewDecision.APPROVED, note="ok")

    reloaded = JsonFileReviewStore(path)
    got = reloaded.get(review.id)
    assert got is not None
    assert got.decision == ReviewDecision.APPROVED
    assert got.note == "ok"
