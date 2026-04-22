# Single Page Review

Use this prompt when you want an IDE agent to evaluate one page on its own terms after extraction and lightweight scoring are already available.

## Prompt

Review this page in the context of the local kurate working copy.

Inputs to consider:

- the Markdown content
- any `*.metadata.json` or `*.analysis.json` sidecars
- any appended `## Export Metadata` or `## Analysis Assessment` blocks
- related report entries if unsupported content or unresolved links are present

Please answer:

1. What kind of page is this?
2. Is it mostly substantive content, mostly organizational/navigation content, or mostly time-bound/project residue?
3. Does it appear durable, stale-but-useful, or obsolete?
4. Is it likely worth curating into a cleaner knowledge base?
5. What specific evidence supports that conclusion?

Return:

- `page_type`
- `substance_level`
- `durability_assessment`
- `suggested_action`
- `confidence`
- `rationale`

Do not assume this page is valuable just because it exists. Do not assume it should be archived just because it is old.
