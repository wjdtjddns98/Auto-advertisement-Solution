"""dry_run 파이프라인 스모크 테스트.

외부 API 키 없이 전 단계가 끝까지 돌고, 검수 게이트가 동작하는지 검증한다.
"""

from __future__ import annotations

import pytest

from nutti.config import Settings
from nutti.integrations.ai_text import FactCheckResult
from nutti.models import ContentFormat, ReviewDecision, ReviewRequest, Stage
from nutti.pipeline.orchestrator import FactCheckFailed, GateRejected, Orchestrator
from nutti.review.gates import AutoApproveGate
from nutti.storage.state_store import PipelineState


def _dry_settings() -> Settings:
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test")


def _tmp_state(tmp_path) -> PipelineState:
    """테스트가 리포지토리의 data/를 건드리지 않도록 tmp 경로 상태를 만든다."""
    return PipelineState(str(tmp_path / "state.json"))


# 상태 경로 격리는 conftest의 전역 autouse(_isolate_state_path)가 담당한다 —
# 종전 로컬 격리는 이 파일만 지켜 다른 파일의 오케스트레이터 테스트가 실제
# data/pipeline_state.json을 오염시켰다(2026-07-10 실측, conftest로 승격).


def test_full_run_dry_run():
    orch = Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
    )
    run = orch.run("강아지 닭가슴살 간식 적정량")

    assert run.script is not None and run.script.body
    assert run.video is not None and run.video.final_url
    assert run.metadata is not None and run.metadata.title
    assert len(run.uploads) == 1  # shorts → youtube만
    assert run.uploads[0].platform == "youtube"
    assert run.current_stage == Stage.ANALYTICS


def test_reels_youtube_auto_and_instagram_manual_handoff(monkeypatch):
    """REELS: 유튜브만 자동 업로드, 인스타는 수동 핸드오프 호출(자동 업로드 폐기, 2026-06-18)."""
    orch = Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
    )
    handoff_calls: list[str] = []
    monkeypatch.setattr(
        orch, "_handoff_for_manual_instagram", lambda run: handoff_calls.append(run.id)
    )

    run = orch.run("강아지 수제간식", content_format=ContentFormat.REELS)

    platforms = {u.platform for u in run.uploads}
    assert platforms == {"youtube"}  # 인스타 자동 업로드 더 이상 안 함
    assert len(handoff_calls) == 1  # REELS는 인스타 수동 핸드오프 1회


def test_shorts_does_not_trigger_instagram_handoff(monkeypatch):
    """SHORTS: 인스타 핸드오프를 호출하지 않는다(유튜브 전용)."""
    orch = Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
    )
    handoff_calls: list[str] = []
    monkeypatch.setattr(
        orch, "_handoff_for_manual_instagram", lambda run: handoff_calls.append(run.id)
    )

    orch.run("강아지 간식", content_format=ContentFormat.SHORTS)

    assert handoff_calls == []


class _FakeTelegramClient:
    """TelegramClient 대체 — send_video/send_message 호출을 기록한다."""

    def __init__(self):
        self.video_calls: list[tuple] = []
        self.message_calls: list[tuple] = []

    def send_video(self, chat_id, video_path, caption="", reply_markup=None):
        self.video_calls.append((chat_id, video_path, caption))
        return 1

    def send_message(self, chat_id, text):
        self.message_calls.append((chat_id, text))
        return 2


def _live_handoff_settings() -> Settings:
    """핸드오프가 실제 전송을 타도록 dry_run=False + 텔레그램 설정."""
    return Settings(
        NUTTI_DRY_RUN=False,
        NUTTI_ENV="test",
        TELEGRAM_BOT_TOKEN="bot_tok",
        TELEGRAM_CHAT_ID="chat_123",
    )


