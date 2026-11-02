# Multiverse Context v0.3.1

Versioned, agent-written research summaries with progressive access to original
evidence. The maintenance program validates, merges and publishes; it makes no
model calls. It runs on Python 3.11+ with no runtime dependencies.

This repository contains the standalone context core used by a native Multiverse
integration. For an advisor-oriented overview, see the Chinese notes on the
[design](docs/research-design-zh.md), [validation results](docs/validation-results-zh.md),
and [next experiment plan](docs/ctxval2-plan-zh.md). The companion integration
branch is published at
[weilemo/gewu-multiverse](https://github.com/weilemo/gewu-multiverse/tree/codex/context-mcp-integration).

## Install and inspect

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/mvctx --help
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Without installation, use `PYTHONPATH=src python3 -m multiverse_context` instead
of `mvctx`. This is also how the container integration works.

## Read context

```sh
mvctx --store ./research-context init
mvctx --store ./research-context discover --task issue:context-research-demo-1
mvctx --store ./research-context open experiment:retrieval-dev --view summary
mvctx --store ./research-context open experiment:retrieval-dev --view evidence
mvctx --store ./research-context open experiment:retrieval-dev --view raw --source artifact:context-research-demo/results/dev.json
```

`card`, `summary`, `evidence`, `raw` are L0–L3. Call any view directly; use `batch`
with a JSON array of open requests to read related evidence together. A truncated
response has `next_cursor`; repeat the same view/sections/source with that cursor.
The limit is UTF-8 bytes including metadata, not an estimated token count.

Native agents choose among assigned tasks using the existing
`forgejo_list_assigned_issues` tool, then call `context_select(repository, issue)`.
The current upstream toolset has no Issue self-assignment/claim tool; an owner
assigns work in Forgejo. A starting-work comment is only an acknowledgment. Selection returns
references, not the entire Issue or summaries. Read its `issue_source` with
`context_open(view="raw")`, find summaries with `context_discover`, and expand
relevant evidence. There is no automatic Issue context injection at agent startup.

For executable comparisons, get a source descriptor with `context_source`, then
call `context_open(ref=SOURCE_REF, view="raw", expected_digest=DIGEST,
save_to="artifacts/peer.json")`. This saves the complete source in the selected
repository and returns a receipt; it never overwrites different existing content.
No model reconstruction of the source is needed.

`context_task()` is an optional convenience read of the selected Issue's original
requirements, current summary and bounded related cards. It does not need to be
called before other reads. If its original requirements exceed the budget, it
returns `needs_budget`; individual raw reads still support pagination. Follow
`changes_next_cursor` from that result, or `next_cursor` from `context_changes`,
until null. Switching Issues preserves each task's read progress within the run.

## Write summaries

1. Register source text with `ingest-file` or a committed file with `ingest-git`.
2. Read `source REF` to obtain its exact descriptor.
3. Write a structured proposal (see [protocol](docs/protocol.md)).
4. `submit proposal.json --actor NAME --outbox .context/outbox` validates and queues
   it. This is the legacy wrapper flow. Native agents instead use
   `context_import_file` then `context_publish` for immediate publication.

`submit` without `--outbox` is the maintainer operation. Updating an existing
summary requires its `expected_revision`. A conflicting proposal remains pending
for rereading and merging; it does not overwrite the new summary.

## Multiverse integration

**v0.3 native integration:** use the companion Multiverse fork based on v0.1.37.
Its normal TUI engine adds only static usage instructions and exposes eight
`context_*` tools through the existing supervisor-owned MCP bridge. Startup makes
no context backend call and selects no Issue. The model decides when to select a
task and read material under its work instructions; the supervisor checks scope,
versions, budgets and publication. Agents can
publish a stage, discover peers' changes, read evidence, and incorporate feedback
**during the same running session**. No external launcher is required.

See the [MCP protocol](docs/protocol.md) and the project notes linked above.
The supported harness is native OpenCode; other harnesses fail explicitly when
this feature is enabled. Agents without configuration behave as upstream.

The original v0.1 one-run wrapper remains available for comparison. It queues
summaries until the run ends; it is not the v0.3 live path. The legacy CLI
`startup` command remains a manual operation and is not invoked by native startup.

## Evidence and limits

- A valid source hash proves the snapshot's identity, not that prose faithfully
  represents it. Scientific claims still need review.
- Freshness covers registered, synchronized sources. `no_known_changes` does not
  promise all external dependencies are current.
- The shared store has one publishing path. Git push conflicts are visible and
  are never force-resolved.
- Canonical frontmatter is JSON, a valid YAML 1.2 subset. Edit via proposals;
  Markdown sections below it are generated for reading.
- Runtime traces and credentials are separate from the published context. Only
  explicitly registered task artifacts are collected.
- The v0.3.1 checkpointed repair completed an A→B→A handoff, including
  independent recomputation, exact evidence copying, review and adoption. It
  required operator scheduling and several continuations; it does not prove
  unattended reliability. A controlled short-task comparison found no benefit
  over ordinary files or Markdown summaries. See the validation report for the
  exact denominators and limits. Raw runtime traces, credentials and per-request
  traffic are deliberately excluded from this public snapshot.
