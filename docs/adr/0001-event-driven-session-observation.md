---
status: accepted
---

# Event-driven session observation

QuakePro sleeps between observation signals. Lifecycle hooks provide lifecycle facts; native filesystem notifications wake transcript reconciliation; presentation changes only after session projection or user-input changes. Recurring polling, rolling age text, and timer-driven animation are excluded because they spend power while adding latency.

## Considered Options

- Recurring polling was rejected because it wakes during idle time and delays updates until next interval.
- Hook-only observation was rejected because hooks do not carry every action needed by full timeline.

## Consequences

- Filesystem notifications are wakeups, not content authority.
- Silence never means completion.
- Watcher failure is visible; QuakePro does not fall back to polling.
