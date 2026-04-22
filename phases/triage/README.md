# Triage Phase

The triage phase turns analysis results into concrete review decisions.

Its purpose is to create a durable handoff artifact that people and later tools can use to decide what to keep, curate, merge, archive, or send to manual review.

This phase is expected to combine:

- deterministic outputs from `kurate`
- structured review artifacts such as manifests
- human and IDE-agent judgment over the local working copy

Today, the first triage utility is a manifest generator that reads analysis outputs and writes:

- `triage-manifest.json`
- `triage-manifest.csv`
- `triage-manifest.md`

If a project defines a top-level `workspace`, triage manifest generation defaults both its `input` and its `output` to that workspace when those fields are omitted.
