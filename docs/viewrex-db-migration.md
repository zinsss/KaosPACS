# ViewRex Database Migration

ViewRex database migration is a future phase.

## Rules

- Extract from ViewRex read-only.
- Do not mutate the ViewRex database.
- Treat imports into KaosPACS as additive metadata.
- Do not migrate proprietary ViewRex workflow logic as the primary KaosPACS
  business logic.

## Direction

Migration tooling should live under:

```text
migration/viewrex/
```

Planned stages:

- `extract/`: read-only source export.
- `transform/`: normalize into KaosPACS import shape.
- `load/`: additive import.
- `schema/`: captured schema notes and scripts.
- `reports/`: reconciliation and audit output.
