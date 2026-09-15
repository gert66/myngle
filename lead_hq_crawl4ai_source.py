"""Crawl4AI own-domain HQ source for the Lead Prioritizer.

This adapter intentionally mirrors ``lead_hq_firecrawl_source`` so the HQ
interpreter can consume either crawler without changing prompt semantics.
Crawl4AI is invoked through an external runner command, keeping the heavy
browser dependency outside the application environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

_DEFAULT_MAX_HQ_PAGES = 3
_HQ_CANDIDATE_PAGE_PATHS = (
    "", "/about", "/about-us", "/company", "/company-profile", "/en/about",
)

def _crawl_one(url: str, runner: str, timeout_seconds: int) -> dict:
    name = f"hq-{uuid.uuid4().hex[:12]}"
    proc = subprocess.run(
        [runner, url, name], capture_output=True, text=True,
        timeout=timeout_seconds, check=False,
    )
    if proc.returncode != 0:
        return {"ok": False, "status": "runner_error", "error": proc.stderr[-300:]}
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return {"ok": False, "status": "missing_output_path"}
    out_path = Path(lines[-1])
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "status": "invalid_output", "error": str(exc)[:200]}
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
    status_code = payload.get("status_code")
    markdown = payload.get("markdown") or ""
    ok = bool(payload.get("success")) and bool(str(markdown).strip())
    if status_code is not None:
        try:
            ok = ok and int(status_code) < 400
        except (TypeError, ValueError):
            ok = False
    return {
        "ok": ok,
        "status": status_code if status_code is not None else ("ok" if ok else "failed"),
        "text": str(markdown).strip(),
        "error": payload.get("error_message"),
    }


def collect_own_domain_hq_pages_crawl4ai(
    domain: Optional[str],
    *,
    runner: str = "",
    max_pages: int = _DEFAULT_MAX_HQ_PAGES,
    candidate_paths: tuple[str, ...] = _HQ_CANDIDATE_PAGE_PATHS,
    timeout_seconds: int = 75,
) -> dict:
    """Return Crawl4AI pages in the same shape as the Firecrawl adapter."""
    domain = (domain or "").strip()
    runner = (runner or os.getenv("CRAWL4AI_RUNNER") or "").strip()
    if not domain or not runner or not Path(runner).exists():
        return {"pages": [], "pages_crawled": [], "used": False}

    base = domain if "://" in domain else f"https://{domain}"
    base = base.rstrip("/")
    pages: list[dict] = []
    pages_crawled: list[dict] = []

    for path in candidate_paths:
        if len(pages) >= max_pages:
            break
        url = base + path
        try:
            result = _crawl_one(url, runner, timeout_seconds)
        except subprocess.TimeoutExpired:
            result = {"ok": False, "status": "timeout"}
        except Exception as exc:
            result = {"ok": False, "status": "runner_exception", "error": str(exc)[:200]}
        pages_crawled.append({"url": url, "status": result.get("status")})
        if result.get("ok"):
            pages.append({
                "url": url,
                "text": result.get("text") or "",
                "source_kind": "own_domain",
                "retrieval_method": "crawl4ai",
            })

    return {"pages": pages, "pages_crawled": pages_crawled, "used": bool(pages)}
