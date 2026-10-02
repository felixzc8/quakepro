"""Event-driven reconciliation of session files into provider models."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from typing import AsyncIterator, Callable, Iterable

from watchfiles import awatch

from .lifecycle import LifecycleJournal, SessionLifecycle
from .session_model import LifecycleSessionModel, SessionModel


class ObservationError(RuntimeError):
    pass


async def reconcile_async(reconcile: Callable[[], bool]) -> bool:
    """Run read-only reconciliation without delaying process shutdown."""
    loop = asyncio.get_running_loop()
    completed = loop.create_future()

    def publish(result, error) -> None:
        if completed.done():
            return
        if error is None:
            completed.set_result(result)
        else:
            completed.set_exception(error)

    def run() -> None:
        try:
            result = reconcile()
            error = None
        except BaseException as exc:
            result = None
            error = exc
        try:
            loop.call_soon_threadsafe(publish, result, error)
        except RuntimeError:
            pass

    threading.Thread(
        target=run,
        name="quakepro-reconcile",
        daemon=True,
    ).start()
    return await completed


def _directory(path: str) -> Path:
    candidate = Path(path).expanduser().absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _minimal_roots(paths: Iterable[str]) -> tuple[str, ...]:
    roots: list[Path] = []
    for candidate in sorted({_directory(path) for path in paths}, key=lambda path: len(path.parts)):
        if any(candidate == root or root in candidate.parents for root in roots):
            continue
        roots.append(candidate)
    return tuple(str(root) for root in roots)


class SessionObservation:
    """Reconcile one session only when its local sources report a change."""

    def __init__(
        self,
        model: SessionModel,
        watcher: Callable[..., AsyncIterator] = awatch,
        settle_limit: int = 32,
    ) -> None:
        self._model = model
        self._watcher = watcher
        self._settle_limit = settle_limit
        identity = getattr(model, "lifecycle_identity", lambda: None)()
        self._lifecycle_identity = None
        self._journal = None
        self._lifecycle = None
        self._bind_lifecycle(identity)
        self._projection = model.nodes

    def _bind_lifecycle(self, identity) -> None:
        if identity is not None:
            if (
                not isinstance(self._model, SessionModel)
                or not isinstance(self._model, LifecycleSessionModel)
                or not callable(getattr(self._model, "apply_lifecycle", None))
            ):
                raise TypeError("non-null lifecycle identity requires LifecycleSessionModel")
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or any(not isinstance(value, str) or not value for value in identity)
            ):
                raise TypeError("LifecycleSessionModel returned invalid lifecycle identity")
            self._journal = LifecycleJournal(identity[0], identity[1])
            self._lifecycle = SessionLifecycle(self._model)
        else:
            self._journal = None
            self._lifecycle = None
        self._lifecycle_identity = identity

    @property
    def ended(self) -> bool:
        return bool(self._lifecycle and self._lifecycle.ended)

    def roots(self) -> tuple[str, ...]:
        paths = set(self._model.observation_paths())
        if self._journal:
            paths.add(str(self._journal.path))
        return _minimal_roots(paths)

    def reconcile(self) -> bool:
        changed = False
        for _ in range(self._settle_limit):
            current = bool(self._model.poll())
            identity = getattr(self._model, "lifecycle_identity", lambda: None)()
            projection = self._model.nodes
            if identity != self._lifecycle_identity or projection is not self._projection:
                self._bind_lifecycle(identity)
            self._projection = projection
            changed |= current
            if current:
                continue
            if self._journal and self._lifecycle:
                current |= self._lifecycle.ingest(self._journal.read())
                current |= self._lifecycle.enforce()
            changed |= current
            if not current:
                return changed
        raise ObservationError("session reconciliation did not settle")

    @staticmethod
    def _accepts(source, paths: frozenset[str]) -> bool:
        accepts = getattr(source, "accepts_observation", None)
        return bool(accepts(paths)) if accepts else True

    async def changes(self) -> AsyncIterator[frozenset[str]]:
        while True:
            roots = self.roots()
            if not roots:
                raise ObservationError("session has no observable paths")
            try:
                async for changes in self._watcher(
                    *roots,
                    debounce=100,
                    step=20,
                    rust_timeout=86_400_000,
                    force_polling=False,
                    recursive=True,
                ):
                    changed_paths = frozenset(path for _, path in changes)
                    journal_changed = bool(
                        self._journal and str(self._journal.path) in changed_paths
                    )
                    model_changed = self._accepts(self._model, changed_paths)
                    if not journal_changed and not model_changed:
                        continue
                    changed = await reconcile_async(self.reconcile)
                    roots_changed = self.roots() != roots
                    if changed:
                        yield changed_paths
                    if roots_changed:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ObservationError("filesystem observation failed") from exc
