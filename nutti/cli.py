"""Nutti CLI. N8n 스케줄러나 수동 실행에서 호출하는 진입점.

예)
    nutti run "강아지 닭가슴살 간식, 하루 적정량은?"
    nutti run "..." --reels
    nutti config
"""

from __future__ import annotations

import typer

from nutti.config import get_settings
from nutti.logging import configure_logging
from nutti.models import ContentFormat
from nutti.pipeline.orchestrator import GateRejected, Orchestrator

app = typer.Typer(help="Nutti 애견간식 콘텐츠 자동화 파이프라인")


@app.command()
def run(
    topic: str = typer.Argument(..., help="대본 주제"),
    reels: bool = typer.Option(False, "--reels", help="인스타 릴스도 함께 업로드"),
    feedback: str = typer.Option("", "--feedback", help="이전 사이클 개선 포인트"),
) -> None:
    """주제 하나로 대본→영상→업로드까지 한 사이클 실행."""
    settings = get_settings()
    configure_logging(settings.log_level)
    fmt = ContentFormat.REELS if reels else ContentFormat.SHORTS

    orchestrator = Orchestrator(settings)
    try:
        result = orchestrator.run(topic, content_format=fmt, feedback=feedback)
    except GateRejected as exc:
        typer.secho(f"검수 중단: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=2) from exc

    typer.secho(f"완료: run={result.id}", fg=typer.colors.GREEN)
    for up in result.uploads:
        typer.echo(f"  - {up.platform}: {up.url}")

    analysis = orchestrator.collect_and_analyze(result)
    typer.echo(f"\n[성과 분석]\n{analysis}")


@app.command()
def config() -> None:
    """현재 설정 요약 출력(민감정보는 마스킹)."""
    s = get_settings()
    typer.echo(f"env={s.env}  dry_run={s.dry_run}  model={s.script_model}")
    typer.echo(f"anthropic_key={'설정됨' if s.anthropic_api_key else '미설정'}")
    typer.echo(f"telegram={'설정됨' if s.telegram_bot_token else '미설정'}")
    typer.echo(f"calculator_url={s.calculator_url}")


if __name__ == "__main__":
    app()
