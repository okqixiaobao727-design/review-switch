# Document Review

Date: 2026-08-31. Decided in a grilling session; the research behind the two axes is
`docs/research/spec-review-options.md`. Vocabulary is the Glossary's
(`brainstorming/review-switch/CONTEXT.md`: Document Review, Parent, requirements axis, design axis).

## What it is

A review of documents before any code exists: the spec `to-spec` wrote, or the tickets
`to-tickets` cut from one. It answers one question — *did to-spec / to-tickets execute their own
rules well on this codebase?* — along two axes, delivered exactly the way a Code Review is:

| Axis | Question | Report shape |
| --- | --- | --- |
| `requirements` | Do the documents carry their Parent, nothing the Parent never asked for, with acceptance criteria that verify what their own document claims? | matt's Spec-axis shape: (a) missing, (b) unasked-for, (c) looks covered but wrong |
| `design` | Do the technical decisions deepen the checkout's existing Modules, Seams, and ADRs? Judged in `codebase-design`'s vocabulary, which the reviewer loads rather than has paraphrased to it (ADR-0010). | (a) duplicate owner, (b) duplicate seam/interface, (c) contradicts ADR/glossary |

One rule for both objects: **a Parent named is the reference; no Parent means the documents are
held to each other.** A ticket set has a Parent by construction (its spec); a lone spec usually
has none.

Not in scope: a third "consistency" axis, a `ready / needs-decision` status machine, basis
fields, a root-cause cap, auto-editing documents, a code graph navigation block, and a separate
finding format. The Code Review contract already has a word limit, a quote rule, and a summary
line; Document Review adds nothing to it.

## Where it lives

Bridge preparation already sits behind a seam: a Lane takes an `AxisBrief` and reads
`preparation.report()`, and nothing below that point knows what a diff is
(`bridge/review_bridge.py`: `Lane` docstring, `axis_brief`, `preparation_report`,
`rounds_per_lineage`, `next_call`). Today that seam has one adapter, `ReviewPreparation`.
Document Review adds the second, `DocumentReviewPreparation`, which makes the seam real. The
delivery half — Lanes, SessionStore, cost, Lifecycle Hooks, Rounds Contract, Response, Next
Call, the JSON result, the Adjudicator — is not edited. Recovery gets one condition, below.

**What the reviewer loads, and what the brief quotes.** `to-spec` and `to-tickets` are
user-only skills (`disable-model-invocation: true`), so their rules reach the reviewer only as
text in the brief: the template baseline. `codebase-design` is model-invocable and installed for
both Lanes on this machine, so the design brief asks the reviewer to load it rather than
paraphrasing it. That is a dependency ADR-0003 removed and the Glossary's "no skill installed"
line denied; ADR-0010 reopens that one consequence for the design axis, and the Glossary's Axis
Brief entry says which brief requires what. A reviewer that cannot load the skill says so in the
first line of its report — the same discipline as an unfetched spec — and no path fallback,
argument, or resolve script exists for it.

**The Bridge stays one file.** `review-bridge` is installed as a single-file symlink (README;
asserted by `tests/test_bridge_command.py`) and the test harness loads that one file by path.
`DocumentReviewPreparation` and its two brief templates are one delimited section of
`review_bridge.py`, beside the Code Review preparation section. This is the decision, not a
deferral.

### The review-kind table

Five places currently hard-code Code Review; they become one table lookup:

| Kind | Axes (`--axis both`) | Rounds per lineage | Prepared by |
| --- | --- | --- | --- |
| `code` | `standards`, `spec` | `spec`: 2, `standards`: 1 | `prepare_review` (unchanged) |
| `documents` | `requirements`, `design` | `requirements`: 2, `design`: 2 | `prepare_document_review` |

The kind is `documents` when `--document` is given, else `code` — the same inference the Bridge
already makes when `--spec` is absent. Argument validation refuses a mix before any Lane opens:
`--document` with `--base` or `--spec`; a `code` axis name with `--document`; a `documents` axis
name without `--document`.

