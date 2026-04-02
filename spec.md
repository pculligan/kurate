# Confluence Sync Tool - Specification

## Goal
Sync a tree of Markdown files from a local git repo into Confluence, recreating the directory tree as pages. Preserve formatting, code blocks, images, and translate relative links to Confluence page links. Detect "zombie" pages on Confluence (pages that were removed locally) and report them.

## High-level behavior
- Walk a given source directory (CLI arg) and find markdown files and related assets (images).
- For each directory, create a corresponding Confluence page (or hierarchy) and upload/replace its contents.
- Convert Markdown to Confluence storage-format (HTML) while preserving formatting and code blocks.
- Attach images to their respective Confluence pages and update image links.
- Translate relative links between local markdown files into Confluence page links.
- Maintain an exclude list (.confluenceignore default) for patterns to skip (e.g., .DS_Store, .git).
- Identify Confluence pages under the target parent that have no corresponding local file ("zombies").

## Inputs / Config
- `source` (positional) — local folder to sync.
- Confluence credentials: API key located in `conf-api-key.txt` in project root (or via env var `CONFLUENCE_API_KEY`).
- Confluence target: `space` (space key) and `parent` (page id or page title) to place the tree under.
- Optional `.confluenceignore` file in source to list ignore globs.

## Output / Reporting
- Log created/updated pages and attachments.
- Print or write a list of zombie pages.
- Exit codes: 0 success, non-zero on fatal errors.

## Requirements
- Preserve Markdown formatting including code fences, tables, and inline HTML where possible.
- Images should be attached to pages and embedded using Confluence attachments.
- Support common markdown features via `markdown` + `BeautifulSoup` transforms.

## Open Questions / Clarifications (need user input)
1. Confluence target mapping: Each directory will have a `readme.md` whose first H1 (`# Title`) is the page title. Every document will have a leading `# [title]` which should be used as the Confluence document name.
2. Existing pages: Skip updating existing Confluence pages if the content is unchanged; otherwise create a new version (update the page).
3. Link translation: Relative links should be translated to Confluence page links; anchors (fragments) should be preserved where possible (e.g., `file.md#heading` -> Confluence page URL + `#heading`).
4. Reporting: Zombie pages should be reported to a `zombies.txt` file at the run root; the file should be deleted at the start of each run if it exists.
5. Conflicts: Keep mapping deterministic by using the directory structure; page titles are taken from each file's H1. If duplicate titles occur in different folders, they will remain distinct pages because they live in different parent pages.

## Exclude patterns
- Default exclude patterns: `.DS_Store`, `.git`, `.gitignore` and any entries in `.confluenceignore` (supports glob patterns).

## Milestones
1. CLI and Confluence client
2. Markdown -> Confluence conversion + attachments
3. Link translation
4. Zombie detection and reporting
5. Tests and documentation

## Next Steps
- Implement initial project skeleton and CLI.
- Ask the clarification questions above and confirm defaults.
