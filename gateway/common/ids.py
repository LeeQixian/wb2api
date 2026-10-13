"""Opaque id generation for translated protocol objects."""
import uuid


def new_id(prefix):
    """Return prefix + a random hex id (e.g. rs_, msg_, resp_)."""
    return prefix + uuid.uuid4().hex
