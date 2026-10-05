---
status: accepted
---

# Accepted transcript prefixes are immutable

QuakePro reads each transcript prefix once, then trusts it for current process and any valid
disposable temp checkpoint. Later reconciliation reads appended suffixes only; rewrite,
truncation, deletion, or mutable side-file edits freeze prior contribution instead of rebuilding
history. This trades live correction of rare source rewrites for work bounded by new activity.
