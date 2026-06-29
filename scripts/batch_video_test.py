"""실연동 배치 영상 생성 — N편(기본 10편) 품질 검증용 (수동 실행 전용).

NUTTI_DRY_RUN=false + FAL_KEY 설정 상태에서 영상 백엔드(veo_fal)를 그대로 타고
실제 영상을 N편 연속 생성한다. **게시는 하지 않고** data/media 산출물만 남긴다.

검증 항목:
  - 자막 없음 (#81 negative_prompt 효과 라이브 재확인)
  - 캐릭터(흰 비숑) 일관성
  - 비트 간 목소리 일관성 (마지막 컷 드리프트)
  - 비트 사이 공백 없음 (대사 8초 채움)

대본은 claude 호출 없이 하드코딩한다(결정적·빠름·대본 인증 불필요) — 영상 단계 품질에 집중.
비용: 편당 약 $1.64(프레임 $0.04 + Veo 32초 $1.60). 10편 ≈ $16.4.
실 API를 호출하므로 CI/테스트에서 실행 금지.

사용법:
  ./.venv/Scripts/python.exe scripts/batch_video_test.py        # 10편
  ./.venv/Scripts/python.exe scripts/batch_video_test.py 3      # 앞 3편만
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VideoStudio
from nutti.models import Script

# Windows 콘솔(cp949)에서 한글·em dash·≈ 등 비ASCII 문자가 UnicodeEncodeError로
# 마지막 출력을 깨뜨리는 것을 막는다(영상 생성과 무관한 print 단계 실패 방지).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 대본 규칙(코드 주석 기준): 4비트(훅·핵심·팁·CTA) + 훅으로 시작(밋밋한 인사 금지)
# + 마지막 비트(CTA)에 브랜드명('Nutti'·'누띠') 언급 금지 + 비트당 약 40~48자(8초를 꽉 채워
# 비트 사이 공백 방지, 50자 이내). 다양한 주제로 실전 품질을 확인한다.
SCRIPTS = [
    Script(
        topic="강아지 간식 급여량, 제대로 알고 주기",
        body="강아지 간식, 대부분 잘못 주고 있다는 사실.",
        beats=[
            "강아지한테 간식 주는 법, 사실 열에 아홉은 잘못하고 있다는 거 아세요?",
            "하루에 주는 간식은 그 아이 하루 권장 칼로리의 십 퍼센트를 넘기면 절대 안 돼요.",
            "그래서 몸무게별 적정 급여량을 계산기에 한 번만 입력해두면 매번 고민할 필요가 없어요.",
            "지금 바로 프로필 링크의 간식 계산기로 우리 아이 딱 맞는 하루 급여량을 확인해보세요!",
        ],
    ),
    Script(
        topic="강아지 산책 적정 시간",
        body="산책, 오래 걷기만 하면 되는 게 아니에요.",
        beats=[
            "강아지 산책, 그냥 오래만 걸으면 된다고 생각하셨다면 그건 큰 오산이에요.",
            "견종과 나이에 따라 하루 적정 산책 시간이 두 배 넘게 차이가 난다는 거 아세요?",
            "소형견은 삼십 분, 대형견은 한 시간 이상 나눠 걷는 게 관절 건강에 훨씬 좋아요.",
            "우리 아이한테 딱 맞는 산책 시간, 지금 프로필 링크에서 바로 확인해보세요!",
        ],
    ),
    Script(
        topic="강아지 양치 습관 들이기",
        body="강아지 치석, 방치하면 큰일 나요.",
        beats=[
            "강아지 입냄새가 점점 심해진다면, 그건 치석이 쌓이고 있다는 위험 신호예요.",
            "세 살이 넘은 반려견 열 마리 중 여덟은 이미 치주 질환을 앓고 있다고 해요.",
            "매일은 어렵더라도 일주일에 세 번만 닦아줘도 치석을 충분히 막을 수 있어요.",
            "우리 아이 양치 습관, 오늘부터 프로필 링크의 가이드로 천천히 시작해보세요!",
        ],
    ),
    Script(
        topic="강아지 분리불안 줄이기",
        body="외출할 때마다 짖는 우리 아이.",
        beats=[
            "주인이 나갈 때마다 짖고 불안해하는 우리 아이, 그냥 두면 점점 심해져요.",
            "분리불안은 외출 직전에 과하게 인사해주는 습관이 오히려 키울 수 있어요.",
            "나갈 때와 들어올 때를 최대한 덤덤하게 대해주는 것만으로도 크게 좋아져요.",
            "우리 아이 분리불안 단계별 해결법, 지금 프로필 링크에서 확인해보세요!",
        ],
    ),
    Script(
        topic="강아지 여름 더위 관리",
        body="강아지는 사람보다 더위에 약해요.",
        beats=[
            "강아지는 땀샘이 거의 없어서 사람보다 더위에 훨씬 약하다는 사실 아세요?",
            "코가 짧은 단두종은 한여름 산책만으로도 열사병에 걸릴 위험이 매우 높아요.",
            "한낮 산책은 피하고 아침저녁 선선할 때, 물은 늘 시원하게 준비해주세요.",
            "우리 아이 여름나기 체크리스트, 지금 프로필 링크에서 받아보세요!",
        ],
    ),
    Script(
        topic="강아지 발톱 관리",
        body="발톱 방치하면 걸음걸이가 망가져요.",
        beats=[
            "강아지가 걸을 때 또각또각 소리가 난다면, 그건 발톱이 너무 길다는 뜻이에요.",
            "발톱을 방치하면 걸음걸이가 틀어지고 관절에까지 무리가 갈 수 있어요.",
            "혈관을 피해 이 주에 한 번, 끝부분만 조금씩 잘라주는 게 가장 안전해요.",
            "우리 아이 발톱 자르는 법, 지금 프로필 링크의 영상으로 따라 해보세요!",
        ],
    ),
    Script(
        topic="강아지 사료 올바른 보관법",
        body="사료, 잘못 보관하면 산패해요.",
        beats=[
            "대용량 사료가 저렴해서 쟁여두셨다면, 보관법부터 다시 확인하셔야 해요.",
            "개봉한 사료는 공기와 만나면 한 달이 지나기도 전에 지방이 산패하기 시작해요.",
            "원래 봉지째 밀폐 용기에 넣고 서늘하고 어두운 곳에 두는 게 가장 좋아요.",
            "우리 아이 사료 신선하게 지키는 법, 지금 프로필 링크에서 확인해보세요!",
        ],
    ),
    Script(
        topic="강아지 비만 자가 체크",
        body="우리 아이, 통통한 게 아니라 비만일 수도.",
        beats=[
            "우리 아이가 통통해서 귀엽다고요? 사실 비만은 만병의 시작일 수 있어요.",
            "갈비뼈를 만졌을 때 살짝만 느껴져야 정상, 전혀 안 만져지면 과체중이에요.",
            "위에서 봤을 때 허리 라인이 잘록하게 들어가는지도 꼭 함께 확인해보세요.",
            "우리 아이 적정 체중인지, 지금 프로필 링크의 체크 가이드로 확인해보세요!",
        ],
    ),
    Script(
        topic="강아지 귀 청소 주기",
        body="귀 냄새, 외이염 신호일 수 있어요.",
        beats=[
            "강아지 귀에서 꼬릿한 냄새가 난다면, 그건 외이염의 초기 신호일 수 있어요.",
            "특히 귀가 덮인 견종은 안이 습해져서 세균과 곰팡이가 자라기 쉬워요.",
            "면봉 대신 전용 세정제를 솜에 묻혀 보이는 곳만 부드럽게 닦아주세요.",
            "우리 아이 귀 건강 지키는 법, 지금 프로필 링크에서 확인해보세요!",
        ],
    ),
    Script(
        topic="강아지 숙면 환경 만들기",
        body="잠을 푹 못 자면 강아지도 예민해져요.",
        beats=[
            "강아지가 자꾸 예민하고 짖는다면, 잠을 푹 못 자고 있어서일 수 있어요.",
            "강아지는 하루 열두 시간 넘게 자야 하는데 시끄러우면 깊이 못 자요.",
            "잠자리는 사람 동선에서 살짝 벗어난, 조용하고 아늑한 구석이 가장 좋아요.",
            "우리 아이 숙면 잠자리 만드는 법, 지금 프로필 링크에서 확인해보세요!",
        ],
    ),
]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else len(SCRIPTS)
    scripts = SCRIPTS[:n]

    s = Settings()
    print(f"[설정] dry_run={s.dry_run}  backend={s.video_backend}  "
          f"fal={'O' if s.fal_key else 'X'}  편수={len(scripts)}")
    if s.dry_run:
        raise SystemExit("dry_run=True 상태 - .env에서 NUTTI_DRY_RUN=false로 바꿔야 실제 생성됩니다.")
    if not s.fal_key:
        raise SystemExit("FAL_KEY 없음 - .env에 FAL_KEY를 설정해야 합니다.")

    studio = VideoStudio(s)
    results: list[tuple[str, bool, str | None, float, float]] = []

    for i, sc in enumerate(scripts, 1):
        print(f"\n===== [{i}/{len(scripts)}] {sc.topic} =====")
        t0 = time.monotonic()
        try:
            asset = studio.produce(sc)
            dt = time.monotonic() - t0
            vp = asset.video_path
            size_mb = Path(vp).stat().st_size / 1_000_000 if vp and Path(vp).exists() else 0.0
            ok = size_mb > 0
            mark = "OK" if ok else "경고: 파일 없음"
            print(f"  [{mark}] {dt:.1f}s  길이={asset.duration_sec}s  "
                  f"{size_mb:.2f}MB  {vp}")
            results.append((sc.topic, ok, vp, dt, size_mb))
        except Exception as exc:  # noqa: BLE001 - 한 편 실패해도 나머지 계속
            dt = time.monotonic() - t0
            print(f"  [실패] {dt:.1f}s  {type(exc).__name__}: {exc}")
            results.append((sc.topic, False, None, dt, 0.0))

    ok_n = sum(1 for r in results if r[1])
    print("\n===== 배치 요약 =====")
    for topic, ok, vp, _dt, mb in results:
        mark = "OK" if ok else "X "
        print(f"  [{mark}] {topic[:26]:26} {mb:5.1f}MB  {vp or '-'}")
    print(f"\n성공 {ok_n}/{len(results)}  예상 비용 약 ${ok_n * 1.64:.2f}")
    print("산출물은 data/media/ 에 저장됨 — 자막/캐릭터/목소리/공백을 직접 확인하세요.")


if __name__ == "__main__":
    main()
