# Confluence Utils Specification

## Goal

Provide two CLI workflows for moving structured content between a local Markdown repo and Confluence Cloud:

- `kurate.py extract`: run either publish or export behavior based on a project file's `activity`

Also provide a project-driven workflow so repeatable efforts can define extraction behavior in YAML and run without retyping a large set of CLI arguments.

The tools are intended to preserve useful structure, support repeatable runs, and produce enough reporting to make sync/export behavior understandable and auditable.

## Project Definitions

### Goal

Support a folder of YAML project files that define repeatable efforts with phase-specific configuration.

The intent is:

- avoid repeatedly typing page ids, output paths, space keys, and parent ids
- make sync and export jobs reviewable and shareable as config
- support richer export plans than the current single-page CLI

### Project storage

Project definitions are expected to live in a folder such as `projects/`.

Each file in that folder defines one named effort, with phase-specific configuration under `phases`.

Examples:

- `projects/publish-segment.yaml`
- `projects/pull-old-segment-content.yaml`

### Execution direction

Project files are the primary runtime input for the top-level entry point.

Preferred direction:

- `kurate.py --project ... extract` reads a project file and dispatches by `phases.extraction`
- shared Confluence extraction orchestration lives in `phases/extraction/providers/confluence/conf_io.py`
- helper code for each direction lives beside that provider orchestration code

### Shared project rules

Project files should follow these rules:

- `phases` is required
- `phases.extraction.provider` is required for extraction projects
- `phases.extraction.activity` is required for extraction projects
- unknown fields should be treated as validation errors
- auth settings should not live in project files
- project files should describe job intent and targets, not credentials
- Confluence identity remains in `confluence-identity.yaml`

Recommended shared top-level fields:

- `name`: optional human-readable label
- `phases`: required phase configuration mapping

### Repo to Confluence project shape

The publish direction is intentionally simple and should remain close to the existing CLI.

Recommended shape:

```yaml
name: corp-segment-push
phases:
  extraction:
    provider: confluence
    activity: publish

    source: ../corp-segment-projects
    space: SA
    parent: 84969480
    excludes:
      - drafts/**
      - archive/**
      - "**/*.tmp.md"
    dry_run: false
```

Required fields:

- `phases.extraction.provider`
- `phases.extraction.activity`
- `phases.extraction.source`
- `phases.extraction.space`
- `phases.extraction.parent`

Optional fields:

- `name`
- `phases.extraction.excludes`
- `phases.extraction.dry_run`

Behavior:

- fields should map closely to the publish workflow inputs
- publish exclusions should live in the project file as `excludes`
- `excludes` should be a list of glob patterns matched against the source tree
- different publish projects may use different `excludes` lists against the same source tree
- project execution should still support normal reporting and manifest generation
- project execution should still respect identity settings from `confluence-identity.yaml`

### Confluence to Repo project shape

The export direction should support grouped collection plans rather than a single page per job.

The preferred model is:

- one project file can cover multiple Confluence spaces
- each space can define multiple page exports
- each page export can decide its own local output path
- each page export can decide whether recursion is enabled

Recommended shape:

```yaml
name: analytics-exports
phases:
  extraction:
    provider: confluence
    activity: export
    metadata:
      - sidecar

    spaces:
      SA:
        pages:
          - id: 1647640729
            output: ./exports/traffic-analysis
            recurse: true
            excludes:
              - 1647640999
              - 1647641000
          - id: 1723456789
            output: ./exports/store-forecast

      OPS:
        pages:
          - id: 1987654321
            output: ./exports/ops-playbook
            recurse: true
```

Required fields:

- `phases.extraction.provider`
- `phases.extraction.activity`
- `phases.extraction.spaces`

Optional top-level fields:

- `name`
- `phases.extraction.metadata`

Required fields for each page entry:

- `id`
- `output`

Optional fields for each page entry:

- `recurse`
- `excludes`

Behavior:

- `phases.extraction.activity: export` requires at least one configured space
- `phases.extraction.metadata` may be `none`, a single metadata output, or a list of metadata outputs
- valid metadata outputs are `sidecar`, `file`, and `content-block`
- `metadata: sidecar` should write per-page metadata sidecars such as `readme.metadata.json`
- `metadata: file` should write one root-level metadata file such as `export.metadata.json`
- `metadata: content-block` should append a clearly labeled Markdown metadata section at the end of each exported file with usefulness-focused signals
- on rerun, existing metadata sidecars or the root metadata file may be used as a cache hint; if the stored Confluence version matches the live page version, the exporter should skip rewriting that page, and it should skip attachment downloads only when the cached attachment metadata still matches the live Confluence attachment metadata
- exported metadata should include rolling analytics windows computed at runtime for `year_to_date`, `trailing_year`, and `all_time_proxy`, each including the exact `from_date`, total `views`, and `unique_viewers`
- each configured space requires a `pages` list
- each page entry is exported as an independent target
- the same project file may export pages from multiple spaces in one run
- the export workflow should not require a space key for API correctness, but the grouped `spaces` structure is still useful for reviewability and validation
- each page entry should be allowed to set `recurse` independently
- each page entry may define `excludes` as a list of Confluence page ids that must not be exported
- excluded page ids must not be written locally and the exporter must not recurse below them

Operational expectations:

- per-page failures should be recorded cleanly in the project report
- one page failure should not necessarily abort all other pages in the same project
- the final project report should summarize each attempted export target