def test_manual_handoff_sends_video_and_caption_to_telegram():
    """라이브: 최종 영상 + 캡션(메타 설명)을 텔레그램으로 보낸다."""
    fake_tg = _FakeTelegramClient()
    orch = Orchestrator(_live_handoff_settings(), tg_client=fake_tg)
    from nutti.models import Metadata, PipelineRun, VideoAsset

    run = PipelineRun(topic="t")
    run.video = VideoAsset(script_id="s1", video_path="data/media/final.mp4")
    run.metadata = Metadata(title="제목", description="설명 본문\n\n#강아지 #간식", hashtags=["#강아지"])

    orch._handoff_for_manual_instagram(run)

    assert len(fake_tg.video_calls) == 1
    chat_id, video_path, _caption = fake_tg.video_calls[0]
    assert chat_id == "chat_123"
    assert video_path == "data/media/final.mp4"
    # 붙여넣을 캡션은 메타데이터 설명 그대로 별도 메시지로 전송
    assert fake_tg.message_calls == [("chat_123", "설명 본문\n\n#강아지 #간식")]


def test_manual_handoff_skipped_in_dry_run():
    """dry_run이면 네트워크 없이 아무것도 전송하지 않는다(dry_run 계약)."""
    fake_tg = _FakeTelegramClient()
    orch = Orchestrator(_dry_settings(), tg_client=fake_tg)
    from nutti.models import Metadata, PipelineRun, VideoAsset

    run = PipelineRun(topic="t")
    run.video = VideoAsset(script_id="s1", video_path="x.mp4")
    run.metadata = Metadata(title="t", description="d", hashtags=[])

    orch._handoff_for_manual_instagram(run)

    assert fake_tg.video_calls == []
    assert fake_tg.message_calls == []


def test_manual_handoff_missing_chat_id_raises():
    """토큰은 있으나 chat_id가 없으면 설정 오류로 명확히 실패한다."""
    fake_tg = _FakeTelegramClient()
    settings = Settings(
        NUTTI_DRY_RUN=False, NUTTI_ENV="test", TELEGRAM_BOT_TOKEN="bot_tok", TELEGRAM_CHAT_ID=""
    )
    orch = Orchestrator(settings, tg_client=fake_tg)
    from nutti.models import Metadata, PipelineRun, VideoAsset

    run = PipelineRun(topic="t")
    run.video = VideoAsset(script_id="s1", video_path="x.mp4")
    run.metadata = Metadata(title="t", description="d", hashtags=[])

    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        orch._handoff_for_manual_instagram(run)
    assert fake_tg.video_calls == []


def test_manual_handoff_no_video_path_skips_quietly():
    """영상 경로(video_path·final_url)가 모두 없으면 경고만 남기고 전송하지 않는다."""
    fake_tg = _FakeTelegramClient()
    orch = Orchestrator(_live_handoff_settings(), tg_client=fake_tg)
    from nutti.models import Metadata, PipelineRun, VideoAsset

    run = PipelineRun(topic="t")
    run.video = VideoAsset(script_id="s1")  # video_path·final_url 모두 None
    run.metadata = Metadata(title="t", description="d", hashtags=[])

    orch._handoff_for_manual_instagram(run)  # 예외 없이 조용히 반환

    assert fake_tg.video_calls == []
    assert fake_tg.message_calls == []


def test_run_completes_recording_when_handoff_fails(monkeypatch):
    """REELS 핸드오프가 예외를 던져도 유튜브 사이클(원장·스토어·비용 기록)은 완주한다."""
    orch = Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
    )

    def _boom(_run):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(orch, "_handoff_for_manual_instagram", _boom)

    run = orch.run("강아지 수제간식", content_format=ContentFormat.REELS)

    # 핸드오프 실패에도 사이클은 완주: 유튜브 업로드 기록·비용 집계·analytics 단계 도달.
    assert {u.platform for u in run.uploads} == {"youtube"}
    assert run.cost is not None
    assert run.current_stage == Stage.ANALYTICS


