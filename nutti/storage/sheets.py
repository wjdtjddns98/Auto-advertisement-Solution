"""Google Sheets 기록(계획서의 '엑셀/시트 저장').

dry_run 또는 자격증명이 없으면 인메모리 리스트로 동작한다.
실연동은 gspread로 구현 완료(원격 경로에서만 lazy import).
"""

from __future__ import annotations

import json
from pathlib import Path

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import PipelineRun, Script

log = get_logger(__name__)


class SheetStore:
    def __init__(self, settings: Settings, *, client=None):
        self.settings = settings
        self._memory: list[dict] = []  # dry_run/로컬 폴백 저장소
        self._client = client  # 주입된 fake가 있으면 우선 사용
        if self._client is None and self.is_remote:
            # 실제 경로에서만 lazy import → dry_run/무자격증명 환경엔 gspread 비강제.
            # 자격증명이 '있지만 깨진' 경우(잘못된 JSON·없는 파일·인증 거부·gspread 미설치 등)에도
            # __init__이 죽지 않도록 방어 → 경고 로그 후 인메모리 폴백으로 동작한다.
            try:
                self._client = self._build_client()
            except Exception as exc:  # noqa: BLE001 - 어떤 실패든 인메모리 폴백으로 흡수
                log.warning("sheets.build_client.failed", error=str(exc))
                self._client = None

    @property
    def is_remote(self) -> bool:
        return bool(
            not self.settings.dry_run
            and self.settings.google_sheets_id
            and self.settings.google_service_account_json
        )

    def _build_client(self):
        """원격 경로에서만 호출 → gspread를 여기서 lazy import한다."""
        import gspread  # 실제 경로에서만 lazy import

        value = self.settings.google_service_account_json
        path = Path(value)
        # 값이 존재하는 파일 경로면 파일을 읽고, 아니면 인라인 JSON 문자열로 간주.
        raw = path.read_text(encoding="utf-8") if path.exists() else value
        creds_dict = json.loads(raw)
        return gspread.service_account_from_dict(creds_dict)

    def _worksheet(self):
        """open_by_key/sheet1 접근을 한 곳에 모아 fake가 흉내내기 쉽게 한다."""
        return self._client.open_by_key(self.settings.google_sheets_id).sheet1

    def log_script(self, script: Script) -> None:
        if self._client is None:
            row = {
                "script_id": script.id,
                "topic": script.topic,
                "body": script.body,
                "prompt": script.prompt,
                "created_at": script.created_at.isoformat(),
            }
            self._memory.append(row)
            log.info("sheets.log_script.local", script_id=script.id)
            return
        self._worksheet().append_row(
            ["script", script.id, script.topic, script.body, script.prompt,
             script.created_at.isoformat()]
        )
        log.info("sheets.log_script.remote", script_id=script.id)

    def log_run(self, run: PipelineRun) -> None:
        if self._client is None:
            self._memory.append({"run_id": run.id, "stage": run.current_stage.value})
            return
        self._worksheet().append_row(["run", run.id, run.current_stage.value])
        log.info("sheets.log_run.remote", run_id=run.id)

    def all_rows(self) -> list[dict]:
        """로컬 폴백 저장소 내용(테스트/디버그용)."""
        return list(self._memory)
