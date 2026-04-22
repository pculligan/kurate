# Knowledge Cleanup Suite

This repo is for building a suite of tools that help turn messy collaboration content into something usable again.

The immediate target is Confluence, but the larger goal is broader: extract bloated knowledge bases into cleaner local forms, analyze them, identify what is still valuable, and move the long tail of stale material into an archive that remains available without drowning day-to-day discovery.

The real problem is not "how do I sync Confluence." It is "how do I separate active knowledge from accumulated documentation sediment."

## Setup

From the repo root:

```sh
bash setup.sh
source .venv/bin/activate  # bash/zsh
python3 kurate.py --help
```

The setup script creates or repairs `.venv`, installs Python dependencies from `requirements.txt`, and checks for Mermaid CLI support.

Confluence workflows also expect an identity file at `confluence-identity.yaml` with:

```yaml
confluence:
  base_url: https://your-domain.atlassian.net
  email: you@example.com
  api_key: your-token
```

If you prefer not to activate the virtualenv, you can run commands directly with `.venv/bin/python`.

## Usage

The main entry point is `kurate.py`.

```sh
python3 kurate.py --project projects/alex-project.yaml extract
python3 kurate.py --project projects/pull-old-segment-content.yaml analyze
python3 kurate.py --project projects/pull-old-segment-content.yaml triage
```

Project YAML files in `projects/` define the workspace, phase configuration, and Confluence targets for each run.

## Purpose

The suite is meant to support a full knowledge-triage workflow:

1. Extract content from systems like Confluence into a cleaner offline representation such as Markdown.
2. Preserve enough metadata and signals to judge freshness, usage, authority, duplication, and cleanup difficulty.
3. Use scripts and AI workflows to score, summarize, cluster, dedupe, and triage the exported corpus.
4. Promote useful material into a more curated knowledge set.
5. Relegate stale or low-value content into an intentionally low-noise archive.

That archive still matters. It just should not compete with the active working knowledge people are actually trying to find.

## Suite Shape

The suite is organized around a few stages:

### Extraction

Pull pages, attachments, metadata, usage signals, unsupported-content markers, and reports into a local Markdown corpus.

### Analysis

Run utilities and prompts that help evaluate:

- freshness
- usage
- likely authority
- duplication
- topical overlap
- cleanup difficulty
- archival candidacy

### Decision Support

Produce useful review outputs such as:

- keep and curate
- stale but historically important
- duplicate or overlapping
- archive candidate
- manual review needed

### Refactoring

Use AI and scripts to:

- draft canonical summaries
- merge overlapping material
- improve information architecture
- generate proposed curated Markdown trees

### Warehousing

Move the long tail into a secondary store that is still accessible but no longer clutters the primary knowledge surface.

Examples might include:

- a local Markdown repo
- a static site
- a search index
- a separate cold-storage corpus

## Current Scope

The current implemented provider is Confluence.

Today, the repo already supports a solid ingestion layer:

- project-driven export from Confluence into Markdown
- project-driven publish from Markdown back to Confluence
- attachment handling
- metadata capture
- usefulness-oriented export signals
- export and publish reports

That Confluence layer is intended to be the first provider inside the broader suite, not the final shape of the repo.

The first analysis utility is now in place as well:

- page scoring over exported metadata
- JSON and CSV score outputs
- optional analysis sidecars and Markdown analysis blocks
- a short analysis report summarizing recommended actions
- cache-aware reruns with a top-level `--force` escape hatch

The first triage utility is also in place:

- a triage manifest generator
- JSON, CSV, and Markdown outputs for human + IDE-agent review

The intended boundary is:

- `kurate` does the deterministic, auditable, low-cost work
- prompt workflows in an IDE agent do the broader interpretive work

## Repo Layout

- `kurate.py`
  Top-level suite entry point. Today it exposes extraction, analysis, and triage workflows.
- `shared/`
  Shared suite plumbing such as project loading and report generation.
- `phases/`
  Phase-level docs describing the intended pipeline from extraction through warehousing.
- `projects/`
  Reusable job definitions that are expected to become a shared contract across stages and tools.
- `prompts/`
  Prompt packs for higher-context review work in an IDE agent against a local kurate working copy.

## Architecture Direction

The suite is meant to grow beyond Confluence without forcing Confluence-specific ideas into every layer.

The intended shape is:

- `shared/`
  Shared functionality used across providers and workflow stages.
- `phases/`
  Workflow stages, each with its own docs and, where appropriate, phase-local implementation.
- `projects/`
  Reusable job definitions that can be shared across extraction, analysis, and later workflow stages.

Today, `shared/` is intentionally small. It handles shared concerns like project loading and reports. Over time it may also grow to include shared metadata models, orchestration helpers, and scoring or triage primitives.

## Expected Next Steps

The current Confluence workflows are the ingestion layer for a broader cleanup system.

Likely next additions include:

- analysis utilities
- duplicate and overlap detection
- archival candidacy scoring
- prompt-assisted summarization and consolidation in IDE agents
- git/repo utilities for storing and curating cleaned corpora
- possible future providers such as Teams or SharePoint

## Docs

- Extraction phase: [phases/extraction/README.md](/Users/patrickculligan/work/kurate/phases/extraction/README.md)
- Analysis phase: [phases/analysis/README.md](/Users/patrickculligan/work/kurate/phases/analysis/README.md)
- Triage phase: [phases/triage/README.md](/Users/patrickculligan/work/kurate/phases/triage/README.md)
- Refactoring phase: [phases/refactoring/README.md](/Users/patrickculligan/work/kurate/phases/refactoring/README.md)
- Warehousing phase: [phases/warehousing/README.md](/Users/patrickculligan/work/kurate/phases/warehousing/README.md)
- Prompt packs: [prompts/README.md](/Users/patrickculligan/work/kurate/prompts/README.md)
- Confluence extraction provider overview: [phases/extraction/providers/confluence/README.md](/Users/patrickculligan/work/kurate/phases/extraction/providers/confluence/README.md)
- Confluence extraction provider spec: [phases/extraction/providers/confluence/spec.md](/Users/patrickculligan/work/kurate/phases/extraction/providers/confluence/spec.md)
