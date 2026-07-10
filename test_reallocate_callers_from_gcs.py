"""Tests for reallocate_callers_from_gcs — pure caller-reallocation logic +
the GCS end-to-end flow (which reuses rescore_from_gcs's CLI plumbing).

No real network calls, no real Google Cloud SDK required: subprocess is
always mocked in the rescore_from_gcs namespace (where download/upload
actually shell out), same style as test_rescore_from_gcs.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rescore_from_gcs import CURRENT_MANIFEST_FILENAME, LIST_FILENAME
from reallocate_callers_from_gcs import (
    REALLOCATE_SCHEMA_VERSION,
    assign_callers,
    build_reallocate_manifest,
    build_reallocated_run,
    caller_distribution,
    compute_caller_ranks,
    default_reallocate_run_folder,
    existing_cold_callers,
    normalize_cold_callers,
    reallocate_all_countries,
    reallocate_country,
    reallocation_movers,
    reassign_detail_record,
    reassign_details_bucket,
    reassign_list_items,
)


# ---------------------------------------------------------------------------
# Fixtures mimicking the companies.list.json / company-details schema the
# export pipeline writes (assigned_cold_caller + rank on both list & detail).
# ---------------------------------------------------------------------------

def make_item(company_id, *, score, rank, caller, tier="🥈 Warm") -> dict:
    return {
        "company_id": company_id,
        "company_name": f"Company {company_id}",
        "commercial_fit_score": score,
        "commercial_tier": tier,
        "assigned_cold_caller": caller,
        "assigned_cold_caller_rank": rank,
    }


def make_current(items) -> dict:
    """A minimal in-memory 'current' bundle: list_items + one details bucket
    keyed by company_id, mirroring download_current_run's return shape."""
    bucket = {it["company_id"]: dict(it, detail_bucket="company-details-000.json")
              for it in items}
    return {
        "manifest": {"generated_at": "2026-07-01T00:00:00Z",
                     "export_country": "Brazil",
                     "cold_callers": existing_cold_callers(items)},
        "list_items": [dict(it, detail_bucket="company-details-000.json") for it in items],
        "detail_files": {"company-details-000.json": bucket},
    }


# ---------------------------------------------------------------------------
# normalize_cold_callers / existing_cold_callers / caller_distribution
# ---------------------------------------------------------------------------

class TestNormalizeColdCallers:
    def test_trims_blanks_and_dedupes_preserving_order(self):
        assert normalize_cold_callers(
            [" Ann ", "Bob", "", "Ann", "  ", "Cara"]) == ["Ann", "Bob", "Cara"]

    def test_none_yields_empty(self):
        assert normalize_cold_callers(None) == []


class TestExistingColdCallers:
    def test_returns_distinct_callers_in_rank_order(self):
        items = [
            make_item("c3", score=1, rank=3, caller="Cara"),
            make_item("c1", score=9, rank=1, caller="Ann"),
            make_item("c2", score=5, rank=2, caller="Bob"),
            make_item("c4", score=0, rank=4, caller="Ann"),
        ]
        assert existing_cold_callers(items) == ["Ann", "Bob", "Cara"]

    def test_blank_callers_ignored(self):
        items = [make_item("c1", score=1, rank=1, caller="")]
        assert existing_cold_callers(items) == []


class TestCallerDistribution:
    def test_counts_by_caller(self):
        items = [
            make_item("c1", score=9, rank=1, caller="Ann"),
            make_item("c2", score=8, rank=2, caller="Bob"),
            make_item("c3", score=7, rank=3, caller="Ann"),
        ]
        assert caller_distribution(items) == {"Ann": 2, "Bob": 1}

    def test_blank_caller_counted_under_empty_string(self):
        items = [make_item("c1", score=1, rank=1, caller=None)]
        assert caller_distribution(items) == {"": 1}


# ---------------------------------------------------------------------------
# compute_caller_ranks / assign_callers
# ---------------------------------------------------------------------------

