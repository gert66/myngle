#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from urllib import request

from lead_list_config_registry import (
    COUNTRY_LABELS, DEFAULT_COLD_CALLERS, DISABLED_COUNTRY_LABELS, country_folder_slug,
)

ORCH_ROOT = Path('/home/myngle/orchestrator')
TOKEN_FILE = ORCH_ROOT / 'secrets' / 'orchestrator_ingest_token'
PUSH_URL_FILE = ORCH_ROOT / 'config' / 'ops_push_url.txt'


def config_url(path: Path = PUSH_URL_FILE) -> str:
    base = path.read_text(encoding='utf-8').strip()
    return base.rsplit('/api/public/', 1)[0] + '/api/public/lead-list-config'


def payload() -> dict:
    return {
        'callers': [{'label': x, 'active': True, 'source': 'vm'} for x in DEFAULT_COLD_CALLERS],
        'countries': [
            {
                'label': label,
                'slug': country_folder_slug(label),
                'active': True,
                'enabled_in_company_hub': label not in DISABLED_COUNTRY_LABELS,
                'source': 'vm',
            }
            for label in COUNTRY_LABELS
        ],
    }


def sync(url: str | None = None, token: str | None = None) -> dict:
    url = url or config_url()
    token = token or TOKEN_FILE.read_text(encoding='utf-8').strip()
    body = json.dumps(payload()).encode('utf-8')
    req = request.Request(
        url, data=body, method='POST',
        headers={
            'accept': 'application/json',
            'content-type': 'application/json',
            'user-agent': 'Sales-Cockpit-Lead-List-Worker/1.0',
            'x-orchestrator-token': token,
        },
    )
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> int:
    result = sync()
    print(f"lead-list config sync: callers={result.get('callers', 0)} countries={result.get('countries', 0)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
