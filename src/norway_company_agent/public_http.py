"""HTTP transport for untrusted public URLs with DNS and connection tied together."""
from __future__ import annotations

import http.client
import ipaddress
import socket
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, build_opener


def _public_addresses(host: str, port: int) -> list[tuple]:
    hostname = host.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    if not addresses:
        raise ValueError("Hostname did not resolve")
    for _, _, _, _, target in addresses:
        ip = ipaddress.ip_address(target[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("Destination address is not public")
    # Prefer IPv4 where available without hiding other entries from validation.
    return sorted(addresses, key=lambda entry: entry[0] != socket.AF_INET)


def assert_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError("Only HTTP(S) URLs with a public host and no credentials are allowed")
    _public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))


def connect_public_socket(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Connect directly to a validated socket address while preserving TLS hostname checks."""
    last_error = None
    for family, kind, protocol, _, destination in _public_addresses(*address):
        connection = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(destination)
            if connection.getpeername()[0] != destination[0]:
                raise OSError("Connected peer differs from the validated address")
            return connection
        except OSError as exc:
            connection.close()
            last_error = exc
    raise last_error or OSError("Public connection failed")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_public_socket


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_public_socket


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PublicHTTPConnection, request)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


def public_opener(*handlers):
    """Disable proxy resolution for untrusted targets and validate each connection."""
    return build_opener(ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(), *handlers)
