# Extraction Phase

The extraction phase pulls content out of an upstream system and converts it into a cleaner local representation that later phases can work with.

For this suite, extraction is currently implemented through the Confluence provider. It exports content into Markdown, downloads referenced attachments, captures metadata and usage signals, and produces reports that make the conversion auditable.

The extraction phase exists to create a stable offline corpus that is easier to inspect, score, compare, and curate than the original source system.

