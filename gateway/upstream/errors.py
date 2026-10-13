"""Upstream error types."""


from gateway.common.log import log


class ContentRejected(Exception):
    """Upstream content review rejected this request (403 / code 11140).

    Not an account problem: another credential gets the same 403 for the same
    content, so it is passed straight through instead of cooling the pool.
    """

    def __init__(self, http_error=None, detail=""):
        self.http_error = http_error
        self.detail = detail or ""
        super().__init__("upstream rejected the request content (403)")

    @property
    def code(self):
        return 403


class RateLimited(Exception):
    """Upstream throttled this model (429 / code 6004). Distinct from a dead
    pool: the credential is fine, only the model is cooling down for a while."""

    def __init__(self, http_error=None, detail="", wait=60, message=""):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        # 429s answered without an upstream call (the pool is parked by the
        # daily token guard) carry their own text instead of the upstream
        # wording.
        self.message = message or ""
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))