def test_analysis_feedback_loop(tmp_path):
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    # run은 업로드를 대기 큐에 넣는다(즉시 분석하지 않음).
    orch.run("강아지 간식")
    assert state.get_pending_uploads()  # 업로드가 큐에 등록됨
    # dry_run은 숙성 지연이 없으니 collect가 즉시 수집·분석한다.
    analysis = orch.collect_ready_feedback()
    assert isinstance(analysis, str) and analysis
    # 피드백 루프: 분석 결과가 상태에 저장돼 다음 사이클로 자동 연결돼야 한다.
    assert state.get_feedback() == analysis
    # 수집을 마친 업로드는 큐에서 빠진다(재조회 방지).
    assert state.get_pending_uploads() == []


def test_collect_defers_until_upload_matures(tmp_path):
    """라이브 모드: 방금 올린 업로드는 숙성 전이라 수집하지 않는다(즉시 조회 시 0 방지)."""
    from datetime import datetime, timedelta, timezone

    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    orch.settings.dry_run = False  # collect만 라이브로 판정(미숙성이라 실제 조회는 안 됨)
    orch.settings.analytics_min_age_hours = 48

    now = datetime.now(timezone.utc)
    # 이제 막 올린 것(0h) → 미숙성, 이틀 하고도 한 시간 지난 것(49h) → 숙성.
    # dry_run=False: 라이브 업로드로 태깅 — 라이브 collect는 dry 항목을 정화하므로
    # (2026-07-10 모드 분리) 이 테스트의 대상은 라이브 항목이어야 한다.
    state.add_pending_upload("youtube", "vid_new", "u", now.isoformat(), dry_run=False)
    state.add_pending_upload(
        "youtube", "vid_old", "u", (now - timedelta(hours=49)).isoformat(), dry_run=False
    )
    fetched: list[str] = []
    orch.publisher.fetch_performance = lambda up: fetched.append(up.external_id) or _report(up)
    orch.ai.analyze_performance = lambda reports: "분석"

    assert orch.collect_ready_feedback(now=now) == "분석"
    assert fetched == ["vid_old"]  # 숙성분만 조회
    remaining = state.get_pending_uploads()
    assert [u["external_id"] for u in remaining] == ["vid_new"]  # 미숙성분은 큐에 남음


def _report(up):
    from nutti.models import PerformanceReport

    return PerformanceReport(platform=up.platform, external_id=up.external_id, views=8)


