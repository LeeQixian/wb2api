"""Built-in model catalog snapshots loaded from data/*.json.
Live sources override them via upstream.catalog.merge_catalog()."""
import json
import os

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def load(name):
    """Read one catalog table (data/<name>.json) and return the parsed list."""
    path = os.path.join(_DATA_DIR, name + ".json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


STATIC_INTL_MODELS = load("catalog_intl")
STATIC_MODELS = STATIC_INTL_MODELS
STATIC_CN_MODELS = load("catalog_cn")
