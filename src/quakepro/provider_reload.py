"""One atomic reload transaction shared by every provider adapter."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, Union

from .bounded_json import (
    JSONL_RECORD_LIMIT,
    JsonNumberToken,
    StreamingJsonValidator,
    decode_json_record,
    validate_json_record,
)
from .provider_admission import (
    AdmittedRecord,
    AdmissionProof,
    read_admission_result,
)
from .provider_schema import finite_json_value
from .source_cohort import (
    DirectoryStamp,
    FileStamp,
    _directory_stamp,
    _open_file_stamp,
    _regular_file_stamp,
    _stamp_tuple,
)


TEXT_SOURCE_LIMIT = 8 * 1024 * 1024
_ACCEPTED_STATE = "_provider_reload_acceptance"
_REVISION_STATE = "_provider_reload_revision"
_SOURCE_REVISION_STATE = "_provider_reload_source_revision"
_VISIBLE_REVISION_STATE = "_provider_reload_visible_revision"


def _canonical(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


@dataclass(frozen=True)
class DirectoryRule:
    path: str
    relevant: Callable[[str, str], bool]
    recursive: bool = False
    include_files: bool = True
    admit: Callable[
        [object, str, object, dict[str, object]], bool | None
    ] | None = None


@dataclass(frozen=True)
class ReloadPlan:
    files: tuple[str, ...] = ()
    directories: tuple[DirectoryRule, ...] = ()
    context: object = None
    optional_files: tuple[str, ...] = ()
    followup: Callable[[object], object] | None = None
    admission_context: object = None
    admission_provider: str | None = None


@dataclass(frozen=True)
class CapturedRecord:
    start: int
    end: int
    raw: bytes
    value: object


@dataclass(frozen=True)
class CapturedFile:
    path: str
    stamp: FileStamp
    records: tuple[CapturedRecord, ...] = ()
    value: object = None
    text: str | None = None
    raw: bytes = b""
    offset: int = 0
    available: bool = True
    complete: bool = True

    @property
    def signature(self) -> tuple[int, int, int, int, int, int]:
        stamp = self.stamp
        return (
            stamp.device,
            stamp.inode,
            stamp.mode,
            stamp.size,
            stamp.mtime_ns,
            stamp.ctime_ns,
        )

    def records_from(self, offset: int) -> tuple[object, ...]:
        return tuple(record.value for record in self.records if record.start >= offset)

    def all_records(self) -> tuple[object, ...]:
        return tuple(record.value for record in self.records)

@dataclass(frozen=True)
class _AdmissionEntry:
    admitted: AdmittedRecord | None
    failure: str = ""

    @property
    def proof(self) -> AdmissionProof | None:
        return self.admitted.proof if self.admitted is not None else None


@dataclass(frozen=True)
class CapturedCohort:
    request: ReloadPlan
    files: tuple[CapturedFile, ...]
    directories: tuple[DirectoryStamp, ...]
    action: str = "bootstrap"
    prior_offsets: tuple[tuple[str, int], ...] = ()
    admission_records: tuple[tuple[str, _AdmissionEntry], ...] = ()
    denied: tuple[tuple[str, FileStamp], ...] = ()

    @property
    def proof(self) -> tuple[object, ...]:
        return (
            tuple((source.path, source.stamp) for source in self.files),
            self.directories,
        )

    def file(self, path: str) -> CapturedFile | None:
        target = _canonical(path)
        return next((source for source in self.files if source.path == target), None)

    def stamp(self, path: str) -> FileStamp | None:
        source = self.file(path)
        return source.stamp if source is not None else None

    def records_for(self, path: str) -> tuple[object, ...]:
        source = self.file(path)
        if source is None:
            return ()
        return source.all_records()

    def admission_for(self, path: str) -> AdmittedRecord | None:
        entry = dict(self.admission_records).get(_canonical(path))
        return entry.admitted if entry is not None else None

    def admission_failure_for(self, path: str) -> str:
        entry = dict(self.admission_records).get(_canonical(path))
        return entry.failure if entry is not None else ""


@dataclass(frozen=True)
class _AcceptedFile:
    stamp: FileStamp
    offset: int
    retired: bool = False
    checked: bool = True

@dataclass(frozen=True)
class _AcceptedCohort:
    files: tuple[tuple[str, _AcceptedFile], ...]
    directories: tuple[DirectoryStamp, ...] | None
    denied: tuple[tuple[str, FileStamp], ...]
    plan_key: tuple[object, ...] | None
    admission_key: tuple[
        tuple[str, FileStamp, AdmissionProof | None], ...
    ] | None
    admission_records: tuple[tuple[str, _AdmissionEntry], ...] = ()
    bootstrapped: bool = True


@dataclass(frozen=True)
class _DiscoveredCohort:
    request: ReloadPlan
    files: tuple[tuple[str, FileStamp], ...]
    directories: tuple[DirectoryStamp, ...]
    denied: tuple[tuple[str, FileStamp], ...] = ()
    admission_records: tuple[tuple[str, _AdmissionEntry], ...] = ()
    admission_key: tuple[
        tuple[str, FileStamp, AdmissionProof | None], ...
    ] | None = None

    @property
    def proof(self) -> tuple[object, ...]:
        return self.files, self.directories, self.denied, self.admission_key


class AppendParticipant(Protocol):
    def reload_plan(self) -> ReloadPlan:
        ...

    def prepare_append(self, batch: CapturedCohort) -> object:
        ...

    def apply_append(self, prepared: object) -> bool:
        ...

    def settle_idle(self) -> bool:
        ...


def _materialize_number(value: JsonNumberToken) -> object:
    lexeme = value.lexeme
    if "." not in lexeme and "e" not in lexeme.lower():
        digits = lexeme[1:] if lexeme.startswith("-") else lexeme
        return int(lexeme) if len(digits) <= 128 else value
    parsed = float(lexeme)
    return parsed if math.isfinite(parsed) else value


def _materialize_numbers(value: object) -> object:
    if type(value) is JsonNumberToken:
        return _materialize_number(value)
    if type(value) is list:
        return [_materialize_numbers(item) for item in value]
    if type(value) is dict:
        return {key: _materialize_numbers(item) for key, item in value.items()}
    return value


def strict_json_record(raw: bytes) -> object:
    if len(raw) > JSONL_RECORD_LIMIT:
        raise ValueError("JSONL record exceeds 64 MiB")
    if not validate_json_record(raw):
        raise ValueError("invalid JSONL record")
    try:
        value = _materialize_numbers(decode_json_record(raw))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid JSONL record") from exc
    if not finite_json_value(value):
        raise ValueError("invalid JSON value")
    return value


def strict_jsonl_records(source: Union[str, bytes]) -> Iterator[object]:
    if type(source) is bytes:
        lines = source.splitlines(keepends=True)
        if source and not source.endswith(b"\n"):
            raise ValueError("incomplete JSONL record")
        for line in lines:
            raw = line[:-1] if line.endswith(b"\n") else line
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if raw.strip():
                yield strict_json_record(raw)
        return

    with open(source, "rb") as stream:
        while True:
            line = stream.readline(JSONL_RECORD_LIMIT + 2)
            if not line:
                return
            complete = line.endswith(b"\n")
            raw = line[:-1] if complete else line
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if len(raw) > JSONL_RECORD_LIMIT:
                raise ValueError("JSONL record exceeds 64 MiB")
            if not complete:
                raise ValueError("incomplete JSONL record")
            if raw.strip():
                yield strict_json_record(raw)


def _admission_record(
    path: str,
    stamp: FileStamp,
    provider: str,
) -> _AdmissionEntry:
    result = read_admission_result(path, provider)
    if result.stamp != stamp:
        raise ValueError("source changed during admission read")
    return _AdmissionEntry(result.admitted, result.failure)


def _admission_key(
    records: dict[str, _AdmissionEntry],
    stamps: dict[str, FileStamp],
) -> tuple[tuple[str, FileStamp, AdmissionProof | None], ...]:
    return tuple(
        (path, stamps[path], record.proof if record is not None else None)
        for path, record in sorted(records.items())
    )


def _drop_listed_file(
    path: str, directory_stamps: dict[str, DirectoryStamp],
) -> None:
    directory = os.path.dirname(path)
    stamp = directory_stamps.get(directory)
    if stamp is None:
        return
    name = os.path.basename(path)
    directory_stamps[directory] = DirectoryStamp(
        stamp.path,
        tuple(entry for entry in stamp.entries if entry[0] != name),
    )


def source_signature(path: str) -> tuple[int, ...]:
    return _stamp_tuple(_regular_file_stamp(_canonical(path)))


def file_signature(path: str) -> tuple[int, ...] | None:
    try:
        return source_signature(path)
    except OSError:
        return None


def _same_generation(left: FileStamp, right: FileStamp) -> bool:
    return (
        left.device,
        left.inode,
        left.mode,
    ) == (
        right.device,
        right.inode,
        right.mode,
    )


def _source_kind(path: str) -> str:
    if path.endswith(".jsonl"):
        return "jsonl"
    if path.endswith(".json"):
        return "json"
    return "text"


def _append_discover(
    request: ReloadPlan,
    accepted: _AcceptedCohort | None,
) -> _DiscoveredCohort:
    required = {_canonical(path) for path in request.files}
    optional = {_canonical(path) for path in request.optional_files}
    paths = required | optional
    listed: set[str] = set()
    directory_stamps: dict[str, DirectoryStamp] = {}
    admissions: dict[
        str, Callable[[object, str, object, dict[str, object]], bool | None]
    ] = {}
    for rule in request.directories:
        pending = [_canonical(rule.path)]
        seen = set()
        while pending:
            directory = pending.pop()
            if directory in seen:
                continue
            seen.add(directory)
            stamp = _directory_stamp(directory, rule.relevant)
            directory_stamps[directory] = stamp
            for name, kind in stamp.entries:
                path = _canonical(os.path.join(directory, name))
                if kind == "directory" and rule.recursive:
                    pending.append(path)
                elif kind == "file" and rule.include_files:
                    paths.add(path)
                    listed.add(path)
                    if rule.admit is not None:
                        admissions[path] = rule.admit

    prior_files = dict(accepted.files) if accepted is not None else {}
    stamps: dict[str, FileStamp] = {}
    for path in sorted(paths):
        try:
            stamps[path] = _regular_file_stamp(path)
        except OSError:
            prior = prior_files.get(path)
            if path in required and prior is None:
                raise ValueError("required source is unavailable")
            if path in listed:
                _drop_listed_file(path, directory_stamps)

    known_records = (
        dict(accepted.admission_records) if accepted is not None else {}
    )
    records: dict[str, _AdmissionEntry] = {}
    provider = request.admission_provider
    if provider is not None and provider not in ("codex", "pi"):
        raise ValueError("provider admission requires provider")
    for path in sorted(required):
        stamp = stamps.get(path)
        if stamp is None or not path.endswith(".jsonl") or provider is None:
            continue
        prior = prior_files.get(path)
        if (
            prior is not None
            and not prior.retired
            and _same_generation(prior.stamp, stamp)
            and stamp.size >= prior.offset
            and path in known_records
        ):
            records[path] = known_records[path]
        elif prior is None:
            records[path] = _admission_record(path, stamp, provider)

    proofs = {
        path: entry.proof if entry is not None else None
        for path, entry in records.items()
    }
    prior_denied = dict(accepted.denied) if accepted is not None else {}
    admitted_files: list[tuple[str, FileStamp]] = []
    denied: list[tuple[str, FileStamp]] = []
    for path, stamp in sorted(stamps.items()):
        admit = admissions.get(path)
        if path in required or path in optional or admit is None:
            admitted_files.append((path, stamp))
            continue
        prior = prior_files.get(path)
        if (
            prior is not None
            and not prior.retired
            and _same_generation(prior.stamp, stamp)
            and stamp.size >= prior.offset
        ):
            admitted_files.append((path, stamp))
            if path in known_records:
                records[path] = known_records[path]
            continue
        prior_denial = prior_denied.get(path)
        prior_record = known_records.get(path)
        if (
            prior_denial is not None
            and (prior_record is None or prior_record.proof is not None)
            and _same_generation(prior_denial, stamp)
            and stamp.size >= prior_denial.size
        ):
            denied.append((path, stamp))
            if path in known_records:
                records[path] = known_records[path]
            continue
        if provider is None:
            raise ValueError("provider admission requires provider")
        try:
            entry = _admission_record(path, stamp, provider)
        except (OSError, ValueError):
            _drop_listed_file(path, directory_stamps)
            continue
        records[path] = entry
        decision = (
            False
            if entry.proof is None
            else admit(request.admission_context, path, entry.proof, proofs)
        )
        if decision is True:
            admitted_files.append((path, stamp))
        else:
            denied.append((path, stamp))
            _drop_listed_file(path, directory_stamps)

    required_records = {
        path: records[path] for path in sorted(required) if path in records
    }
    admission_key = (
        _admission_key(required_records, stamps)
        if provider is not None else None
    )
    return _DiscoveredCohort(
        request,
        tuple(admitted_files),
        tuple(directory_stamps[path] for path in sorted(directory_stamps)),
        tuple(denied),
        tuple(sorted(records.items())),
        admission_key,
    )


def _read_jsonl_suffix(
    stream,
    size: int,
    prior_offset: int,
) -> tuple[tuple[CapturedRecord, ...], int, bool]:
    if prior_offset < 0 or prior_offset > size:
        raise ValueError("accepted offset exceeds source")
    stream.seek(prior_offset)
    position = prior_offset
    records = []
    while position < size:
        remaining = size - position
        line = stream.readline(min(JSONL_RECORD_LIMIT + 3, remaining))
        if not line:
            raise ValueError("source changed during suffix read")
        if not line.endswith(b"\n"):
            if len(line) > JSONL_RECORD_LIMIT + 1 or len(line) != remaining:
                raise ValueError("JSONL record exceeds 64 MiB")
            return tuple(records), position, False
        raw = line[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if len(raw) > JSONL_RECORD_LIMIT:
            raise ValueError("JSONL record exceeds 64 MiB")
        start = position
        position += len(line)
        if raw.strip():
            records.append(CapturedRecord(
                start,
                position,
                raw,
                strict_json_record(raw),
            ))
    return tuple(records), position, True


def _read_append_file(
    path: str,
    stamp: FileStamp,
    prior_offset: int,
    optional: bool,
    opener: Callable,
) -> CapturedFile:
    kind = _source_kind(path)
    if kind == "text" and optional and stamp.size > TEXT_SOURCE_LIMIT:
        return CapturedFile(
            path, stamp, offset=stamp.size, available=False,
        )
    with opener(path, "rb") as stream:
        opened = _open_file_stamp(stream)
        if not _same_generation(opened, stamp) or opened.size < stamp.size:
            raise ValueError("source changed before read")
        if kind == "jsonl":
            records, offset, _tail_complete = _read_jsonl_suffix(
                stream, stamp.size, prior_offset,
            )
            source = CapturedFile(
                path,
                stamp,
                records=records,
                offset=offset,
                complete=True,
            )
        else:
            limit = JSONL_RECORD_LIMIT if kind == "json" else TEXT_SOURCE_LIMIT
            length = stamp.size - prior_offset
            if prior_offset < 0 or length < 0 or length > limit:
                raise ValueError("source exceeds read limit")
            stream.seek(prior_offset)
            raw = stream.read(length + 1)
            if len(raw) != length:
                raise ValueError("source changed during read")
            try:
                text_value = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("invalid source UTF-8") from exc
            if kind == "json":
                value = strict_json_record(raw)
                source = CapturedFile(
                    path,
                    stamp,
                    value=value,
                    raw=raw,
                    offset=stamp.size,
                )
            else:
                source = CapturedFile(
                    path,
                    stamp,
                    text=text_value,
                    raw=raw,
                    offset=stamp.size,
                )
        finished = _open_file_stamp(stream)
        if not _same_generation(finished, stamp) or finished.size < stamp.size:
            raise ValueError("source changed during read")
    return source


def _captured_stable(source: CapturedFile) -> bool:
    try:
        current = _regular_file_stamp(source.path)
    except OSError:
        return False
    if not _same_generation(current, source.stamp):
        return False
    if current.size < source.stamp.size:
        return False
    if _source_kind(source.path) == "json" and current != source.stamp:
        return False
    return current == source.stamp or current.size > source.stamp.size


def reload_acceptance_state(participant: AppendParticipant):
    accepted = getattr(participant, _ACCEPTED_STATE, None)
    if not isinstance(accepted, _AcceptedCohort):
        return None, ()
    directories = (
        None
        if accepted.directories is None
        else tuple((stamp.path, stamp.entries) for stamp in accepted.directories)
    )
    denied = tuple(
        (path, _stamp_tuple(stamp)) for path, stamp in accepted.denied
    )
    return directories, denied


def valid_reload_acceptance_state(directories, denied) -> bool:
    if directories is not None and (
        type(directories) is not tuple
        or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or item[0] != _canonical(item[0])
            or type(item[1]) is not tuple
            or any(
                type(entry) is not tuple
                or len(entry) != 2
                or type(entry[0]) is not str
                or not entry[0]
                or entry[1] not in {"file", "directory", "nonregular"}
                for entry in item[1]
            )
            for item in directories
        )
        or len({item[0] for item in directories}) != len(directories)
    ):
        return False
    return (
        type(denied) is tuple
        and all(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is str
            and item[0] == _canonical(item[0])
            and type(item[1]) is tuple
            and len(item[1]) == 6
            and all(type(value) is int for value in item[1])
            for item in denied
        )
        and len({item[0] for item in denied}) == len(denied)
    )


def seed_reload_acceptance(
    participant: AppendParticipant,
    files,
    directories=None,
    denied=(),
) -> None:
    if not valid_reload_acceptance_state(directories, denied):
        raise ValueError("invalid reload acceptance state")
    accepted = []
    for item in files:
        if len(item) == 4:
            path, signature, offset, _digest = item
        elif len(item) == 5:
            path, signature, offset, _digest, _legacy_state = item
        else:
            raise ValueError("invalid reload acceptance file")
        accepted.append((
            _canonical(path),
            _AcceptedFile(
                FileStamp(*signature),
                offset,
                False,
                False,
            ),
        ))
    request = participant.reload_plan()
    if not isinstance(request, ReloadPlan):
        raise TypeError("reload participant returned invalid plan")
    setattr(
        participant,
        _ACCEPTED_STATE,
        _AcceptedCohort(
            tuple(sorted(accepted)),
            None if directories is None else tuple(
                DirectoryStamp(path, entries) for path, entries in directories
            ),
            tuple(
                (_canonical(path), FileStamp(*signature))
                for path, signature in denied
            ),
            _plan_key(request),
            None,
            (),
            True,
        ),
    )


def reload_revision(participant: AppendParticipant) -> int:
    value = getattr(participant, _REVISION_STATE, 0)
    return value if type(value) is int and value >= 0 else 0


def source_revision(participant: AppendParticipant) -> int:
    value = getattr(
        participant,
        _SOURCE_REVISION_STATE,
        getattr(participant, _REVISION_STATE, 0),
    )
    return value if type(value) is int and value >= 0 else 0


def visible_revision(participant: AppendParticipant) -> int:
    if hasattr(participant, _VISIBLE_REVISION_STATE):
        value = getattr(participant, _VISIBLE_REVISION_STATE)
    elif hasattr(participant, _SOURCE_REVISION_STATE):
        value = 0
    else:
        value = getattr(participant, _REVISION_STATE, 0)
    return value if type(value) is int and value >= 0 else 0


def advance_visible_revision(participant: AppendParticipant) -> int:
    revision = visible_revision(participant) + 1
    _set_visible_revision(participant, revision)
    return revision


def accepts_observation(participant: AppendParticipant, paths) -> bool:
    accepted = getattr(participant, _ACCEPTED_STATE, None)
    admitted = dict(accepted.files) if isinstance(accepted, _AcceptedCohort) else {}
    denied = dict(accepted.denied) if isinstance(accepted, _AcceptedCohort) else {}
    known_records = (
        dict(accepted.admission_records)
        if isinstance(accepted, _AcceptedCohort) else {}
    )
    request = participant.reload_plan()
    if not isinstance(request, ReloadPlan):
        return True
    admit = next(
        (rule.admit for rule in request.directories if rule.admit is not None),
        None,
    )
    denial_context_current = (
        isinstance(accepted, _AcceptedCohort)
        and accepted.plan_key == _plan_key(request)
    )
    if denial_context_current:
        accepted_files = dict(accepted.files)
        try:
            denial_context_current = all(
                path in accepted_files
                and _regular_file_stamp(path) == accepted_files[path].stamp
                for path in (_canonical(item) for item in request.files)
            )
        except OSError:
            denial_context_current = False
    for raw_path in paths:
        path = _canonical(raw_path)
        if path in admitted:
            return True
        prior = denied.get(path)
        if prior is not None:
            if not denial_context_current:
                return True
            try:
                current = _regular_file_stamp(path)
                prior_record = known_records.get(path)
                if current == prior:
                    continue
                if not (
                    (prior_record is None or prior_record.proof is not None)
                    and _same_generation(current, prior)
                    and current.size >= prior.size
                ):
                    return True
            except OSError:
                return True
            continue
        if admit is None or not path.endswith(".jsonl"):
            return True
        try:
            stamp = _regular_file_stamp(path)
            provider = request.admission_provider
            if provider not in ("codex", "pi"):
                return True
            record = _admission_record(path, stamp, provider)
            if not isinstance(accepted, _AcceptedCohort):
                return True
            known = known_records
            required_paths = tuple(
                candidate
                for candidate in (
                    _canonical(item) for item in request.files
                )
                if candidate.endswith(".jsonl")
            )
            if any(candidate not in known for candidate in required_paths):
                return True
            required = {
                candidate: known[candidate] for candidate in required_paths
            }
        except (OSError, ValueError):
            return True
        proofs = {
            required_path: required_record.proof
            if required_record is not None else None
            for required_path, required_record in required.items()
        }
        if record.proof is None or admit(
            request.admission_context, path, record.proof, proofs,
        ) is not False:
            return True
    return False


_CANDIDATE_FAILURES = (
    OSError,
    UnicodeError,
    TypeError,
    ValueError,
    AttributeError,
    KeyError,
    RecursionError,
)


def _plan_key(request: ReloadPlan) -> tuple[object, ...]:
    rules = sorted(
        request.directories,
        key=lambda rule: (
            _canonical(rule.path), rule.recursive, rule.include_files,
        ),
    )
    return (
        tuple(sorted(_canonical(path) for path in request.files)),
        tuple(sorted(_canonical(path) for path in request.optional_files)),
        tuple(
            (
                _canonical(rule.path),
                rule.recursive,
                rule.include_files,
                rule.relevant,
                rule.admit,
            )
            for rule in rules
        ),
        request.context,
        request.followup,
        request.admission_context,
        request.admission_provider,
    )


def _set_source_revision(participant: AppendParticipant, value: int) -> None:
    setattr(participant, _SOURCE_REVISION_STATE, value)
    setattr(participant, _REVISION_STATE, value)


def _set_visible_revision(participant: AppendParticipant, value: int) -> None:
    setattr(participant, _VISIBLE_REVISION_STATE, value)


def _retired(cursor: _AcceptedFile) -> _AcceptedFile:
    return _AcceptedFile(
        cursor.stamp,
        cursor.offset,
        retired=True,
        checked=True,
    )


def poll_append(participant: AppendParticipant) -> bool:
    accepted = getattr(participant, _ACCEPTED_STATE, None)
    if not isinstance(accepted, _AcceptedCohort):
        accepted = None
    failures = getattr(participant, "_provider_reload_failures", {})
    if type(failures) is not dict:
        failures = {}
    try:
        request = participant.reload_plan()
        if not isinstance(request, ReloadPlan):
            raise TypeError("append participant returned invalid plan")
        discovered = _append_discover(request, accepted)
        if request.admission_provider is not None and accepted is None:
            records = dict(discovered.admission_records)
            if any(
                path.endswith(".jsonl")
                and (
                    path not in records
                    or records[path].proof is None
                )
                for path in (_canonical(item) for item in request.files)
            ):
                return False
    except _CANDIDATE_FAILURES:
        return False

    prior_files = dict(accepted.files) if accepted is not None else {}
    current_files = dict(discovered.files)
    if any(current_files.get(path) != stamp for path, stamp in failures.items()):
        failures = {}
    next_files = dict(prior_files)
    candidates: list[tuple[str, FileStamp, int, bool]] = []
    progress = False
    unchecked: set[str] = set()

    for path, cursor in prior_files.items():
        if cursor.retired:
            continue
        stamp = current_files.get(path)
        if stamp is None:
            next_files[path] = _retired(cursor)
            progress = True
            continue
        if not _same_generation(cursor.stamp, stamp) or stamp.size < cursor.offset:
            next_files[path] = _retired(cursor)
            progress = True
            continue
        if not cursor.checked:
            unchecked.add(path)
            next_files[path] = _AcceptedFile(
                stamp,
                cursor.offset,
                retired=False,
                checked=True,
            )
            cursor = next_files[path]
        kind = _source_kind(path)
        if kind == "json":
            if cursor.checked and path not in unchecked and stamp != cursor.stamp:
                next_files[path] = _retired(cursor)
                progress = True
            continue
        if (
            cursor.checked
            and path not in unchecked
            and stamp.size == cursor.offset
            and cursor.stamp.size == cursor.offset
            and stamp != cursor.stamp
        ):
            next_files[path] = _retired(cursor)
            progress = True

    optional = {_canonical(path) for path in request.optional_files}
    for path, stamp in discovered.files:
        cursor = next_files.get(path)
        if cursor is not None and cursor.retired:
            continue
        if failures.get(path) == stamp:
            continue
        if cursor is None:
            candidates.append((path, stamp, 0, path in optional))
            continue
        if _source_kind(path) == "json":
            continue
        if stamp.size < cursor.offset:
            continue
        if path in unchecked:
            if stamp.size > cursor.offset:
                candidates.append(
                    (path, stamp, cursor.offset, path in optional),
                )
            continue
        if stamp == cursor.stamp:
            continue
        if stamp.size >= cursor.offset:
            candidates.append((path, stamp, cursor.offset, path in optional))

    captured: list[CapturedFile] = []
    try:
        for path, stamp, offset, is_optional in candidates:
            captured.append(_read_append_file(
                path, stamp, offset, is_optional, open,
            ))
    except OSError:
        return False
    except _CANDIDATE_FAILURES:
        for path, stamp, _offset, _optional in candidates:
            failures[path] = stamp
        setattr(participant, "_provider_reload_failures", failures)
        return False

    if accepted is None and not captured:
        return False

    action = (
        "bootstrap"
        if accepted is None or not accepted.bootstrapped
        else "append"
    )
    batch = CapturedCohort(
        request,
        tuple(captured),
        discovered.directories,
        action,
        tuple((path, cursor.offset) for path, cursor in sorted(prior_files.items())),
        discovered.admission_records,
        discovered.denied,
    )
    if captured:
        try:
            prepared = participant.prepare_append(batch)
        except OSError:
            return False
        except _CANDIDATE_FAILURES:
            for source in captured:
                failures[source.path] = source.stamp
            setattr(participant, "_provider_reload_failures", failures)
            return False
        if not all(_captured_stable(source) for source in captured):
            return False
        visible_changed = participant.apply_append(prepared)
        if type(visible_changed) is not bool:
            raise TypeError("append participant returned invalid change flag")
        for source in captured:
            next_files[source.path] = _AcceptedFile(
                source.stamp,
                source.offset,
                retired=False,
                checked=True,
            )
            failures.pop(source.path, None)
            prior = prior_files.get(source.path)
            if prior is None or source.offset != prior.offset:
                progress = True
    else:
        visible_changed = participant.settle_idle()
        if type(visible_changed) is not bool:
            raise TypeError("append participant returned invalid idle flag")

    directories = discovered.directories
    denied = discovered.denied
    if accepted is None or accepted.directories != directories:
        progress = True
    if accepted is None or accepted.denied != denied:
        progress = True
    next_acceptance = _AcceptedCohort(
        tuple(sorted(next_files.items())),
        directories,
        denied,
        _plan_key(request),
        discovered.admission_key,
        discovered.admission_records,
        True,
    )
    setattr(participant, _ACCEPTED_STATE, next_acceptance)
    setattr(participant, "_provider_reload_failures", failures)
    if progress:
        _set_source_revision(participant, source_revision(participant) + 1)
    if visible_changed:
        _set_visible_revision(participant, visible_revision(participant) + 1)
    depth = getattr(participant, "_provider_reload_followup_depth", 0)
    if request.followup is not None and depth < 2:
        try:
            followup = request.followup(participant)
        except _CANDIDATE_FAILURES:
            followup = None
        if (
            isinstance(followup, ReloadPlan)
            and _plan_key(followup) != _plan_key(request)
        ):
            setattr(participant, "_provider_reload_followup_depth", depth + 1)
            try:
                visible_changed = poll_append(participant) or visible_changed
            finally:
                if depth:
                    setattr(participant, "_provider_reload_followup_depth", depth)
                else:
                    try:
                        delattr(participant, "_provider_reload_followup_depth")
                    except AttributeError:
                        pass
    return visible_changed
