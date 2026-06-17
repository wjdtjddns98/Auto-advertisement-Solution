"""로컬 시연 스크립트 (dry-run, API 키 불필요).

여태 만든 것을 한 번에 보여준다:
1) 전체 파이프라인 (쇼츠/릴스)
2) 팩트체크 자동 거절/재생성 (#1 수정)
3) 텔레그램 검수 인가 (#2 수정) — 인가/미인가 콜백

요구사항: 통합된 전체 파이프라인이 필요하다(텔레그램·팩트체크 PR 머지 후 동작).
실행(저장소 루트에서):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/demo.py
또는 `pip install -e .` 후:
    python scripts/demo.py
"""

from nutti.config import Settings
from nutti.integrations.ai_text import FactCheckResult
from nutti.models import ContentFormat, ReviewRequest, Stage
from nutti.pipeline.orchestrator import FactCheckFailed, Orchestrator
from nutti.review.gates import AutoApproveGate, TelegramGate
from nutti.storage.reviews import InMemoryReviewStore


def line(t):
    print("\n" + "=" * 60 + f"\n  {t}\n" + "=" * 60)


def dry():
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="demo")


# 1) 전체 파이프라인 ---------------------------------------------------------
line("1) 전체 파이프라인 (쇼츠) — 대본→팩트체크→검수→영상→메타→업로드→분석")
orch = Orchestrator(dry(), telegram=AutoApproveGate(), discord=AutoApproveGate())
run = orch.run("강아지 닭가슴살 간식, 하루 적정량은?")
print(f"  대본 생성됨   : {run.script.body.splitlines()[0]}")
print(f"  팩트체크 통과 : {run.script.fact_checked}")
print(f"  영상 최종 URL : {run.video.final_url}")
print(f"  메타 제목     : {run.metadata.title}")
print(f"  업로드        : {[u.platform + ' → ' + u.url for u in run.uploads]}")
print(f"  성과 분석     : {orch.collect_and_analyze(run)[:50]}...")

line("1-b) 릴스 — 유튜브 + 인스타 동시 업로드")
run2 = orch.run("강아지 수제간식 레시피", content_format=ContentFormat.REELS)
print(f"  업로드 플랫폼 : {sorted(u.platform for u in run2.uploads)}")


# 2) 팩트체크 자동 거절/재생성 (#1) ------------------------------------------
line("2) 팩트체크 실패 → 자동 재생성 후 한도 초과 시 거절 (#1 수정)")
orch_fc = Orchestrator(
    dry(), telegram=AutoApproveGate(), discord=AutoApproveGate(), max_factcheck_retries=1
)
fc_calls = {"n": 0}
gen_calls = {"n": 0}
_real_gen = orch_fc.ai.generate_script


def failing_check(_script):
    fc_calls["n"] += 1
    return FactCheckResult(passed=False, issues=["근거 없는 효능 주장 발견"])


def counting_gen(topic, feedback=""):
    gen_calls["n"] += 1
    return _real_gen(topic, feedback=feedback)


orch_fc.ai.fact_check_script = failing_check
orch_fc.ai.generate_script = counting_gen
try:
    orch_fc.run("강아지에게 OO를 먹이면 암이 낫는다")  # 위험한 주제
    print("  (도달 불가)")
except FactCheckFailed as e:
    print(f"  대본 생성 횟수   : {gen_calls['n']} (최초 + 재생성 1회)")
    print(f"  팩트체크 호출    : {fc_calls['n']}회")
    print(f"  최종 결과        : 차단됨 → {e}")


# 3) 텔레그램 검수 인가 (#2) -------------------------------------------------
line("3) 텔레그램 검수 인가 — 설정된 채팅만 승인 가능 (#2 보안 수정)")


class FakeTg:
    def __init__(self, batches):
        self._b = list(batches)
        self.answered = []
        self.edited = []

    def send_review(self, chat_id, review):
        return 1001

    def get_updates(self, offset=None, timeout=0):
        return self._b.pop(0) if self._b else []

    def answer_callback(self, cid):
        self.answered.append(cid)

    def edit_message(self, *a):
        self.edited.append(a)


def cb(review_id, value, chat_id):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cbq",
            "data": f"nutti:{review_id}:{value}",
            "message": {"chat": {"id": chat_id}},
            "from": {"id": chat_id},
        },
    }


tg_settings = Settings(
    NUTTI_DRY_RUN=False,
    TELEGRAM_BOT_TOKEN="x",
    TELEGRAM_CHAT_ID="123",
    NUTTI_REVIEW_TIMEOUT_SEC=5,
    NUTTI_REVIEW_POLL_INTERVAL_SEC=0,
)

# (a) 인가된 채팅(123)에서 승인
rev = ReviewRequest(stage=Stage.SCRIPT, title="대본 검수", preview="...")
gate_ok = TelegramGate(
    tg_settings, client=FakeTg([[cb(rev.id, "approved", 123)]]),
    store=InMemoryReviewStore(), sleep=lambda _: None,
)
print(f"  인가된 채팅(123) 승인 콜백  → {gate_ok.request(rev).value}")

# (b) 다른 채팅(999)에서 승인 시도 → 무시 → 타임아웃 거절
rev2 = ReviewRequest(stage=Stage.SCRIPT, title="대본 검수", preview="...")
ticks = iter([0.0, 0.0, 9999.0])
gate_bad = TelegramGate(
    tg_settings, client=FakeTg([[cb(rev2.id, "approved", 999)]]),
    store=InMemoryReviewStore(), clock=lambda: next(ticks), sleep=lambda _: None,
)
print(f"  미인가 채팅(999) 승인 시도  → {gate_bad.request(rev2).value} (무시됨!)")

print("\n시연 끝. (전부 dry-run, 실제 API 호출 0건)\n")
