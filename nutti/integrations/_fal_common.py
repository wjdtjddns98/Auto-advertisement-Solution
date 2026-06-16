"""fal.ai 큐 REST 공통 헬퍼(여러 fal 백엔드가 공유).

image_kontext(FLUX.1 Kontext 프레임)·video_veo_fal(Veo 3.1) 등 fal.ai 큐 REST를
쓰는 클라이언트가 공유하는 상수·검증·헤더 헬퍼를 모은 모듈이다. 과거엔 이 심볼들이
video_kling.py(Kling 백엔드)에 있었으나, Kling 백엔드 제거 후에도 활성 fal 경로가
의존하므로 백엔드 중립 모듈로 추출했다.

저수준 공통 헬퍼(VideoRenderError·_HttpClosingMixin·_read_bytes·_guess_image_mime)는
video.py가 정의·사용하므로 여기서 재수출(re-export)만 한다 — fal 모듈은 이 모듈
하나에서 필요한 심볼을 모두 import한다.

계약:
- redaction: 검증 실패 메시지에 응답 본문·URL·식별자 원문 금지(형식·길이만 노출).
- API 응답값(request id·모델 경로·영상 URL)은 신뢰 불가 입력 → URL 삽입/다운로드 전 검증.
- 자격증명(Authorization: Key)은 queue.fal.run 요청에만 — CDN 다운로드엔 미첨부.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from nutti.config import Settings
from nutti.integrations.video import (
    VideoRenderError,
    _guess_image_mime,
    _HttpClosingMixin,
    _read_bytes,
)

# video.py에서 정의·사용하는 저수준 헬퍼를 fal 모듈이 이 모듈 하나로 가져오도록 재수출한다.
__all__ = [
    "_FAL_QUEUE_BASE",
    "_FAL_SAFE_HOSTS",
    "_MAX_TRANSIENT_RETRIES",
    "_RETRY_BACKOFF_SEC",
    "_fal_headers",
    "_guess_image_mime",
    "_validate_model_id",
    "_validate_request_id",
    "_read_bytes",
    "_validate_fal_video_url",
    "_HttpClosingMixin",
]

# fal.ai 큐 API 베이스. 제출/상태/결과 모두 이 호스트(자격증명 헤더는 여기에만 붙인다).
_FAL_QUEUE_BASE = "https://queue.fal.run"
# 결과 영상/이미지 다운로드를 허용하는 fal CDN 루트 호스트(신뢰 불가 응답 URL의 SSRF 방어).
# 검증은 `host == s or host.endswith(".{s}")`(suffix 매칭)이라 v3.fal.media 등 모든
# *.fal.media 서브도메인이 자동 포함된다(Kontext pro 결과 URL = v3.fal.media). 루트만 둬
# 허용 표면을 명확히 한다 — v3.fal.media를 따로 추가하면 중복(동작 동일)이라 두지 않는다.
_FAL_SAFE_HOSTS = frozenset({"fal.media", "fal.run"})
# fal request id 허용 형태(폴링 URL에 삽입 전 검증). 영숫자·`-`·`_`만 허용.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_REQUEST_ID_CHARS = 128
# 모델 경로(설정값) 허용 형태. fal 모델 id는 `fal-ai/.../image-to-video` 꼴.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_MODEL_ID_CHARS = 256
# 폴링 중 일시 오류(429/5xx) 최대 재시도와 backoff 기준(초). video.py와 동일 원칙.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0


def _fal_headers(settings: Settings) -> dict:
    """fal.ai 인증 헤더(`Authorization: Key <FAL_KEY>`). 큐 호스트 요청에만 붙인다.

    이 헤더는 자격증명이므로 **queue.fal.run 요청에만** 사용한다 — 결과 영상은
    fal CDN(fal.media)에서 키 없이 내려받으므로, CDN 요청에 키를 실으면 그 호스트의
    로그/중간자에게 키가 샌다(Gemini 키 격리와 동일 원칙).
    """
    return {"Authorization": f"Key {settings.fal_key}", "Content-Type": "application/json"}


def _validate_request_id(request_id: str) -> str:
    """fal request id가 폴링 URL에 안전하게 삽입 가능한 형태인지 검증한다.

    제출 응답의 request_id를 `{base}/{model}/requests/{id}/status` 등으로 이어
    붙이므로 신뢰 불가 입력으로 본다. 허용 문자(영숫자·`-`·`_`)·길이만 통과.
    """
    rid = (request_id or "").strip()
    if not rid or len(rid) > _MAX_REQUEST_ID_CHARS or not _REQUEST_ID_RE.match(rid):
        raise VideoRenderError(f"fal request id 형식이 올바르지 않습니다 (길이 {len(request_id or '')})")
    return rid


def _validate_model_id(model_id: str, *, env_name: str = "NUTTI_MODEL") -> str:
    """모델 경로(설정값)를 URL에 삽입하기 전에 형식을 검증한다.

    설정값이라 신뢰도는 높지만, 오설정으로 `:`·`?`·공백 등이 들어가면 요청 대상이
    변조될 수 있으므로 허용 문자(영숫자·`.`·`_`·`/`·`-`)·길이만 통과시킨다.
    여러 fal 모델 설정이 같은 검증을 공유하므로, 오류 메시지에 어느 설정인지
    `env_name`으로 구분해 노출한다.
    """
    mid = (model_id or "").strip().strip("/")
    if not mid or len(mid) > _MAX_MODEL_ID_CHARS or not _MODEL_ID_RE.match(mid):
        raise VideoRenderError(f"모델 id({env_name}) 형식이 올바르지 않습니다")
    return mid


def _validate_fal_video_url(url: str) -> None:
    """결과 영상 다운로드 URL이 허용된 fal CDN 호스트인지 검증한다(SSRF 방어).

    scheme=https + host가 _FAL_SAFE_HOSTS(또는 그 서브도메인)여야 한다.
    API 응답값(영상 URL)은 신뢰 불가 입력이므로 다운로드 전에 검증한다.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise VideoRenderError("fal 다운로드: 영상 URL scheme 불허 (허용: https)")
    host = (parsed.hostname or "").lower()
    if not any(host == s or host.endswith(f".{s}") for s in _FAL_SAFE_HOSTS):
        raise VideoRenderError("fal 다운로드: 영상 URL 호스트 불허 (허용: fal.media, fal.run)")
