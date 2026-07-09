import json
from pathlib import Path

import pytest

from batch_checkpoint import (
    make_checkpoint_callback,
    read_checkpoint_file,
    write_checkpoint_file,
)


class TestWriteReadRoundTrip:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        write_checkpoint_file(
            path,
            enriched_rows=[{"company_name": "Acme"}],
            evidence_rows=[{"source_index": 0}],
            signal_rows=[],
            processed=1,
            selected_rows=10,
        )
        data = read_checkpoint_file(path)
        assert data["processed"] == 1
        assert data["selected_rows"] == 10
        assert data["enriched_rows"] == [{"company_name": "Acme"}]
        assert data["evidence_rows"] == [{"source_index": 0}]
        assert data["signal_rows"] == []

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "checkpoint.json"
        write_checkpoint_file(path, [], [], [], processed=0, selected_rows=0)
        assert path.exists()

    def test_overwrites_previous_checkpoint(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        write_checkpoint_file(path, [{"a": 1}], [], [], processed=1, selected_rows=5)
        write_checkpoint_file(path, [{"a": 1}, {"a": 2}], [], [], processed=2, selected_rows=5)
        data = read_checkpoint_file(path)
        assert data["processed"] == 2
        assert len(data["enriched_rows"]) == 2

    def test_no_leftover_temp_file(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        write_checkpoint_file(path, [], [], [], processed=0, selected_rows=0)
        leftovers = list(tmp_path.glob(".checkpoint_*"))
        assert leftovers == []

    def test_non_json_serializable_value_falls_back_to_str(self, tmp_path):
        path = tmp_path / "checkpoint.json"

        class Weird:
            def __str__(self):
                return "weird-value"

        write_checkpoint_file(
            path, [{"x": Weird()}], [], [], processed=1, selected_rows=1)
        data = read_checkpoint_file(path)
        assert data["enriched_rows"][0]["x"] == "weird-value"


class TestReadCheckpointFileMissingOrCorrupt:
    def test_missing_file_returns_none(self, tmp_path):
        assert read_checkpoint_file(tmp_path / "nope.json") is None

    def test_corrupt_json_returns_none(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert read_checkpoint_file(path) is None


class TestWriteCheckpointFileNeverRaises:
    def test_unwritable_path_does_not_raise(self, tmp_path):
        # A path whose parent is actually a file (not a dir) can never be
        # created -- write_checkpoint_file must swallow this, not propagate.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        bad_path = blocker / "checkpoint.json"
        write_checkpoint_file(bad_path, [], [], [], processed=0, selected_rows=0)  # no raise


class TestMakeCheckpointCallback:
    def test_callback_writes_file_with_deferred_selected_rows(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        calls = []

        def get_selected_rows():
            calls.append(1)
            return 42

        callback = make_checkpoint_callback(path, get_selected_rows=get_selected_rows)
        callback([{"a": 1}, {"a": 2}], [], [])

        data = read_checkpoint_file(path)
        assert data["processed"] == 2
        assert data["selected_rows"] == 42
        assert len(calls) == 1  # only called once per invocation, not eagerly at build time