class TestComputeCallerRanks:
    def test_preserves_existing_rank_by_default(self):
        items = [
            make_item("c2", score=100, rank=2, caller="X"),
            make_item("c1", score=1, rank=1, caller="X"),
        ]
        assert compute_caller_ranks(items) == {"c1": 1, "c2": 2}

    def test_missing_rank_gets_fresh_unique_rank_after_max(self):
        items = [
            make_item("c1", score=9, rank=1, caller="X"),
            {"company_id": "c-no-rank", "commercial_fit_score": 5},
        ]
        ranks = compute_caller_ranks(items)
        assert ranks["c1"] == 1
        assert ranks["c-no-rank"] == 2
        assert len(set(ranks.values())) == 2  # unique

    def test_rerank_by_score_orders_by_score_descending(self):
        items = [
            make_item("low", score=10, rank=1, caller="X"),
            make_item("high", score=90, rank=2, caller="X"),
            make_item("mid", score=50, rank=3, caller="X"),
        ]
        ranks = compute_caller_ranks(items, rerank_by_score=True)
        assert ranks == {"high": 1, "mid": 2, "low": 3}

    def test_rerank_tie_breaks_on_previous_rank_then_id(self):
        items = [
            make_item("b", score=5, rank=2, caller="X"),
            make_item("a", score=5, rank=1, caller="X"),
        ]
        ranks = compute_caller_ranks(items, rerank_by_score=True)
        assert ranks == {"a": 1, "b": 2}


class TestAssignCallers:
    def test_round_robin_matches_export_formula(self):
        items = [make_item(f"c{i}", score=100 - i, rank=i, caller="old")
                 for i in range(1, 5)]
        assignment = assign_callers(items, ["Ann", "Bob", "Cara"])
        # rank 1->Ann, 2->Bob, 3->Cara, 4->Ann  (exactly (rank-1) % len)
        assert assignment["c1"] == ("Ann", 1)
        assert assignment["c2"] == ("Bob", 2)
        assert assignment["c3"] == ("Cara", 3)
        assert assignment["c4"] == ("Ann", 4)

    def test_empty_caller_pool_raises(self):
        items = [make_item("c1", score=1, rank=1, caller="old")]
        with pytest.raises(ValueError, match="[Aa]t least one cold caller"):
            assign_callers(items, [])

    def test_single_caller_gets_everyone(self):
        items = [make_item(f"c{i}", score=100 - i, rank=i, caller="old")
                 for i in range(1, 4)]
        assignment = assign_callers(items, ["Solo"])
        assert {v[0] for v in assignment.values()} == {"Solo"}


# ---------------------------------------------------------------------------
# reassign_list_items / reassign_detail_record / reassign_details_bucket
# ---------------------------------------------------------------------------

class TestReassignListItems:
    def test_updates_caller_and_rank_without_mutating_input(self):
        items = [make_item("c1", score=9, rank=1, caller="old")]
        snapshot = json.loads(json.dumps(items))
        updated = reassign_list_items(items, {"c1": ("Ann", 1)})
        assert updated[0]["assigned_cold_caller"] == "Ann"
        assert updated[0]["assigned_cold_caller_rank"] == 1
        assert items == snapshot
        assert updated[0] is not items[0]

    def test_company_missing_from_assignment_passes_through(self):
        items = [make_item("c1", score=9, rank=1, caller="old")]
        updated = reassign_list_items(items, {})
        assert updated[0]["assigned_cold_caller"] == "old"


