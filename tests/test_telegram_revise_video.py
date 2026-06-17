"""텔레그램 [수정] 버튼 → 텍스트 입력 대기, 영상 MP4 전송, 오케스트레이터 REVISE 처리 테스트.

네트워크 없이 FakeTelegramClient + 가짜 clock/sleep 주입으로 동작한다.
"""

from __future__ import annotations

import pytest

from nutti.config import Settings
from nutti.integrations.telegram import TelegramClient, TelegramError
from nutti.models import ReviewDecision, ReviewRequest, Script, Stage, VideoAsset
from nutti.pipeline.orchestrator import GateRejected, Orchestrator
from nutti.review.gates import TelegramGate
from nutti.storage.reviews import InMemoryReviewStore
from nutti.storage.sheets import SheetStore


# ---------------------------------------------------------------------------
# Fake 클라이언트
# ---------------------------------------------------------------------------


class FakeTelegramClient:
    """시나리오별 업데이트 큐를 반환하는 가짜 텔레그램 클라이언트."""

    def __init__(self, update_batches: list[list[dict]]):
        self._batches = list(update_batches)
        self.sent_reviews: list[ReviewRequest] = []
        self.sent_videos: list[tuple[str, str, str]] = []  # (chat_id, video_path, caption)
        self.sent_messages: list[tuple[str, str]] = []      # (chat_id, text)
        self.answered: list[str] = []
        self.edited: list[tuple[int, str]] = []
        self.next_message_id = 7777

    def send_review(self, chat_id: str, review: ReviewRequest) -> int:
        self.sent_reviews.append(review)
        return self.next_message_id

    def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str = "",
        reply_markup: dict | None = None,
    ) -> int:
        self.sent_videos.append((chat_id, video_path, caption))
        return self.next_message_id

    def send_message(self, chat_id: str, text: str) -> int:
        self.sent_messages.append((chat_id, text))
        return self.next_message_id + 1

    def get_updates(self, offset=None, timeout: int = 0) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def answer_callback(self, callback_query_id: str) -> None:
        self.answered.append(callback_query_id)

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self.edited.append((message_id, text))


def _live_settings() -> Settings:
    return Settings(
        NUTTI_DRY_RUN=False,
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="123",
        NUTTI_REVIEW_TIMEOUT_SEC=10,
        NUTTI_REVIEW_POLL_INTERVAL_SEC=0,
    )


def _dry_settings() -> Settings:
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test")


def _callback_update(
    review_id: str, value: str, update_id: int = 1, chat_id: str = "123"
) -> dict:
    """인라인 버튼 콜백 업데이트 픽스처."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cbq1",
            "data": f"nutti:{review_id}:{value}",
            "message": {"chat": {"id": int(chat_id)}},
        },
    }


def _text_update(text: str, update_id: int = 2, chat_id: str = "123") -> dict:
    """일반 텍스트 메시지 업데이트 픽스처."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 100,
            "text": text,
            "chat": {"id": int(chat_id)},
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


# ---------------------------------------------------------------------------
# TelegramGate REVISE + 텍스트 입력 대기 테스트
# ---------------------------------------------------------------------------


def test_revise_then_text_input_sets_revised_content():
    """[수정] 버튼 탭 후 텍스트 입력하면 review.revised_content에 저장된다."""
    review = _review()
    revised_text = "수정된 대본 내용입니다."
    batches = [
        [_callback_update(review.id, "revise", update_id=1)],  # 콜백
        [_text_update(revised_text, update_id=2)],              # 텍스트 입력
    ]
    client = FakeTelegramClient(batches)
    decision = _gate(client).request(review)

    assert decision == ReviewDecision.REVISE
    assert review.revised_content == revised_text
    # 수정 안내 메시지가 전송됐는지 확인.
    assert any("수정할 대본" in msg for _, msg in client.sent_messages)


def test_revise_sends_prompt_message():
    """REVISE 결정 시 수정 안내 메시지가 send_message로 전송된다."""
    review = _review()
    batches = [
        [_callback_update(review.id, "revise", update_id=1)],
        [_text_update("새 대본", update_id=2)],
    ]
    client = FakeTelegramClient(batches)
    _gate(client).request(review)

    assert len(client.sent_messages) >= 1
    _, text = client.sent_messages[0]
    assert "수정" in text


