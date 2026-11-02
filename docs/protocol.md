# Protocol v0.3.0

## Summary proposal

```json
{
  "ref": "experiment:retrieval-dev",
  "kind": "experiment",
  "title": "Development retrieval evaluation",
  "description": "Compare two fixed methods on development queries",
  "task": "issue:context-research-demo-1",
  "state": "executed",
  "review_status": "unreviewed",
  "sections": {
    "observations": "Actual measured results, with conditions.",
    "limitations": "Small fixture; holdout not evaluated yet.",
    "next_steps": "Reproduce dev and evaluate holdout."
  },
  "sources": [],
  "evidence": [],
  "relations": [{"type": "task", "ref": "issue:context-research-demo-1"}]
}
```

Replace `sources` with descriptors returned by `source REF`; an empty list is
rejected. Evidence entries use `source`, `start`, `end` (inclusive 1-based lines)
and optional `sections`. A reviewed status additionally requires `review_evidence`.
For updates add `expected_revision` from the current card. Registration and
proposals can be generated with Python; do not copy hashes by hand.

Prefer `draft REF --kind experiment --task ISSUE_REF --sources SOURCE_REF ...
--output proposal.json` to get a correctly bound starting document. Fill its
description, sections and evidence before submitting. Draft refuses to overwrite
an existing file and automatically includes the current expected revision.

Types: project, repository, issue, module, file, commit, pr, experiment, handoff.
They share a protocol; selection and source import differ by object. v0.1 provides
Git-file, local-artifact and Forgejo Issue/comment importers. PR diff and external
paper parsing can be registered as explicit evidence; automatic PR/paper import
is not part of this release.

## Store

```text
research-context/
  objects/*.md       # authoritative summary metadata + generated readable prose
  sources/*.json     # most recently synchronized source descriptors
  blobs/<hash>.txt   # immutable evidence text
  tasks/*.json      # supervisor-bound task -> original Issue identity
  .runtime/         # locks, logs, full snapshots and frozen delta pages; not published
```

Historical objects remain in Git commits. To audit a prior publication, use a
separate clone/worktree at the recorded context commit and run the same read
commands. Do not reset a live publisher or an agent's source worktree.

## Native live lifecycle

TUI Start -> normal engine provisions the run -> trusted backend configuration and
assigned repositories are frozen -> static tool usage instructions and eight
context tools become available -> the agent chooses an assigned Issue through the
existing Forgejo tools -> `context_select(repository, issue)` selects its context
scope -> the agent reads raw requirements, discovers summaries and opens evidence
on demand -> worker publishes a stage immediately -> peer checks changes and
reads the result during its own run.

No original Issue text, task summary, snapshot or delta is injected at startup.
There is no context backend call at startup, so an unavailable backend does not
block ordinary native startup. Explicit tool calls can fail visibly if their
backend is unavailable. The system makes no additional model calls to assemble
or summarize task context.

### Selection and authority

`context_select` requires a repository and positive Issue number. The supervisor
checks the selected repository against this run's assigned repositories in
`forgejo.json`, synchronizes the Issue through that agent's Forgejo identity, and
resolves its stable task identity. Selection is not Issue claiming, assignment or
an indication that requirements have been read. It returns compact references,
including `task` and `issue_source`; it does not return the full requirement or
summary. Assignment is managed separately in Forgejo; the current toolset does not provide self-assignment.

The other seven context tools require a selection and return `task_required`
otherwise. `context_task()` optionally assembles a bounded overview. It replaces
the old native `context_startup` name; it is not a required precondition for
`context_discover`, `context_open`, `context_changes` or writing stage results.

The published `tasks/*.json` mapping binds a task to its original Issue,
independently of editable summary sources. Rebinding a task to a different Issue
fails with `conflict`. Original requirements and synchronized comments remain
reachable even when a summary cites only experimental artifacts. Existing
bindings/custom task refs are reused, avoiding a duplicate summary history.
New tasks use `issue:<repository>#<number>`.

Configuration now requires only `enabled`, `python`, `backend` and `store`.
Legacy `repository`, `issue` and `task` fields are accepted as migration hints;
they never select a task automatically. Backend paths, credentials, publisher
location and caller identity are never model-controlled tool arguments.

