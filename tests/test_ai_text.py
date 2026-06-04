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


def test_extract_tool_input_non_dict_returns_none():
    # 매칭되는 tool_use 블록이 있어도 input이 dict가 아니면 None (가드 검증).
    msg = _Msg([_Block("tool_use", name="emit_metadata", input=None)])
    assert _extract_tool_input(msg, "emit_metadata") is None


# --- 라이브 경로(가짜 Anthropic 클라이언트 주입, 네트워크 없음) ---

class _FakeMessages:
    def __init__(self, msg):
        self._msg = msg

    def create(self, **_kwargs):
        return self._msg


class _FakeAnthropic:
    def __init__(self, msg):
        self.messages = _FakeMessages(msg)


def _live_client(msg) -> AITextClient:
    client = AITextClient(_dry_settings())
    client._client = _FakeAnthropic(msg)  # dry_run 분기 우회 → 라이브 경로
    return client


def test_fact_check_parse_failure_fails_safe():
    # record_fact_check tool_use가 없는 응답 → 보수적으로 passed=False.
    client = _live_client(_Msg([_Block("text", text="도구 호출 없음")]))
    result = client.fact_check_script(Script(topic="t", body="본문"))
    assert result.passed is False
    assert result.issues  # 비어있지 않음


def test_generate_metadata_live_appends_url_and_defaults_hashtags():
    url = "https://example.com/calc/"
    msg = _Msg([
        _Block("tool_use", name="emit_metadata",
               input={"title": "제목", "description": "설명 본문", "hashtags": []}),
    ])
    meta = _live_client(msg).generate_metadata(Script(topic="t", body="b"), url)
    assert url in meta.description          # 누락된 링크 보정
    assert len(meta.hashtags) >= 1          # 빈 해시태그 → 기본값 폴백


def test_generate_metadata_no_duplicate_link():
    # 설명 중간에 URL이 이미 있으면(뒤에 마침표) 중복 추가하지 않아야 한다(#7).
    url = "https://example.com/calc/"
    msg = _Msg([
        _Block("tool_use", name="emit_metadata",
               input={"title": "제목", "description": f"여기 링크({url}) 참고하세요.",
                      "hashtags": ["#강아지"]}),
    ])
    meta = _live_client(msg).generate_metadata(Script(topic="t", body="b"), url)
    assert meta.description.count(url) == 1
