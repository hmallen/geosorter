# Wiki Schema

## Focus

geosorter architecture, operating contracts, DJI media-format knowledge, safety
invariants, and the current user-facing/maintenance surface. Pages should describe
shipped behavior on `main`, not the historical task phase in which it first landed.

## Page Types

- **summary** — Summary of a source document. One page per source file in raw/.
- **entity** — A person, organization, system, API, or named concept.
- **topic** — A theme or subject area synthesized across multiple sources.
- **analysis** — A comparison, evaluation, or synthesis produced from a query.
- **current-state** — A maintained snapshot of the shipped product, entry points,
  constraints, and known operational limits.

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

For code-backed pages, prefer the current implementation and tests over old task
names or roadmap labels. Mark optional dependencies and security boundaries
explicitly, and keep CLI/API/UI wording aligned with actual behavior.

## Excluded Topics

Nothing excluded by default. Add topics here that should be skipped during ingest.
