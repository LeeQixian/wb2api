"""Request handler assembled from the base and route mixins."""

from http.server import BaseHTTPRequestHandler

from gateway.http.base import HTTPBase
from gateway.http.routes.get import GetRoutes
from gateway.http.routes.post import PostRoutes


class Handler(HTTPBase, GetRoutes, PostRoutes, BaseHTTPRequestHandler):
    """All routes; see the mixins for their handlers."""

    #: small JSON answers pay ~40ms in delayed-ACK without this.
    disable_nagle_algorithm = True
