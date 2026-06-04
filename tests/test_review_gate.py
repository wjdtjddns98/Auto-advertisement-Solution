"""TelegramGate 단위 테스트.

네트워크 없이 FakeTelegramClient + InMemoryReviewStore + 가짜 clock/sleep을 주입해
콜백→결정 매핑, 타임아웃, dry_run 자동승인, JSON 영속을 검증한다.
"""

from __future__ import annotations

import json

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
        self.offsets: list = []  # get_updates에 전달된 offset 기록(전진 검증용)
        self.next_message_id = 4242

    def send_review(self, chat_id: str, review: ReviewRequest) -> int:
        self.sent.append(review)
        return self.next_message_id

    def get_updates(self, offset=None, timeout: int = 0) -> list[dict]:
        self.offsets.append(offset)
        if self._batches:
            return self._batches.pop(0)
        return []

    def answer_callback(self, callback_query_id: str) -> None:
        self.answered.append(callback_query_id)

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self.edited.append((message_id, text))


class FlakyTelegramClient(FakeTelegramClient):
    """첫 N회 get_updates가 예외를 던지는 클라이언트(폴링 복원력 검증용)."""

    def __init__(self, update_batches, fail_times: int = 1):
        super().__init__(update_batches)
        self._fail = fail_times

    def get_updates(self, offset=None, timeout: int = 0) -> list[dict]:
        self.offsets.append(offset)
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("일시적 네트워크 오류")
        if self._batches:
            return self._batches.pop(0)
        return []


def _live_settings() -> Settings:
    # dry_run=False + 토큰 존재 → 실제 폴링 경로를 타게 한다(클라이언트는 주입).
    return Settings(
        NUTTI_DRY_RUN=False,
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="123",
        NUTTI_REVIEW_TIMEOUT_SEC=10,
        NUTTI_REVIEW_POLL_INTERVAL_SEC=0,
    )


def _callback_update(
    review_id: str, value: str, update_id: int = 1, chat_id: str = "123"
) -> dict:
    # chat_id 기본값은 _live_settings()의 TELEGRAM_CHAT_ID와 일치(인가됨).
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cbq1",
            "data": f"nutti:{review_id}:{value}",
            "message": {"chat": {"id": int(chat_id)}},
            "from": {"id": int(chat_id)},
        },
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


# --- #11: 알 수 없는 콜백 값은 보수적으로 거절 ---

def test_unknown_callback_value_rejected():
    review = _review()
    client = FakeTelegramClient([[_callback_update(review.id, "garbage")]])
    assert _gate(client).request(review) == ReviewDecision.REJECTED


# --- #2: 설정된 검수 채팅이 아닌 콜백은 무시(아무나 승인 차단) ---

def test_unauthorized_callback_is_ignored():
    review = _review()
    bad = _callback_update(review.id, "approved", chat_id="999")  # 다른 채팅
    client = FakeTelegramClient([[bad]])
    ticks = iter([0.0, 0.0, 9999.0])  # 1회 처리 후 타임아웃
    decision = _gate(client, clock=lambda: next(ticks)).request(review)

    assert decision == ReviewDecision.REJECTED  # 인가 안 됨 → 무시 → 타임아웃
    assert client.answered == ["cbq1"]  # 스피너는 제거(answer_callback 호출됨)


# --- #3: 폴링 중 일시적 예외가 run을 죽이지 않음 ---

def test_poll_error_is_resilient():
    review = _review()
    client = FlakyTelegramClient([[_callback_update(review.id, "approved")]], fail_times=1)
    assert _gate(client).request(review) == ReviewDecision.APPROVED


# --- #8: offset이 전진해 처리한 업데이트를 재수신하지 않음 ---

def test_offset_advances_to_avoid_redelivery():
    review = _review()
    other = _callback_update("deadbeef", "approved", update_id=5)
    mine = _callback_update(review.id, "approved", update_id=6)
    client = FakeTelegramClient([[other], [mine]])
    _gate(client).request(review)

    assert client.offsets[0] is None       # 첫 폴은 offset 없음
    assert client.offsets[1] == 6          # update_id=5 처리 후 6으로 전진


# --- #6: JsonFileReviewStore 손상복원 + 원자적 쓰기 ---

def test_json_store_corrupt_file_does_not_raise(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text("not json {{{", encoding="utf-8")
    store = JsonFileReviewStore(path)  # 손상 파일이어도 예외 없이 생성
    assert store.all() == []


def test_json_store_skips_bad_row(tmp_path):
    path = tmp_path / "reviews.json"
    good = _review()
    payload = {good.id: good.model_dump(mode="json"), "bad": {"not": "valid"}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = JsonFileReviewStore(path)
    assert store.get(good.id) is not None  # 정상 행은 로드
    assert store.get("bad") is None        # 깨진 행은 스킵


def test_json_store_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "reviews.json"
    store = JsonFileReviewStore(path)
    store.save(_review())
    assert path.exists()
    assert not (tmp_path / "reviews.json.tmp").exists()  # 임시파일 잔존 없음
