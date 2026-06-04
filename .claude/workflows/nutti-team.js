export const meta = {
  name: 'nutti-team',
  description: 'Autonomous role-specialized team for the Nutti project: plan -> research -> parallel build (one opus developer per disjoint-file workstream) -> review/fix loop -> QA -> report. The PO passes a directive via args.',
  whenToUse: 'When the product owner gives a feature/work directive and wants the team to implement (with feature-level parallelism), review, verify, and report it autonomously.',
  phases: [
    { title: 'Plan', model: 'opus' },
    { title: 'Research', model: 'sonnet' },
    { title: 'Build', model: 'opus' },
    { title: 'Review', model: 'sonnet' },
    { title: 'Fix', model: 'opus' },
    { title: 'QA', model: 'sonnet' },
    { title: 'Report', model: 'haiku' },
  ],
}

const directive =
  typeof args === 'string' && args.trim()
    ? args.trim()
    : '(directive not provided as args — read .omc/po-request.md for the PO request, else stop and report that no directive was given)'

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    goal: { type: 'string' },
    scope: { type: 'string' },
    shared_prep: {
      type: 'object',
      properties: { needed: { type: 'boolean' }, files: { type: 'array', items: { type: 'string' } }, what: { type: 'string' } },
      required: ['needed'],
    },
    workstreams: {
      type: 'array',
      description: 'Feature-level units with DISJOINT file ownership, each built by one developer in parallel.',
      items: { type: 'object', properties: { name: { type: 'string' }, files: { type: 'array', items: { type: 'string' } }, detail: { type: 'string' } }, required: ['name', 'files', 'detail'] },
    },
    acceptance_criteria: { type: 'array', items: { type: 'string' } },
    research_topics: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
  },
  required: ['goal', 'scope', 'workstreams', 'acceptance_criteria', 'research_topics'],
}
const RESEARCH_SCHEMA = { type: 'object', properties: { topic: { type: 'string' }, summary: { type: 'string' }, impl_notes: { type: 'string' }, sources: { type: 'array', items: { type: 'string' } }, uncertainties: { type: 'array', items: { type: 'string' } } }, required: ['topic', 'summary', 'impl_notes'] }
const BUILD_SCHEMA = { type: 'object', properties: { summary: { type: 'string' }, files_changed: { type: 'array', items: { type: 'string' } }, tests_added: { type: 'array', items: { type: 'string' } }, blocked: { type: 'array', items: { type: 'string' } } }, required: ['summary', 'files_changed'] }
const REVIEW_SCHEMA = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, file: { type: 'string' }, line: { type: 'number' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, kind: { type: 'string' }, description: { type: 'string' }, suggestion: { type: 'string' } }, required: ['title', 'file', 'severity', 'description'] } } }, required: ['findings'] }
const QA_SCHEMA = { type: 'object', properties: { verdict: { type: 'string', enum: ['PASS', 'FAIL'] }, evidence: { type: 'array', items: { type: 'string' } }, failures: { type: 'array', items: { type: 'string' } } }, required: ['verdict', 'evidence'] }
const REPORT_SCHEMA = { type: 'object', properties: { markdown: { type: 'string' } }, required: ['markdown'] }

// --- Phase 1: Plan ---
phase('Plan')
const plan = await agent(
  `You are the planner. The product owner's directive is:\n\n"${directive}"\n\nProduce a concrete, file-level implementation spec for the Nutti project. Split the work into WORKSTREAMS with DISJOINT file ownership so multiple developers can build them in parallel without conflict. If shared files (e.g. nutti/models.py, nutti/config.py) must change, put those in shared_prep (done once, first) and keep them OUT of any workstream's files. Aim for 1-4 workstreams; use 1 if the task is genuinely indivisible.`,
  { label: 'plan', phase: 'Plan', model: 'opus', agentType: 'nutti-planner', schema: PLAN_SCHEMA }
)

// --- Phase 2: Research (only if the plan flagged topics) ---
let research = []
const topics = (plan.research_topics || []).filter((t) => t && t.trim())
if (topics.length) {
  phase('Research')
  research = (await parallel(
    topics.map((t) => () =>
      agent(`Research this for the Nutti integration work: ${t}`, { label: `research:${t.slice(0, 24)}`, phase: 'Research', model: 'sonnet', agentType: 'nutti-researcher', schema: RESEARCH_SCHEMA })
    )
  )).filter(Boolean)
}

const planText = JSON.stringify(plan)
const researchText = research.length ? JSON.stringify(research) : '(none)'

