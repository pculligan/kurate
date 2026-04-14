# Confluence Utils

Small CLI tools for moving content between a local Markdown repo and Confluence:

- `repo_to_confluence.py`: sync a local Markdown tree into Confluence pages.
- `confluence_to_repo.py`: export a Confluence page tree back into Markdown files.

## What This Repo Does

`repo_to_confluence.py`:
- Walks a local folder tree and treats each Markdown file as a Confluence page.
- Uses each file's first `# H1` as the Confluence page title.
- Uploads local images as Confluence attachments.
- Rewrites relative Markdown links to Confluence page links when the target page is part of the same sync.
- Writes a `confluence-map.json` manifest so later runs can update pages by stable page id.
- If the source folder is a git repo, attempts to stage and commit `confluence-map.json` automatically.
- Produces a timestamped run report in `reports/`.

`confluence_to_repo.py`:
- Pulls a Confluence page tree into a local folder tree.
- Writes page content as Markdown.
- Downloads attachments referenced by exported pages.
- Rewrites internal Confluence page links to relative Markdown links when possible.
- Writes a `confluence-map.json` manifest and a timestamped run report in `reports/`.

## Requirements

- Python 3.10+
- A Confluence Cloud API token
- Your Confluence account email
- The correct Confluence base URL, for example `https://mcd-tools.atlassian.net`

Important:
- Pass the site base URL without `/wiki`.
- Reports are written automatically to `reports/`, which is gitignored.

## Setup

Recommended:

```bash
./setup.sh
```

What `setup.sh` does:

- Creates or reuses `.venv`
- Installs `requirements.txt`
- Checks whether `conf-api-key.txt` already exists
- Falls back to checking `CONFLUENCE_API_KEY`
- Tells you what to do next only if no token is found

Manual setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Preferred:

```bash
printf '%s\n' 'your-token' > conf-api-key.txt
```

Fallback:

```bash
export CONFLUENCE_API_KEY="your-token"
```

## Sync A Local Repo To Confluence

Basic command:

```bash
python3 repo_to_confluence.py /path/to/local/repo \
  --space YOURSPACE \
  --parent PARENT_PAGE_ID \
  --base-url https://mcd-tools.atlassian.net \
  --email you@example.com
```

Recommended first run:

```bash
python3 repo_to_confluence.py /path/to/local/repo \
  --space YOURSPACE \
  --parent PARENT_PAGE_ID \
  --base-url https://mcd-tools.atlassian.net \
  --email you@example.com \
  --dry-run
```

Segment example:

```bash
python3 repo_to_confluence.py ../corp-segment-projects \
  --space SA \
  --parent 84969480 \
  --base-url https://mcd-tools.atlassian.net \
  --email patrick.culligan@us.mcd.com
```

Expected source conventions:

- Each Markdown file should have a leading `# Title`.
- A folder `readme.md` becomes that folder's page.
- Other Markdown files in the folder become child pages under that folder page.
- Optional `confluenceignore` files can exclude files or folders with glob patterns.

## Export From Confluence To Markdown

Basic command:

```bash
python3 confluence_to_repo.py /path/to/output \
  --page PAGE_ID \
  --base-url https://mcd-tools.atlassian.net \
  --email you@example.com \
  --recurse
```

Example:

```bash
python3 confluence_to_repo.py ./output \
  --page 1647640729 \
  --base-url https://mcd-tools.atlassian.net \
  --email patrick.culligan@us.mcd.com \
  --recurse
```

Reference page:

`https://mcd-tools.atlassian.net/wiki/spaces/SA/pages/1647640729/US+Restaurant+Next+Traffic+Analysis`

## CLI Reference

`repo_to_confluence.py`:

- `source`: local folder to sync
- `--space`: required Confluence space key
- `--parent`: required numeric parent page id
- `--base-url`: optional, defaults to `https://your-domain.atlassian.net`
- `--email`: optional, but normally required for API auth
- `--exclude`: optional path to a `confluenceignore` file
- `--dry-run`: preview changes without writing to Confluence
- `--verbose` or `-v`: verbose logging

`confluence_to_repo.py`:

- `output`: local folder to write the export into
- `--page`: required root Confluence page id
- `--base-url`: optional, defaults to `https://your-domain.atlassian.net`
- `--email`: optional, but normally required for API auth
- `--recurse`: export child pages recursively
- `--verbose` or `-v`: verbose logging

## Files The Tools Create

- `confluence-map.json`: stable page-id manifest written into the source or output tree
- `reports/*.md`: timestamped run reports for each sync or export

For `repo_to_confluence.py`, if the source folder is inside a git repo, the tool will try to stage and commit `confluence-map.json` for you. You should still push that commit afterward.

Report filenames look like:

- `reports/repo_to_confluence_report_2026-04-09_14-32-10.md`
- `reports/confluence_to_repo_report_2026-04-09_14-32-10.md`

## Usage Notes

- Start with `--dry-run`.
- Do not include `/wiki` in `--base-url`.
- The first `# H1` in each Markdown file becomes the Confluence page title.
- The ignore file is named `confluenceignore` without a leading period in the source repo.
- Review the generated report in `reports/` after each run.
