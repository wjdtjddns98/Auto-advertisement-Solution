"""Claude 기반 텍스트 생성: 대본(1단계) · 메타데이터(3단계) · 성과 분석(5단계)."""

from __future__ import annotations

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Metadata, PerformanceReport, Script

log = get_logger(__name__)

SCRIPT_SYSTEM_PROMPT = (
    "너는 애견 수제간식 브랜드 'Nutti'의 콘텐츠 작가다. "
    "수의학·사실에 기반한 강아지 건강/다이어트/음식 정보를 다룬다. "
    "60초 내외 쇼츠/릴스용 대본을 쓰되, 반드시 팩트체크 가능한 내용만 포함한다. "
    "과장·근거 없는 의학 주장은 금지한다."
)


class AITextClient:
    """Anthropic SDK 래퍼. dry_run이면 더미 대본/메타데이터를 생성한다."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        if not settings.dry_run and settings.anthropic_api_key:
            # 실제 호출 시에만 SDK 로드 (dry_run 환경에 의존성 강제 안 함)
            from anthropic import Anthropic

            self._client = Anthropic(api_key=settings.anthropic_api_key)

    def generate_script(self, topic: str, feedback: str = "") -> Script:
        """주제로부터 대본 생성. feedback은 5단계 분석 결과를 반영할 때 사용."""
        prompt = f"주제: {topic}\n"
        if feedback:
            prompt += f"\n[이전 사이클 개선 포인트]\n{feedback}\n"
        prompt += "\n위 주제로 60초 쇼츠 대본을 작성해줘."

        if self._client is None:
            log.info("dry_run.generate_script", topic=topic)
            body = (
                f"[DRY-RUN 대본] {topic}\n"
                "훅: 우리 강아지, 이 간식 먹어도 될까요?\n"
                "본문: 수의학적으로 안전한 재료와 적정량을 소개합니다.\n"
                "CTA: 프로필 링크의 간식계산기로 우리 아이 맞춤 간식을 확인하세요!"
            )
            return Script(topic=topic, body=body, prompt=prompt, fact_checked=True)

        msg = self._client.messages.create(
            model=self.settings.script_model,
            max_tokens=1024,
            system=SCRIPT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        body = msg.content[0].text  # type: ignore[union-attr]
        return Script(topic=topic, body=body, prompt=prompt, fact_checked=True)

    def generate_metadata(self, script: Script, calculator_url: str) -> Metadata:
        """대본으로부터 제목·설명·해시태그 생성."""
        if self._client is None:
            log.info("dry_run.generate_metadata", script_id=script.id)
            return Metadata(
                title=f"강아지 건강 간식 꿀팁 | {script.topic}",
                description=(
                    f"{script.topic}에 대한 수의학 기반 정보입니다.\n\n"
                    f"🐾 우리 아이 맞춤 간식 계산기 → {calculator_url}"
                ),
                hashtags=["#강아지간식", "#수제간식", "#반려견건강", "#Nutti", "#강아지쇼츠"],
            )

        prompt = (
            f"다음 대본에 맞는 YouTube Shorts 제목, 설명, 해시태그 5개를 만들어줘. "
            f"설명 마지막에 간식계산기 링크({calculator_url})를 넣어줘.\n\n{script.body}"
        )
        msg = self._client.messages.create(
            model=self.settings.script_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text  # type: ignore[union-attr]
        # 간단 파싱(실서비스에서는 구조화 출력/tool use 권장)
        title = text.splitlines()[0][:100] if text else script.topic
        return Metadata(title=title, description=text, hashtags=["#강아지간식", "#Nutti"])

    def analyze_performance(self, reports: list[PerformanceReport]) -> str:
        """성과 리포트를 요약하고 다음 대본 개선 포인트를 도출(5단계)."""
        if self._client is None:
            log.info("dry_run.analyze_performance", n=len(reports))
            total_views = sum(r.views for r in reports)
            return (
                f"[DRY-RUN 분석] 총 조회수 {total_views}. "
                "Q&A형 포맷의 시청 지속률이 가장 높음 → 다음 사이클은 Q&A 비중 확대 권장."
            )

        summary = "\n".join(
            f"- {r.platform}/{r.external_id}: 조회 {r.views}, 평균시청 {r.avg_view_duration_sec}s"
            for r in reports
        )
        prompt = f"다음 성과 데이터를 분석해 다음 대본 개선 포인트를 3가지로 요약해줘.\n{summary}"
        msg = self._client.messages.create(
            model=self.settings.script_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text  # type: ignore[union-attr]
