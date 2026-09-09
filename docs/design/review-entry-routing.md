# Review entry routing

Issue: #57. Product direction confirmed during triage. Implementation design
selected by inspecting the existing Modules; not implemented or live-validated.

## Confirmed constraints

- Keep the upstream Matt Pocock skills unchanged. Review-Switch owns the control
  that routes review requests through the Bridge.
- Cover review requested directly and review requested by `implement`, for both
  Codex and Claude callers.
- Govern the bare `code-review` name as well as
  `mattpocock-skills:code-review`. This includes calls that resolve to Claude's
  built-in review: an explicit invocation of that entry is also redirected to
  Review-Switch. Confirmed by the maintainer during #57 triage.
- Once entered, the Bridge keeps its existing authority to resolve the Lane
  from caller arguments and the Machine Config.
- A Bridge startup failure or incomplete review must not pause or interrupt
  the `implement` workflow. The proposed mandatory stop before commit was
  rejected by the maintainer. Automatically fall back to the current agent
  performing the upstream Matt review, then continue the workflow. Clearly
  report the fallback and that the configured Bridge review did not complete.
- Close the observed entry-point gap with a concise, effective implementation.
  Exhaustive prevention of every possible bypass is not a goal. Do not add
  layers of enforcement to make the workflow infallible.

## Evidence and limits

- The #57 incident records a Codex caller reading the upstream review skill
  through a shell and spawning its own review subagents without calling the
  Bridge. That is outside the installed Claude `PreToolUse` / `Skill` matcher.
- Historical Claude cutover validation in brainstorming #126 proved the
  qualified plugin call was denied and redirected. It also recorded a bare
  `code-review` bypass, filed as brainstorming #127.
- Script-level comparisons of the August 11, 13, 14, and 24 implementations and
  the current hook all denied the qualified plugin call and returned no
  decision for bare-name, shell-read, and subagent-call inputs. These comparisons
  do not prove that a host dispatched those events to the hook.
- The current-session skill-read simulation selected the Matt plugin file. It
  was not a fresh end-to-end `implement` run and does not establish deterministic
  skill selection by either host.

## Implementation design

Keep the existing three Modules and their responsibilities:

| Module | Responsibility in this change |
| --- | --- |
| Adjudicator | Recognize the supported review entries and direct the caller to the Dispatcher. |
| Dispatcher | Complete the target, call the Bridge, interpret its result, and choose the approved fallback on operational failure. |
| Bridge | Prepare and deliver the review, resolve Machine Config, and return the existing result and Next Call. No routing-hook or fallback implementation belongs here. |

### One hook Interface, host event Adapters

Extend the existing `review-adjudicator.sh`. Its Interface remains host event JSON
on stdin, environment including `REVIEW_COORDINATOR`, and a decision or contextual
instruction on stdout. Tests invoke that same script Interface. Keep event
recognition private to the script; no host-specific router class, command-line
mode, or additional configuration vocabulary is needed.

Use the events that correspond to the actual loading paths:

- `PreToolUse` / `Skill`: deny both `mattpocock-skills:code-review` and
  `code-review`, pointing at the Dispatcher. Keep the existing permitted
  Dispatcher entry and coordinator standdown behavior.
- `PreToolUse` / `Bash` and `Read`: when a call explicitly reads the upstream
  review skill file, leave the read permitted and attach `additionalContext`
  telling the caller to use the Dispatcher for an initial review. This covers
  Codex's observed shell-read path and Claude's direct file-read path. Match
  the upstream skill's directory/file identity, not a personal home path or
  plugin version. Cover the literal file-read shapes observed in the incident
  and simulation; do not build a general shell or Python interpreter.
- `UserPromptSubmit`: recognize explicit `implement` and `code-review` skill
  invocations, including the qualified names, and attach the same routing
  instruction for the review step. This covers direct skill content supplied
  by the host without a later file-read tool call. It does not start a review
  before implementation and does not classify arbitrary prose as a review.