class TestReassignDetailRecord:
    def test_writes_audit_and_new_values_without_mutating(self):
        detail = make_item("c1", score=9, rank=1, caller="old")
        detail["scoring_inputs"] = {"signals": {}}  # untouched extra field
        snapshot = json.loads(json.dumps(detail))
        new_detail = reassign_detail_record(
            detail, "Ann", 5, now_iso="2026-07-09T00:00:00Z", rerank_by_score=True)

        assert new_detail["assigned_cold_caller"] == "Ann"
        assert new_detail["assigned_cold_caller_rank"] == 5
        audit = new_detail["caller_reallocation_audit"]
        assert audit["schema_version"] == REALLOCATE_SCHEMA_VERSION
        assert audit["reallocated_at"] == "2026-07-09T00:00:00Z"
        assert audit["rerank_by_score"] is True
        assert audit["previous_cold_caller"] == "old"
        assert audit["previous_cold_caller_rank"] == 1
        assert audit["assigned_cold_caller"] == "Ann"
        assert audit["assigned_cold_caller_rank"] == 5
        # score/tier and unrelated fields are untouched
        assert new_detail["commercial_fit_score"] == 9
        assert new_detail["scoring_inputs"] == {"signals": {}}
        assert detail == snapshot

    def test_reassign_details_bucket_reassigns_each_and_carries_over_unknown(self):
        bucket = {
            "c1": make_item("c1", score=9, rank=1, caller="old"),
            "c2": make_item("c2", score=8, rank=2, caller="old"),
        }
        assignment = {"c1": ("Ann", 1)}  # c2 not in assignment
        result = reassign_details_bucket(
            bucket, assignment, now_iso="2026-07-09T00:00:00Z", rerank_by_score=False)
        assert result["c1"]["assigned_cold_caller"] == "Ann"
        assert "caller_reallocation_audit" in result["c1"]
        assert result["c2"]["assigned_cold_caller"] == "old"
        assert "caller_reallocation_audit" not in result["c2"]


# ---------------------------------------------------------------------------
# reallocation_movers / build_reallocate_manifest
# ---------------------------------------------------------------------------

class TestReallocationMovers:
    def test_only_changed_callers_sorted_by_rank(self):
        items = [
            make_item("c1", score=9, rank=1, caller="Ann"),
            make_item("c2", score=8, rank=2, caller="Bob"),
            make_item("c3", score=7, rank=3, caller="Cara"),
        ]
        # New pool [Ann, Bob] -> c1 Ann(unchanged), c2 Bob(unchanged), c3 Ann(changed)
        assignment = assign_callers(items, ["Ann", "Bob"])
        movers = reallocation_movers(items, assignment)
        assert [m["company_id"] for m in movers] == ["c3"]
        assert movers[0]["caller_before"] == "Cara"
        assert movers[0]["caller_after"] == "Ann"


class TestBuildReallocateManifest:
    def test_shape(self):
        items = [
            make_item("c1", score=9, rank=1, caller="Ann"),
            make_item("c2", score=8, rank=2, caller="Bob"),
        ]
        assignment = assign_callers(items, ["Zoe"])  # everyone -> Zoe
        new_items = reassign_list_items(items, assignment)
        manifest = build_reallocate_manifest(
            country_folder="brazil",
            source_current_manifest={"generated_at": "2026-01-01T00:00:00Z"},
            run_folder="2026-07-09_reallocate",
            original_list_items=items,
            rescaled_list_items=new_items,
            new_cold_callers=["Zoe"],
            rerank_by_score=False,
            generated_at="2026-07-09T00:00:00Z",
            assignment=assignment,
        )
        assert manifest["schema_version"] == REALLOCATE_SCHEMA_VERSION
        assert manifest["country_folder"] == "brazil"
        assert manifest["run_folder"] == "2026-07-09_reallocate"
        assert manifest["previous_cold_callers"] == ["Ann", "Bob"]
        assert manifest["cold_callers"] == ["Zoe"]
        assert manifest["companies_total"] == 2
        assert manifest["companies_reallocated"] == 2
        assert manifest["companies_unchanged"] == 0
        assert manifest["caller_distribution_before"] == {"Ann": 1, "Bob": 1}
        assert manifest["caller_distribution_after"] == {"Zoe": 2}
        assert manifest["promoted_to_current"] is False
        assert manifest["rerank_by_score"] is False