// --- Phase 3: Build (shared prep first, then parallel developers per workstream) ---
phase('Build')
if (plan.shared_prep && plan.shared_prep.needed) {
  await agent(
    `You are the developer doing SHARED PREP first (sequential, before parallel feature work). Modify only the shared files ${JSON.stringify(plan.shared_prep.files || [])} so the parallel workstreams won't conflict. What: ${plan.shared_prep.what || ''}\nPLAN: ${planText}`,
    { label: 'build:shared-prep', phase: 'Build', model: 'opus', agentType: 'nutti-developer', schema: BUILD_SCHEMA }
  )
}

const workstreams = plan.workstreams && plan.workstreams.length ? plan.workstreams : [{ name: 'main', files: [], detail: plan.goal }]
const parallelMode = workstreams.length > 1

let builds
if (parallelMode) {
  // One opus developer per workstream, in parallel. Disjoint files; full pytest deferred to QA.
  builds = (await parallel(
    workstreams.map((ws) => () =>
      agent(
        `You are ONE of ${workstreams.length} developers running IN PARALLEL. Implement workstream "${ws.name}".\nTouch ONLY these files (+ their tests): ${JSON.stringify(ws.files)}. Do NOT modify other workstreams' files or the shared files (already prepped).\nDetail: ${ws.detail}\nRun \`ruff check\` on YOUR files. Do NOT block on the FULL pytest (siblings are editing the tree) — the QA stage runs the full suite after all developers finish. Add targeted tests for your code.\nPLAN: ${planText}\nRESEARCH: ${researchText}`,
        { label: `build:${ws.name}`.slice(0, 40), phase: 'Build', model: 'opus', agentType: 'nutti-developer', schema: BUILD_SCHEMA }
      )
    )
  )).filter(Boolean)
} else {
  // Single indivisible task — one developer runs the full ruff + pytest to green.
  const ws = workstreams[0]
  builds = [
    await agent(
      `You are the developer (solo). Implement workstream "${ws.name}". Files: ${JSON.stringify(ws.files)}. Detail: ${ws.detail}\nRun BOTH ruff and the full pytest until green.\nPLAN: ${planText}\nRESEARCH: ${researchText}\nPO DIRECTIVE: ${directive}`,
      { label: `build:${ws.name}`.slice(0, 40), phase: 'Build', model: 'opus', agentType: 'nutti-developer', schema: BUILD_SCHEMA }
    ),
  ]
}

// --- Phase 4-5: Review <-> Fix loop (bounded) ---
const allFindings = []
let round = 0
let open = []
do {
  round += 1
  const review = await agent(
    `You are the reviewer. Review ALL changes made for the directive "${directive}" (git diff origin/dev...HEAD). Pay special attention to integration points BETWEEN the parallel workstreams (e.g. a shared registry, imports, or a file one stream expected another to update). Focus on correctness, security, robustness, test adequacy.`,
    { label: `review:r${round}`, phase: 'Review', model: 'sonnet', agentType: 'nutti-reviewer', schema: REVIEW_SCHEMA }
  )
  open = (review.findings || []).filter((f) => f.severity === 'critical' || f.severity === 'high')
  allFindings.push(...(review.findings || []))
  if (open.length) {
    await agent(
      `You are the developer (solo fix). Fix these confirmed review findings, then run BOTH ruff and the full pytest until green:\n${JSON.stringify(open)}`,
      { label: `fix:r${round}`, phase: 'Fix', model: 'opus', agentType: 'nutti-developer', schema: BUILD_SCHEMA }
    )
  }
} while (open.length && round < 2)

// --- Phase 6: QA (runs the FULL suite over the joined result) ---
phase('QA')
const qa = await agent(
  `You are QA. The build may have run several developers in parallel — verify the JOINED result. Run ruff + the FULL pytest and any dry_run smoke. Check the directive "${directive}" against acceptance criteria: ${JSON.stringify(plan.acceptance_criteria)}. Report PASS/FAIL with evidence.`,
  { label: 'qa', phase: 'QA', model: 'sonnet', agentType: 'nutti-qa', schema: QA_SCHEMA }
)

// --- Phase 7: Report ---
phase('Report')
const report = await agent(
  `You are the reporter. Write a Korean 작업 보고 + 다음 Todo for what was just done for the directive "${directive}". Use git diff/log for evidence and incorporate the QA verdict: ${JSON.stringify(qa)}. Return markdown only (do not write to Notion).`,
  { label: 'report', phase: 'Report', model: 'haiku', agentType: 'nutti-reporter', schema: REPORT_SCHEMA }
)

log(`nutti-team done: workstreams=${workstreams.length} (parallel=${parallelMode}), review rounds=${round}, QA=${qa.verdict}`)

return {
  directive,
  plan,
  research,
  builds,
  review_findings: allFindings,
  qa,
  report_markdown: report.markdown,
}
