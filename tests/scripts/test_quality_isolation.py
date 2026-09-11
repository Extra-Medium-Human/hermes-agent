"""Exercise the offline boundary in a fresh interpreter, before application imports."""
import os
import json
from pathlib import Path
import subprocess
import sys


def test_quality_process_can_use_local_fixture_but_not_remote_provider(tmp_path):
    guard = Path(__file__).resolve().parents[2] / 'scripts/quality/offline'
    program = '''
import socket
server = socket.socket()
server.bind(('127.0.0.1', 0))
server.listen()
client = socket.socket()
client.connect(server.getsockname())
client.close()
server.close()
try:
    socket.socket().connect(('203.0.113.1', 443))
except OSError as error:
    assert 'Quality blocked' in str(error)
else:
    raise AssertionError('Remote provider connection escaped isolation')
'''
    result = subprocess.run([sys.executable, '-c', program], env={
        'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'PYTHONPATH': str(guard),
    }, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_quality_home_keeps_unix_socket_budget_under_nested_runner_temp(tmp_path):
    runner = Path(__file__).resolve().parents[2] / 'scripts/quality/run.py'
    nested = tmp_path / ('nested-runner-home-' * 5)
    nested.mkdir()
    program = 'import json,os; print(json.dumps({"home":os.environ["HOME"], "tmp":os.environ["TMPDIR"]}))'
    result = subprocess.run([sys.executable, str(runner), sys.executable, '-c', program], env={
        'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'TMPDIR': str(nested),
    }, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    socket = Path(settings['home']) / '.hermes/desktop-ssh/0123456789abcdef.sock.0123456789abcdef'
    assert len(os.fsencode(socket)) < 104
    assert not Path(settings['home']).exists(), 'Owned check home must be cleaned'
    assert nested.is_dir(), 'The parent runner temporary home must be preserved'
