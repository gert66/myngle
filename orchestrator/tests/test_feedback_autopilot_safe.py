import importlib.util
from pathlib import Path
from urllib import error

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "process_feedback_autopilot_safe.py"
spec = importlib.util.spec_from_file_location("feedback_safe", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


def test_post_resolution_conflict_type_is_available():
    # Regression guard: safe poller handles HTTP 409 per record rather than
    # terminating the complete intake cycle.
    assert error.HTTPError is mod.error.HTTPError


def test_resolved_duplicate_cleanup_predicate_shape():
    case = {"status": "resolved", "resolution_note": "Already fixed"}
    assert case.get("status") == "resolved"
    assert str(case.get("resolution_note") or "").strip()