The five sites: `run_bridge` (scope resolution + `prepare_review` call), `requested_axes`,
`rounds_per_lineage`, the `--axis` choices in `build_parser`, and
`ReviewPreparation.axis_brief_text`'s axis dispatch (moves into the kind table so each adapter
owns only its own axes).

### Bridge Interface additions

```
--parent   <ref>        optional; issue reference or file path, same syntax as --spec
--document <ref>        repeatable, required for the documents kind
--axis requirements | design | both
```

`--base` is absent for the documents kind; no Review Scope is resolved. Every new option is
caller-owned and is echoed into `nextCall.argv` by the existing `caller_arguments` mechanism;
no Next Call code changes.

`DocumentReviewPreparation` reuses `read_spec` for the Parent and for every document — an
issue is fetched with `gh` and written beside the report files (ADR-0007), a path is read where
it lies, a failure is recorded — and reuses `read_standards_sources` for the design axis. Its
Interface is the same three methods a Lane already calls: `brief(axis)`, `briefs(axes)`,
`report()`.

Failure rules: a Parent that cannot be fetched is carried the way an unfetched spec is — the
brief names the reference and the failure, the receipt records it, the review runs. A document
that cannot be fetched fails preparation: the documents are the object of the review, and
without one there is nothing to hold to the Parent.

### The receipt

```
"preparation": {
  "parentSource":  "<path> | not provided | not fetched: <ref>",
  "parentFile":    "<path> | null",
  "parentFailure": "<detail> | null",
  "documents":     [{"source": "...", "file": "..."}],
  "standardsFiles": [...], "standardsCondition": "...",
  "codeGraphUsed": false,
  "responseFile":  "<path> | null"
}
```

Recovery today fills the legacy Code Review fields (`specFile`, `specFailure`, `responseFile`)
into whatever receipt it reads back, so a records-era receipt has every field. That fill is made
conditional on the receipt being a Code Review one (it carries `specSource`); a Document Review
receipt is returned as written. This is the one edit to the delivery half.

## The two Axis Briefs

Both begin with the environment line every brief carries, and both end with matt's report
sentence shape. Each carries a **template baseline** — the rules `to-spec` and `to-tickets` wrote
the documents under, drawn from mattpocock-skills v1.2.3 and condensed to *what it is → how to
fix*, the way the Standards brief condenses Fowler's smells — and one gate sentence in place of
a finding taxonomy. One baseline item is Document Review's own rather than a template's, and
says so: the no-Parent rule that a spec's sections are held to each other. Neither brief
mentions rounds.

### requirements

```
Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Parent: {parent_slot}
Documents:
{document_list}

Template baseline (the rules these documents were written under; every item is a judgement call; report only what would change what gets built — not wording, format, or hypothetical future needs):
- Vertical slice: a ticket cuts one complete path through every layer and is demoable or verifiable on its own. → a ticket that is one layer of several is recut.
- Blocking edges: a ticket is blocked only by the tickets that genuinely gate it. → an edge that does not gate is removed; a gate that is missing is added.
- Acceptance criteria: written from the user's perspective, each verifying the behaviour "What to build" claims. → a criterion that checks something else, or nothing observable, is rewritten.
- One context window: a ticket is sized to finish in a single fresh session. → split it.
- No parent (Document Review's own rule): a spec's Problem Statement, Solution, User Stories, Implementation Decisions, Testing Decisions, and Out of Scope are held to one another. → the section that contradicts the others is the finding.

Report: (a) requirements in the parent that the documents miss or carry only in part; (b) scope or decisions the parent never asked for; (c) acceptance criteria that do not verify what their own document claims. With no parent, hold the documents to each other. Quote the line for each finding. Under 400 words.
```

`{parent_slot}` is `<path> — <summary>. Read it before reviewing.` (the `SpecSlot` shape),
`not provided; hold the documents to each other.`, or the unfetched shape with its failure
detail. `{document_list}` is one `- <path>` line per document, with an issue's summary where
the Bridge wrote it out.

