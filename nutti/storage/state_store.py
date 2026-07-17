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
from pathlib import Path

from nutti.logging import get_logger
from nutti.storage import atomic_write_json

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
        atomic_write_json(self.path, data)

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

    # --- 성과 수집 대기 큐(업로드 → 며칠 숙성 후 조회) ---
    # YouTube Analytics 지연 때문에 업로드 직후 조회하면 조회수가 0으로 나온다. 업로드를
    # 여기 쌓아두고(add_pending_upload), 오케스트레이터가 get_pending_uploads로 읽어
    # 숙성된 것만 조회·분석한 뒤 replace_pending_uploads로 남은 것만 다시 저장한다.
    # 항목: {"platform", "external_id", "url", "uploaded_at"(ISO8601), "dry_run"(bool)}.

    def get_pending_uploads(self) -> list[dict]:
        """성과 수집을 기다리는 업로드 목록(원소는 dict, 순서 보존)."""
        raw = self._load().get("pending_uploads", [])
        return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    def add_pending_upload(
        self,
        platform: str,
        external_id: str,
        url: str,
        uploaded_at: str,
        *,
        dry_run: bool = True,
    ) -> None:
        """업로드 1건을 성과 수집 대기 큐 끝에 추가한다.

        `dry_run`은 어느 모드가 만든 항목인지 태깅한다 — dry_run 항목의 external_id는
        가짜(yt_<script_id>)라 라이브 Analytics로 조회하면 HTTP 400이 난다(2026-07-10
        실측: 라이브 run이 시작 즉시 사망). collect_ready_feedback가 모드 일치 항목만
        조회한다. 기본 True(보수적) — 플래그 없는 레거시 항목도 dry로 간주돼 라이브에서
        정화된다(현 큐 오염분 자동 청소).
        """
        if not external_id:
            return
        data = self._load()
        queue = data.get("pending_uploads")
        if not isinstance(queue, list):
            queue = []
        queue.append(
            {
                "platform": platform,
                "external_id": external_id,
                "url": url,
                "uploaded_at": uploaded_at,
                "dry_run": bool(dry_run),
            }
        )
        data["pending_uploads"] = queue
        self._save(data)
        log.info("state.pending_upload.added", external_id=external_id, n=len(queue))

    def replace_pending_uploads(self, uploads: list[dict]) -> None:
        """대기 큐를 통째로 교체한다(숙성분을 꺼내고 남은 것만 다시 저장할 때 사용)."""
        data = self._load()
        data["pending_uploads"] = [dict(x) for x in uploads]
        self._save(data)
