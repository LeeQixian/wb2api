"""HTTP server class and socket tuning."""

from http.server import ThreadingHTTPServer

from gateway.http.handler import Handler


class GatewayServer(ThreadingHTTPServer):
    """stdlib backlog=5 drops SYNs under bursts (client retransmits cost ~1s)."""
    request_queue_size = 128


def build_server(host, port, handler=Handler):
    httpd = GatewayServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
