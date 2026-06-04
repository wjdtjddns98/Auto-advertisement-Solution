export const meta = {
  name: 'nutti-review',
  description: 'Adversarial code review using the nutti-reviewer agent: dimensions -> refute-verify -> confirmed findings',
  whenToUse: 'Review the current branch diff (or a given target) for the Nutti pipeline before merging.',
  phases: [
    { title: 'Review', model: 'sonnet' },
    { title: 'Verify', model: 'haiku' },
  ],
}

// args: optional string describing WHAT to review (files, branch, or focus).
// Defaults to "the diff of the current branch vs origin/dev".
const target =
  typeof args === 'string' && args.trim()
    ? args.trim()
    : 'the changes on the current git branch (diff vs origin/dev). Run `git diff origin/dev...HEAD --stat` and read the changed files.'

const CTX = `Review target: ${target}

You are reviewing the Nutti pipeline. Read the actual changed files. Honor the Nutti
contract (dry_run determinism, the intentionally-sync TelegramGate) and do not re-flag the
known-correct patterns in your agent instructions. Find REAL defects only.`

const DIMENSIONS = [
  { key: 'correctness', focus: 'logic defects, edge cases, parsing, loop bounds, fact-check wiring, offset handling' },
  { key: 'security', focus: 'token/secret leakage, Telegram callback authorization (chat.id only), injection, unsafe deserialization, prompt injection' },
  { key: 'robustness', focus: 'unhandled exceptions, transient vs permanent error handling, atomic writes, concurrency, resource leaks' },
  { key: 'test-adequacy', focus: 'do new tests pin the behavior (fail if reverted)? bypassing fakes, weak assertions, uncovered live paths' },
]

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'number' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          kind: { type: 'string', enum: ['unresolved', 'new-regression', 'test-gap'] },
          description: { type: 'string' },
          suggestion: { type: 'string' },
        },
        required: ['title', 'file', 'severity', 'kind', 'description'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    isReal: { type: 'boolean' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    reasoning: { type: 'string' },
  },
  required: ['isReal', 'reasoning'],
}

log('nutti-review: nutti-reviewer agents (sonnet) -> refute-verify (haiku)')

const results = await pipeline(
  DIMENSIONS,
  (d) =>
    agent(
      `${CTX}\n\nReview DIMENSION: ${d.key}.\nFocus: ${d.focus}\n\nReport concrete findings with file:line and kind. Empty findings if sound. No style nitpicks.`,
      { label: `review:${d.key}`, phase: 'Review', model: 'sonnet', agentType: 'nutti-reviewer', schema: FINDINGS_SCHEMA }
    ),
  (review, dim) =>
    parallel(
      (review.findings || []).map((f) => () =>
        agent(
          `${CTX}\n\nAdversarially VERIFY (try to REFUTE) one ${dim.key} finding. Read ${f.file}.\nFinding [${f.kind}]: "${f.title}" at ${f.file}:${f.line || '?'} — ${f.description}\n\nIs it REAL and actionable, not intended design, not already-correct? Default isReal=false if uncertain. Give reasoning + corrected severity.`,
          { label: `verify:${dim.key}`, phase: 'Verify', model: 'haiku', agentType: 'nutti-reviewer', schema: VERDICT_SCHEMA }
        ).then((v) => ({ ...f, dimension: dim.key, verdict: v }))
      )
    )
)

const all = results.flat().filter(Boolean)
const confirmed = all.filter((f) => f.verdict && f.verdict.isReal)
const rank = { critical: 0, high: 1, medium: 2, low: 3 }
confirmed.sort((a, b) => (rank[a.verdict.severity] ?? 9) - (rank[b.verdict.severity] ?? 9))
log(`nutti-review done: ${all.length} raw, ${confirmed.length} confirmed`)

return {
  rawCount: all.length,
  confirmedCount: confirmed.length,
  verdict: confirmed.length === 0 ? 'CLEAN' : 'ISSUES FOUND',
  confirmed: confirmed.map((f) => ({
    severity: f.verdict.severity,
    kind: f.kind,
    title: f.title,
    file: f.file,
    line: f.line,
    description: f.description,
    suggestion: f.suggestion,
    why_real: f.verdict.reasoning,
  })),
}
