# Review entry routing acceptance (#57)

Run on 2026-09-08 (Pacific/Auckland), from the issue-57 worktree based on
`623076585fc608be1b2a9874ad56310b711df15c`.

## Method

Disposable Git repositories contained `greet.py`, `spec.md`, and a short
`CODING_STANDARDS.md`. The baseline greeting omitted its required trailing `!`.
Direct-review fixtures had the one-line fix committed; implementation fixtures
started at baseline. Callers received the fixed point `baseline` and spec reference
`spec.md`, with no reviewer override. Actual Bridge receipts resolved `claude`,
`claude-opus-5`, and `medium`, all from Machine Config.

Host versions: Codex CLI `0.153.4`; Claude Code `2.1.263`. Codex caller model was
`gpt-6-astra`; Claude caller transcripts identified `claude-fable-5-1`.
The reviewer model came from the Bridge receipt, independently of the caller.

Session-only registrations used the README event/matcher shape and an observing
wrapper around the worktree's Adjudicator. The wrapper recorded the original host
input, script output, and exit code, and forwarded output unchanged. Existing user
hooks were retained; no global registration was replaced. Claude used `--settings`
and its normal print-mode loading. Codex used inline hook config and the documented
`--dangerously-bypass-hook-trust` automation option for these locally inspected
scripts. These runs prove delivery and caller behavior, not interactive `/hooks`
trust acceptance; normal installation/trust instructions are in the README.

The run's local artifact directory is named `review-switch-57-qh8zqe7b`; it holds
`run.py`, host JSONL transcripts, `hooks.jsonl`, fault fixtures, and upstream hashes.
Caller and Bridge session IDs below are the evidence index; raw machine transcripts
are not published in this repository.

## Successful entry paths

| Caller entry | Observed delivery and outcome | Caller session / Bridge reports (Standards, Spec) |
| --- | --- | --- |
| Codex `$mattpocock-skills:code-review` | UserPromptSubmit context, worktree Dispatcher read, Bridge call, both reports returned with zero findings. | `01a07e28-250b-7a40-b125-34ce9c0f5973`; `f07b7c98-2015-4faf-9afb-b2aaf45975cf`, `80dd68ab-c934-45f9-b7db-42b4144df163` |
| Codex `$mattpocock-skills:implement` | Prompt context; implementation and checks, Bridge review, both reports returned, commit `2f6a152`. | `01a07e28-a0df-7780-88f2-95927a224eb1`; `7cab6ab1-1f90-4280-bed9-66dfdfe6b7f6`, `b67f93a7-c08f-4e99-bac9-f49ad99207a0` |
| Codex original review-file read | Prompt contained no explicit slash/dollar invocation. Caller located and read the installed original with `cat`; PreToolUse/Bash supplied context, then Dispatcher and Bridge returned both reports. | `01a07e2a-9239-7551-a1ef-032deed1a0e8`; `b062aead-cee5-4650-a0e4-1876ae52a1f5`, `1c11103f-0e7d-49c1-b1c0-7e8d734f0c39` |
| Claude native `/code-review` | Prompt context redirected the built-in entry to the Dispatcher and Bridge; both reports returned with zero findings. | `cdbb802e-bdfd-4ab6-a75b-c8033a211a4f`; `ca6a8f88-c262-4a87-9af9-b408bc341c5e`, `4339e40f-cc47-4c06-901f-f7611e21fbc1` |
| Claude native `/mattpocock-skills:implement` | Prompt context, actual implement workflow, Bridge review. Findings were fixed with the existing single Spec re-review; commit `b479c2a`. | `678ff08c-0b8c-49a3-afb9-aa8d75b42e5f`; `1eccd9b4-768b-4168-b340-b73c30e5a6cf`, `8bd93c0a-1d39-496d-9ba6-ef62fd9f7aad` |

An earlier Claude dollar-prefixed implement request made a model Skill call which
upstream refused (`disable-model-invocation`). The caller completed the plain task
and routed review correctly, but that was not counted as native implement evidence;
the slash invocation above supplies that evidence.

## Operational failure

