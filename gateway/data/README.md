# data

Pure model-catalogue data, loaded at import by `gateway/catalog.py`.

- `catalog_intl.json` - international realm snapshot.
- `catalog_cn.json` - domestic realm snapshot.

These are the bundled fallback model list, so a machine without the desktop
app (and its cache) still sees the full catalogue. A live fetch from the
upstream `/v3/config` is layered on top by `gateway/upstream/catalog.py`;
whatever the live source reports wins field by field, and the snapshot fills
the gaps.
