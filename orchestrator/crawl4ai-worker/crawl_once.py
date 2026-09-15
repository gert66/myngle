#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

BROWSER_CACHE = "/home/myngle/orchestrator/crawl4ai-worker/ms-playwright-links"

async def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: crawl_once.py <https-url> <output.json>")
    url, out_path = sys.argv[1], Path(sys.argv[2])
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("only absolute http/https URLs are allowed")

    browser = BrowserConfig(
        browser_type="chromium",
        headless=True,
        verbose=False,
        memory_saving_mode=True,
        light_mode=True,
    )
    run_cfg = CrawlerRunConfig()

    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)

    markdown = getattr(result, "markdown", "")
    if not isinstance(markdown, str):
        markdown = str(markdown)
    payload = {
        "url": url,
        "success": bool(getattr(result, "success", False)),
        "status_code": getattr(result, "status_code", None),
        "error_message": getattr(result, "error_message", None),
        "metadata": getattr(result, "metadata", None),
        "markdown": markdown,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    asyncio.run(main())
