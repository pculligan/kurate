# Site Story Review

Use this prompt when you want the IDE agent to reason about a whole extracted area rather than one page or one pair of pages.

## Prompt

Review this extracted documentation area as a whole.

Consider:

- the structure of folders and pages
- high-level page titles
- analysis scores and metadata
- unsupported-content markers
- obvious clusters of project-specific material

Please answer:

1. What is the overall story of this documentation area?
2. What appears to be the core durable knowledge?
3. What appears to be navigational scaffolding, process exposition, or project residue?
4. Where is the content architecture weak, confusing, or duplicative?
5. What would a cleaner information architecture look like?

Return:

- `area_summary`
- `durable_content_candidates`
- `organizational_content_candidates`
- `project_residue_candidates`
- `architecture_problems`
- `proposed_cleanup_direction`

Focus on helping a human understand the shape of the mess, not just scoring individual pages.
