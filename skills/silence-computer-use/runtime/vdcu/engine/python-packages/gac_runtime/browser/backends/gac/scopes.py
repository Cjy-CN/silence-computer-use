"""In-memory document scope allocation and origin diagnostics."""

import json
import os
import sys
import tempfile
import time

from typing import Any

MAX_PERSISTED_SCOPES = 1000

persist_dir = os.getenv('CUA_CACHE_DIR')
if not persist_dir:
    persist_dir = tempfile.gettempdir()
if not persist_dir:
    persist_dir = os.getenv('TEMP')
if not persist_dir:
    persist_dir = os.getenv('TMPDIR')
if not persist_dir:
    persist_dir = os.getenv('CUA_PACK_DIR')
if not persist_dir:
    if os.name == 'nt':
        persist_dir = os.path.join('C:', 'Users', os.getlogin(), 'AppData', 'Local', 'Temp')
    else:
        persist_dir = '/tmp'

persist_dir = os.path.abspath(persist_dir)
persist_dir = os.path.join(persist_dir, '.silent_gac')

class ScopeStoreMixin:
    def _validate_scope_data(self):
        assert self._scope_counter >= 0
        for key, value in self._scope_origins.items():
            assert isinstance(key, str)
            assert isinstance(value, dict)
            assert isinstance(value['url'], str)
            assert isinstance(value['title'], str)
            assert isinstance(value['t'], (int, float))
            value['t'] = float(value['t'])

    def _load_scope_data(self):
        try:
            with open(os.path.join(persist_dir, 'saved_scopes.json')) as fp:
                data = json.load(fp)
            self._scope_counter = int(data['scope_counter'])
            self._scope_origins = dict(data['scope_origins'])
            self._validate_scope_data()
        except FileNotFoundError:
            self._scope_counter = 0
            self._scope_origins = {}
        except Exception as e:
            print(f'WARNING: cannot load stored scope data, starting from scratch. error={e}', file = sys.stderr)
            self._scope_counter = 0
            self._scope_origins = {}

    def _save_scope_data(self):
        try:
            os.makedirs(persist_dir, exist_ok = True)
            with open(os.path.join(persist_dir, 'saved_scopes.json'), 'w') as fp:
                data = {}
                data['scope_counter'] = self._scope_counter
                data['scope_origins'] = self._scope_origins
                json.dump(data, fp)
        except Exception as e:
            print(f'WARNING: cannot save stored scope data, discarded. error={e}', file = sys.stderr)

    def _allocate_scope(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._scope_counter += 1
        scope = f"d{self._scope_counter}"
        self._scope_origins[scope] = {
            "url": str(payload.get("url") or ""),
            "title": str(payload.get("title") or ""),
            "t": time.time(),
        }
        if len(self._scope_origins) > MAX_PERSISTED_SCOPES:
            earliest_time = time.time()
            earliest_key = None
            for key, value in self._scope_origins.items():
                if value['t'] < earliest_time:
                    earliest_time = value['t']
                    earliest_key = key
            assert earliest_key is not None
            del self._scope_origins[earliest_key]
        self._save_scope_data()
        return {"scope": scope}

    def _doc_origin(self, scope: Any) -> dict[str, Any]:
        origin = self._scope_origins.get(str(scope or ""), {})
        return {"origin": dict(origin)}
