"""파이프라인 오케스트레이터.

대본 → (검수①) → 영상 → (검수②) → 메타데이터 → (검수③) → 업로드 → 분석.
검수에서 거절/수정이 나오면 해당 단계에서 중단하고 상태를 반환한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nutti.config import Settings, get_settings
from nutti.integrations.ai_text import AITextClient
from nutti.integrations.publishing import Publisher
from nutti.integrations.telegram import TelegramClient
from nutti.integrations.video import VideoStudio, pick_episode_style
from nutti.logging import get_logger
from nutti.models import (
    ContentFormat,
    PipelineRun,
    ReviewDecision,
    ReviewRequest,
    Script,
    Stage,
    UploadResult,
)
from nutti.pipeline.cost import estimate_run_cost
from nutti.pipeline.cost_ledger import CostLedger
from nutti.review.gates import ReviewGate, TelegramGate
from nutti.storage.sheets import SheetStore
from nutti.storage.state_store import PipelineState

log = get_logger(__name__)

# 한국어 발화 기준 8초 ≈ 44~50자(이 정도로 채워야 발화 뒤 빈 시간에 영상이 헛돌지 않는다).
# 50자를 넘기면 8초 안에 다 못 말하고 잘릴 수 있어 경고한다.
_BEAT_CHARS_WARN = 50


def _beats_preview(script: Script) -> str:
    """대본을 영상 클립(비트) 단위로 나눠 보여준다 — 어디서 잘리는지 + 8초 초과 경고.

    각 비트가 8초 클립 하나가 되므로, PO가 클립 경계와 길이를 미리 확인하고 필요하면
    텔레그램에서 그 자리에서 수정할 수 있다(REVISE).
    """
    beats = script.beats or [script.body]
    lines = [f"[대본 검수 — {len(beats)}개 클립 · 약 {8 * len(beats)}초]"]
    for i, beat in enumerate(beats, start=1):
        warn = "  ⚠️8초보다 길 수 있음(줄여주세요)" if len(beat) > _BEAT_CHARS_WARN else ""
        lines.append(f"{i}. 8초: {beat}{warn}")
    return "\n".join(lines)


class GateRejected(Exception):
    """검수에서 승인되지 않아 파이프라인을 중단할 때 발생."""

    def __init__(self, stage: Stage, decision: ReviewDecision):
        self.stage = stage
        self.decision = decision
        super().__init__(f"{stage.value} 단계 검수 결과: {decision.value}")


class FactCheckFailed(Exception):
    """팩트체크가 재생성 한도 내에 통과하지 못해 파이프라인을 중단할 때 발생."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("팩트체크 실패: " + "; ".join(issues) if issues else "팩트체크 실패")


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        telegram: ReviewGate | None = None,
        max_factcheck_retries: int = 1,
        state: PipelineState | None = None,
        ledger: CostLedger | None = None,
        tg_client: TelegramClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.ai = AITextClient(self.settings)
        self.studio = VideoStudio(self.settings)
        self.publisher = Publisher(self.settings)
        # 인스타 수동 업로드 핸드오프용 텔레그램 클라이언트(테스트 시 fake 주입 가능).
        self._tg_client = tg_client
        self.store = SheetStore(self.settings)
        # 실행 간 영속 상태(직전 피드백·최근 주제). 테스트 시 tmp 경로 주입 가능.
        self.state = state or PipelineState(self.settings.state_path)
        # 제작 비용 누적 원장(일/월 합산용). 테스트 시 tmp 경로 주입 가능.
        self.ledger = ledger or CostLedger(self.settings.cost_ledger_path)
        # 팩트체크 실패 시 issues를 피드백으로 대본을 재생성하는 최대 횟수.
        self.max_factcheck_retries = max_factcheck_retries
        # 검수 게이트 주입 가능(테스트 시 AutoApproveGate). 텔레그램 원툴(2026-06-18 PO):
        # 메타데이터 검수까지 전 단계 텔레그램으로 통일(Discord 게이트는 2026-07-17 제거).
        self.telegram: ReviewGate = telegram or TelegramGate(self.settings)

    def resolve_inputs(self, topic: str | None = None, feedback: str = "") -> tuple[str, str]:
        """실행 입력을 확정한다(피드백 자동 연결 + 주제 자동 생성).

        - feedback이 비어 있으면 직전 사이클에서 저장한 분석 결과를 자동으로 불러온다.
        - topic이 비어 있으면 (피드백·최근 주제를 반영해) 주제를 자동 생성한다.
        - 확정된 주제는 최근 주제 목록에 기록해 다음 자동 생성 시 중복을 피한다.
        """
        effective_feedback = feedback or self.state.get_feedback()
        # 직전 런의 검수 반려 사유를 맨 앞에 붙인다(2026-07-29 PO) — 주제 생성·대본
        # 생성이 같은 feedback 문자열을 공유하므로 한 번만 얹으면 둘 다 반영된다.
        # 성과 분석보다 우선순위가 높다는 걸 문안으로 명시한다(PO 직접 지시라서).
        reject_note = self.state.get_reject_note()
        if reject_note:
            effective_feedback = (
                f"[직전 편 검수 반려 사유 — 최우선 반영]\n{reject_note}\n\n"
                f"{effective_feedback}"
            ).strip()
        if topic and topic.strip():
            chosen = topic.strip()
        else:
            chosen = self.ai.suggest_topic(effective_feedback, self.state.get_recent_topics())
        self.state.add_topic(chosen)
        log.info("pipeline.inputs_resolved", topic=chosen, has_feedback=bool(effective_feedback))
        return chosen, effective_feedback

    def run(
        self,
        topic: str,
        *,
        content_format: ContentFormat = ContentFormat.SHORTS,
        feedback: str = "",
    ) -> PipelineRun:
        """한 편의 콘텐츠를 끝까지 처리한다."""
        run = PipelineRun(topic=topic, content_format=content_format)
        log.info("pipeline.start", run_id=run.id, topic=topic)

        # 1단계: 대본 (생성 → 팩트체크 → 검수①)
        # 주의: log_script는 팩트체크를 통과한 대본만 기록한다. 끝내 실패하면
        # _fact_check가 FactCheckFailed를 던져 로깅에 도달하지 않으며, 거절 사실은
        # factcheck.rejected 로그로 남는다.
        run.current_stage = Stage.SCRIPT
        # 편 포맷 확정: 주제 해시 + 직전 편 포맷 회피(연속 중복 방지, 2026-07-21 PO).
        # 회피 결과는 해시로 재현 불가라 state에 저장해 다음 편이 참조한다 — 단 저장은
        # 업로드 성공 후에만(리뷰 지적: 시작 시 저장하면 게이트 반려·팩트체크 실패로
        # 죽은 런의 포맷이 회피 기준을 오염시켜, 실제 게시 편끼리는 연속 중복이 재발한다.
        # last_format = "마지막으로 실제 게시된 편의 포맷"이 계약).
        from nutti.integrations.ai_text import pick_episode_format

        episode_format = pick_episode_format(topic, avoid=self.state.get_last_format())
        run.script = self.ai.generate_script(
            topic, feedback=feedback, episode_format=episode_format
        )
        self._fact_check(run, topic, feedback)
        self.store.log_script(run.script)
        # REVISE: 수정 내용이 있으면 script.body에 반영하고 비트를 재분할 후 시트를 업데이트한다.
        # 검수 카드는 비트(8초 클립)별로 보여줘 PO가 잘림 지점·길이를 미리 확인하게 한다.
        script_review = ReviewRequest(
            stage=Stage.SCRIPT, title="대본 검수(클립별)", preview=_beats_preview(run.script)
        )
        script_decision = self.telegram.request(script_review)
        # 반려 사유는 다음 런 대본·주제 프롬프트로 넘긴다(resolve_inputs가 읽는다).
        # 통과했으면 그 지시는 반영된 것으로 보고 지운다 — 안 지우면 낡은 지시가 계속 붙는다.
        if script_decision == ReviewDecision.REJECTED:
            self.state.save_reject_note(script_review.note)
        elif script_decision == ReviewDecision.APPROVED:
            self.state.clear_reject_note()
        if script_decision == ReviewDecision.REVISE and script_review.revised_content:
            # 사용자가 텔레그램에서 직접 입력한 수정본이므로 팩트체크를 재실행하지 않는다.
            # PO가 내용을 직접 확인·수정했다는 전제 하에 신뢰하는 설계다.
            run.script.body = script_review.revised_content
            run.script.beats = self.ai.split_beats(run.script.body, run.script.topic)  # 수정본 → 비트 재분할
            self.store.update_script(run.script)
        elif script_decision != ReviewDecision.APPROVED:
            log.warning(
                "pipeline.gate_blocked", stage=Stage.SCRIPT.value, decision=script_decision.value
            )
            raise GateRejected(Stage.SCRIPT, script_decision)

        # 2단계: 영상 (REVISE 시 대본 수정 → 영상 재생성 루프)
        run.current_stage = Stage.VIDEO
        # 직전 게시 편의 시각 축 사용값(의상·장소·구도) — 연속 편 시각 중복 방지.
        # last_format과 같은 계약: 읽기는 여기, 저장은 업로드 성공 후에만.
        style_avoid = self.state.get_last_style()
        run.video = self.studio.produce(run.script, style_avoid=style_avoid)
        while True:
            video_review = ReviewRequest(
                stage=Stage.VIDEO,
                title="영상 품질 검수",
                preview=run.video.preview_url or "",
                media_path=run.video.video_path,
            )
            video_decision = self.telegram.request(video_review)
            if video_decision == ReviewDecision.APPROVED:
                break
            if video_decision == ReviewDecision.REVISE and video_review.revised_content:
                run.script.body = video_review.revised_content
                run.script.beats = self.ai.split_beats(run.script.body, run.script.topic)  # 수정본 → 비트 재분할
                self.store.update_script(run.script)
                run.video = self.studio.produce(run.script, style_avoid=style_avoid)
                continue
            # 영상 반려 사유도 다음 런 대본에 반영한다(연출 지적이 대본 톤과 얽혀 있다).
            if video_decision == ReviewDecision.REJECTED:
                self.state.save_reject_note(video_review.note)
            log.warning(
                "pipeline.gate_blocked", stage=Stage.VIDEO.value, decision=video_decision.value
            )
            raise GateRejected(Stage.VIDEO, video_decision)

        # 3단계: 메타데이터
        run.current_stage = Stage.METADATA
        run.metadata = self.ai.generate_metadata(run.script, self.settings.calculator_url)
        # 메타데이터 검수도 텔레그램(원툴).
        self._gate(self.telegram, Stage.METADATA, "메타데이터 검수", run.metadata.title)

        # 4단계: 업로드 — 유튜브는 자동 업로드. 인스타는 수동 업로드로 전환(2026-06-18 PO 결정):
        # 자동 게시 대신(관련 코드는 2026-07-17 제거) 최종 영상 + 캡션을 텔레그램으로
        # 보내 사람이 직접 올린다. REELS 포맷일 때만 인스타용 핸드오프를 수행한다.
        run.current_stage = Stage.UPLOAD
        run.uploads.append(self.publisher.upload_youtube(run.video, run.metadata))
        # 업로드 성공 = 편이 실제 게시됨 → 이제서야 last_format 갱신(위 포맷 확정 주석 참조).
        self.state.save_format(run.script.episode_format)
        # 시각 스타일도 같은 계약으로 저장 — pick_episode_style은 순수 함수라 produce가
        # 쓴 것과 동일한 값이 재계산된다(같은 script.id·fmt·avoid 입력).
        used_style = pick_episode_style(
            run.script.id, topic, fmt=run.script.episode_format or None, avoid=style_avoid
        )
        self.state.save_style(
            {
                "outfit": used_style.outfit,
                "setting": used_style.setting,
                "prop": used_style.prop,
                "shot": used_style.shot,
            }
        )
        if content_format == ContentFormat.REELS:
            # 인스타 핸드오프는 best-effort 부수효과 — 유튜브 업로드는 이미 성공했으므로,
            # 텔레그램 전송 실패(설정 오류·전송 오류)가 아래 비용·원장·스토어 기록을 막지
            # 않도록 예외를 삼키고 경고만 남긴다(유튜브 결과 기록 누락 방지).
            try:
                self._handoff_for_manual_instagram(run)
            except Exception as exc:
                log.warning("instagram.manual_handoff.failed", run_id=run.id, error=str(exc))

        # 비용 집계: 산출물(영상 길이·프레임·생성 텍스트) 기준으로 편당 제작 비용을 명세화.
        # 업로드까지 완주한 경우에만 도달한다 — 게이트 거절/팩트체크 실패 경로에서는
        # 예외로 빠져 run.cost는 None으로 남는다(CLI는 None 가드로 출력을 건너뛴다).
        run.cost = estimate_run_cost(run, self.settings)
        log.info("pipeline.cost", run_id=run.id, total_usd=run.cost.total_usd, dry_run=run.cost.dry_run)
        # 누적 원장에 기록 — `nutti cost`로 일/월/전체 실제 지출을 조회한다.
        self.ledger.record(run)

        # 5단계: 성과 수집 예약 — 이번 업로드를 대기 큐에 넣는다. 실제 조회·분석은
        # analytics_min_age_hours가 지난 뒤(다음 사이클의 collect_ready_feedback)에 한다.
        # 업로드 직후엔 YouTube Analytics가 아직 0이라 즉시 조회하면 루프가 오염된다.
        run.current_stage = Stage.ANALYTICS
        for up in run.uploads:
            self.state.add_pending_upload(
                up.platform,
                up.external_id,
                up.url,
                up.uploaded_at.isoformat(),
                dry_run=self.settings.dry_run,
            )
        self.store.log_run(run)
        log.info("pipeline.done", run_id=run.id, uploads=len(run.uploads))
        return run

    def _handoff_for_manual_instagram(self, run: PipelineRun) -> None:
        """인스타 수동 업로드 핸드오프: 최종 영상 + 캡션을 텔레그램으로 보낸다.

        인스타는 자동 게시 대신 사람이 직접 올린다(2026-06-18 PO 결정). 검수 끝난 최종
        영상 파일과 붙여넣을 캡션(메타데이터 설명=계산기 링크·해시태그 포함)을 검수 채팅으로
        보내 PO가 바로 받아 업로드하게 한다.

        dry_run이거나 텔레그램 토큰이 없으면 네트워크 없이 로그만 남기고 반환한다
        (dry_run 계약 유지). 토큰은 있으나 chat_id가 없으면 설정 오류로 명확히 실패한다.
        """
        if self.settings.dry_run or not self.settings.telegram_bot_token:
            log.info(
                "instagram.manual_handoff.skipped",
                run_id=run.id,
                dry_run=self.settings.dry_run,
            )
            return
        if not self.settings.telegram_chat_id:
            raise ValueError(
                "TELEGRAM_CHAT_ID가 비어 있습니다 — 인스타 수동 업로드 핸드오프를 보낼 수 없습니다."
            )
        video_path = (run.video.video_path or run.video.final_url) if run.video else None
        if not video_path:
            log.warning("instagram.manual_handoff.no_video", run_id=run.id)
            return
        caption = run.metadata.description if run.metadata else ""
        chat_id = self.settings.telegram_chat_id
        client = self._tg_client or TelegramClient(self.settings.telegram_bot_token)
        # 영상은 짧은 안내와 함께, 붙여넣을 캡션은 별도 텍스트로 보낸다(복사 편의 + 캡션 길이
        # 제한 회피 — sendVideo caption은 1024자, sendMessage는 4096자).
        client.send_video(
            chat_id,
            video_path,
            caption="[인스타 수동 업로드용 영상] 아래 캡션을 복사해 올려주세요.",
        )
        if caption:
            client.send_message(chat_id, caption)
        log.info("instagram.manual_handoff.sent", run_id=run.id)

    def _fact_check(self, run: PipelineRun, topic: str, feedback: str) -> None:
        """대본 팩트체크. 실패하면 issues를 피드백으로 재생성하고, 한도를 넘으면 거절한다.

        dry_run에서는 fact_check_script가 항상 통과를 반환하므로 재생성 루프는 돌지 않는다.
        """
        result = self.ai.fact_check_script(run.script)
        attempts = 0
        while not result.passed and attempts < self.max_factcheck_retries:
            attempts += 1
            log.warning("factcheck.retry", attempt=attempts, issues=result.issues)
            retry_feedback = feedback + "\n[팩트체크 지적 — 아래 문제를 반드시 수정]\n" + "\n".join(
                f"- {issue}" for issue in result.issues
            )
            # 재생성도 같은 편 포맷을 유지한다(회피 확정값 — run()에서 최초 결정).
            run.script = self.ai.generate_script(
                topic, feedback=retry_feedback, episode_format=run.script.episode_format
            )
            result = self.ai.fact_check_script(run.script)

        run.script.fact_checked = result.passed
        if not result.passed:
            log.error("factcheck.rejected", issues=result.issues)
            raise FactCheckFailed(result.issues)

    def collect_ready_feedback(self, *, now: datetime | None = None) -> str:
        """숙성된 업로드의 성과를 수집·분석해 다음 대본 개선 피드백으로 저장한다(피드백 루프).

        업로드 직후엔 YouTube Analytics가 조회수 0을 돌려주므로(집계 지연), 대기 큐에서
        analytics_min_age_hours가 지난 업로드만 꺼내 조회한다. dry_run은 지연이 없으니
        전부 즉시 수집한다. 숙성분은 조회 후 큐에서 제거하고(재조회 방지), 남은 미숙성분은
        다시 저장한다. 분석 결과가 비어 있으면 기존 피드백을 유지한다(save_feedback가 무시).

        모드 분리(2026-07-10): 항목의 dry_run 플래그가 현재 모드와 일치할 때만 조회한다.
        라이브 run은 dry 항목(가짜 ID — 조회 시 HTTP 400)을 큐에서 정화하고, dry run은
        라이브 항목을 보존한다(가짜 0 지표로 소모 방지). 항목별 조회 실패는 경고 후 그
        항목만 제거 — 어떤 경우에도 수집이 run 전체를 죽이지 않는다.

        반환: 이번에 도출·저장한 분석 문자열(수집 대상이 없으면 빈 문자열).
        """
        pending = self.state.get_pending_uploads()
        if not pending:
            return ""
        now = now or datetime.now(timezone.utc)
        min_age = 0.0 if self.settings.dry_run else float(self.settings.analytics_min_age_hours)
        ready: list[dict] = []
        remaining: list[dict] = []
        purged = 0
        for item in pending:
            # 모드 분리(2026-07-10 실측 결함): dry_run 항목의 external_id는 가짜
            # (yt_<script_id>)라 라이브 Analytics 조회 시 HTTP 400으로 run이 시작 즉시
            # 죽는다. 플래그 없는 레거시 항목은 dry로 간주(add_pending_upload 기본과
            # 동일) — 현 큐의 오염분이 첫 라이브 run에서 자동 정화된다.
            item_dry = bool(item.get("dry_run", True))
            if not self.settings.dry_run and item_dry:
                purged += 1  # 라이브 run: dry 항목은 조회 불가 쓰레기 — 큐에서 제거
                continue
            if self.settings.dry_run and not item_dry:
                remaining.append(item)  # dry run: 라이브 항목은 건드리지 않고 보존
                continue
            if self._upload_age_hours(item.get("uploaded_at"), now) >= min_age:
                ready.append(item)
            else:
                remaining.append(item)
        if purged:
            log.warning("pipeline.feedback.purged_dry_items", n=purged)
        if not ready:
            if purged:
                self.state.replace_pending_uploads(remaining)  # 정화 결과 영속화
            return ""
        # 항목별 조회 격리(2026-07-10): 조회 실패(불량 ID·일시 오류) 하나가 run 전체를
        # 죽이면 안 된다 — 피드백 루프는 best-effort. 실패 항목은 경고 후 큐에서 제거한다
        # (남기면 영구 불량 ID가 매 run 같은 오류를 반복).
        reports = []
        fetched: list[dict] = []
        for item in ready:
            try:
                reports.append(
                    self.publisher.fetch_performance(
                        UploadResult(
                            platform=item.get("platform", "youtube"),
                            external_id=item.get("external_id", ""),
                            url=item.get("url", ""),
                        )
                    )
                )
                fetched.append(item)
            except Exception as exc:
                # error 필드 포함(리뷰 medium): Analytics 400이 아닌 코드 버그(TypeError
                # 등)로 항목이 드롭될 때 원인 추적이 가능해야 한다(_handoff 관례와 동일).
                log.warning(
                    "pipeline.feedback.fetch_failed",
                    external_id=item.get("external_id", ""),
                    error=str(exc),
                )
        if not reports:
            self.state.replace_pending_uploads(remaining)
            return ""
        analysis = self.ai.analyze_performance(reports)
        if not (analysis and analysis.strip()):
            # 분석 실패(라이브 LLM 폴백 오류 등) — 조회 성공분(fetched)은 큐에 보존해
            # 다음 사이클에 재분석한다(이미 성공한 성과 신호 유실 방지, 리뷰 지적 medium).
            # LLM이 계속 실패하면 큐에 쌓이지만, 그건 피드백 루프 자체가 죽은 환경이라
            # 유실보다 낫다. 정화(purged)·조회 실패 제거는 이 경로에서도 영속화된다.
            log.info("pipeline.feedback.analysis_empty", n_ready=len(fetched))
            self.state.replace_pending_uploads(remaining + fetched)
            return ""
        # 분석 성공 — 숙성분을 큐에서 제거(재조회 방지)하고 피드백 저장.
        self.state.replace_pending_uploads(remaining)
        self.state.save_feedback(analysis)
        log.info(
            "pipeline.feedback.collected", n_ready=len(fetched), n_remaining=len(remaining)
        )
        return analysis

    @staticmethod
    def _upload_age_hours(uploaded_at: str | None, now: datetime) -> float:
        """업로드 ISO8601 시각과 now의 시간차(시간). 파싱 불가면 무한대(=항상 숙성 처리).

        타임스탬프가 깨졌거나 없는 항목을 영구히 큐에 남기지 않도록 '충분히 오래됨'으로
        본다 — 한 번 조회하고 큐에서 빼는 편이 무한 적재보다 안전하다.
        """
        if not uploaded_at:
            return float("inf")
        try:
            ts = datetime.fromisoformat(uploaded_at)
        except (ValueError, TypeError):
            return float("inf")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0

    def _gate(self, gate: ReviewGate, stage: Stage, title: str, preview: str) -> None:
        review = ReviewRequest(stage=stage, title=title, preview=preview)
        decision = gate.request(review)
        if decision is not ReviewDecision.APPROVED:
            log.warning("pipeline.gate_blocked", stage=stage.value, decision=decision.value)
            raise GateRejected(stage, decision)