class TestDefaultReallocateRunFolder:
    def test_format(self):
        now = datetime(2026, 7, 9, 10, 30, tzinfo=timezone.utc)
        assert default_reallocate_run_folder(now) == "2026-07-09_reallocate"


# ---------------------------------------------------------------------------
# build_reallocated_run — pure no-I/O core
# ---------------------------------------------------------------------------

class TestBuildReallocatedRun:
    def _items(self):
        return [make_item(f"c{i}", score=100 - i, rank=i, caller="old")
                for i in range(1, 5)]

    def test_shape_and_round_robin_applied_to_list_and_details(self):
        current = make_current(self._items())
        run = build_reallocated_run(
            current, ["Ann", "Bob"], country_folder="brazil",
            run_folder="2026-07-09_reallocate", now_iso="2026-07-09T00:00:00Z")

        assert set(run) == {"list_items", "detail_files", "manifest"}
        callers = [it["assigned_cold_caller"] for it in run["list_items"]]
        assert callers == ["Ann", "Bob", "Ann", "Bob"]
        # detail records mirror the list assignment
        bucket = run["detail_files"]["company-details-000.json"]
        for it in run["list_items"]:
            assert bucket[it["company_id"]]["assigned_cold_caller"] == \
                it["assigned_cold_caller"]
        assert run["manifest"]["cold_callers"] == ["Ann", "Bob"]

    def test_does_not_mutate_input_current(self):
        current = make_current(self._items())
        snapshot = json.loads(json.dumps(current))
        build_reallocated_run(
            current, ["Solo"], country_folder="brazil",
            run_folder="run1", now_iso="2026-07-09T00:00:00Z")
        assert current == snapshot

    def test_empty_callers_raises(self):
        current = make_current(self._items())
        with pytest.raises(ValueError, match="[Aa]t least one cold caller"):
            build_reallocated_run(
                current, [], country_folder="brazil",
                run_folder="run1", now_iso="2026-07-09T00:00:00Z")

    def test_rerank_by_score_reassigns_by_current_score(self):
        # Ranks stored as reverse of score; rerank should follow score, not rank.
        items = [
            make_item("low", score=10, rank=1, caller="old"),
            make_item("high", score=90, rank=2, caller="old"),
        ]
        current = make_current(items)
        run = build_reallocated_run(
            current, ["Ann", "Bob"], country_folder="brazil",
            run_folder="run1", now_iso="2026-07-09T00:00:00Z", rerank_by_score=True)
        by_id = {it["company_id"]: it for it in run["list_items"]}
        assert by_id["high"]["assigned_cold_caller"] == "Ann"   # rank 1 after rerank
        assert by_id["low"]["assigned_cold_caller"] == "Bob"    # rank 2 after rerank


# ---------------------------------------------------------------------------
# End-to-end reallocate_country — subprocess mocked in rescore_from_gcs
# ---------------------------------------------------------------------------

