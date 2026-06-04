"""SheetStore 단위 테스트.

폴백(인메모리) 경로와 fake gspread 클라이언트 주입을 통한 원격 append 경로를
네트워크 없이 검증한다. 실제 gspread.service_account_from_dict는 client 주입으로
우회되므로(_build_client 미진입) 더미 자격증명으로도 안전하다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.models import PipelineRun, Script
from nutti.storage.sheets import SheetStore


def _dry_settings() -> Settings:
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test")


def _remote_settings() -> Settings:
    return Settings(
        NUTTI_DRY_RUN=False,
        GOOGLE_SHEETS_ID="sheet123",
        GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account"}',
        NUTTI_ENV="test",
    )


class _FakeWorksheet:
    def __init__(self):
        self.rows: list[list] = []

    def append_row(self, values):
        self.rows.append(values)


class _FakeSpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    @property
    def sheet1(self):
        return self._worksheet


class _FakeGspreadClient:
    def __init__(self):
        self.worksheet = _FakeWorksheet()
        self.opened_keys: list[str] = []

    def open_by_key(self, key):
        self.opened_keys.append(key)
        return _FakeSpreadsheet(self.worksheet)


def test_dry_run_uses_memory_fallback():
    store = SheetStore(_dry_settings())
    assert store.is_remote is False
    store.log_script(Script(topic="강아지 간식", body="본문"))
    store.log_run(PipelineRun(topic="강아지 간식"))
    rows = store.all_rows()
    assert len(rows) == 2
    assert any("script_id" in r for r in rows)
    assert any("run_id" in r for r in rows)


def test_missing_credentials_uses_fallback():
    # dry_run=False지만 자격증명 비움 → is_remote False, 폴백 동작.
    # 자격증명을 명시적으로 비워 로컬 .env 값이 새어들지 않게 한다(테스트 격리).
    settings = Settings(
        NUTTI_DRY_RUN=False,
        NUTTI_ENV="test",
        GOOGLE_SHEETS_ID="",
        GOOGLE_SERVICE_ACCOUNT_JSON="",
    )
    store = SheetStore(settings)
    assert store.is_remote is False
    store.log_script(Script(topic="t", body="본문"))
    assert len(store.all_rows()) == 1


def test_log_script_appends_remote():
    fake = _FakeGspreadClient()
    store = SheetStore(_remote_settings(), client=fake)
    script = Script(topic="강아지 사과 급여", body="본문")
    store.log_script(script)

    rows = fake.worksheet.rows
    assert len(rows) == 1
    assert rows[0][0] == "script"
    assert rows[0][1] == script.id
    assert rows[0][2] == script.topic
    assert script.body in rows[0]  # 60초 대본 본문도 저장된다
    # 인메모리 폴백에는 기록되지 않는다.
    assert store.all_rows() == []


def test_log_run_appends_remote():
    fake = _FakeGspreadClient()
    store = SheetStore(_remote_settings(), client=fake)
    run = PipelineRun(topic="강아지 닭가슴살")
    store.log_run(run)

    rows = fake.worksheet.rows
    assert len(rows) == 1
    assert rows[0][0] == "run"
    assert rows[0][1] == run.id
    assert rows[0][2] == run.current_stage.value


def test_remote_opens_correct_sheet_key():
    fake = _FakeGspreadClient()
    store = SheetStore(_remote_settings(), client=fake)
    store.log_script(Script(topic="t", body="b"))
    assert fake.opened_keys == ["sheet123"]


def test_broken_credentials_falls_back_to_memory(monkeypatch):
    # 자격증명이 '있지만 깨진'(잘못된 JSON) 경우에도 __init__이 죽지 않고 인메모리 폴백.
    settings = Settings(
        NUTTI_DRY_RUN=False,
        GOOGLE_SHEETS_ID="sheet123",
        GOOGLE_SERVICE_ACCOUNT_JSON="{not valid json",
        NUTTI_ENV="test",
    )
    # is_remote는 True지만 _build_client가 실패 → _client는 None으로 폴백.
    store = SheetStore(settings)
    assert store.is_remote is True
    assert store._client is None
    store.log_script(Script(topic="t", body="b"))
    store.log_run(PipelineRun(topic="t"))
    assert len(store.all_rows()) == 2


def test_build_client_failure_does_not_raise(monkeypatch):
    # _build_client가 어떤 예외(예: gspread 인증 거부)를 던져도 __init__은 통과해야 한다.
    def _boom(self):
        raise RuntimeError("auth rejected")

    monkeypatch.setattr(SheetStore, "_build_client", _boom)
    store = SheetStore(_remote_settings())
    assert store._client is None
    store.log_script(Script(topic="t", body="b"))
    assert len(store.all_rows()) == 1


def test_no_gspread_import_in_fallback(monkeypatch):
    # dry_run 경로에서는 gspread import가 불가능해도(_build_client 미진입) 동작해야 한다.
    import sys

    monkeypatch.setitem(sys.modules, "gspread", None)
    store = SheetStore(_dry_settings())  # _build_client 미호출
    assert store.is_remote is False
    store.log_script(Script(topic="t", body="b"))
    store.log_run(PipelineRun(topic="t"))
    assert len(store.all_rows()) == 2