### design

```
Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Load the codebase-design skill from the mattpocock-skills plugin before reviewing and judge in its vocabulary; if you cannot load it, say so in the first line of your report.
Parent: {parent_slot}
Documents:
{document_list}
Standards sources: {standards_files}

Template baseline (the rules these documents were written under; every item is a judgement call; report only what would change what gets built — not wording, format, or hypothetical future needs):
- Seams: existing seams are preferred to new ones, the highest possible, and as few as possible. → a new seam says why no existing one serves.
- Testing decisions test external behaviour through the interface, not implementation. → a test plan that must cross the interface is a finding about the module's shape.
- The glossary's vocabulary and the ADRs are followed. → name the term or the ADR.
- No file paths or code snippets, except a prototype's snippet that carries a decision. → cut it.

Report: (a) a Module the design adds that an existing one already owns; (b) a Seam or Interface the design adds where the checkout already has one; (c) a decision that contradicts an ADR or the glossary. Name the code or ADR beside the quoted line for each finding. Under 400 words.
```

`Standards sources:` is the Code Review line reused: it names the tracked standards documents
including `docs/agents/*.md`, and this repository's `docs/agents/domain.md` is what points a
reviewer at the private glossary and ADRs.

## The Dispatcher

`/review-switch` stays the only skill. `SKILL.md` gains a three-line route before *Completing
the call*: when the caller asks to review documents — a spec, a ticket set, an issue's sub-issues,
a `crewtask/<n>/` run — rather than a change, read `references/document-review.md` to complete
the call; *Result* and everything after it apply unchanged. A Code Review agent reads those three
lines and nothing else of Document Review.

`references/document-review.md` holds only what is Document Review's:

- **References from prose.** A parent issue (`#11`, an issue URL) → `--parent '#11'` and one
  `--document` per open sub-issue, listed with `gh`. A `crewtask/<n>/` directory → `spec.md` as
  `--parent` and every ticket file as a `--document`. Explicit paths or issue numbers → passed as
  given. No parent named → no `--parent`. The Dispatcher locates references and never opens a
  document, as it never opens a spec today.
- **The preparation line.** `Parent: <parentSource> · Documents: <n> · Standards: <condition>`
  followed by one line per axis, as the Code Review summary is. A design report whose first line
  says the skill could not be loaded is relayed as such, the way an unfetched spec is.

## What does not change

Lanes and their dependencies · SessionStore, report and spec files · cost harvest · Lifecycle
Hooks and their variables · Rounds Contract mechanism · Response · Next Call · the Adjudicator ·
both Code Review briefs · the Code Review receipt · the install. Recovery changes by one
condition, above.

## Tests to add (edge cases)

Bridge, through the existing harness, at the preparation Interface and the argument layer:

1. `--document` with a parent issue, a parent file, and no parent → the Parent slot in each brief and `parentSource` in the receipt.
2. A parent that cannot be fetched → brief names reference and failure, `parentFailure` set, review proceeds.
3. A document that cannot be fetched → preparation fails before any Lane opens, `review-end` hook still fires.
4. `--document` with `--base` / `--spec`; a code axis with `--document`; `requirements` without `--document` → refused before any Lane opens.
5. `--axis both` under the documents kind → `requirements`, `design`, in that order.
6. Rounds: `design` earns one re-review; its Next Call `argv` echoes `--parent` and every `--document`.
7. A resume rebuilds `DocumentReviewPreparation` and appends the Response, as a Code Review resume does.
8. Recovery returns the documents receipt as written — no `specFile` / `specFailure` keys added — and a Code Review receipt with its legacy fields filled, as today.
9. No tracked standards documents → the design brief carries the existing `none documented` wording.
10. Code Review: every existing test unchanged and green — the proof the kind table changed nothing for the `code` kind.

Dispatcher: `skills/review-switch/tests/test-skill-contract.sh` gains the route lines and the
reference file.
