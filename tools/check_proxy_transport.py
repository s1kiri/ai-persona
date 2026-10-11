#!/usr/bin/env python3
"""Optional installed-CLI probe: mock SOCKS only, no credentials/model/remote forwarding.

Run from a regular macOS terminal; the child uses the production network guard.
A temporary empty CODEX_HOME avoids reading existing CLI authorization.
"""
import json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_proxy import server, config, Vault, SECRET
from persona_runtime.proxy import save, transport
from persona_runtime.codex import CodexBackend
with tempfile.TemporaryDirectory(prefix='persona-proxy-probe-') as directory:
    path = Path(directory) / 'proxy.json'
    cli_home = Path(directory) / 'cli-home'
    cli_home.mkdir()
    with server(reply=5) as mock:
        settings = config(mock.server_address[1])
        Vault().put(settings['account'], SECRET)
        save(settings, path)
        def local_transport(env, **kwargs):
            env['CODEX_HOME'] = str(cli_home)
            return transport(env, path, timeout=3, keychain_factory=Vault)
        with patch('persona_runtime.codex.transport', side_effect=local_transport):
            result = CodexBackend(timeout=20, model='synthetic').doctor()
        print(json.dumps({'cli_version': result['cli_version'], 'model_called': result['model_called'], 'proxy': result['proxy'], 'checks': result['checks'], 'socks_auth_connections':len(mock.credentials), 'socks_connect_requests':len(mock.destinations), 'remote_forwarding':'intentionally denied by mock'}, indent=2))
        assert mock.credentials, 'Codex did not reach the SOCKS5 bridge'
