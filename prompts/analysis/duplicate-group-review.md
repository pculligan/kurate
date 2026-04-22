# Duplicate Group Review

Use this prompt after a likely duplicate or overlap group has already been identified by lightweight heuristics or manual suspicion.

## Prompt

Compare these pages from the local kurate working copy.

Inputs to consider:

- Markdown content for each page
- extraction metadata
- analysis metadata
- titles, paths, and relative locations in the corpus

Please determine:

1. Are these pages duplicative, overlapping, complementary, or conflicting?
2. Is one page the most likely canonical reference?
3. Which pages, if any, should probably be merged?
4. Which pages, if any, should probably be archived or retained only as historical context?
5. What evidence supports those conclusions?

Return:

- `relationship_type`
- `canonical_candidate`
- `merge_candidates`
- `archive_candidates`
- `keep_separate_reason`
- `rationale`

Prefer explicit uncertainty over pretending to know.
