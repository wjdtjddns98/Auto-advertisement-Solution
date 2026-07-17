"""저장소 계층: 대본/실행 기록 영속화."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data) -> None:
    """JSON을 원자적으로 쓴다(tmp 작성 후 os.replace).

    크래시·동시 실행 시 파일이 잘리거나 0바이트로 남지 않도록 한다.
    PipelineState·CostLedger가 공유하는 단일 구현.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        # 실패 시 임시 파일을 남기지 않는다.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