def _live_collect_orch(tmp_path):
    """라이브 모드 collect 테스트용 (orch, state) — 숙성 임계 48h."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    orch.settings.dry_run = False
    orch.settings.analytics_min_age_hours = 48
    return orch, state


def test_collect_live_purges_dry_items_without_fetching(tmp_path):
    """라이브 run은 dry 항목(가짜 ID)을 조회 없이 정화한다(2026-07-10 Analytics 400 결함).

    dry_run 실행·테스트가 남긴 yt_<hex> 가짜 ID를 라이브 Analytics로 조회하면 HTTP
    400으로 run이 시작 즉시 죽었다 — 플래그 없는 레거시 항목도 dry로 간주해 정화한다.
    """
    from datetime import datetime, timedelta, timezone

    orch, state = _live_collect_orch(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=49)).isoformat()
    state.add_pending_upload("youtube", "yt_fake_legacy", "u", old)  # 플래그 없음 → dry 간주
    state.add_pending_upload("youtube", "yt_fake_tagged", "u", old, dry_run=True)
    state.add_pending_upload("youtube", "RealVideoId", "u", old, dry_run=False)
    fetched: list[str] = []
    orch.publisher.fetch_performance = lambda up: fetched.append(up.external_id) or _report(up)
    orch.ai.analyze_performance = lambda reports: "분석"

    assert orch.collect_ready_feedback(now=now) == "분석"
    assert fetched == ["RealVideoId"]  # 가짜 항목은 조회 자체가 발생하지 않는다
    assert state.get_pending_uploads() == []  # 정화 + 수집 완료


def test_collect_live_purge_only_persists_and_returns_empty(tmp_path):
    """라이브 run: 큐가 dry 항목뿐이면 조회 없이 정화만 영속화하고 빈 문자열을 돌려준다."""
    from datetime import datetime, timezone

    orch, state = _live_collect_orch(tmp_path)
    now = datetime.now(timezone.utc)
    state.add_pending_upload("youtube", "yt_fake1", "u", now.isoformat())

    def boom(up):
        raise AssertionError("가짜 항목인데 조회가 발생함")

    orch.publisher.fetch_performance = boom
    assert orch.collect_ready_feedback(now=now) == ""
    assert state.get_pending_uploads() == []  # 정화가 저장됨(다음 run에 재등장 금지)


def test_collect_dry_preserves_live_items(tmp_path):
    """dry run은 라이브 항목을 소모하지 않는다 — 가짜 0 지표로 진짜 성과 신호 소모 방지."""
    from datetime import datetime, timezone

    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    now = datetime.now(timezone.utc)
    state.add_pending_upload("youtube", "RealVideoId", "u", now.isoformat(), dry_run=False)
    fetched: list[str] = []
    orch.publisher.fetch_performance = lambda up: fetched.append(up.external_id) or _report(up)

    assert orch.collect_ready_feedback(now=now) == ""
    assert fetched == []
    assert [u["external_id"] for u in state.get_pending_uploads()] == ["RealVideoId"]


def test_collect_fetch_failure_drops_item_and_survives(tmp_path):
    """항목별 조회 실패(Analytics 400 등)는 그 항목만 제거하고 run을 죽이지 않는다."""
    from datetime import datetime, timedelta, timezone

    from nutti.integrations.publishing import PublishError

    orch, state = _live_collect_orch(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=49)).isoformat()
    state.add_pending_upload("youtube", "BadVideoId", "u", old, dry_run=False)
    state.add_pending_upload("youtube", "GoodVideoId", "u", old, dry_run=False)
    fetched: list[str] = []

    def fetch(up):
        if up.external_id == "BadVideoId":
            raise PublishError("YouTube Analytics 조회 HTTP 400")
        fetched.append(up.external_id)
        return _report(up)

    orch.publisher.fetch_performance = fetch
    orch.ai.analyze_performance = lambda reports: "분석"

    assert orch.collect_ready_feedback(now=now) == "분석"  # 예외 없이 완주
    assert fetched == ["GoodVideoId"]
    assert state.get_pending_uploads() == []  # 실패 항목도 제거(매 run 반복 오류 방지)


def test_collect_all_fetches_fail_returns_empty_without_crash(tmp_path):
    """숙성분 전부 조회 실패해도 예외 없이 빈 문자열 + 큐 정리(런 생존이 최우선)."""
    from datetime import datetime, timedelta, timezone

    from nutti.integrations.publishing import PublishError

    orch, state = _live_collect_orch(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=49)).isoformat()
    state.add_pending_upload("youtube", "BadVideoId", "u", old, dry_run=False)

    def fetch(up):
        raise PublishError("YouTube Analytics 조회 HTTP 400")

    orch.publisher.fetch_performance = fetch
    assert orch.collect_ready_feedback(now=now) == ""
    assert state.get_pending_uploads() == []


# --- 피드백 자동 연결 + 주제 자동 생성(resolve_inputs) ---

def test_resolve_inputs_auto_loads_saved_feedback(tmp_path):
    """feedback 미지정 시 직전 사이클이 저장한 분석을 자동으로 불러온다."""
    state = _tmp_state(tmp_path)
    state.save_feedback("Q&A 포맷 지속률 우수 → 비중 확대")
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    topic, feedback = orch.resolve_inputs("명시 주제", "")
    assert topic == "명시 주제"
    assert feedback == "Q&A 포맷 지속률 우수 → 비중 확대"


def test_resolve_inputs_explicit_feedback_wins(tmp_path):
    """명시한 feedback이 저장된 값보다 우선한다."""
    state = _tmp_state(tmp_path)
    state.save_feedback("저장된 피드백")
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    _, feedback = orch.resolve_inputs("주제", "명시 피드백")
    assert feedback == "명시 피드백"


def test_resolve_inputs_auto_generates_topic_when_omitted(tmp_path):
    """주제 미지정 시 자동 생성하고, 최근 주제에 기록한다."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    topic, _ = orch.resolve_inputs(None, "")
    assert topic  # 비어있지 않은 자동 생성 주제
    assert state.get_recent_topics()[0] == topic  # 최신 주제로 기록됨


