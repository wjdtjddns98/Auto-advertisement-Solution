"""실행 간 영속 상태 저장소(피드백 루프 닫기 + 주제 자동 생성 지원).

N8n 스케줄러는 매 사이클을 별도 프로세스(`nutti run ...`)로 호출하므로,
한 사이클의 성과 분석 결과를 다음 사이클로 넘기려면 프로세스 밖에 상태를 남겨야 한다.
여기서는 외부 의존성 없이 동작하도록 로컬 JSON 파일에 보관한다.

저장 항목:
- last_feedback: 직전 사이클의 성과 분석 결과(다음 대본 생성 시 feedback으로 자동 주입)
- recent_topics: 최근 다룬 주제(주제 자동 생성 시 중복 회피용, 최신순·상한 적용)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from nutti.logging import get_logger

log = get_logger(__name__)


class PipelineState:
    """피드백·최근 주제를 JSON 파일에 보관하는 경량 영속 저장소.

    파일이 없거나 깨져 있어도 죽지 않고 기본값(빈 상태)으로 폴백한다.
    """

    def __init__(self, path: str, *, max_topics: int = 20):
        self.path = Path(path)
        self.max_topics = max_topics

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
            # 최초 실행(파일 없음)·손상 파일 모두 빈 상태로 폴백.
            if not isinstance(exc, FileNotFoundError):
                log.warning("state.load.failed", path=str(self.path), error=str(exc))
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        # 원자적 쓰기(tmp 작성 후 os.replace) — 크래시·동시 실행 시 파일이
        # 잘리거나 0바이트로 남지 않도록. JsonFileReviewStore와 동일한 패턴.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except Exception:
            # 실패 시 임시 파일을 남기지 않는다.
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- 피드백(성과 분석 → 다음 대본 개선) ---

    def get_feedback(self) -> str:
        """직전 사이클에서 저장된 개선 피드백(없으면 빈 문자열)."""
        return str(self._load().get("last_feedback", "") or "")

    def save_feedback(self, text: str) -> None:
        """이번 사이클 분석 결과를 다음 사이클용 피드백으로 저장(빈 값은 무시)."""
        if not text or not text.strip():
            return
        data = self._load()
        data["last_feedback"] = text
        self._save(data)
        log.info("state.feedback.saved", chars=len(text))

    # --- 최근 주제(자동 생성 시 중복 회피) ---

    def get_recent_topics(self) -> list[str]:
        """최근 다룬 주제 목록(최신순)."""
        raw = self._load().get("recent_topics", [])
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def add_topic(self, topic: str) -> None:
        """주제를 최근 목록 맨 앞에 추가(중복 제거 후 상한 적용)."""
        if not topic or not topic.strip():
            return
        data = self._load()
        topics = data.get("recent_topics", [])
        if not isinstance(topics, list):
            topics = []
        topics = [str(t) for t in topics if str(t) != topic]
        topics.insert(0, topic)
        data["recent_topics"] = topics[: self.max_topics]
        self._save(data)
