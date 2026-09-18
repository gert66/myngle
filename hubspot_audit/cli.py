"""Command-line entry point.

Default mode is ``fixture`` (safe, offline, no HubSpot token needed). ``live``
mode requires an explicit ``--mode live`` flag *and* a configured
``HUBSPOT_ACCESS_TOKEN`` -- neither is required to build or test this
package, per the build instructions.
"""
from __future__ import annotations

import argparse
import os
import time

from .config import AuditConfig
from .runner import run_audit


def make_run_id() -> str:
    return time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hubspot_audit", description="Read-only HubSpot CRM audit")
    parser.add_argument("command", choices=["run"], help="Only 'run' is supported")
    parser.add_argument("--mode", choices=["fixture", "dry_run", "live"], default="fixture")
    parser.add_argument("--output-dir", default=None, help="Defaults to ./hubspot_audit_runs/<run_id>")
    parser.add_argument("--fixtures-dir", default=None, help="Defaults to the bundled fixtures/ directory")
    args = parser.parse_args(argv)

    run_id = make_run_id()
    output_dir = args.output_dir or os.path.join(os.getcwd(), "hubspot_audit_runs", run_id)

    if args.mode == "live" and not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        print(
            "Live mode requires HUBSPOT_ACCESS_TOKEN to be set. Refusing to start a live "
            "audit without it (this build intentionally never requires a live token)."
        )
        return 2

    config = AuditConfig.build(run_id=run_id, output_dir=output_dir, mode=args.mode, fixtures_dir=args.fixtures_dir)
    result = run_audit(config)
    print(f"Run complete: {config.run_id}")
    print(f"Report: {result['report_path']}")
    print(f"Control Center status: {result['control_center_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