- Codex session `01a07e29-e4de-7b33-8f02-b4950f9e46d5`: a session-local executable
  returned exit 127 with `review-bridge: command not found (acceptance injection)`.
  The caller disclosed the failure, read the unchanged Matt skill, ran both original
  axes as Codex subagents against the uncommitted working tree, reported zero findings,
  and committed `f517c1d`. No fallback approval or recursive Dispatcher call occurred.
- Claude session `98454224-a217-4d2d-a2e1-9a82056553dc`: a session-local Bridge double
  returned a completed Standards report with fixture finding F-57 and a failed Spec
  axis. It supplied the exact single-axis Next Call and returned the same failure on
  retry. The caller made one Spec retry, retained and addressed F-57, read the original
  skill, ran both fallback axes, separated their reports from Bridge results, and
  committed `54211bf`. The native implement invocation reported the fallback host
  explicitly as Claude. These were injected results, not actual Claude Lane findings.

- Inspection-only Claude session `07fc6bdd-7284-4d2e-b9d4-dc8899252f6a` read the
  original skill using literal `cat`, received PreToolUse context, returned its
  headings, and made no Bridge or review-agent call.
- SHA-256 checks of all four installed Claude/Codex Matt implement and code-review
  files before and after acceptance were identical.

The original Matt procedure is loaded intact. The Dispatcher supplies the existing
working-tree Review Scope for its diff/empty check so pre-commit changes remain in
scope. No upstream procedure was copied into Review-Switch.

## Automated checks and boundaries

The hook suite exercises JSON stdin/stdout, both Skill names, coordinator ownership,
explicit prompts, literal reads, unrelated calls, and changed installation paths.
Passed: 45 Adjudicator cases, Dispatcher contract checks, 22 switcher cases,
repository lint, Bash syntax, and `git diff --check`. The full Python suite passed:
466 tests and 86 subtests (245.96 seconds). This is targeted routing coverage; arbitrary shell evaluation
and subagent interception are deliberately outside the design.

The private domain glossary and ADR-0003 already contain the #57 amendment recording
both caller hosts and the operational-failure exception. They remain in their owning
private repository rather than being copied here.

## Review follow-up checks

- Native `/review-switch` fallback: Claude session
  `58e7a11c-c1ec-4633-ad88-e2cf3e6902b7` loaded the worktree skill via a project
  symlink, executed Git scope commands and two `Agent` calls, and returned both
  fallback reports labelled Claude. Its `allowed-tools` frontmatter was unchanged.
  [Claude documents this field as permission grants, not a tool restriction](https://code.claude.com/docs/en/skills#pre-approve-tools-for-a-skill).
  This one test used `--setting-sources project,local` and the installed Matt plugin
  via `--plugin-dir`: the personal Dispatcher otherwise takes precedence and loads
  the old main-checkout skill. That initial old-skill run was excluded from acceptance.
- New marketplace Read/Bash cases and a missing-Dispatcher-directory case pass
  through the JSON Interface. Dispatcher-path expectations now come from the test
  repository root and a literal relocated fixture path, independently of the hook.
- A supplemental Codex marketplace call was rejected before execution by the
  service: the selected model required a newer CLI. Earlier Codex caller successes
  above stand as recorded; this additional attempt is not counted as acceptance.
- Marketplace live read: Claude session `c55dd6b3-a39d-4769-be26-da7c17746b1e`
  read the marketplace skill, received Bash hook context, and entered the Dispatcher
  and real Bridge. Both reports completed with zero findings: Standards
  `79a47931-bfe0-4365-aff4-553c820f806d`, Spec `de475553-085d-4df3-82af-ee1674a30830`.

## Implementation review disposition

Both axes ran through the Claude Lane. Standards findings were fixed or explicitly
declined for simplicity; no extra Standards round was requested. The single Spec
re-review closed all five original findings, including the incorrect `allowed-tools`
assumption, and identified one regression introduced by guarding path resolution:
a missing Dispatcher directory had begun allowing the governed Skill calls.

That regression was fixed after reproducing both names as failing JSON cases.
Directory resolution now happens only for context responses; Skill denial remains
independent of installation completeness. All 45 cases pass, including both denied
names and permitted Dispatcher entry with the sibling directory absent. This final
fix was locally validated, not subjected to a third review round. The final reviewer
receipt is `Retained: 0; New: 1`, with `next: escalate`; it is not a clean re-review
verdict. The accepted new finding is addressed by the final regression fix.
