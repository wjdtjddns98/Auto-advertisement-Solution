"""extend 400 본문 노출 진단 (수동 실행 전용, 유료 ~$1).

_produce_clips_veo 경로의 첫 extend가 HTTP 400을 내는 진짜 원인을 확정한다.
프로덕션 `_submit_body`는 400 본문을 redact하므로, 여기서는 첫 클립 generate URI를
받은 뒤 동일한 extend body를 raw POST해 status/body를 그대로 노출한다.
모델은 fast로 강제(.env가 lite여도 init 우선). 출력은 cp949 호환(에러 본문은 영어).
"""

from __future__ import annotations

from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import (
    _GEMINI_BASE,
    _VEO_NEGATIVE_PROMPT,
    VeoClient,
    VeoPromptBuilder,
    _gemini_headers,
    pick_episode_style,
)

BEATS = [
    "강아지 간식, 대부분 잘못 주고 있다는 거 아세요?",
    "하루 간식은 권장 칼로리의 10%를 넘기면 안 돼요.",
]


def main() -> None:
    s = Settings(NUTTI_VEO_MODEL="veo-3.1-fast-generate-preview")
    print(f"[설정] dry_run={s.dry_run}  model={s.veo_model}  "
          f"gemini={'O' if s.gemini_api_key else 'X'}")
    if s.dry_run:
        raise SystemExit("dry_run=True - .env NUTTI_DRY_RUN=false 필요")

    frames = sorted(
        Path(s.nutti_media_dir).glob("frame_*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not frames:
        raise SystemExit("재사용할 프레임 없음")
    frame = frames[0]
    print(f"[frame] {frame.name}  ({frame.stat().st_size/1_000_000:.2f} MB)")

    builder = VeoPromptBuilder()
    client = VeoClient(s)
    style = pick_episode_style("diag")
    try:
        print("[1] 첫 클립 generate URI 수신 (full4와 동일: build_beat + style)")
        uri = client._generate_uri(str(frame), builder.build_beat(BEATS[0], style=style))
        print(f"[uri] {uri!r}")

        params = {
            "aspectRatio": "9:16",
            "resolution": "720p",
            "negativePrompt": _VEO_NEGATIVE_PROMPT,
        }
        body = {
            "instances": [
                {"prompt": builder.build_extend_beat(BEATS[1]), "video": {"uri": uri}}
            ],
            "parameters": params,
        }
        url = f"{_GEMINI_BASE}/models/{s.veo_model}:predictLongRunning"
        print("[2] raw extend POST (본문 노출)")
        resp = client._client().post(url, headers=_gemini_headers(s), json=body)
        print(f"[status] {resp.status_code}")
        print(f"[body] {resp.text[:1800]}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
