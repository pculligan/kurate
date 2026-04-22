# Archive Decision

Use this prompt when a page or group of pages looks like an archive candidate but the decision still needs human-quality judgment.

## Prompt

Evaluate whether this content should remain in the active working knowledge set or be relegated to archive storage.

Consider:

- content substance
- current usefulness
- historical value
- readership signals
- whether the content is superseded elsewhere
- whether it is primarily useful for background rather than day-to-day discovery

Please answer:

1. Should this content stay active, be curated, be merged elsewhere, or be archived?
2. If it should be archived, what is the strongest reason?
3. What risk would we take by archiving it?
4. What short archive note should accompany that decision?

Return:

- `decision`
- `reason`
- `risk`
- `archive_note`

Treat archive as “relegated but still available,” not “destroyed.”
