"""Quality subprocesses may contact their local fixtures, never remote providers."""
import ipaddress
import socket

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _local(address):
    if not isinstance(address, tuple):  # local Unix sockets
        return
    host = str(address[0])
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = host.lower() == 'localhost'
    if not allowed:
        raise OSError('Quality blocked a connection outside local fixtures')


def connect(self, address):
    _local(address)
    return _connect(self, address)


def connect_ex(self, address):
    _local(address)
    return _connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
