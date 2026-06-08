"""AITextClient dry_run 단위 테스트.

모든 테스트는 NUTTI_DRY_RUN=True로 동작하므로 외부 API 키/네트워크가 필요 없다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.integrations.ai_text import (
    AITextClient,
    FactCheckResult,
    _clean_topic,
    _extract_tool_input,
    _first_text,
    _split_into_beats,
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


def test_generate_script_dry_run_fills_four_beats():
    """dry_run 대본은 영상 비트 4개(8+7*3=29초)로 분할돼 채워진다."""
    script = _client().generate_script("강아지 닭가슴살 간식 적정량")
    assert len(script.beats) == 4
    assert all(b.strip() for b in script.beats)


def test_split_into_beats_by_lines():
    assert _split_into_beats("훅\n설명1\n설명2\n마무리") == ["훅", "설명1", "설명2", "마무리"]


def test_split_into_beats_strips_bullets_and_numbers():
    assert _split_into_beats("1. 훅\n2. 설명\n3. 설명2\n4. 끝") == ["훅", "설명", "설명2", "끝"]


def test_split_into_beats_falls_back_to_sentences():
    """줄이 부족하면 문장 종결부호 기준으로 쪼개 n개로 분배한다."""
    beats = _split_into_beats("문장1. 문장2! 문장3? 문장4.", n=4)
    assert len(beats) == 4


def test_split_into_beats_fewer_than_n_returns_available():
    beats = _split_into_beats("한 문장만 있어요", n=4)
    assert len(beats) >= 1
    assert all(b.strip() for b in beats)


def test_split_into_beats_empty_returns_empty():
    assert _split_into_beats("") == []
    assert _split_into_beats("   ") == []


def test_split_into_beats_more_lines_than_n_chunks_evenly():
    """줄 수가 n보다 많으면 균등 묶어 정확히 n개(빈 비트 없음, 순서 보존)."""
    beats = _split_into_beats("a\nb\nc\nd\ne\nf", n=4)
    assert len(beats) == 4
    assert all(b.strip() for b in beats)
    assert beats[0].startswith("a")
    assert beats[-1].endswith("f")


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


# --- 주제 자동 생성(suggest_topic) ---

def test_suggest_topic_dry_run_returns_seed():
    topic = _client().suggest_topic()
    assert isinstance(topic, str) and topic.strip()


def test_suggest_topic_dry_run_avoids_recent():
    # 최근 주제로 첫 시드를 막으면 다른 주제를 골라야 한다.
    client = _client()
    first = client.suggest_topic(recent_topics=[])
    second = client.suggest_topic(recent_topics=[first])
    assert second != first


def test_suggest_topic_dry_run_all_seeds_used_still_returns():
    # 모든 시드를 최근에 다뤄도 빈 문자열이 아니라 변형 주제를 돌려줘야 한다.
    from nutti.integrations.ai_text import _SEED_TOPICS

    client = _client()
    topic = client.suggest_topic(recent_topics=list(_SEED_TOPICS))
    assert topic.strip()


def _live_topic_client(msg) -> AITextClient:
    """비-dry 설정 + 가짜 Anthropic 주입 → suggest_topic 라이브 경로.

    suggest_topic은 self.settings.dry_run으로 분기하므로(_client 여부가 아님),
    라이브 경로를 타려면 dry_run=False 설정이 필요하다.
    """
    settings = Settings(NUTTI_DRY_RUN=False, ANTHROPIC_API_KEY="", NUTTI_ENV="test")
    client = AITextClient(settings)
    client._client = _FakeAnthropic(msg)
    return client


def test_suggest_topic_live_uses_first_text():
    # 라이브 경로(가짜 Anthropic): text 응답을 한 줄 주제로 정리해 반환.
    client = _live_topic_client(_Msg([_Block("text", text="강아지 여름철 수분 간식 3가지")]))
    assert client.suggest_topic(feedback="여름 소재 반응 좋음") == "강아지 여름철 수분 간식 3가지"


def test_suggest_topic_live_empty_falls_back_to_seed():
    # 모델이 빈 응답을 주면 시드로 폴백(파이프라인이 멈추지 않도록).
    client = _live_topic_client(_Msg([_Block("text", text="   ")]))
    assert client.suggest_topic().strip()


# --- _clean_topic 정리 로직 ---

def test_clean_topic_strips_bullets_and_quotes():
    assert _clean_topic('- "강아지 간식 적정량"') == "강아지 간식 적정량"
    assert _clean_topic("1. 노령견 관절 간식") == "노령견 관절 간식"
    assert _clean_topic("```\n강아지 치아 건강\n```") == "강아지 치아 건강"


def test_clean_topic_takes_first_nonempty_line():
    assert _clean_topic("\n\n강아지 수분 보충\n부가 설명") == "강아지 수분 보충"


def test_clean_topic_preserves_leading_numbers_in_title():
    # 번호 매김(1. )은 제거하되, 제목 자체의 숫자("10가지"·"2024년"·"5분")는 보존해야 한다.
    assert _clean_topic("10가지 강아지 간식") == "10가지 강아지 간식"
    assert _clean_topic("2024년 강아지 트렌드") == "2024년 강아지 트렌드"
    assert _clean_topic("5분 안에 만드는 간식") == "5분 안에 만드는 간식"
    # 진짜 번호 매김은 여전히 제거.
    assert _clean_topic("10) 강아지 간식") == "강아지 간식"
    assert _clean_topic("3] 강아지 간식") == "강아지 간식"


def test_clean_topic_empty_returns_blank():
    assert _clean_topic("") == ""
    assert _clean_topic("   \n  ") == ""
