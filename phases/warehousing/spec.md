# Warehousing Phase Spec

## Goal

Store low-value, stale, or historical material in a form that remains accessible but no longer competes with active working knowledge.

## Inputs

- triage manifests
- curated content sets
- archive candidates
- source exports and metadata

## Outputs

- cold-storage Markdown corpora
- static archive sites
- searchable secondary repositories
- archive indexes or manifests

## Possible Implementations

- a local Markdown repo
- a static site
- a search index
- a separate archive-oriented git repository

## Success Criteria

- historical material is still recoverable
- day-to-day search surfaces are less noisy
- archived content is clearly identified as archival rather than current guidance

## Handoff

This phase is the terminal storage layer for relegated content, but it should still preserve enough structure to support future retrieval and auditing.

