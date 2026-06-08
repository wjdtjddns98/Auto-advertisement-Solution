"""파이프라인 오케스트레이터.

대본 → (검수①) → 영상 → (검수②) → 메타데이터 → (검수③) → 업로드 → 분석.
검수에서 거절/수정이 나오면 해당 단계에서 중단하고 상태를 반환한다.
"""

from __future__ import annotations

from nutti.config import Settings, get_settings
from nutti.integrations.ai_text import AITextClient
from nutti.integrations.publishing import Publisher
from nutti.integrations.video import VideoStudio
from nutti.logging import get_logger
from nutti.models import (
    ContentFormat,
    PipelineRun,
    ReviewDecision,
    ReviewRequest,
    Stage,
)
from nutti.review.gates import DiscordGate, ReviewGate, TelegramGate
from nutti.storage.sheets import SheetStore
from nutti.storage.state_store import PipelineState

log = get_logger(__name__)


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
        discord: ReviewGate | None = None,
        max_factcheck_retries: int = 1,
        state: PipelineState | None = None,
    ):
        self.settings = settings or get_settings()
        self.ai = AITextClient(self.settings)
        self.studio = VideoStudio(self.settings)
        self.publisher = Publisher(self.settings)
        self.store = SheetStore(self.settings)
        # 실행 간 영속 상태(직전 피드백·최근 주제). 테스트 시 tmp 경로 주입 가능.
        self.state = state or PipelineState(self.settings.state_path)
        # 팩트체크 실패 시 issues를 피드백으로 대본을 재생성하는 최대 횟수.
        self.max_factcheck_retries = max_factcheck_retries
        # 검수 게이트 주입 가능(테스트 시 AutoApproveGate)
        self.telegram: ReviewGate = telegram or TelegramGate(self.settings)
        self.discord: ReviewGate = discord or DiscordGate(self.settings)

    def resolve_inputs(self, topic: str | None = None, feedback: str = "") -> tuple[str, str]:
        """실행 입력을 확정한다(피드백 자동 연결 + 주제 자동 생성).

        - feedback이 비어 있으면 직전 사이클에서 저장한 분석 결과를 자동으로 불러온다.
        - topic이 비어 있으면 (피드백·최근 주제를 반영해) 주제를 자동 생성한다.
        - 확정된 주제는 최근 주제 목록에 기록해 다음 자동 생성 시 중복을 피한다.
        """
        effective_feedback = feedback or self.state.get_feedback()
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
        run.script = self.ai.generate_script(topic, feedback=feedback)
        self._fact_check(run, topic, feedback)
        self.store.log_script(run.script)
        # REVISE: 수정 내용이 있으면 script.body에 반영하고 시트를 업데이트한다.
        script_review = ReviewRequest(stage=Stage.SCRIPT, title="대본 검수", preview=run.script.body)
        script_decision = self.telegram.request(script_review)
        if script_decision == ReviewDecision.REVISE and script_review.revised_content:
            # 사용자가 텔레그램에서 직접 입력한 수정본이므로 팩트체크를 재실행하지 않는다.
            # PO가 내용을 직접 확인·수정했다는 전제 하에 신뢰하는 설계다.
            run.script.body = script_review.revised_content
            self.store.update_script(run.script)
        elif script_decision != ReviewDecision.APPROVED:
            log.warning(
                "pipeline.gate_blocked", stage=Stage.SCRIPT.value, decision=script_decision.value
            )
            raise GateRejected(Stage.SCRIPT, script_decision)

        # 2단계: 영상
        run.current_stage = Stage.VIDEO
        run.video = self.studio.produce(run.script)
        # VIDEO 단계: 영상 파일이 있으면 MP4를 텔레그램으로 직접 전송(인라인 재생).
        video_review = ReviewRequest(
            stage=Stage.VIDEO,
            title="영상 품질 검수",
            preview=run.video.preview_url or "",
            media_path=run.video.video_path,
        )
        video_decision = self.telegram.request(video_review)
        if video_decision not in (ReviewDecision.APPROVED,):
            log.warning(
                "pipeline.gate_blocked", stage=Stage.VIDEO.value, decision=video_decision.value
            )
            raise GateRejected(Stage.VIDEO, video_decision)

        # 3단계: 메타데이터
        run.current_stage = Stage.METADATA
        run.metadata = self.ai.generate_metadata(run.script, self.settings.calculator_url)
        self._gate(self.discord, Stage.METADATA, "메타데이터 검수", run.metadata.title)

        # 4단계: 업로드
        run.current_stage = Stage.UPLOAD
        run.uploads.append(self.publisher.upload_youtube(run.video, run.metadata))
        if content_format == ContentFormat.REELS:
            run.uploads.append(self.publisher.upload_instagram(run.video, run.metadata))

        # 5단계: 성과 수집(분석/피드백은 collect_and_analyze에서 별도 주기로 수행)
        run.current_stage = Stage.ANALYTICS
        self.store.log_run(run)
        log.info("pipeline.done", run_id=run.id, uploads=len(run.uploads))
        return run

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
            run.script = self.ai.generate_script(topic, feedback=retry_feedback)
            result = self.ai.fact_check_script(run.script)

        run.script.fact_checked = result.passed
        if not result.passed:
            log.error("factcheck.rejected", issues=result.issues)
            raise FactCheckFailed(result.issues)

    def collect_and_analyze(self, run: PipelineRun) -> str:
        """업로드된 콘텐츠의 성과를 수집하고 다음 대본 개선안을 도출(피드백 루프).

        도출한 분석 결과를 상태에 저장해, 다음 사이클의 resolve_inputs가 이를
        feedback으로 자동 주입하도록 한다(피드백 루프 닫기).
        """
        run.reports = [self.publisher.fetch_performance(u) for u in run.uploads]
        analysis = self.ai.analyze_performance(run.reports)
        self.state.save_feedback(analysis)
        return analysis

    def _gate(self, gate: ReviewGate, stage: Stage, title: str, preview: str) -> None:
        review = ReviewRequest(stage=stage, title=title, preview=preview)
        decision = gate.request(review)
        if decision is not ReviewDecision.APPROVED:
            log.warning("pipeline.gate_blocked", stage=stage.value, decision=decision.value)
            raise GateRejected(stage, decision)
