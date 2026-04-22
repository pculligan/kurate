# Analysis Phase

The analysis phase evaluates the extracted corpus and turns raw content plus metadata into signals that can support human decisions.

This is where the suite should begin answering questions like:

- what is stale
- what is still used
- what appears authoritative
- what overlaps with other pages
- what looks expensive to clean up

Today, the first analysis utility is a page scoring pass that reads exported metadata and produces:

- staleness scores
- usefulness scores
- archive scores
- recommended actions such as `keep`, `curate`, `archive`, or `manual_review`
- optional `*.analysis.json` sidecars
- an optional `## Analysis Assessment` block appended to Markdown pages

Reruns reuse existing analysis when the underlying extraction metadata has not changed. Use `kurate.py --project ... --force analyze` when you want to recompute everything anyway.

If a project defines a top-level `workspace`, analysis scoring defaults both its `corpus` and its `output` to that workspace when those fields are omitted.

## Boundary

This phase is intentionally not meant to automate every semantic judgment.

`kurate analyze` should stop at the point where the work is:

- cheap
- explainable
- auditable
- useful as substrate for later review

That means the suite should happily automate:

- scoring
- metadata shaping
- cache-aware reuse
- candidate generation
- review queues and manifests

But broader questions like:

- what is the real story of this documentation area
- which page is truly canonical
- whether multiple pages should be merged
- whether something is mostly organizational context or mostly durable knowledge

are often better handled by an IDE agent working against the local corpus.

The prompt packs under [prompts/](/Users/patrickculligan/work/kurate/prompts/README.md) are meant to support that handoff.
