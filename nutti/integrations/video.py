"""영상 생성 연동: Hedra(캐릭터) · Seedance/Kling(씬) · AssemblyAI(자막).

실제 API 연동부는 TODO로 표시. dry_run에서는 더미 URL을 채워 파이프라인을 검증한다.
각 메서드의 시그니처/반환 형태는 실제 연동 시 그대로 유지하도록 설계했다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Script, VideoAsset

log = get_logger(__name__)


class VideoStudio:
    """대본 → 최종 영상 합성을 담당하는 파사드(facade)."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def produce(self, script: Script) -> VideoAsset:
        """캐릭터 영상 + 씬 영상 + 자막을 합성해 최종 영상을 만든다."""
        character = self._render_character(script)
        scenes = self._render_scenes(script)
        subtitle = self._generate_subtitles(character)
        final, preview = self._compose(character, scenes, subtitle)
        return VideoAsset(
            script_id=script.id,
            character_clip_url=character,
            scene_clip_urls=scenes,
            subtitle_url=subtitle,
            final_url=final,
            preview_url=preview,
            duration_sec=60.0,
        )

    def _render_character(self, script: Script) -> str:
        """Hedra Character-3: 고정 마스코트가 대본을 읽는 립싱크 영상."""
        if self.settings.dry_run:
            log.info("dry_run.hedra", script_id=script.id)
            return f"https://dryrun.local/hedra/{script.id}.mp4"
        # TODO: Hedra Character-3 API 호출 (settings.hedra_character_id 사용)
        raise NotImplementedError("Hedra Character-3 연동 미구현")

    def _render_scenes(self, script: Script) -> list[str]:
        """Seedance 2.0 / Kling 3.0: 배경 씬 영상."""
        if self.settings.dry_run:
            log.info("dry_run.seedance", script_id=script.id)
            return [f"https://dryrun.local/seedance/{script.id}_scene{i}.mp4" for i in range(2)]
        # TODO: Seedance 2.0 (기본) 또는 Kling 3.0(고화질) API 호출
        raise NotImplementedError("Seedance/Kling 연동 미구현")

    def _generate_subtitles(self, video_url: str) -> str:
        """AssemblyAI/Whisper: 자동 자막."""
        if self.settings.dry_run:
            return video_url.replace(".mp4", ".srt")
        # TODO: AssemblyAI API 호출
        raise NotImplementedError("AssemblyAI 연동 미구현")

    def _compose(self, character: str, scenes: list[str], subtitle: str) -> tuple[str, str]:
        """클립 합성 → (최종 URL, 미리보기 URL). 실제로는 ffmpeg/렌더 서비스."""
        if self.settings.dry_run:
            final = character.replace("hedra", "final")
            return final, final.replace(".mp4", "_preview.gif")
        # TODO: ffmpeg 등으로 캐릭터+씬+자막 합성
        raise NotImplementedError("영상 합성 미구현")
