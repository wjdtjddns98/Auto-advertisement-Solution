"""검수 요청 상태 영속화.

비동기 승인(사람이 버튼을 누를 때까지 대기)을 견디기 위해 검수 상태를 저장한다.
테스트/dry_run은 InMemoryReviewStore, 실제 실행은 JsonFileReviewStore를 쓴다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from nutti.logging import get_logger
from nutti.models import ReviewDecision, ReviewRequest

log = get_logger(__name__)


class ReviewStore(Protocol):
    """검수 요청 저장소 인터페이스."""

    def save(self, review: ReviewRequest) -> None: ...

    def get(self, review_id: str) -> ReviewRequest | None: ...

    def update_decision(
        self, review_id: str, decision: ReviewDecision, note: str = ""
    ) -> None: ...

    def all(self) -> list[ReviewRequest]: ...


class InMemoryReviewStore:
    """딕셔너리 기반 저장소(테스트/dry_run용)."""

    def __init__(self) -> None:
        self._items: dict[str, ReviewRequest] = {}

    def save(self, review: ReviewRequest) -> None:
        self._items[review.id] = review

    def get(self, review_id: str) -> ReviewRequest | None:
        return self._items.get(review_id)

    def update_decision(
        self, review_id: str, decision: ReviewDecision, note: str = ""
    ) -> None:
        review = self._items.get(review_id)
        if review is None:
            return
        review.decision = decision
        if note:
            review.note = note

    def all(self) -> list[ReviewRequest]:
        return list(self._items.values())


class JsonFileReviewStore:
    """JSON 파일 영속 저장소. id를 키로 ReviewRequest를 직렬화한다.

    재시작 후에도 대기 중인 검수 상태가 살아남도록 초기화 시 로드하고,
    save/update마다 파일에 기록한다.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: dict[str, ReviewRequest] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 손상 파일을 조용히 비우면 대기 중인 검수가 흔적 없이 사라지므로 경고를 남긴다.
            log.warning("review_store.load_failed", path=str(self.path), error=str(exc))
            return
        for review_id, data in (raw or {}).items():
            try:
                self._items[review_id] = ReviewRequest(**data)
            except Exception as exc:  # 한 행이 깨져도 전체 로드를 막지 않는다.
                log.warning("review_store.bad_row", review_id=review_id, error=str(exc))

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            review_id: review.model_dump(mode="json")
            for review_id, review in self._items.items()
        }
        # 원자적 쓰기: 임시 파일에 기록 후 교체해 중간 크래시로 인한 손상을 방지.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def save(self, review: ReviewRequest) -> None:
        self._items[review.id] = review
        self._flush()

    def get(self, review_id: str) -> ReviewRequest | None:
        return self._items.get(review_id)

    def update_decision(
        self, review_id: str, decision: ReviewDecision, note: str = ""
    ) -> None:
        review = self._items.get(review_id)
        if review is None:
            return
        review.decision = decision
        if note:
            review.note = note
        self._flush()

    def all(self) -> list[ReviewRequest]:
        return list(self._items.values())
