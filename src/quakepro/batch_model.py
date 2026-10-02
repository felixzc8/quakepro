"""Shared state projection for grouped child-agent launches."""
from __future__ import annotations


def sync_batch_statuses(nodes) -> bool:
    """Update batch state and activity time from its children."""
    changed = False
    for batch in (node for node in nodes.values() if node.kind == "dispatch"):
        children = [nodes[child_id] for child_id in batch.children if child_id in nodes]
        statuses = {child.status for child in children}
        status = (
            "error" if "error" in statuses else
            "running" if "running" in statuses else
            "waiting" if "waiting" in statuses else
            "done" if children else
            "running"
        )
        if batch.status != status:
            batch.status = status
            changed = True
        latest = max((child.last_ts for child in children), default=0.0)
        if batch.last_ts != latest:
            batch.last_ts = latest
            changed = True
    return changed
