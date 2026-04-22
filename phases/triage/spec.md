# Triage Phase Spec

## Goal

Convert analysis outputs into a decision-oriented manifest that supports human review and downstream automation.

## Inputs

- page scores
- duplicate and overlap candidates
- exported metadata
- optional human review notes
- optional IDE-agent review notes

## Outputs

- CSV triage manifests
- JSON triage manifests
- Markdown review summaries

## Expected Decision Categories

- `keep`
- `curate`
- `merge`
- `archive`
- `manual_review`

## Likely Near-Term Utility

- triage manifest generator
  Outputs a reviewable artifact with suggested actions, rationale, and supporting signals

## Review Artifact

A useful near-term triage artifact is a manifest that can be reviewed or edited by humans and IDE agents.

Suggested fields:

- `title`
- `markdown_relative_path`
- `confluence_page_id`
- `page_type`
- `suggested_action`
- `final_action`
- `confidence`
- `rationale`
- `related_group_id`
- `canonical_candidate`
- `notes`

Expected project shape:

```yaml
name: old-segment-content
workspace: ../old-segment-content
phases:
  triage:
    manifest: {}
```

Current behavior:

- reads `analysis.metadata.json` when present, otherwise falls back to `*.analysis.json` sidecars
- defaults `input` and `output` to the project `workspace` when those fields are omitted
- writes `triage-manifest.json`
- writes `triage-manifest.csv`
- writes `triage-manifest.md`
- writes a timestamped triage report in `reports/`

## Success Criteria

- outputs are easy to inspect in a spreadsheet or repo
- each recommendation includes enough context to be challenged or accepted
- the manifest can act as the handoff contract for refactoring prompts and scripts

## Boundary

This phase should not assume that all triage decisions can be made by script.

Instead, it should support a mixed workflow:

- `kurate` produces structured candidate artifacts
- a human or IDE agent makes the harder semantic calls
- resulting decisions are captured in durable review artifacts

## Handoff

The output of triage feeds the refactoring phase.