The shared instruction points at the existing Dispatcher file, located relative
to the installed hook's own repository, so Codex can read it without a `Skill`
tool. No copy of the Dispatcher's Bridge command, result rules, or model/config
resolution goes into the hook. If a coordinator owns the review, point to that
coordinator's existing instructions instead of introducing a competing route.
Reading a skill for inspection is not itself a request to execute a review.

The distinction between denial and context is intentional: a Claude Skill call
can execute the unwanted review, whereas a file read only loads instructions.
The latter must remain usable for inspection and for the approved fallback.
Do not make an allow decision that overrides other permission checks; attach
context and otherwise leave the host's normal permissions in place.

### The existing Dispatcher owns fallback

Make the Dispatcher usable inside either caller host. Its normal Bridge call,
argument precedence, receipt reporting, and Next Call handling stay together in
the existing skill. Recover a lost result first and keep the existing bounded
`run again` retry; add no new retry policy.

When the command is unavailable, fails, returns malformed data, or remains
operationally incomplete after those existing recovery/retry paths, announce
the reason and run the original Matt Code Review procedure in the current
agent. Preserve the review target and any already-returned Bridge findings.
Use the original procedure as a whole rather than writing another partial-axis
review protocol; distinguish that fallback report from any completed Bridge
reports. Continue the implementation workflow afterward.

Load the original review skill as a file from the installed skill catalog (or
reuse its already-loaded content), rather than invoking `Skill(code-review)`
again. The read hook's instruction explicitly allows the Dispatcher-selected
fallback to follow that original content. This avoids a redirect loop without
session state, temporary permission files, or reinstating sentinel tokens.
Never edit or vendor the upstream skill. If the original skill is unavailable
too, disclose that review could not run and continue without claiming success.

Operational incompleteness is not a review finding. A completed report that
finds defects, requests fixes, or retains a disagreement is processed by the
existing result rules; it is not rerun locally to obtain a different verdict.
This fallback concerns Code Review, not Document Review, whose axes the Matt
code-review skill does not provide. Coordinator-owned workflows retain their
own result handling.

### Installation and authority

Document registrations for both hosts that call the same repository-owned
script: the existing Claude settings location and Codex's hooks configuration.
Preserve unrelated hooks. Use the host's normal hook trust/loading procedure;
trust-state records alone are not proof of a loaded hook. No new installer
framework is needed. Original Matt plugin files stay byte-for-byte unchanged.

The domain glossary and ADR-0003 require an amendment: the Dispatcher is no
longer Claude-only, and the approved operational-failure fallback is an explicit
exception to the previous prohibition on in-session review. It is a caller
fallback, not a new Lane or a second Bridge protocol.

## Validation

- Exercise the Adjudicator through JSON stdin and stdout: both Skill names,
  explicit prompt entry, observed file-read shapes, unrelated inputs, and
  coordinator ownership. Vary plugin version and installation root.
- Verify the Dispatcher routes first and can load the original skill for
  fallback without being sent back to itself. Existing successful Bridge
  results and completed findings keep their established handling.
- In disposable repositories, run actual Codex and Claude direct-review and
  implement-to-review paths. Observe the hook, the Bridge call, its configured
  Lane, and the reported result. A mocked hook result or a preselected filename
  is not an end-to-end acceptance run.
- Exercise an unavailable Bridge and an incomplete result; verify disclosed
  original-skill fallback and workflow continuation, with no recursive dispatch.
  When only one Bridge axis completed, its findings must remain visible.
- Run existing hook, Dispatcher, and repository checks appropriate to the
  implementation. Add no tests for hypothetical evasion techniques.

## Deliberate limits and sources

There is no arbitrary-subagent surveillance, completion gate, new session state
machine, or enforcement against every shell spelling. Contextual routing is a
host-supported instruction, not an absolute prevention guarantee. The observed
paths must pass live acceptance before this design is called implemented.

Official event contracts consulted:

- [Codex tool coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage),
  [PreToolUse](https://learn.chatgpt.com/docs/hooks#pretooluse), and
  [UserPromptSubmit](https://learn.chatgpt.com/docs/hooks#userpromptsubmit).
- [Claude hook reference](https://code.claude.com/docs/en/hooks), including
  PreToolUse and UserPromptSubmit context output.
