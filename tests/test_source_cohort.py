from dataclasses import FrozenInstanceError, fields, replace
import importlib

import pytest


def source_types():
    module = importlib.import_module("quakepro.source_cohort")
    return module.FileStamp, module.DirectoryStamp


def test_file_stamp_is_frozen_and_every_stat_field_defines_identity():
    FileStamp, _ = source_types()
    expected_fields = (
        "device",
        "inode",
        "mode",
        "size",
        "mtime_ns",
        "ctime_ns",
    )
    stamp = FileStamp(11, 12, 0o600, 13, 14, 15)

    assert tuple(field.name for field in fields(FileStamp)) == expected_fields
    for field_name in expected_fields:
        assert replace(stamp, **{field_name: getattr(stamp, field_name) + 1}) != stamp
    with pytest.raises(FrozenInstanceError):
        stamp.ctime_ns = 16


@pytest.mark.parametrize(
    "changed_entries",
    [
        pytest.param((("other.jsonl", "file"),), id="name"),
        pytest.param((("child.jsonl", "directory"),), id="kind"),
    ],
)
def test_directory_stamp_is_frozen_and_name_plus_kind_define_identity(
        changed_entries):
    _, DirectoryStamp = source_types()
    stamp = DirectoryStamp("/sessions", (("child.jsonl", "file"),))

    assert tuple(field.name for field in fields(DirectoryStamp)) == ("path", "entries")
    assert replace(stamp, entries=changed_entries) != stamp
    with pytest.raises(FrozenInstanceError):
        stamp.entries = ()