def test_resolve_inputs_auto_topic_avoids_recent(tmp_path):
    """연속 자동 생성 시 직전 주제와 겹치지 않는다(중복 회피)."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    first, _ = orch.resolve_inputs(None, "")
    second, _ = orch.resolve_inputs(None, "")
    assert first != second


def test_feedback_loop_closes_end_to_end(tmp_path):
    """한 사이클의 분석이 다음 사이클 resolve_inputs의 feedback으로 자동 연결된다."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )
    orch.run("강아지 간식")
    analysis = orch.collect_ready_feedback()
    # 다음 사이클: feedback 인자 없이도 직전 분석이 자동 주입돼야 한다.
    _, next_feedback = orch.resolve_inputs(None, "")
    assert next_feedback == analysis


def test_collect_ready_feedback_persists_nonempty_skips_empty(tmp_path, monkeypatch):
    """비어있지 않은 분석은 저장하고, 빈 분석은 기존 피드백을 덮어쓰지 않는다."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), state=state
    )

    # 비어있지 않은 분석 → 저장됨(dry_run이라 큐의 업로드가 즉시 숙성 처리).
    orch.run("주제")
    monkeypatch.setattr(orch.ai, "analyze_performance", lambda reports: "실제 분석 결과")
    assert orch.collect_ready_feedback() == "실제 분석 결과"
    assert state.get_feedback() == "실제 분석 결과"

    # 빈 분석(예: 라이브 LLM 폴백 오류) → 직전 피드백 유지 + 숙성분을 큐에 남겨 재분석.
    orch.run("주제2")
    assert len(state.get_pending_uploads()) == 1  # 2번째 업로드가 큐에 있음
    monkeypatch.setattr(orch.ai, "analyze_performance", lambda reports: "")
    assert orch.collect_ready_feedback() == ""
    assert state.get_feedback() == "실제 분석 결과"
    # 분석만 실패했으니 이미 성공한 조회 결과를 버리지 않고 큐에 남긴다(다음 사이클 재분석).
    assert len(state.get_pending_uploads()) == 1


class _RejectGate:
    def request(self, review: ReviewRequest) -> ReviewDecision:
        return ReviewDecision.REJECTED


class _TrackingGate:
    """승인하되 어떤 단계에서 호출됐는지 기록하는 검수 게이트(라우팅 핀용)."""

    def __init__(self) -> None:
        self.stages: list[Stage] = []

    def request(self, review: ReviewRequest) -> ReviewDecision:
        self.stages.append(review.stage)
        return ReviewDecision.APPROVED


def test_metadata_review_goes_to_telegram():
    """텔레그램 원툴: 메타데이터 검수도 텔레그램 게이트로 간다."""
    tg = _TrackingGate()
    orch = Orchestrator(_dry_settings(), telegram=tg)

    orch.run("강아지 간식")

    # 대본·영상·메타데이터 3단계 모두 텔레그램 게이트로 라우팅된다.
    assert Stage.SCRIPT in tg.stages
    assert Stage.VIDEO in tg.stages
    assert tg.stages.count(Stage.METADATA) == 1


def test_gate_rejection_stops_pipeline():
    orch = Orchestrator(_dry_settings(), telegram=_RejectGate())
    try:
        orch.run("부적절한 주제")
        assert False, "검수 거절 시 GateRejected가 발생해야 한다"
    except GateRejected as exc:
        assert exc.stage == Stage.SCRIPT
        assert exc.decision == ReviewDecision.REJECTED


# --- 팩트체크 배선(#1): 오케스트레이터가 fact_check_script를 실제로 호출하는지 ---

def _approving_orch(max_retries: int = 1) -> Orchestrator:
    return Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
        max_factcheck_retries=max_retries,
    )


def _log_counter(store, monkeypatch):
    """store.log_script 호출 횟수를 세는 카운터를 설치하고 반환한다."""
    n = {"count": 0}

    def _logged(_script):
        n["count"] += 1

    monkeypatch.setattr(store, "log_script", _logged)
    return n


def test_factcheck_is_wired_and_passes(monkeypatch):
    orch = _approving_orch()
    calls = {"n": 0}

    def passing(_script):
        calls["n"] += 1
        return FactCheckResult(passed=True, issues=[])

    monkeypatch.setattr(orch.ai, "fact_check_script", passing)
    logged = _log_counter(orch.store, monkeypatch)
    run = orch.run("안전한 주제")
    assert calls["n"] == 1  # 호출됨(데드코드 아님)
    assert run.script.fact_checked is True
    assert logged["count"] == 1  # 통과 시 대본 기록됨
    assert run.uploads  # 정상 진행


def test_factcheck_regenerates_with_issue_feedback_then_rejects(monkeypatch):
    orch = _approving_orch(max_retries=1)
    fc_calls = {"n": 0}
    feedbacks: list[str] = []

    def always_fail(_script):
        fc_calls["n"] += 1
        return FactCheckResult(passed=False, issues=["근거 없는 효능 주장"])

    real_gen = orch.ai.generate_script

    def capturing_gen(topic, feedback=""):
        feedbacks.append(feedback)
        return real_gen(topic, feedback=feedback)

    monkeypatch.setattr(orch.ai, "fact_check_script", always_fail)
    monkeypatch.setattr(orch.ai, "generate_script", capturing_gen)
    logged = _log_counter(orch.store, monkeypatch)

    with pytest.raises(FactCheckFailed) as exc:
        orch.run("위험한 주제")
    assert exc.value.issues == ["근거 없는 효능 주장"]
    assert fc_calls["n"] == 2  # 최초 + 재생성 1회
    assert len(feedbacks) == 2  # 재생성으로 대본 다시 생성됨
    # 재생성 피드백에 팩트체크 지적이 실제로 담겨야 한다(빈 호출 방지).
    assert "근거 없는 효능 주장" in feedbacks[1]
    assert logged["count"] == 0  # 거절된 대본은 기록 안 됨


def test_factcheck_zero_retries_fails_immediately(monkeypatch):
    orch = _approving_orch(max_retries=0)
    fc_calls = {"n": 0}

    def always_fail(_script):
        fc_calls["n"] += 1
        return FactCheckResult(passed=False, issues=["문제"])

    monkeypatch.setattr(orch.ai, "fact_check_script", always_fail)
    with pytest.raises(FactCheckFailed):
        orch.run("주제")
    assert fc_calls["n"] == 1  # 재생성 없이 최초 1회 후 즉시 거절


def test_factcheck_passes_after_one_retry(monkeypatch):
    orch = _approving_orch(max_retries=2)
    results = iter([
        FactCheckResult(passed=False, issues=["수정 필요"]),
        FactCheckResult(passed=True, issues=[]),
    ])
    monkeypatch.setattr(orch.ai, "fact_check_script", lambda _s: next(results))
    run = orch.run("주제")
    assert run.script.fact_checked is True
    assert run.uploads  # 재생성 후 통과 → 정상 진행
