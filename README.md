# Confluence Utils

This tool syncs a tree of local Markdown files into Confluence, preserving formatting, images, and translating relative links.
It also includes a reverse-direction exporter that can scrape a Confluence page tree back into a local Markdown repo structure.

Quick start (macOS):

```bash
# create and activate venv
python3 -m venv .venv
source .venv/bin/activate

# install dependencies
pip install -r requirements.txt

# run (example)
python repo_to_confluence.py /path/to/local/repo --space YOURSPACE --parent PARENT_PAGE_ID

# Segment Sync example
python repo_to_confluence.py ../corp-segment-projects --space SA --parent 84969480 --base-url https://mcd-tools.atlassian.net --email patrick.culligan@us.mcd.com

# export from Confluence back to a local folder tree
python confluence_to_repo.py /path/to/output --page PAGE_ID --base-url https://mcd-tools.atlassian.net --email you@example.com --recurse

python confluence_to_repo.py ./output --page 1647640729 --base-url https://mcd-tools.atlassian.net --email patrick.culligan@us.mcd.com --recurse

> https://mcd-tools.atlassian.net/wiki/spaces/SA/pages/1647640729/US+Restaurant+Next+Traffic+Analysis

Prerequisites:
- Place your Confluence API key in `conf-api-key.txt` at the project root (single line API token), or set `CONFLUENCE_API_KEY` env var.
- Ensure the `space` key and target `parent` page (id) exist in your Confluence instance.

Files created:
- `spec.md` — project specification and open questions.
- `repo_to_confluence.py` — publish a local Markdown tree into Confluence.
- `confluence_to_repo.py` — export a Confluence page tree into local Markdown files.
- `requirements.txt` — Python dependencies.

CLI arguments
-------------

When running `repo_to_confluence.py`, supply the following arguments:

- `source` (positional): Path to the local folder to sync. The script will walk this directory and treat each Markdown file as a Confluence page.
- `--space`: (required) Confluence space key where pages will be created or updated.
- `--parent`: (required) Parent page id under which the tree will be created. Use the numeric page id of the Confluence parent.
- `--base-url`: (optional) Base Confluence URL; defaults to `https://your-domain.atlassian.net/wiki`. For this project you will typically use `https://mcd-tools.atlassian.net`.
- `--email`: (optional) The Confluence account email used for API authentication (e.g., `patrick.culligan@us.mcd.com`). If omitted you'll be warned and must set the `CONFLUENCE_API_KEY` env var or `conf-api-key.txt`.
- `--exclude`: (optional) Path to a `.confluenceignore` file with glob patterns to exclude from the sync. If not provided, a `.confluenceignore` in the source root will be used if present. Default excludes include `.DS_Store`, `.git`, and `.gitignore`.
- `--dry-run`: (flag) Run without making changes to Confluence. Useful for previewing actions.
- `--verbose` / `-v`: (flag) Enable verbose logging for debugging.

When running `confluence_to_repo.py`, supply the following arguments:

- `output` (positional): Local output folder to write into. The root page becomes `readme.md` in this folder.
- `--page`: (required) Root Confluence page id to export.
- `--base-url`: (optional) Base Confluence URL; defaults to `https://your-domain.atlassian.net`.
- `--email`: (optional) The Confluence account email used for API authentication.
- `--recurse`: (flag) Export child pages recursively. Without this flag, only the specified page is exported.
- `--verbose` / `-v`: (flag) Enable verbose logging for debugging.

Exporter output shape:
- Pages with children become folders containing a `readme.md`.
- Leaf pages become Markdown files named from the page title.
- Referenced attachments and images are downloaded next to the exported page content.
- Internal Confluence page links are rewritten to relative Markdown links when the target page is part of the export.

Page identity mapping:
- Both scripts write a `.confluence-map.json` manifest into the source/output tree.
- The manifest stores local Markdown paths mapped to Confluence page IDs.
- `repo_to_confluence.py` reads this file on later runs so pages can be updated by stable page ID even if titles change.

Run reports:
- `repo_to_confluence.py` writes `repo_to_confluence_report.md` in the current working directory for each run.
- `confluence_to_repo.py` writes `confluence_to_repo_report.md` in the current working directory for each run.
- These reports overwrite the previous report of the same name and collect the run summary, warnings, conflicts, and link or attachment issues in a single Markdown file.

Config and auth files
---------------------

- `conf-api-key.txt`: Place your Confluence API token in this file at the project root (single line). Alternatively set the `CONFLUENCE_API_KEY` environment variable.
- `.confluenceignore`: Optional file placed in the `source` root listing glob patterns (one per line) to exclude from the sync.

Next steps: configure options and run the script once implemented. See `spec.md` for questions about behavior defaults.
