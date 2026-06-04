"""Google Sheets 기록(계획서의 '엑셀/시트 저장').

dry_run 또는 자격증명이 없으면 인메모리 리스트로 동작한다.
실제 연동은 gspread / Google Sheets API로 교체.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import PipelineRun, Script

log = get_logger(__name__)


class SheetStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._memory: list[dict] = []  # dry_run/로컬 폴백 저장소

    @property
    def is_remote(self) -> bool:
        return bool(
            not self.settings.dry_run
            and self.settings.google_sheets_id
            and self.settings.google_service_account_json
        )

    def log_script(self, script: Script) -> None:
        row = {
            "script_id": script.id,
            "topic": script.topic,
            "prompt": script.prompt,
            "created_at": script.created_at.isoformat(),
        }
        if not self.is_remote:
            self._memory.append(row)
            log.info("sheets.log_script.local", script_id=script.id)
            return
        # TODO: Google Sheets append_row
        raise NotImplementedError("Google Sheets 연동 미구현")

    def log_run(self, run: PipelineRun) -> None:
        if not self.is_remote:
            self._memory.append({"run_id": run.id, "stage": run.current_stage.value})
            return
        # TODO: Google Sheets append_row
        raise NotImplementedError("Google Sheets 연동 미구현")

    def all_rows(self) -> list[dict]:
        """로컬 폴백 저장소 내용(테스트/디버그용)."""
        return list(self._memory)
