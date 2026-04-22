# Extraction Phase Spec

## Goal

Pull messy content out of a source system and turn it into a local corpus that later phases can analyze and act on.

## Inputs

- provider-specific project definitions
- provider credentials and identity settings
- source-system content trees, attachments, and metadata

## Outputs

- Markdown content
- downloaded local attachments
- exported metadata artifacts
- manifests such as `confluence-map.json`
- timestamped reports

## Current Implementation

The current implemented extraction provider is Confluence.

Confluence-specific behavior lives in:

- [phases/extraction/providers/confluence/README.md](/Users/patrickculligan/work/confluence-util/phases/extraction/providers/confluence/README.md)
- [phases/extraction/providers/confluence/spec.md](/Users/patrickculligan/work/confluence-util/phases/extraction/providers/confluence/spec.md)

## Success Criteria

- exported content is readable offline
- links are rewritten as cleanly as possible
- unresolved or unsupported content is clearly surfaced
- metadata is sufficient to support later analysis and triage
- reruns are efficient and auditable

## Handoff

The output of extraction feeds the analysis phase.
