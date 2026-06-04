"""Claude 기반 텍스트 생성: 대본(1단계) · 메타데이터(3단계) · 성과 분석(5단계)."""

from __future__ import annotations

import re

from pydantic import BaseModel

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

# 주제 자동 생성용 시스템 프롬프트(다음 사이클에 다룰 쇼츠 주제 1개 제안).
TOPIC_SYSTEM_PROMPT = (
    "너는 애견 수제간식 브랜드 'Nutti'의 콘텐츠 기획자다. "
    "수의학·사실에 기반한 강아지 건강/다이어트/음식 정보를 다루는 60초 쇼츠 주제를 "
    "딱 한 개 제안한다. 최근 다룬 주제와 겹치지 않게 하고, 성과 분석 피드백이 있으면 "
    "그 방향(잘 된 포맷·소재)을 반영한다. 검색·시청 욕구를 자극하되 과장은 피한다."
)

# dry_run 및 폴백용 주제 시드(외부 호출 없이 매 사이클 다른 주제가 나오도록).
_SEED_TOPICS = [
    "강아지 닭가슴살 간식, 하루 적정량은?",
    "강아지가 먹으면 안 되는 음식 5가지",
    "노령견 관절 건강에 좋은 간식 고르는 법",
    "강아지 다이어트 중 간식, 이렇게 주세요",
    "강아지 고구마 간식, 얼마나 줘도 될까?",
    "강아지 치아 건강을 위한 덴탈 간식 진실",
    "강아지 수분 보충, 간식으로도 가능할까?",
    "강아지 알레르기, 간식으로 확인하는 법",
    "강아지 단백질 간식 제대로 고르는 기준",
    "수제간식 보관, 이렇게 하면 안 상해요",
]

# 팩트체크 시스템 프롬프트(수의학적 위험·근거 없는 주장 탐지).
FACT_CHECK_SYSTEM_PROMPT = (
    "너는 수의학 콘텐츠 팩트체커다. 주어진 대본에서 근거가 없거나 위험한 "
    "수의학적 주장(과장된 효능, 잘못된 급여량, 위험한 음식 추천 등)을 찾아낸다. "
    "반드시 record_fact_check 도구를 사용해 결과를 보고한다."
)

# 메타데이터 구조화 출력용 tool 스키마.
_METADATA_TOOL = {
    "name": "emit_metadata",
    "description": "YouTube Shorts/Reels 업로드용 메타데이터를 구조화해 반환한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "영상 제목(100자 이내)"},
            "description": {"type": "string", "description": "영상 설명"},
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "해시태그 5개(각각 # 포함)",
            },
        },
        "required": ["title", "description", "hashtags"],
    },
}

# 팩트체크 구조화 출력용 tool 스키마.
_FACT_CHECK_TOOL = {
    "name": "record_fact_check",
    "description": "대본의 팩트체크 결과를 구조화해 보고한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "passed": {
                "type": "boolean",
                "description": "근거 없는/위험한 주장이 없으면 true",
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "발견된 문제 목록(없으면 빈 배열)",
            },
        },
        "required": ["passed", "issues"],
    },
}


class FactCheckResult(BaseModel):
    """대본 팩트체크 결과."""

    passed: bool
    issues: list[str] = []


