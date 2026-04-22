# Analysis Phase Spec

## Goal

Transform extracted content and metadata into useful evaluation signals that help identify what should be kept, merged, curated, archived, or manually reviewed.

## Inputs

- exported Markdown corpus
- exported metadata files
- provider reports
- attachment and unsupported-content signals

## Outputs

- page scores
- duplicate or overlap candidates
- content clusters
- explanation fields that justify scoring or grouping decisions
- review-ready artifacts for human and IDE-agent interpretation

## Boundary

The suite should automate analysis only up to the point where the result is still cheap, explainable, and auditable.

Good fit for `kurate analyze`:

- numeric or rule-based signals
- structured metadata
- cache-aware scoring
- duplicate or overlap candidates
- sortable review queues

Better fit for IDE-agent prompt workflows:

- whole-area interpretation
- canonicality judgments
- merge recommendations
- conflict resolution
- narrative or architecture-level understanding

The intended handoff is: `kurate` prepares the corpus and structured cues, then a human uses prompts against the local repo to do higher-context interpretation.

## Likely Near-Term Utilities

- page scoring script
  Inputs: export metadata and light content stats
  Outputs: staleness, usefulness, and archive scores

- clustering / duplication finder
  Inputs: titles, paths, and Markdown content
  Outputs: probable duplicate pairs and overlapping groups

## Success Criteria

- results are explainable rather than opaque
- scoring is good enough to prioritize work, not necessarily perfect
- duplicate detection produces useful review candidates rather than pretending to be exact

## Current Implementation

The first implemented analysis utility is page scoring.

Expected project shape:

```yaml
name: old-segment-content
workspace: ./work/old-segment-content
phases:
  analysis:
    scoring:
      outputs:
        - file
        - sidecar
```

Current behavior:

- reads `export.metadata.json` when present, otherwise falls back to `*.metadata.json` sidecars
- when the project defines `workspace`, omitted `corpus` and `output` default to that workspace
- computes staleness, usefulness, and archive scores
- recommends one of `keep`, `curate`, `archive`, or `manual_review`
- writes `analysis.metadata.json` when `outputs` includes `file`
- writes `*.analysis.json` sidecars when `outputs` includes `sidecar`
- appends a `## Analysis Assessment` block to Markdown files when `outputs` includes `content-block`
- writes `page-scores.csv`
- writes a timestamped analysis report in `reports/`

## Handoff

The output of analysis feeds the triage phase.

In practice, that handoff may happen through:

- triage manifests
- prompt-driven review sessions in an IDE agent
- manually curated notes and proposed restructures
