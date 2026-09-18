# Agent instruction files: current guidance and repository application

Checked 2026-08-28 against first-party OpenAI/Codex and Anthropic Claude Code
documentation and source only.

## Question

How should this repository structure and shorten `AGENTS.md` and `CLAUDE.md`
without losing the project-specific rules that coding agents need?

## Executive finding

Use `AGENTS.md` as the short, repository-wide operating contract and make
`CLAUDE.md` import it with `@AGENTS.md`. Do not maintain two copies of the same
instructions. Keep only non-obvious, broadly applicable commands, architecture
boundaries, data-safety rules, and documentation routes in the always-loaded
file. Put explanations, inventories, and specialized procedures in the existing
`README.md`, `CONTEXT.md`, `docs/guides/`, `docs/agents/`, and `docs/adr/` sources.

This follows both vendors' current guidance:

- OpenAI reports that a giant `AGENTS.md` consumes scarce context, dilutes
  important guidance, becomes stale, and is difficult to verify. Its internal
  pattern is a short `AGENTS.md` used as a map to a structured documentation
  system of record ([OpenAI, *Harness engineering*](https://openai.com/index/harness-engineering/)).
- Anthropic says `CLAUDE.md` should be specific, concise, structured, and under
  200 lines; it should contain only broadly applicable facts that are hard to
  infer from code. Detailed procedures should become skills or path-scoped
  rules ([Anthropic, *How Claude remembers your project*](https://code.claude.com/docs/en/memory#write-effective-instructions)).
- Anthropic explicitly recommends a `CLAUDE.md` containing `@AGENTS.md` when a
  repository already supports other coding agents, allowing one shared source
  plus any genuinely Claude-specific additions
  ([Anthropic, *AGENTS.md*](https://code.claude.com/docs/en/memory#agentsmd)).

## Repository audit

The current working tree has:

| File | Current size | Observation |
| --- | ---: | --- |
| `AGENTS.md` | 446 lines / 22,155 bytes | The original 14-line routing file now has a `# CLAUDE.md` section containing effectively the entire project `CLAUDE.md`, including a second copy of the agent-skills routes. |
| `CLAUDE.md` | 430 lines / 22,226 bytes | A useful project encyclopedia, but much larger than Anthropic's under-200-line target and heavily duplicates `README.md`, guides, source layout, and command help. |

The duplication has three concrete costs:

1. Codex loads `AGENTS.md` before work and limits the combined project
   instruction chain to 32 KiB by default. The current root file alone consumes
   about two thirds of that budget
   ([OpenAI, *Custom instructions with AGENTS.md*](https://developers.openai.com/codex/guides/agents-md#how-codex-discovers-guidance)).
2. Claude loads `CLAUDE.md` into every session. Anthropic says longer files use
   more context and reduce adherence; its best-practices checklist excludes
   detailed API docs, frequently changing information, long tutorials, and
   file-by-file descriptions
   ([Anthropic, *Write an effective CLAUDE.md*](https://code.claude.com/docs/en/best-practices#write-an-effective-claudemd)).
3. Two independently edited copies can disagree. Anthropic warns that
   conflicting instructions may be followed arbitrarily and recommends
   periodically removing stale or conflicting guidance
   ([Anthropic, *Write effective instructions*](https://code.claude.com/docs/en/memory#write-effective-instructions)).

## Recommended target structure

### 1. Make `AGENTS.md` the shared authority

Target roughly 80-120 lines. OpenAI's documented internal pattern uses a root
file of roughly 100 lines as a table of contents rather than an encyclopedia
([OpenAI, *Harness engineering*](https://openai.com/index/harness-engineering/)).
Keep these sections:

- **Project shape:** one short paragraph stating the organizing rule: county ETL
  is county-owned; the web surface is shared through `counties/common/` and
  adapters.
- **Non-negotiable boundaries:** pinned Django app labels, county-scoped shared
  tax queries, runtime data only under resolved per-county `var/` paths,
  no county-specific copies of shared pages, and the source-aware Brazos annual
  refresh boundary.
- **Workflow:** Docker Compose only; one canonical focused-test command, full
  suite command, and lint/type-check commands.
- **Verification:** inspect/reproduce/implement/verify/report in compact form,
  with evidence from the exact checks run. Anthropic recommends giving the
  agent a pass/fail check and reporting evidence rather than merely claiming
  success
  ([Anthropic, *Give Claude a way to verify its work*](https://code.claude.com/docs/en/best-practices#give-claude-a-way-to-verify-its-work)).
- **Routes to deeper context:** `CONTEXT.md`, `docs/adr/`, `docs/guides/`, and
  the three existing `docs/agents/` files, with a sentence explaining when to
  read each.

Do not retain architecture tables, every model/module/URL, management-command
catalogs, similarity weights, source archive inventories, or long cap/tax
explanations in the root instruction file. Those are reference material and
should live in normal documentation or source-level docstrings. OpenAI says
`AGENTS.md` is best for non-obvious operating context such as conventions,
business logic, known quirks, and dependencies the agent cannot infer from the
repository
([OpenAI, *How OpenAI uses Codex*, pp. 10-11](https://cdn.openai.com/pdf/6a2631dc-783e-479b-b1a4-af0cfbd38630/how-openai-uses-codex.pdf)).

### 2. Reduce `CLAUDE.md` to an import shim

Use:

```md
@AGENTS.md

# Claude Code

<!-- Add only genuinely Claude-specific guidance here. -->
```

If there is no Claude-specific requirement, `@AGENTS.md` alone is sufficient.
Anthropic documents this exact interoperability pattern and notes that imports
are expanded at launch
([Anthropic, *AGENTS.md*](https://code.claude.com/docs/en/memory#agentsmd),
[Anthropic, *Import additional files*](https://code.claude.com/docs/en/memory#import-additional-files)).

Do not reverse the relationship by placing the encyclopedia in `CLAUDE.md` and
copying it into `AGENTS.md`. Codex reads one project instruction file per
directory, preferring `AGENTS.override.md`, then `AGENTS.md`, then configured
fallback names; a same-directory fallback is not merged with `AGENTS.md`
([OpenAI, *How Codex discovers guidance*](https://developers.openai.com/codex/guides/agents-md#how-codex-discovers-guidance)).

### 3. Use existing docs as progressive disclosure

This repository already has the needed destinations:

| Instruction content | Source of truth |
| --- | --- |
| Domain language and bounded contexts | `CONTEXT.md` |
| Approved architecture decisions | `docs/adr/` |
| Setup, database, GIS, similarity, deployment | `docs/guides/` |
| Issue, label, and domain-document workflows | `docs/agents/` |
| Project overview and adding a county | `README.md` |

The root instruction should route to these files only when the task calls for
them. Avoid importing all of them from `CLAUDE.md`: Anthropic notes that
imported files still consume startup context. For Claude-only specialized
rules, `.claude/rules/` supports path scoping; task-specific procedures belong
in skills that load on demand
([Anthropic, *Organize rules with .claude/rules/*](https://code.claude.com/docs/en/memory#organize-rules-with-clauderules)).

For Codex, add nested `AGENTS.md` or `AGENTS.override.md` files only if a
subtree needs materially different operating rules. Codex concatenates
instructions from the repository root down to the current directory, with the
nearest file taking precedence
([OpenAI, *Layer project instructions*](https://developers.openai.com/codex/guides/agents-md#layer-project-instructions)).
This repository is currently documented as a single context, so new nested
files are not justified merely to distribute the existing encyclopedia.

### 4. Phrase rules for action and maintenance

- Use direct, testable language: exact commands, exact file boundaries, and
  explicit safe extension points. Anthropic contrasts concrete instructions
  such as a precise test command or directory with vague requests to test or
  organize code
  ([Anthropic, *Write effective instructions*](https://code.claude.com/docs/en/memory#write-effective-instructions)).
- State both the forbidden move and the safe path when a surprising invariant
  matters. OpenAI recommends concise review rules that say what to flag and
  provide the safe path or exception, while leaving format/lint enforcement to
  CI
  ([OpenAI, *Add code review rules*](https://developers.openai.com/codex/guides/agents-md#add-code-review-rules)).
- Remove generic behavior already supplied by the tool or global instructions.
  The first-party Codex prompt specifies that direct system, developer, and
  user instructions outrank `AGENTS.md`, and that nested files govern their
  directory trees
  ([OpenAI Codex source, `prompt.md`](https://github.com/openai/codex/blob/main/codex-rs/models-manager/prompt.md)).
- Prefer stable rules. Anthropic explicitly excludes information that changes
  frequently from `CLAUDE.md`; link to the maintained source instead
  ([Anthropic, *Write an effective CLAUDE.md*](https://code.claude.com/docs/en/best-practices#write-an-effective-claudemd)).

## Proposed rewrite outline

```text
AGENTS.md
├── Project contract (short purpose + organizing rule)
├── Architecture and data boundaries (5-8 bullets)
├── Development and verification (exact Docker Compose commands)
├── Documentation routing (CONTEXT, ADRs, guides, agent workflows)
└── Scope and worktree safety (project-specific only)

CLAUDE.md
├── @AGENTS.md
└── Claude-specific additions, if any
```

The rewrite should preserve the current working tree's substantive domain
updates by relocating any fact that does not already have a durable home before
deleting the duplicate prose. It should not silently discard unresolved ADR or
issue boundaries.

## Verification after rewriting

1. Confirm each file's line/byte count and check that `CLAUDE.md` imports
   `AGENTS.md` exactly once.
2. Search for duplicate headings and contradictory commands across
   `AGENTS.md`, `CLAUDE.md`, `README.md`, `CONTEXT.md`, and `docs/agents/`.
3. Start a fresh Codex run from the repository root and ask it to summarize the
   active instructions; repeat from a relevant subdirectory. OpenAI documents
   these as instruction-discovery checks
   ([OpenAI, *Verify your setup*](https://developers.openai.com/codex/guides/agents-md#verify-your-setup)).
4. In a fresh Claude Code session, use `/context` to confirm `CLAUDE.md` loaded
   and that the `@AGENTS.md` import is present. Anthropic documents `/context`
   for this purpose
   ([Anthropic, *Set up a project CLAUDE.md*](https://code.claude.com/docs/en/memory#set-up-a-project-claudemd)).
5. Run the repository's documentation/pre-commit checks that apply to Markdown;
   no Django test run is needed for documentation-only changes unless the
   repository's hooks require it.

## Primary sources

- [OpenAI: Custom instructions with AGENTS.md](https://developers.openai.com/codex/guides/agents-md)
- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)
- [OpenAI: How OpenAI uses Codex](https://cdn.openai.com/pdf/6a2631dc-783e-479b-b1a4-af0cfbd38630/how-openai-uses-codex.pdf)
- [OpenAI Codex source: AGENTS.md instruction specification](https://github.com/openai/codex/blob/main/codex-rs/models-manager/prompt.md)
- [Anthropic: How Claude remembers your project](https://code.claude.com/docs/en/memory)
- [Anthropic: Best practices for Claude Code](https://code.claude.com/docs/en/best-practices)
