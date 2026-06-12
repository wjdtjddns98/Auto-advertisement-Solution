"""영상 백엔드 실연동 1편 생성 테스트 (수동 실행 전용).

NUTTI_DRY_RUN=false + GEMINI_API_KEY 설정 상태에서 .env의 NUTTI_VIDEO_BACKEND
경로를 그대로 타고 실제로 프레임→비트별 클립×N→ffmpeg 스티칭까지 돌려
산출물 경로/길이를 출력한다. 비용이 드는 실 API를 호출하므로 CI/테스트에서 실행 금지.

기본 백엔드 = veo(3.1 Lite): 비트당 8초, 네이티브 한국어 발화(별도 TTS 없음).
풀 4비트 기준 예상 비용 약 $1.60(720p $0.05/초) + 프레임 생성비.
"""

from __future__ import annotations

import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VideoStudio
from nutti.models import Script

# 풀 4비트 실테스트(PR #49 머지 후 검증 항목: 한국어 발화·비트 간 목소리 일관성·자막 없음).
# 대본은 PR #46 규칙을 따른다: 4비트(훅·핵심·팁·CTA) + 훅으로 시작(밋밋한 인사 금지)
# + 마지막 비트(CTA)에 브랜드명('Nutti'·'누띠') 언급 금지 + 비트당 45자 이내.
SCRIPT = Script(
    topic="강아지 간식 급여량, 제대로 알고 주기",
    body="강아지 간식, 대부분 잘못 주고 있다는 사실.",
    beats=[
        "강아지 간식, 대부분 잘못 주고 있다는 거 아세요?",
        "하루 간식은 권장 칼로리의 10%를 넘기면 안 돼요.",
        "몸무게별 적정량, 계산기로 3초면 확인할 수 있어요.",
        "프로필 링크 간식 계산기로 우리 아이 맞춤량을 확인하세요!",
    ],
)


def main() -> None:
    s = Settings()
    print(f"[설정] dry_run={s.dry_run}  backend={s.video_backend}  "
          f"fal={'O' if s.fal_key else 'X'}  gemini={'O' if s.gemini_api_key else 'X'}")
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
