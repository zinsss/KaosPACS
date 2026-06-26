# Modality Worklist

MWL is future scope. Do not implement it in the initial Orthanc/PostgreSQL
stack.

## Fixed Production Identity

```text
AET:  VIEWREX_WL
Port: 105
```

Legacy modalities expect this identity. Production behavior must preserve it.

## First Milestone

The first MWL implementation milestone should return a hardcoded patient for
BMD testing. This proves association, C-FIND handling, patient display, and
modality workflow before adding eGHIS integration.

## Later Milestone

Later MWL should derive worklist entries from eGHIS orders using read-only
database access or a read-only upstream feed. eGHIS polling must never mutate
the eGHIS database.