def test_revise_timeout_no_text_revised_content_none():
    """REVISE 후 텍스트 입력 없이 타임아웃 → revised_content는 None 유지."""
    review = _review()
    batches = [
        [_callback_update(review.id, "revise", update_id=1)],
        [],  # 텍스트 없음 → 타임아웃
    ]
    client = FakeTelegramClient(batches)
    # 두 번째 폴링(텍스트 대기) 시 타임아웃 발생하도록 clock 제어.
    call_count = {"n": 0}

    def ticking_clock():
        call_count["n"] += 1
        # 첫 번째 while(콜백 폴링)은 시간 여유, 두 번째 while(텍스트 대기)에서 타임아웃.
        return 0.0 if call_count["n"] <= 4 else 9999.0

    gate = TelegramGate(
        _live_settings(),
        client=client,
        store=InMemoryReviewStore(),
        clock=ticking_clock,
        sleep=lambda _s: None,
    )
    decision = gate.request(review)

    assert decision == ReviewDecision.REVISE
    assert review.revised_content is None


def test_revise_ignores_unauthorized_text_then_accepts_authorized():
    """텍스트 대기 중 다른 채팅의 메시지는 무시하고 인가된 채팅만 수락한다."""
    review = _review()
    batches = [
        [_callback_update(review.id, "revise", update_id=1)],
        [_text_update("해커의 대본", update_id=2, chat_id="999")],  # 미인가 채팅
        [_text_update("정상 수정 내용", update_id=3, chat_id="123")],  # 인가 채팅
    ]
    client = FakeTelegramClient(batches)
    decision = _gate(client).request(review)

    assert decision == ReviewDecision.REVISE
    assert review.revised_content == "정상 수정 내용"


# ---------------------------------------------------------------------------
# send_video 메서드 테스트 (TelegramClient 레벨)
# ---------------------------------------------------------------------------


class _OkMultipartHttp:
    """multipart POST를 받아 성공 응답을 반환하는 가짜 httpx 클라이언트."""

    def __init__(self, message_id: int = 1234):
        self.calls: list[dict] = []
        self._message_id = message_id

    def post(self, url, data=None, files=None, json=None):
        self.calls.append({"url": url, "data": data, "files": files})
        return _FakeResp({"ok": True, "result": {"message_id": self._message_id}})


class _FakeResp:
    def __init__(self, json_data: dict):
        self._json = json_data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._json


def test_send_video_calls_send_video_api(tmp_path):
    """send_video가 sendVideo API를 multipart로 호출하고 message_id를 반환한다."""
    video_file = tmp_path / "test.mp4"
    video_file.write_bytes(b"\x00" * 16)  # 더미 MP4 바이트

    http = _OkMultipartHttp(message_id=5555)
    client = TelegramClient("tok", http=http)
    result = client.send_video(
        "123",
        str(video_file),
        caption="영상 검수",
        reply_markup={"inline_keyboard": []},
    )

    assert result == 5555
    assert len(http.calls) == 1
    assert "sendVideo" in http.calls[0]["url"]
    # files 키에 video가 포함됐는지 확인.
    assert "video" in http.calls[0]["files"]


def test_send_video_missing_file_raises_telegram_error(tmp_path):
    """존재하지 않는 파일 경로를 넘기면 TelegramError가 발생한다."""
    http = _OkMultipartHttp()
    client = TelegramClient("tok", http=http)
    with pytest.raises(TelegramError, match="영상 파일 읽기 실패"):
        client.send_video("123", str(tmp_path / "nonexistent.mp4"))


def test_send_video_ok_false_raises_telegram_error(tmp_path):
    """sendVideo API가 ok:false를 반환하면 TelegramError가 발생한다."""
    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"\x00" * 8)

    class _OkFalseResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": False, "description": "wrong file type"}

    class _OkFalseHttp:
        def post(self, url, data=None, files=None, json=None):
            return _OkFalseResp()

    client = TelegramClient("tok", http=_OkFalseHttp())
    with pytest.raises(TelegramError):
        client.send_video("123", str(video_file))


def test_send_message_calls_send_message_api():
    """send_message가 sendMessage API를 호출하고 message_id를 반환한다."""

    class _OkJsonHttp:
        def __init__(self):
            self.calls: list[dict] = []

        def post(self, url, json=None, data=None, files=None):
            self.calls.append({"url": url, "json": json})
            return _FakeResp({"ok": True, "result": {"message_id": 9999}})

    http = _OkJsonHttp()
    client = TelegramClient("tok", http=http)
    result = client.send_message("123", "안내 메시지")

    assert result == 9999
    assert len(http.calls) == 1
    assert "sendMessage" in http.calls[0]["url"]


