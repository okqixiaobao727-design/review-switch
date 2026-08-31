# Document Review

Use this branch when the caller names documents rather than a change. Assemble the references
without opening the documents, make one Bridge call, then return to the Result section in
`SKILL.md`.

## Assemble the references

1. **Parent issue.** Pass the caller's issue reference unchanged to `--parent`. From the repository
   being reviewed, list its sub-issues with:

   ```bash
   gh api --paginate 'repos/{owner}/{repo}/issues/<n>/sub_issues' \
     --jq '.[] | select(.state == "open") | .number'
   ```

   Pass each returned number as one `--document '#<number>'`; that is the issue-reference syntax
   `--spec` accepts. If the endpoint is unavailable, read the Parent body with
   `gh api 'repos/{owner}/{repo}/issues/<n>' --jq .body` and follow
   `docs/agents/issue-tracker.md`: use each open task-list item as one issue reference. Read only
   the task-list entries needed to locate those references; the Parent body is routing metadata
   here, not review material.
2. **`crewtask/<n>/` run.** Pass its `spec.md` as `--parent`, then pass every ticket file in that
   directory as one `--document`. Locate the paths; leave their contents for the Lane.
3. **Explicit references.** Pass each path or issue number the caller named as one `--document`,
   unchanged.
4. **No Parent.** When the caller names no Parent, omit `--parent`.

Every named document must have exactly one `--document` when this step is complete.

## Complete the call

Use `requirements`, `design`, or `both` for the axis; use `both` when the caller leaves it open.
Make one call from the repository being reviewed, with no fixed point, spec, or session handle:

```bash
review-bridge --reviewer '<LANE>' \
  --parent '<PARENT>' \
  --document '<DOCUMENT>' --document '<DOCUMENT>' \
  --axis '<AXIS>' \
  <LIFECYCLE_HOOK_OPTIONS>
```

Omit the `--parent` line when there is no Parent, and repeat `--document` once per assembled
reference. Pass the caller's other Bridge options exactly as asked; an option they did not ask for
stays omitted. The configured Lane and lifecycle hooks keep the rules in `SKILL.md`.

The call is complete when every assembled reference appears once, the axis matches the caller's
ask, and no `--base` or `--spec` is present. Run it, then continue at Result in `SKILL.md`.

## What you write

Replace the Code Review preparation line with:

```markdown
Parent: <preparation.parentSource> · Documents: <n> · Standards: <preparation.standardsCondition>
```

Then write one line per returned axis using the Result rules in `SKILL.md`. If the first line of a
design report says the `codebase-design` skill could not be loaded, include that line verbatim in
the design-axis line.