def _write_fixture_current_run(country_dir: Path) -> list[dict]:
    country_dir.mkdir(parents=True, exist_ok=True)
    items = [make_item(f"c{i}", score=100 - i, rank=i, caller="old")
             for i in range(1, 5)]
    bucket = {it["company_id"]: dict(it, detail_bucket="company-details-000.json")
              for it in items}
    list_items = [dict(it, detail_bucket="company-details-000.json") for it in items]
    manifest = {"generated_at": "2026-07-01T00:00:00Z", "export_country": "Brazil",
                "cold_callers": ["old"]}
    (country_dir / LIST_FILENAME).write_text(json.dumps(list_items), encoding="utf-8")
    (country_dir / "company-details-000.json").write_text(json.dumps(bucket), encoding="utf-8")
    (country_dir / CURRENT_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return items


def _fake_gcs_tool_for_local_dir(remote_root: Path):
    """subprocess.run stand-in serving ls/cp from a local dir tree as if it
    were gs://bucket-a/... — identical to test_rescore_from_gcs's helper.

    'cp' handles both the single-source form (upload_file / promote's
    download_file: ``cp source dest_file``) and the batch download form
    (``download_files_batch``: ``cp source1 source2 ... dest_dir/``)."""

    def _run(cmd, capture_output=True, text=True, timeout=None):
        if "ls" in cmd:
            target = cmd[-1]
            rel = target.replace("gs://bucket-a/", "").rstrip("/")
            local_dir = remote_root / rel
            if not local_dir.is_dir():
                return MagicMock(returncode=1, stdout="", stderr="not found")
            lines = [f"{target.rstrip('/')}/{p.name}" for p in sorted(local_dir.iterdir())]
            return MagicMock(returncode=0, stdout="\n".join(lines) + ("\n" if lines else ""), stderr="")
        if "cp" in cmd:
            cp_idx = cmd.index("cp")
            *sources, dest = cmd[cp_idx + 1:]
            if len(sources) > 1 or dest.endswith("/"):
                dest_dir = Path(dest)
                dest_dir.mkdir(parents=True, exist_ok=True)
                for source in sources:
                    rel = source.replace("gs://bucket-a/", "")
                    local_source = remote_root / rel
                    (dest_dir / local_source.name).write_bytes(local_source.read_bytes())
                return MagicMock(returncode=0, stdout="", stderr="")
            source = sources[0]
            if source.startswith("gs://"):
                rel = source.replace("gs://bucket-a/", "")
                Path(dest).write_bytes((remote_root / rel).read_bytes())
            else:
                rel = dest.replace("gs://bucket-a/", "")
                remote_path = remote_root / rel
                remote_path.parent.mkdir(parents=True, exist_ok=True)
                remote_path.write_bytes(Path(source).read_bytes())
            return MagicMock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


class TestReallocateCountry:
    def test_end_to_end_writes_to_new_run_folder_never_current(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run",
                   side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = reallocate_country(
                "bucket-a", "brazil", ["Ann", "Bob", "Cara"],
                run_folder="2026-07-09_reallocate",
                now=datetime(2026, 7, 9, tzinfo=timezone.utc),
            )

        assert manifest["companies_total"] == 4
        assert manifest["cold_callers"] == ["Ann", "Bob", "Cara"]
        destinations = {r["destination"] for r in manifest["upload_results"]}
        assert all(d.startswith("gs://bucket-a/brazil/runs/2026-07-09_reallocate/")
                   for d in destinations)
        assert all("/current/" not in d for d in destinations)
        assert all(r["success"] for r in manifest["upload_results"])

        # current/ on the fake remote must be untouched (still "old").
        current_bucket = json.loads(
            (remote_root / "brazil" / "current" / "company-details-000.json").read_text())
        assert all(d["assigned_cold_caller"] == "old" for d in current_bucket.values())

        # new run has the round-robin reallocation.
        new_bucket = json.loads(
            (remote_root / "brazil" / "runs" / "2026-07-09_reallocate"
             / "company-details-000.json").read_text())
        assert new_bucket["c1"]["assigned_cold_caller"] == "Ann"
        assert new_bucket["c4"]["assigned_cold_caller"] == "Ann"

    def test_dry_run_skips_upload(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run",
                   side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = reallocate_country(
                "bucket-a", "brazil", ["Ann"], upload=False,
                run_folder="2026-07-09_reallocate",
                now=datetime(2026, 7, 9, tzinfo=timezone.utc),
            )

        assert manifest["upload_results"] == []
        assert not (remote_root / "brazil" / "runs").exists()


class TestReallocateAllCountries:
    def test_one_country_failing_does_not_stop_the_others(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        # "italy" has no current/ folder -> should error, not raise.

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run",
                   side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            results = reallocate_all_countries(
                "bucket-a", ["Ann", "Bob"], countries=["brazil", "italy"],
                run_folder="2026-07-09_reallocate",
            )

        assert results["brazil"]["companies_total"] == 4
        assert "error" in results["italy"]
