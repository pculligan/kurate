# Prompt Packs

This folder is for prompts intended to be used against a local kurate working copy in an IDE agent such as Codex, Cursor, or Claude Code.

The point is not to replace the deterministic parts of the suite. It is to take over where cheap, auditable automation stops being the right tool.

## Boundary

`kurate` should do the work that is:

- cheap
- repeatable
- explainable
- easy to validate
- useful as structured substrate for later judgment

That includes things like:

- extraction
- metadata capture
- attachment handling
- unsupported-content reporting
- lightweight scoring
- cache-aware reruns
- triage manifests and review queues

The prompt packs are for the work that is:

- interpretive
- comparative
- story-level
- architecture-level
- more valuable with broad repo context than with isolated API calls

That includes things like:

- deciding whether a page is mostly substantive or mostly organizational
- identifying canonical content
- understanding conflicting or overlapping pages
- proposing merged replacements
- understanding the structure and purpose of an entire documentation area

## Expected Workflow

1. Run `kurate extract` to build the working copy.
2. Run `kurate analyze` to produce lightweight signals and structured metadata.
3. Open the working copy in your IDE agent.
4. Use the prompts in this folder against the local corpus, reports, and metadata.
5. Write the outcomes back into review artifacts such as manifests, notes, or curated content.

## Current Packs

- [analysis/single-page-review.md](/Users/patrickculligan/work/kurate/prompts/analysis/single-page-review.md)
- [analysis/duplicate-group-review.md](/Users/patrickculligan/work/kurate/prompts/analysis/duplicate-group-review.md)
- [analysis/site-story-review.md](/Users/patrickculligan/work/kurate/prompts/analysis/site-story-review.md)
- [triage/archive-decision.md](/Users/patrickculligan/work/kurate/prompts/triage/archive-decision.md)
