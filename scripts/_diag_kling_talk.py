"""1회 진단 생성: Kling 프롬프트로 '말하는 입'이 어떻게 나오는지 확인(수동 실행 전용).

Kling LipSync가 강아지 얼굴을 인식하지 못해(2026-06-11, face_detection_error) 후처리
립싱크가 막힘 → 대안 후보: 생성 프롬프트 자체에 말하는 동작을 지시하고 TTS를 mux하는
방식. 기존 프레임을 재사용해 5초 클립 1개만 생성한다(비용 최소, ~$0.4).

평가 포인트(PO 육안 확인): 입 움직임이 자연스러운가 / 말하는 것처럼 보이는가 /
기존 무발화 프롬프트 대비 품질 저하(왜곡·치아 아티팩트 등)가 없는가.
"""
import sys
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video_kling import KlingClient

# 어제(6/11) 실테스트가 만든 마스코트 프레임 재사용. 인자로 다른 프레임 지정 가능.
FRAME = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/media/frame_306cd3987942.png")

# 기존 KlingPromptBuilder._MOTION 기조를 유지하되 _NO_SPEAK를 정반대로 뒤집은 변형:
# 마스코트가 카메라를 보고 활기차게 말하는 중(입 움직임 명시). 자막·추가 인물 금지는 동일.
TALK_PROMPT = (
    "A photorealistic dog mascot in a cozy warmly lit studio, looking at the camera and "
    "enthusiastically talking, its mouth clearly moving as if speaking, with natural lip and "
    "jaw movements, friendly expressive face, occasional small head gestures while talking. "
    "Camera: locked-off tripod, medium close-up, eye-level, no camera movement. "
    "Format: vertical 9:16. "
    "Strictly no additional animals, no people. Absolutely no text, subtitles, captions, "
    "letters, words, or writing anywhere in the frame."
)

s = Settings()
if s.dry_run:
    raise SystemExit("dry_run=True — .env에서 NUTTI_DRY_RUN=false여야 실제 생성됩니다.")
if not FRAME.exists():
    raise SystemExit(f"프레임 파일 없음: {FRAME}")

print(f"[diag] frame={FRAME.name}  model={s.kling_model}  duration=5s")
client = KlingClient(s)
try:
    path = client.generate(str(FRAME), TALK_PROMPT, 5)
finally:
    client.close()
size_mb = Path(path).stat().st_size / 1_000_000
# 주의: Windows 콘솔(cp949)에서 em-dash 등 일부 유니코드가 UnicodeEncodeError를 내므로 ASCII 구두점만 쓴다.
print(f"[diag] 생성 완료: {path} ({size_mb:.2f} MB) - 입 움직임을 육안으로 확인하세요.")