`context-active-task.json` records this run's selected scope. Each task has its
own `context-tasks/<key(task)>/` directory containing `context-read-snapshot.json`,
`context-change-continuation.json` and pending proposals. Switching from Issue 47
to 52 and back retains Issue 47's pending page and baseline. A new run starts
unselected; another run's private read progress is not silently reused. A run has
one active selection: serialize selection and dependent operations rather than
batching work on different Issues in parallel. Native publication reserves
`issue:` refs for the selected task root; identity collisions fail explicitly.

### Bounded reads

The optional `context_task` response has a 32,000 UTF-8 byte budget. The original
Issue body is mandatory in that particular response; optional summary text and
related cards may be omitted with explicit counts/flags. A genuinely oversized
original requirement makes this optional read return `needs_budget`; it does not
prevent selection, summary discovery or paginated raw reads.

`snapshot` is a compact handle (`id`, `task`, `observed_at`, `object_count`,
`source_count`), not an inline manifest. Full snapshots live in local
`.runtime/snapshots/`; handles require the same store and are not portable Git
artifacts. Legacy full snapshots remain accepted as inputs to the core.

`context_task` includes the first delta page, including existing objects/sources
on a first read. Follow its `changes_next_cursor` with
`context_changes(cursor=...)`. Direct change checks accept optional `limit`
(2,000–100,000 bytes; default 8,000); follow each `next_cursor` until null. Delta
pages are frozen in `.runtime/change-pages/`: writes arriving during pagination
appear in the next check. A task's read baseline advances only after the final
page. Retrying the last requested cursor replays that page; omitting the cursor
while a check is pending replays the last page instead of skipping it. Cursors
for another task/run are rejected. Retry with at least the previous page budget.

Freshness displays at most eight hints plus total/omitted counts. Use change
pages and source refs for full evidence. `context_open` independently paginates
long summaries and raw material. Runtime snapshot/page garbage collection is not
implemented. Read cursors and event records stay outside the native-writable
workspace. The system does not inject asynchronous messages into the model.

## Legacy wrapper lifecycle

Issue creation -> deterministic original-text extraction -> native run receives
startup context -> worker reads evidence and runs an experiment -> worker queues
summaries -> maintainer validates registered source snapshots and expected object
revision -> shared Git commit/push -> idempotent Issue handoff notification.

`sync-issue` fetches every comment page before reporting complete coverage. Derived
notifications contain `<!-- mvctx:... -->` and are not re-ingested as evidence.
Comment edits are new source versions; deleted/unavailable remote sources require
explicit reconciliation in future adapters, not a claim of perpetual freshness.

## Error semantics

`missing`, `denied`, `unavailable`, `stale`, `conflict`, `stale_cursor`,
`select_source`, `task_required`, `needs_budget`, `truncated` and `invalid` are distinct statuses.
CLI failures exit 2. A partial batch preserves individual failures. A partial
summary publication reports each accepted/rejected proposal; rejected work is
retained in the run outbox for recovery.

## Cost

There are no additional summarizer model calls. Agent-authored summaries consume
the same native run budget as experiment work. Provider token usage can be
aggregated per native run; per-purpose token attribution cannot be inferred from
which output file was written. A monetary amount is reported only when a known
provider tariff or actual billing record is available.


## Exact local evidence copies (v0.3.1)

For programmatic reproduction, first get a source descriptor with `context_source`.
Call `context_open` with that direct source `ref`, `view: "raw"`,
`expected_digest`, and `save_to` relative to the selected repository. This copies
the entire registered UTF-8 snapshot (up to 2 MB) and returns its source descriptor,
path, byte count and digest. It does not pass the full data through model text.
Do not combine this mode with partial reads, cursors, sections or `source`.

Changed sources fail with `conflict`. Existing identical output is an idempotent
retry; existing different output is preserved. Symlinks, parent traversal and
protected directories are refused. The operation does not execute the evidence,
verify scientific claims, or mark a review complete. Existing card/summary/raw
reads remain unchanged. This is local shared-host support, not remote file upload.
