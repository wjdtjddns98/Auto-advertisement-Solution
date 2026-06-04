"""PipelineState(실행 간 영속 상태) 단위 테스트.

모든 테스트는 tmp_path를 사용해 리포지토리 data/를 건드리지 않는다.
"""

from __future__ import annotations

from nutti.storage.state_store import PipelineState


def _state(tmp_path, **kw) -> PipelineState:
    return PipelineState(str(tmp_path / "state.json"), **kw)


def test_missing_file_returns_defaults(tmp_path):
    s = _state(tmp_path)
    assert s.get_feedback() == ""
    assert s.get_recent_topics() == []


def test_save_and_load_feedback(tmp_path):
    s = _state(tmp_path)
    s.save_feedback("다음엔 Q&A 비중 확대")
    # 새 인스턴스로 읽어도(=다른 프로세스 모사) 값이 보존돼야 한다.
    assert _state(tmp_path).get_feedback() == "다음엔 Q&A 비중 확대"


def test_save_feedback_ignores_blank(tmp_path):
    s = _state(tmp_path)
    s.save_feedback("실내용")
    s.save_feedback("   ")  # 공백은 무시 → 기존 값 유지
    assert s.get_feedback() == "실내용"


def test_add_topic_orders_newest_first(tmp_path):
    s = _state(tmp_path)
    s.add_topic("주제A")
    s.add_topic("주제B")
    assert s.get_recent_topics() == ["주제B", "주제A"]


def test_add_topic_dedupes_and_moves_to_front(tmp_path):
    s = _state(tmp_path)
    s.add_topic("A")
    s.add_topic("B")
    s.add_topic("A")  # 재등장 → 중복 제거 후 맨 앞으로
    assert s.get_recent_topics() == ["A", "B"]


def test_recent_topics_capped(tmp_path):
    s = _state(tmp_path, max_topics=3)
    for i in range(5):
        s.add_topic(f"주제{i}")
    topics = s.get_recent_topics()
    assert len(topics) == 3
    assert topics == ["주제4", "주제3", "주제2"]  # 최신 3개만


def test_add_topic_ignores_blank(tmp_path):
    s = _state(tmp_path)
    s.add_topic("")
    s.add_topic("   ")
    assert s.get_recent_topics() == []


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not valid json ", encoding="utf-8")
    s = PipelineState(str(path))
    # 손상 파일이어도 죽지 않고 빈 상태로 동작.
    assert s.get_feedback() == ""
    assert s.get_recent_topics() == []
    # 이후 저장은 정상 동작(파일을 덮어쓴다).
    s.save_feedback("복구")
    assert PipelineState(str(path)).get_feedback() == "복구"


def test_feedback_and_topics_coexist(tmp_path):
    s = _state(tmp_path)
    s.save_feedback("피드백")
    s.add_topic("주제")
    reloaded = _state(tmp_path)
    assert reloaded.get_feedback() == "피드백"
    assert reloaded.get_recent_topics() == ["주제"]
