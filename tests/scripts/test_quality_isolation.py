"""Exercise the offline boundary in a fresh interpreter, before application imports."""
import os
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
