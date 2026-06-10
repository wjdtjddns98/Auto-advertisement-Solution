---
name: nutti-day
description: 아침 풀오토 루프 — PO가 지시(한 건 또는 여러 건)를 주면 리드가 작업별로 [팀 빌드 → 검증 → 브랜치 → 커밋 → PR(base dev) → CI 감시 → 머지]를 끝까지 무인 수행하고 종합 보고. 트리거 — "아침 지시", "오늘 할 일", "풀오토", "하루 돌려", "/nutti-day", 또는 PO가 작업 목록을 주며 끝까지 알아서 하라고 할 때.
---

# nutti-day — 아침 지시 → 전 공정 자동 루프

당신(메인 세션)은 **리드**다. PO는 지시만 주고 자리를 비운다. 중간 질문으로 멈추지 말 것 —
합리적 추정으로 끝까지 진행하고, 모든 판단은 마지막 종합 보고에 기록한다.

## 0. 지시 파싱
- PO 메시지를 **작업 단위**로 쪼갠다(줄·불릿·번호 = 작업 1건). 한 줄이면 1건.
- 작업별 PO 옵션을 그대로 워크플로우 args에 전달: `opus`(개발자 상향) · `lite`(리서치·보고 생략).
- 비개발 지시(미팅·단가 등)는 자동화 대상 아님 — 보고에 "수동 처리 필요"로 분류만 한다.

## 1. 프리플라이트 (하루 1회)
```
git status --short          # 기존 dirty 파일 확인 — 절대 임의 커밋/리셋하지 말고 목록만 기억
git checkout dev && git pull
```
- 기존 working-tree 변경(예: 진행 중 WIP)은 **건드리지도, 스테이징하지도 않는다.**
- dirty 파일 때문에 `git checkout dev`가 실패하면 **stash·force 없이 중단**하고 충돌 내용을 보고한다.
- `gh` 경로: `/c/Program Files/GitHub CLI/gh.exe`.

## 2. 작업별 루프 (순차 — 작업 N건이면 N회 반복)
1. **팀 빌드**: `Workflow({ name: 'nutti-team', args: '<작업 지시문(+옵션)>' })`
   - 반환: `plan / builds / confirmed_findings / qa / report_markdown / git.{branch_name, commit_title}`
   - `error` 반환 시: 1회 재시도 → 그래도 실패면 이 작업은 SKIP으로 기록하고 다음 작업.
2. **리드 최종 검증**: `./.venv/Scripts/python.exe -m ruff check .` + `-m pytest -q`
   - 둘 다 green이어야 정상 트랙. 샌드박스 네트워크 차단성 실패(localhost connection refused)는
     env-artifact — CI를 게이트로 삼고 진행하되 보고에 명시.
3. **브랜치·커밋**: `git checkout -b <git.branch_name>` (충돌 시 `-2` 등 접미사)
   - **`builds` 배열의 모든 항목의 `files_changed`를 합산**해 그 파일들+테스트만 `git add`
     (프리플라이트의 기존 dirty 파일 제외).
   - 커밋: `<git.commit_title>` + 본문 요약 + 트레일러
     `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
4. **PR**: `git push -u origin <branch>` → `gh pr create --base dev` (본문 = report_markdown 요약).
   - QA가 최종 FAIL인 작업은 **`--draft`로만** 올리고 머지하지 않는다(작업물 보존 목적).
5. **CI 감시**: `gh pr checks <n> --watch` (또는 폴링). 빨간 체크 →
   `Agent(nutti-developer, model: sonnet)`에 실패 로그를 줘 수정 → 커밋·푸시 → 재감시. **최대 2회**,
   그래도 red면 draft 전환 + 보고에 블로커 기록 후 다음 작업.
6. **머지(dev)**: QA PASS + CI green + 팀 내부 적대적 리뷰에서 high/critical 0건(수정·재검증 완료)
   이면 자동 머지(`gh pr merge --squash --delete-branch`). 리뷰어가 PR에 추가 지적을 남긴 경우엔
   수정 후 **같은 리뷰어 재승인 없이는 머지 금지.** 자기 PR `--approve` 금지(코멘트만).
7. **정리**: `git checkout dev && git pull` → 다음 작업으로.

## 3. 종합 보고 (마지막에 1회)
작업별 표: 지시 → 결과(머지됨/draft/SKIP) → PR 링크 → QA/CI 증거 → 블로커.
이어서 `nutti-reporter`(haiku) 형식의 한국어 보고 + 다음 Todo. PO가 평소 Notion 기록을 원했으면
일일 업무보고 DB에도 기록.

## 절대 금지 (가드레일)
- main/dev **직접 push 금지** — 항상 브랜치+PR. dev→main 릴리스는 PO 명시 승인 필수.
- `git reset --hard`·`git checkout -f/--force`·`git stash`(타인 WIP)·force-push·기존 dirty 파일 커밋 금지.
  테스트 약화/삭제로 green 만들기 금지.
- 같은 작업에 무한 재시도 금지 — 재시도 한도 소진 시 기록하고 **다음 작업으로 전진.**
- `.env`·시크릿 열람/출력 금지.

## 중단 조건
- 모든 작업 처리 완료(머지/draft/SKIP 어느 쪽이든 결론) + 종합 보고 출력 = 종료.
- PO가 "stop"/"중단"이라 하면 즉시 현재 상태 보고 후 종료.