def _extract_tool_input(msg, tool_name: str) -> dict | None:
    """Anthropic 응답에서 지정한 tool_use 블록의 input(dict)을 안전하게 추출."""
    content = getattr(msg, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


def _first_text(msg) -> str:
    """응답에서 첫 text 블록을 안전하게 추출.

    content[0]을 직접 인덱싱하면 thinking 블록·빈 content 등에서 깨질 수 있어,
    type == "text"인 첫 블록을 찾고 없으면 빈 문자열로 폴백한다.
    """
    for block in getattr(msg, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


def _clean_topic(raw: str) -> str:
    """모델이 돌려준 주제 텍스트를 한 줄 제목으로 정리.

    여러 줄이면 첫 비어있지 않은 줄을 쓰고, 글머리표(-, *, 1.)·따옴표·백틱을 제거한다.
    """
    for line in (raw or "").splitlines():
        line = line.strip().strip("`").strip()
        # 글머리표 제거: "- ", "* ", "• ".
        line = line.lstrip("-*•").strip()
        # 번호 매김만 제거: "1. ", "10) ", "3] ". 단, "10가지"·"2024년"처럼
        # 숫자 뒤에 구두점이 없는 정상 제목은 건드리지 않는다(글자 단위 제거 금지).
        line = re.sub(r"^\d+[.)\]]\s*", "", line)
        line = line.strip().strip('"').strip("'").strip()
        if line:
            return line
    return ""


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

        if self.settings.dry_run:
            log.info("dry_run.generate_script", topic=topic)
            body = (
                f"[DRY-RUN 대본] {topic}\n"
                "훅: 우리 강아지, 이 간식 먹어도 될까요?\n"
                "본문: 수의학적으로 안전한 재료와 적정량을 소개합니다.\n"
                "CTA: 프로필 링크의 간식계산기로 우리 아이 맞춤 간식을 확인하세요!"
            )
            # dry_run은 팩트체크 통과를 시뮬레이션.
            return Script(topic=topic, body=body, prompt=prompt, fact_checked=True)

        if self._client is None:
            # API 키 없음 + 비-dry_run → Claude Code(Max 구독)로 생성(API 추가 과금 없음).
            return self._generate_via_claude_code(topic, prompt)

        # 시스템 프롬프트에 prompt caching 적용(ephemeral).
        msg = self._client.messages.create(
            model=self.settings.script_model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SCRIPT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        body = _first_text(msg)
        # 실제 모드에서는 호출자가 fact_check_script로 검증/갱신한다.
        return Script(topic=topic, body=body, prompt=prompt, fact_checked=False)

    def _claude_cli(self, full_prompt: str) -> str:
        """claude -p(헤드리스 print 모드)로 프롬프트를 보내고 stdout(텍스트)을 반환.

        API 키가 없을 때 Claude Code(Max 구독)를 통해 생성하므로 API 추가 과금이 없다.
        대본 생성·주제 제안 등 텍스트 생성 경로가 공유한다.
        """
        import subprocess

        try:
            proc = subprocess.run(
                ["claude", "-p", full_prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "claude CLI를 찾을 수 없습니다 (Claude Code 설치/PATH 확인)."
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p 실패: {(proc.stderr or '').strip()[:200]}")
        return (proc.stdout or "").strip()

    def _generate_via_claude_code(self, topic: str, prompt: str) -> Script:
        """Anthropic API 대신 Claude Code(Max 구독)로 대본 생성 — API 추가 과금 없음."""
        full = (
            f"{SCRIPT_SYSTEM_PROMPT}\n\n{prompt}\n\n"
            "대본 본문만 출력해줘. 머리말·설명·코드블록 없이 대본 텍스트만."
        )
        body = self._claude_cli(full)
        log.info("script.generated_via_claude_code", topic=topic, chars=len(body))
        return Script(topic=topic, body=body, prompt=prompt, fact_checked=False)

    def suggest_topic(self, feedback: str = "", recent_topics: list[str] | None = None) -> str:
        """다음 사이클에 다룰 쇼츠 주제를 한 개 제안한다(주제 자동 최적화).

        generate_script와 동일한 3-way 분기:
        dry_run→시드 주제, API 키 있음→Anthropic API, 없음→Claude Code(claude -p).
        recent_topics와 겹치지 않게 하고, feedback(직전 성과 분석)이 있으면 반영한다.
        """
        recent = recent_topics or []

        if self.settings.dry_run:
            log.info("dry_run.suggest_topic", n_recent=len(recent))
            return self._dry_topic(recent)

        prompt = "아래 조건으로 새 쇼츠 주제를 딱 한 개만 제안해줘.\n"
        if recent:
            prompt += "\n[최근 다룬 주제 — 겹치지 말 것]\n" + "\n".join(
                f"- {t}" for t in recent
            ) + "\n"
        if feedback:
            prompt += f"\n[직전 성과 분석 — 다음 주제에 반영]\n{feedback}\n"
        prompt += "\n주제 문장 한 줄만 출력해줘. 따옴표·번호·머리말·설명 없이 제목 텍스트만."

        if self._client is None:
            # API 키 없음 → Claude Code(Max)로 주제 생성.
            raw = self._claude_cli(f"{TOPIC_SYSTEM_PROMPT}\n\n{prompt}")
        else:
            msg = self._client.messages.create(
                model=self.settings.script_model,
                max_tokens=128,
                system=[
                    {
                        "type": "text",
                        "text": TOPIC_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            raw = _first_text(msg)

        topic = _clean_topic(raw)
        # 모델이 빈 응답/형식 깨짐을 주면 시드로 폴백(파이프라인이 멈추지 않도록).
        if not topic:
            log.warning("topic.suggest.empty_fallback")
            return self._dry_topic(recent)
        log.info("topic.suggested", topic=topic)
        return topic

    def _dry_topic(self, recent: list[str]) -> str:
        """외부 호출 없이 최근 주제와 겹치지 않는 시드 주제를 고른다(결정적)."""
        for seed in _SEED_TOPICS:
            if seed not in recent:
                return seed
        # 모든 시드를 최근에 다뤘다면 인덱스로 변형해 새 주제를 만든다.
        idx = len(recent) % len(_SEED_TOPICS)
        return f"{_SEED_TOPICS[idx]} (심화편)"

    def fact_check_script(self, script: Script) -> FactCheckResult:
        """대본의 수의학적 주장에 대한 팩트체크. 호출자가 Script.fact_checked를 갱신한다."""
        if self._client is None:
            log.info("dry_run.fact_check", script_id=script.id)
            return FactCheckResult(passed=True, issues=[])

        prompt = (
            "다음 대본에서 근거가 없거나 위험한 수의학적 주장을 찾아 "
            "record_fact_check 도구로 보고해줘. 문제가 없으면 passed=true, "
            f"issues=[] 로 보고해.\n\n{script.body}"
        )
        msg = self._client.messages.create(
            model=self.settings.script_model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": FACT_CHECK_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_FACT_CHECK_TOOL],
            tool_choice={"type": "tool", "name": "record_fact_check"},
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_tool_input(msg, "record_fact_check")
        if not data:
            # 파싱 실패 시 보수적으로 실패 처리(검수 게이트로 넘긴다).
            log.warning("fact_check.parse_failed", script_id=script.id)
            return FactCheckResult(passed=False, issues=["팩트체크 응답 파싱 실패"])

        passed = bool(data.get("passed", False))
        raw_issues = data.get("issues") or []
        issues = [str(x) for x in raw_issues] if isinstance(raw_issues, list) else []
        return FactCheckResult(passed=passed, issues=issues)

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
            f"설명 마지막에 반드시 간식계산기 링크({calculator_url})를 넣고, "
            f"emit_metadata 도구로 구조화해 반환해줘.\n\n{script.body}"
        )
        msg = self._client.messages.create(
            model=self.settings.script_model,
            max_tokens=512,
            tools=[_METADATA_TOOL],
            tool_choice={"type": "tool", "name": "emit_metadata"},
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_tool_input(msg, "emit_metadata") or {}

        title = str(data.get("title") or script.topic)[:100]
        description = str(data.get("description") or "")
        raw_tags = data.get("hashtags") or []
        hashtags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        if not hashtags:
            hashtags = ["#강아지간식", "#Nutti"]

        # 설명에 calculator_url이 없으면 추가(endswith가 아니라 포함 검사 — URL 뒤에
        # 닫는 괄호·마침표가 붙어도 중복 추가되지 않도록).
        if calculator_url not in description:
            sep = "\n\n" if description.strip() else ""
            description = f"{description.rstrip()}{sep}🐾 간식 계산기 → {calculator_url}"

        return Metadata(title=title, description=description, hashtags=hashtags)

    def analyze_performance(self, reports: list[PerformanceReport]) -> str:
        """성과 리포트를 요약하고 다음 대본 개선 포인트를 도출(5단계)."""
        if self._client is None:
            log.info("dry_run.analyze_performance", n=len(reports))
            total_views = sum(r.views for r in reports)
            return (
                f"[DRY-RUN 분석] 총 조회수 {total_views}. "
                "Q&A형 포맷의 시청 지속률이 가장 높음 → 다음 사이클은 Q&A 비중 확대 권장."
            )

        if not reports:
            return ""  # 분석할 데이터 없음

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
        return _first_text(msg)