# ---------------------------------------------------------------------------
# TelegramGate: media_path 있을 때 send_video 호출 테스트
# ---------------------------------------------------------------------------


def test_gate_sends_video_when_media_path_set(tmp_path):
    """review.media_path가 있으면 send_review 대신 send_video를 호출한다."""
    video_file = tmp_path / "preview.mp4"
    video_file.write_bytes(b"\x00" * 8)

    review = ReviewRequest(
        stage=Stage.VIDEO,
        title="영상 품질 검수",
        preview="http://example.com/preview",
        media_path=str(video_file),
    )
    batches = [[_callback_update(review.id, "approved", update_id=1)]]
    client = FakeTelegramClient(batches)
    decision = _gate(client).request(review)

    assert decision == ReviewDecision.APPROVED
    # send_video가 호출됐고 send_review는 호출 안 됨.
    assert len(client.sent_videos) == 1
    assert len(client.sent_reviews) == 0
    sent_chat, sent_path, sent_caption = client.sent_videos[0]
    assert sent_path == str(video_file)
    assert "영상 품질 검수" in sent_caption


def test_gate_uses_send_review_when_no_media_path():
    """review.media_path가 없으면 기존 send_review를 사용한다."""
    review = _review()
    batches = [[_callback_update(review.id, "approved", update_id=1)]]
    client = FakeTelegramClient(batches)
    _gate(client).request(review)

    assert len(client.sent_reviews) == 1
    assert len(client.sent_videos) == 0


# ---------------------------------------------------------------------------
# dry_run 경로: REVISE 시 자동 승인(revised_content 없이)
# ---------------------------------------------------------------------------


def test_dry_run_revise_auto_approves():
    """dry_run 모드에서는 REVISE가 아닌 APPROVED를 반환한다(네트워크 없이)."""
    review = ReviewRequest(stage=Stage.SCRIPT, title="대본 검수", preview="미리보기")
    gate = TelegramGate(Settings(NUTTI_DRY_RUN=True))
    decision = gate.request(review)

    assert decision == ReviewDecision.APPROVED
    assert review.revised_content is None  # dry_run에서는 텍스트 입력 없음


# ---------------------------------------------------------------------------
# SheetStore: update_script, log_video 테스트
# ---------------------------------------------------------------------------


class _FakeWorksheet:
    def __init__(self):
        self.rows: list[list] = []

    def append_row(self, values):
        self.rows.append(values)


class _FakeSpreadsheet:
    def __init__(self, ws):
        self._ws = ws

    @property
    def sheet1(self):
        return self._ws


class _FakeGspreadClient:
    def __init__(self):
        self.worksheet = _FakeWorksheet()

    def open_by_key(self, key):
        return _FakeSpreadsheet(self.worksheet)


def _remote_settings() -> Settings:
    return Settings(
        NUTTI_DRY_RUN=False,
        GOOGLE_SHEETS_ID="sheet123",
        GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account"}',
        NUTTI_ENV="test",
    )


def test_update_script_dry_run_appends_memory():
    """update_script dry_run 경로: _memory에 script_update 행이 추가된다."""
    store = SheetStore(_dry_settings())
    script = Script(topic="강아지 간식", body="수정된 대본")
    store.update_script(script)

    rows = store.all_rows()
    assert len(rows) == 1
    assert rows[0]["type"] == "script_update"
    assert rows[0]["body"] == "수정된 대본"
    assert rows[0]["script_id"] == script.id


def test_update_script_remote_appends_row():
    """update_script 원격 경로: worksheet.append_row로 script_update 행이 기록된다."""
    fake = _FakeGspreadClient()
    store = SheetStore(_remote_settings(), client=fake)
    script = Script(topic="강아지 간식", body="수정된 대본")
    store.update_script(script)

    rows = fake.worksheet.rows
    assert len(rows) == 1
    assert rows[0][0] == "script_update"
    assert rows[0][1] == script.id
    assert rows[0][3] == "수정된 대본"


def test_log_video_dry_run_appends_memory():
    """log_video dry_run 경로: _memory에 video 행이 추가된다."""
    store = SheetStore(_dry_settings())
    video = VideoAsset(script_id="abc123", video_path="/tmp/v.mp4", final_url="file:///tmp/v.mp4")
    store.log_video(video)

    rows = store.all_rows()
    assert len(rows) == 1
    assert rows[0]["type"] == "video"
    assert rows[0]["video_path"] == "/tmp/v.mp4"
    assert rows[0]["script_id"] == "abc123"