### Validation expectations

Project validation should be explicit and strict.

Validation should catch:

- missing required fields
- unsupported `activity` values
- malformed publish `excludes` lists
- malformed `spaces` structures
- page entries without `id` or `output`
- malformed `excludes` lists
- unexpected top-level or nested keys
- wrong scalar types such as non-string page ids or paths

### Reporting expectations

Project-driven runs should produce a clear report describing:

- project file used
- selected activity
- each target attempted
- success or failure per target
- warnings and validation issues

For export projects, reports should include:

- each space processed
- each page id processed
- local output path used
- whether recursion was requested

## Supported Flows

### Repo to Confluence

The publish flow is designed for a source repo that contains Markdown files and related local assets.

Current behavior:

- Walk the provided source directory and collect Markdown files.
- Use the first `# H1` in each Markdown file as the Confluence page title.
- Treat `readme.md` as the page for its containing folder.
- Treat other Markdown files in the folder as child pages under that folder page.
- Convert Markdown to Confluence storage-format HTML.
- Upload relative image references as Confluence attachments and replace them with Confluence image markup.
- Translate relative Markdown links to Confluence page links when the target page is part of the same sync.
- Detect unresolved local Markdown links and report them.
- Detect "zombie" pages under the target Confluence parent when they do not map to local files.
- Maintain a `confluence-map.json` manifest in the source tree so later runs can update pages by stable Confluence page id.
- Write a timestamped run report in `reports/`.

Additional behavior:

- If the source folder is inside a git repo, the tool attempts to stage and commit `confluence-map.json` automatically after a successful live run.
- The user is still responsible for pushing that commit.
- In `--dry-run` mode, the tool does not write the manifest and does not change Confluence.

### Confluence to Repo

The export flow is designed to pull a Confluence page tree into a local Markdown folder structure.

Current behavior:

- Export a root Confluence page into a target output directory.
- When `--recurse` is provided, recursively export child pages.
- Pages with children become folders containing a `readme.md`.
- Leaf pages with attachments also become folders containing a `readme.md`.
- Leaf pages without attachments remain Markdown files.
- For folderized pages, local attachments should sit beside `readme.md` in that page folder rather than under a nested `assets/` folder.
- Download attachments and images referenced by exported pages.
- Rewrite internal page links to relative Markdown links when the target page is also part of the export.
- Maintain a `confluence-map.json` manifest in the output tree.
- Write a timestamped run report in `reports/`.

## Auth and Setup

Authentication behavior:

- Preferred identity source: `confluence-identity.yaml` at the repo root.
- Required auth source: `api_key` in `confluence-identity.yaml`.
- Both tools can read `base_url`, `email`, and `api_key` from the identity file.
- CLI flags such as `--base-url` and `--email` override identity config values.

Setup behavior:

- `setup.sh` creates a virtual environment and installs `requirements.txt`.
- `setup.sh` checks for `confluence-identity.yaml` and whether it includes `api_key`.
- `setup.sh` detects `mmdc` and attempts to install Mermaid CLI with npm if it is missing.

## Ignore, Manifest, and Report Files

### Publish excludes

- Publish projects should define `excludes` as glob patterns in the project file.
- The glob patterns are applied while collecting Markdown files from the source tree.
- Default excludes still include `.DS_Store`, `.git`, and `.gitignore`.

### Manifest

- Both directions write `confluence-map.json`.
- In publish mode, the manifest stores local Markdown paths, Confluence page ids, content hashes, and known Confluence versions.
- In export mode, the manifest stores exported Markdown paths mapped to Confluence page ids.

### Reports

- Both directions write timestamped Markdown reports under `reports/`.
- Reports summarize created/updated/skipped items, warnings, and link or attachment issues.
- Publish reports also include zombie pages and conflicts.

## Mermaid Behavior

Publish mode supports Mermaid fences such as:

````md
```mermaid
flowchart LR
  A --> B
```
````

Current behavior:

- If `mmdc` is available, Mermaid fences are rendered to SVG attachments.
- The rendered SVG is embedded into Confluence as an attachment image.
- The tool applies size and whitespace-trimming logic intended to improve readability in Confluence.
- If Mermaid rendering fails, the sync continues and the failure is recorded in the report.

## Operational Assumptions

- The tools target Confluence Cloud.
- `--base-url` should be the site base URL without `/wiki`.
- Publish mode assumes page titles must be unique enough within the chosen parent hierarchy for deterministic mapping.
- Publish mode stops on page-title conflicts when a matching title exists elsewhere in the space and cannot be safely matched to the expected parent.

## Known Limitations

- Mermaid rendering depends on local Mermaid CLI availability and local browser execution through that toolchain.
- Confluence storage-format handling is pragmatic rather than exhaustive; some Confluence macros and complex rich content may round-trip imperfectly.
- Exported Markdown is intended to be useful and editable, not a perfect source-level reconstruction of all Confluence semantics.
- Publish mode detects zombie pages but does not delete them automatically.
- Automated git commit behavior is best-effort and may fail due to repo state, missing git config, hooks, or permissions.

## Near-Term Refactor Direction

The repo is structured so:

- root scripts remain the entry points
- shared library code lives under `lib/`

The intended next evolution is to continue extracting genuinely shared utilities into `lib/` while keeping direction-specific application flow in the root scripts.
