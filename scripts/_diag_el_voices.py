"""1회 진단: ElevenLabs 아이 목소리 후보 A/B 샘플 합성(수동 실행 전용).

Starter 결제 확인(2026-06-12) 후 라이브러리 후보를 내 보이스에 추가하고 같은 대사로
샘플을 합성해 PO가 귀로 비교한다. 현재 기본(Jessica premade)도 대조군으로 포함.
산출물: data/media/el_sample_<이름>.mp3 — 마음에 드는 보이스의 id를
.env NUTTI_ELEVENLABS_VOICE_ID로 교체하면 적용된다.
"""
import sys
from pathlib import Path

import httpx

from nutti.config import Settings

SAMPLE_TEXT = "안녕! 우리 댕댕이가 제일 좋아하는 간식, 바로 누띠야. 매일 줘도 안심이지!"

# (표시이름, public_owner_id, voice_id) — 2026-06-12 shared-voices 검색 결과에서 선정.
CANDIDATES = [
    (
        "cherry_twinkle_cartoon_girl",
        "9678631cb37e3563db1009a63ef157907498ecd3622f3449de0cac253c6faaaf",
        "XJ2fW4ybq7HouelYYGcL",
    ),
    (
        "leo_energetic_kid",
        "b11ba57b5815bf861c2cb764605fd53a9544948008706505e87a2765ac4b5717",
        "1tDEBGOo8EqEPApM49eJ",
    ),
    (
        "jy_kculture_vlog_girl",
        "4cdd03c1070f72e8655549f6d5dee68fca7b917615dfa81af84f7f540ed0d25f",
        "bQlkYuipD5BHEhntA5iz",
    ),
]

s = Settings()
h = {"xi-api-key": s.elevenlabs_api_key}
out_dir = Path(s.nutti_media_dir)
out_dir.mkdir(parents=True, exist_ok=True)


def synth(c: httpx.Client, voice_id: str, label: str) -> None:
    """voice_id로 샘플 대사를 합성해 mp3로 저장한다."""
    r = c.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers=h,
        json={"text": SAMPLE_TEXT, "model_id": s.elevenlabs_model_id},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"[diag] synth FAIL {label} status={r.status_code} body={r.text[:200]}")
        return
    out = out_dir / f"el_sample_{label}.mp3"
    out.write_bytes(r.content)
    print(f"[diag] synth OK {label} -> {out} ({len(r.content)}B)")


with httpx.Client(timeout=60.0) as c:
    # 대조군: 현재 기본 보이스(.env NUTTI_ELEVENLABS_VOICE_ID, premade Jessica).
    synth(c, s.elevenlabs_voice_id, "current_default")
    for label, pub, vid in CANDIDATES:
        # 공유 보이스는 먼저 내 보이스로 추가해야 TTS 가능. 이미 추가됐으면 그대로 진행.
        add = c.post(
            f"https://api.elevenlabs.io/v1/voices/add/{pub}/{vid}",
            headers=h,
            json={"new_name": label},
        )
        if add.status_code == 200:
            my_vid = add.json().get("voice_id", vid)
            print(f"[diag] add OK {label} my_voice_id={my_vid}")
        else:
            # 이미 추가된 경우 등 — 원본 voice_id로 합성 시도.
            print(f"[diag] add status={add.status_code} {label} body={add.text[:150]}")
            my_vid = vid
        synth(c, my_vid, label)

print("[diag] 완료 - data/media/el_sample_*.mp3 를 들어보고 마음에 드는 보이스를 고르세요.")
sys.exit(0)