def test_log_video_remote_appends_row():
    """log_video 원격 경로: worksheet.append_row로 video 행이 기록된다."""
    fake = _FakeGspreadClient()
    store = SheetStore(_remote_settings(), client=fake)
    video = VideoAsset(
        script_id="abc123",
        video_path="/data/media/v.mp4",
        final_url="file:///data/media/v.mp4",
        duration_sec=8.0,
    )
    store.log_video(video)

    rows = fake.worksheet.rows
    assert len(rows) == 1
    assert rows[0][0] == "video"
    assert rows[0][1] == "abc123"
    assert rows[0][2] == "/data/media/v.mp4"
    assert rows[0][4] == 8.0


# ---------------------------------------------------------------------------
# 오케스트레이터: SCRIPT 단계 REVISE 처리 테스트
# ---------------------------------------------------------------------------


class _ReviseGate:
    """SCRIPT 단계에서 REVISE + revised_content를 주입하고, 그 외 단계는 APPROVED를 반환한다."""

    def __init__(self, revised_text: str):
        self._revised = revised_text
        self._call_count = 0

    def request(self, review: ReviewRequest) -> ReviewDecision:
        self._call_count += 1
        if review.stage == Stage.SCRIPT:
            review.revised_content = self._revised
            return ReviewDecision.REVISE
        return ReviewDecision.APPROVED


class _ReviseVideoGate:
    """VIDEO 단계에서 REVISE를 반환하는 가짜 게이트(GateRejected 확인용)."""

    def request(self, review: ReviewRequest) -> ReviewDecision:
        return ReviewDecision.REVISE


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTTI_STATE_PATH", str(tmp_path / "state.json"))


def test_orchestrator_revise_updates_script_body(tmp_path, monkeypatch):
    """SCRIPT 단계 REVISE + revised_content → script.body가 수정 내용으로 교체된다."""
    from nutti.review.gates import AutoApproveGate

    revised_text = "수정된 최종 대본입니다."
    script_gate = _ReviseGate(revised_text)

    orch = Orchestrator(
        _dry_settings(),
        telegram=script_gate,
        discord=AutoApproveGate(),
    )

    # update_script 호출 캡처.
    updated_scripts: list[Script] = []
    monkeypatch.setattr(orch.store, "update_script", lambda s: updated_scripts.append(s))

    run = orch.run("강아지 간식")

    # script.body가 수정 내용으로 교체됐는지 확인.
    assert run.script is not None
    assert run.script.body == revised_text
    # update_script가 호출됐는지 확인.
    assert len(updated_scripts) == 1
    assert updated_scripts[0].body == revised_text


def test_orchestrator_revise_without_revised_content_raises(monkeypatch):
    """REVISE인데 revised_content가 None(텍스트 미입력)이면 GateRejected를 던진다."""
    from nutti.review.gates import AutoApproveGate

    class _ReviseNoContent:
        def request(self, review: ReviewRequest) -> ReviewDecision:
            # revised_content를 설정하지 않음(텍스트 입력 없는 시나리오).
            return ReviewDecision.REVISE

    orch = Orchestrator(
        _dry_settings(),
        telegram=_ReviseNoContent(),
        discord=AutoApproveGate(),
    )

    with pytest.raises(GateRejected) as exc:
        orch.run("강아지 간식")
    assert exc.value.stage == Stage.SCRIPT
    assert exc.value.decision == ReviewDecision.REVISE


def test_orchestrator_video_revise_raises_gate_rejected(monkeypatch):
    """VIDEO 단계 REVISE는 GateRejected(Stage.VIDEO, REVISE)를 발생시킨다."""
    from nutti.review.gates import AutoApproveGate

    orch = Orchestrator(
        _dry_settings(),
        telegram=_ReviseVideoGate(),
        discord=AutoApproveGate(),
    )
    # SCRIPT 단계는 통과하도록 패치.
    def _stage_aware(review: ReviewRequest) -> ReviewDecision:
        if review.stage == Stage.SCRIPT:
            return ReviewDecision.APPROVED
        return ReviewDecision.REVISE

    orch.telegram.request = _stage_aware  # type: ignore[method-assign]  # noqa: E501

    with pytest.raises(GateRejected) as exc:
        orch.run("강아지 간식")
    assert exc.value.stage == Stage.VIDEO
    assert exc.value.decision == ReviewDecision.REVISE
