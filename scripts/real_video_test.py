"""영상 백엔드 실연동 1편 생성 테스트 (수동 실행 전용).

NUTTI_DRY_RUN=false + FAL_KEY 설정 상태에서 영상 백엔드(veo_fal)를 그대로 타고
실제로 프레임→비트별 클립×N→ffmpeg 스티칭까지 돌려 산출물 경로/길이를 출력한다.
비용이 드는 실 API를 호출하므로 CI/테스트에서 실행 금지.

백엔드 = veo_fal(fal.ai Veo 3.1 Lite): 비트당 8초, 네이티브 한국어 발화(별도 TTS 없음).
풀 4비트 기준 예상 비용 약 $1.60(720p $0.05/초) + 프레임 생성비.
"""

from __future__ import annotations

import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VideoStudio
from nutti.models import Script

# 풀 4비트 실테스트(검증 항목: 한국어 발화·비트 간 목소리 일관성·자막 없음·비트 간 공백).
# 대본 규칙: 4비트(훅·핵심·팁·CTA) + 훅으로 시작(밋밋한 인사 금지) + 마지막 비트(CTA)에
# 브랜드명('Nutti'·'누띠') 언급 금지 + 비트당 40~48자(8초를 꽉 채워 비트 사이 공백 방지,
# 50자 이내 — 2026-06-16 PO 피드백: 비트 간 공백 줄이기).
SCRIPT = Script(
    topic="강아지 간식 급여량, 제대로 알고 주기",
    body="강아지 간식, 대부분 잘못 주고 있다는 사실.",
    beats=[
        "강아지한테 간식 주는 법, 사실 열에 아홉은 잘못하고 있다는 거 아세요?",
        "하루에 주는 간식은 그 아이 하루 권장 칼로리의 십 퍼센트를 넘기면 절대 안 돼요.",
        "그래서 몸무게별 적정 급여량을 계산기에 한 번만 입력해두면 매번 고민할 필요가 없어요.",
        "지금 바로 프로필 링크의 간식 계산기로 우리 아이 딱 맞는 하루 급여량을 확인해보세요!",
    ],
)


def main() -> None:
    s = Settings()
    print(f"[설정] dry_run={s.dry_run}  backend={s.video_backend}  "
          f"fal={'O' if s.fal_key else 'X'}")
    if s.dry_run:
        raise SystemExit("dry_run=True 상태 - .env에서 NUTTI_DRY_RUN=false로 바꿔야 실제 생성됩니다.")

    studio = VideoStudio(s)
    t0 = time.monotonic()
    asset = studio.produce(SCRIPT)
    dt = time.monotonic() - t0

    print("\n===== 생성 완료 =====")
    print(f"  소요시간   : {dt:.1f}s")
    print(f"  프레임     : {asset.frame_image_path}")
    print(f"  영상       : {asset.video_path}")
    print(f"  길이(초)   : {asset.duration_sec}")
    vp = asset.video_path
    if vp and Path(vp).exists():
        size_mb = Path(vp).stat().st_size / 1_000_000
        # 이모지 금지: Windows 콘솔(cp949)에서 UnicodeEncodeError로 마지막 줄이 깨진다(2026-06-12 실측).
        print(f"  파일크기   : {size_mb:.2f} MB  (존재 확인 OK)")
    else:
        print("  [경고] 영상 파일이 디스크에 없음 - 경로 확인 필요")


if __name__ == "__main__":
    main()
