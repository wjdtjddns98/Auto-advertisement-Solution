"""AITextClient dry_run 단위 테스트.

모든 테스트는 NUTTI_DRY_RUN=True로 동작하므로 외부 API 키/네트워크가 필요 없다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.integrations.ai_text import (
    AITextClient,
    FactCheckResult,
    _extract_tool_input,
    _first_text,
)
from nutti.models import Script


class _Block:
    """Anthropic 응답 블록 흉내(테스트용)."""

    def __init__(self, type, text=None, name=None, input=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input


class _Msg:
    def __init__(self, content):
        self.content = content


def _dry_settings() -> Settings:
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test")


def _client() -> AITextClient:
    return AITextClient(_dry_settings())


def test_generate_script_dry_run():
    script = _client().generate_script("강아지 닭가슴살 간식 적정량")
    assert isinstance(script, Script)
    assert script.body.strip()
    assert script.fact_checked is True


def test_fact_check_passes_in_dry_run():
    client = _client()
    script = client.generate_script("강아지 사과 급여")
    result = client.fact_check_script(script)
    assert isinstance(result, FactCheckResult)
    assert result.passed is True
    assert result.issues == []


def test_generate_metadata_dry_run():
    client = _client()
    calculator_url = "https://example.com/calculator/"
    script = client.generate_script("강아지 수제간식")
    meta = client.generate_metadata(script, calculator_url)

    assert meta.title.strip()
    assert len(meta.hashtags) >= 1
    assert calculator_url in meta.description


# --- 라이브 경로 헬퍼(dry_run이 건드리지 않음) 단위 테스트 ---

def test_first_text_skips_non_text_blocks():
    # thinking 블록이 먼저 와도 첫 text 블록을 골라야 한다(HIGH 수정 검증).
    msg = _Msg([_Block("thinking", text=None), _Block("text", text="안녕")])
    assert _first_text(msg) == "안녕"


def test_first_text_empty_content_returns_blank():
    assert _first_text(_Msg([])) == ""
    assert _first_text(_Msg(None)) == ""


def test_extract_tool_input_finds_named_tool():
    msg = _Msg([
        _Block("text", text="무시"),
        _Block("tool_use", name="emit_metadata", input={"title": "T"}),
    ])
    assert _extract_tool_input(msg, "emit_metadata") == {"title": "T"}


def test_extract_tool_input_missing_returns_none():
    msg = _Msg([_Block("text", text="없음")])
    assert _extract_tool_input(msg, "emit_metadata") is None
