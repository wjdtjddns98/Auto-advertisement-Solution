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

log = get_logger(__name__)


class GateRejected(Exception):
    """검수에서 승인되지 않아 파이프라인을 중단할 때 발생."""

    def __init__(self, stage: Stage, decision: ReviewDecision):
        self.stage = stage
        self.decision = decision
        super().__init__(f"{stage.value} 단계 검수 결과: {decision.value}")


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        telegram: ReviewGate | None = None,
        discord: ReviewGate | None = None,
    ):
        self.settings = settings or get_settings()
        self.ai = AITextClient(self.settings)
        self.studio = VideoStudio(self.settings)
        self.publisher = Publisher(self.settings)
        self.store = SheetStore(self.settings)
        # 검수 게이트 주입 가능(테스트 시 AutoApproveGate)
        self.telegram: ReviewGate = telegram or TelegramGate(self.settings)
        self.discord: ReviewGate = discord or DiscordGate(self.settings)

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

        # 1단계: 대본
        run.current_stage = Stage.SCRIPT
        run.script = self.ai.generate_script(topic, feedback=feedback)
        self.store.log_script(run.script)
        self._gate(self.telegram, Stage.SCRIPT, "대본 검수", run.script.body)

        # 2단계: 영상
        run.current_stage = Stage.VIDEO
        run.video = self.studio.produce(run.script)
        self._gate(self.telegram, Stage.VIDEO, "영상 품질 검수", run.video.preview_url or "")

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

    def collect_and_analyze(self, run: PipelineRun) -> str:
        """업로드된 콘텐츠의 성과를 수집하고 다음 대본 개선안을 도출(피드백 루프)."""
        run.reports = [self.publisher.fetch_performance(u) for u in run.uploads]
        return self.ai.analyze_performance(run.reports)

    def _gate(self, gate: ReviewGate, stage: Stage, title: str, preview: str) -> None:
        review = ReviewRequest(stage=stage, title=title, preview=preview)
        decision = gate.request(review)
        if decision is not ReviewDecision.APPROVED:
            log.warning("pipeline.gate_blocked", stage=stage.value, decision=decision.value)
            raise GateRejected(stage, decision)
