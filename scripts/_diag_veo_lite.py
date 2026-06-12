"""Veo 3.1 Lite 한국어 네이티브 발화 probe (수동 실행 전용, ~$0.44).

B안 검증(2026-06-12 설계): Kling+mux의 입-소리 이질감 폐기 후, Veo가 영상·음성을
동시 생성하는 경로를 재평가한다. 1비트(8초) 클립 하나로 다음을 PO 육안 확인:
  ① 한국어 대사를 한국어로 발화하는가 (영어 번역·왜곡 없이)
  ② 강아지 마스코트 입이 발화와 자연스럽게 동기화되는가 (이질감 해소 여부)
  ③ 화면에 깨진 한글 자막이 박히지 않는가 (negativePrompt 방어 확인)
  ④ 목소리 품질 (Supertone 포기를 감수할 수준인가)

비용: Veo 3.1 Lite 720p 8초 = $0.40. 시작 프레임은 기존 생성분(data/media/frame_*.png)
중 최신 파일을 재사용해 Gemini 이미지 호출(과금·ReadTimeout 재발 지점)을 건너뛴다.
실 API 과금이 있으므로 CI/테스트에서 실행 금지. GEMINI_API_KEY 크레딧 사용.
"""

from __future__ import annotations

import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VeoClient, VeoPromptBuilder

# 1비트만. 대사는 PR #46 대본 규칙(훅·45자 이내·브랜드명 없음).
DIALOGUE = "강아지 간식, 대부분 잘못 주고 있다는 거 아세요?"


def main() -> None:
    # init kwargs가 .env보다 우선이므로 .env 기본 모델을 Lite로 덮어쓴다.
    s = Settings(NUTTI_VEO_MODEL="veo-3.1-lite-generate-preview")
    print(f"[설정] dry_run={s.dry_run}  model={s.veo_model}  "
          f"gemini={'O' if s.gemini_api_key else 'X'}")
    if s.dry_run:
        raise SystemExit("dry_run=True 상태 — .env에서 NUTTI_DRY_RUN=false로 바꿔야 실제 생성됩니다.")

    frames = sorted(
        Path(s.nutti_media_dir).glob("frame_*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not frames:
        raise SystemExit(f"재사용할 프레임이 없습니다 — {s.nutti_media_dir}/frame_*.png 확인.")
    frame_path = str(frames[0])
    print(f"[프레임] 재사용: {frame_path}")

    prompt = VeoPromptBuilder().build_beat(DIALOGUE)
    print(f"[프롬프트] {prompt}")

    # Lite는 negativePrompt 미지원(실측 400: "isn't supported by this model") —
    # VeoClient.generate 대신 negativePrompt 없는 본문을 내부 메서드로 직접 제출한다.
    # 자막 억제는 프롬프트 본문의 'no on-screen text' + _NEGATIVE 문구가 1차 방어.
    import base64

    frame_bytes = Path(frame_path).read_bytes()
    body = {
        "instances": [
            {
                "prompt": prompt,
                "image": {
                    "bytesBase64Encoded": base64.b64encode(frame_bytes).decode("ascii"),
                    "mimeType": "image/png",
                },
            }
        ],
        "parameters": {"aspectRatio": "9:16"},
    }
    client = VeoClient(s)
    t0 = time.monotonic()
    try:
        op_name = client._submit_body(body)
        print(f"[제출] operation={op_name}")
        uri = client._poll(op_name)
        video_path = client._download(uri)
    finally:
        client.close()
    dt = time.monotonic() - t0

    print("\n===== 생성 완료 =====")
    print(f"  소요시간   : {dt:.1f}s")
    print(f"  영상       : {video_path}")
    if Path(video_path).exists():
        size_mb = Path(video_path).stat().st_size / 1_000_000
        print(f"  파일크기   : {size_mb:.2f} MB  (존재 확인 OK)")
    print("\n[확인 항목] ①한국어 발화(번역X) ②입-소리 동기화 ③자막 없음 ④목소리 품질")


if __name__ == "__main__":
    main()
