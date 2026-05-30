# Wiki Schema

## Focus

General-purpose project knowledge base. Customize this section to describe what this wiki tracks.

## Page Types

- **summary** — Summary of a source document. One page per source file in raw/.
- **entity** — A person, organization, system, API, or named concept.
- **topic** — A theme or subject area synthesized across multiple sources.
- **analysis** — A comparison, evaluation, or synthesis produced from a query.

## Page Format Example

```yaml
---
title: Authentication Flow
tags: [auth, architecture, security]
created: 2026-01-15
updated: 2026-01-20
sources: [auth-design-doc.md, api-spec.md]
---
```

## Tag Conventions

- Lowercase, hyphen-separated: `machine-learning`, `api-design`
- Use broad category tags plus specific topic tags
- Keep the tag vocabulary under 30 unique tags for navigability

## Ingest Focus

When ingesting sources, emphasize: key facts, named entities, decisions with rationale, relationships between concepts, and anything that contradicts or extends existing wiki pages.

## Excluded Topics

Nothing excluded by default. Add topics here that should be skipped during ingest.
