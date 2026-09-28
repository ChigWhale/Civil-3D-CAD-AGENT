"""One built spatial index per drawing, kept until the drawing changes.

The plugin keeps a change counter per drawing (GET /revision, and the
X-Drawing-Revision header on every response). An index remembers the counter
it was built from; a query asks for the counter first and reuses the index
when it matches, so repeated questions about an unchanged drawing cost one
tiny request instead of a model-space download and a full build. Writes made
through either server drop the entry as well, so a stale plugin that never
learned to count still cannot serve a stale index past this process's own
writes.

Two things besides the drawing can make an index stale: the vocabulary
(layer_tags.json) changing, and the set of xref layers merged in. Both are part
of the key.

This module is synchronous and uses `requests`; the dashboard wraps calls in
asyncio.to_thread.
"""

import hashlib
import json
import os
import threading
import time

import requests

import spatial

import cadagent_config
PLUGIN_BASE = cadagent_config.get("plugin_base")
TIMEOUT = 30
REVISION_TIMEOUT = 5

_lock = threading.Lock()
_entries: dict = {}     # drawing key -> Entry
_acks: dict = {}        # drawing key -> {token: time}
ACK_TTL = 15 * 60       # an acknowledgement is good for a quarter hour


class PluginError(RuntimeError):
    pass


class Entry:
    __slots__ = ("revision", "tags_version", "layers", "data", "index", "built_at", "fingerprint")

    def __init__(self, revision, tags_version, layers, data, index, fingerprint):
        self.revision = revision
        self.tags_version = tags_version
        self.layers = layers
        self.data = data
        self.index = index
        self.built_at = time.time()
        self.fingerprint = fingerprint


def drawing_key(file) -> str:
    return os.path.normcase(os.path.abspath(file)) if file else ""


def _params(file):
    return {"file": file} if file else None


# ── Talking to the plugin ────────────────────────────────────────────────────


def revision(file, base=None):
    """The drawing's change counter, or None when the plugin does not have
    the route (an older build) or is not reachable."""
    base = base or PLUGIN_BASE
    try:
        r = requests.get(f"{base}/revision", params=_params(file), timeout=REVISION_TIMEOUT)
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or "revision" not in body:
        return None
    try:
        return int(body["revision"])
    except (TypeError, ValueError):
        return None


def fetch_snapshot(file, layers=None, base=None) -> tuple:
    """Model space plus the nested xref geometry on `layers`, merged, and the
    revision the plugin reported alongside it (None on an older plugin)."""
    base = base or PLUGIN_BASE
    try:
        r = requests.get(f"{base}/modelspace", params=_params(file), timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        raise PluginError("Civil 3D is not reachable — make sure AutoCAD is open with the "
                          "CADAgent plugin loaded.")
    except requests.exceptions.RequestException as e:
        raise PluginError(f"request to the plugin failed: {e}")
    try:
        data = r.json()
    except ValueError:
        raise PluginError("The drawing data came back unreadable.")
    if not isinstance(data, dict):
        raise PluginError("The drawing data came back in an unexpected shape.")
    if "error" in data:
        raise PluginError(str(data["error"]))
    rev = r.headers.get("X-Drawing-Revision")
    try:
        rev = int(rev) if rev is not None else None
    except ValueError:
        rev = None

    layers = list(layers) if layers else []
    if layers:
        try:
            n = requests.get(f"{base}/nested", params={**(_params(file) or {}), "layers": ",".join(layers)},
                             timeout=TIMEOUT)
            nested = n.json()
            if isinstance(nested, dict) and "error" not in nested:
                for category in ("polylines", "lines", "arcs"):
                    if nested.get(category):
                        # Stamp provenance: an xref-dependent layer reads "xref|layer",
                        # so the exemplar split can tell base material (the xrefs) from
                        # what was drawn in this drawing.
                        stamped = []
                        for e in nested[category]:
                            layer = str(e.get("layer") or "")
                            stamped.append({**e, "source": "nested",
                                            "xref": layer.split("|", 1)[0] if "|" in layer else ""})
                        data[category] = list(data.get(category) or []) + stamped
        except (requests.exceptions.RequestException, ValueError):
            pass    # older plugin without /nested: model space alone
    return data, rev


def _fingerprint(data: dict) -> str:
    """A stand-in for the revision on a plugin that has none: a hash of the
    geometry categories the index reads."""
    h = hashlib.sha1()
    for cat in ("lines", "arcs", "polylines", "blocks", "texts", "points", "circles", "dimensions",
                "labels", "pipe_networks", "feature_lines", "alignments"):
        h.update(json.dumps(data.get(cat) or [], sort_keys=True, default=str).encode())
    return h.hexdigest()


# ── The cache ─────────────────────────────────────────────────────────────────


def get(file, layers=None, base=None, force=False) -> Entry:
    """The current index for a drawing, built only when needed."""
    # An edit to the rules or the word dictionary made through the other server
    # process must reach this one: reload when the files moved, which bumps the
    # vocabulary version and so retires every cached index below.
    try:
        spatial.TAGS.refresh_if_changed()
    except Exception:
        pass
    layers = list(layers) if layers is not None else spatial.merge_layers()
    key = drawing_key(file)
    want_layers = tuple(sorted(set(l.lower() for l in layers)))

    with _lock:
        entry = _entries.get(key)
    if entry is not None and not force and entry.tags_version == spatial.TAGS.version \
            and entry.layers == want_layers:
        rev = revision(file, base)
        if rev is not None and entry.revision is not None and rev == entry.revision:
            return entry
        if rev is None and entry.revision is None:
            # No counter on this plugin: the only way to know is to look again.
            data, _ = fetch_snapshot(file, layers, base)
            if _fingerprint(data) == entry.fingerprint:
                return entry
            return _store(key, data, None, want_layers)

    data, rev = fetch_snapshot(file, layers, base)
    if rev is None:
        rev = revision(file, base)
    return _store(key, data, rev, want_layers)


def _store(key, data, rev, layers) -> Entry:
    index = spatial.build(data)
    entry = Entry(rev, spatial.TAGS.version, layers, data, index, _fingerprint(data))
    with _lock:
        _entries[key] = entry
    return entry


def peek(file) -> Entry | None:
    """Whatever is cached, without asking the plugin whether it is current."""
    with _lock:
        return _entries.get(drawing_key(file))


def invalidate(file=None) -> None:
    """Forget a drawing's index (every drawing when file is None)."""
    with _lock:
        if file is None:
            _entries.clear()
        else:
            _entries.pop(drawing_key(file), None)


def invalidate_all() -> None:
    invalidate(None)


# ── Acknowledgements ──────────────────────────────────────────────────────────
#
# A blocked write reports its conflicts with a token. The agent acknowledges
# the token (a logged decision), then repeats the write; the same conflicts
# then pass. Tokens are per drawing and expire.


def conflict_token(conflicts: list) -> str:
    body = json.dumps(conflicts, sort_keys=True, default=str).encode()
    return hashlib.sha1(body).hexdigest()[:12]


def acknowledge(file, token: str) -> None:
    key = drawing_key(file)
    with _lock:
        _acks.setdefault(key, {})[str(token).strip()] = time.time()


def acknowledged(file, token: str) -> bool:
    key = drawing_key(file)
    now = time.time()
    with _lock:
        acks = _acks.get(key) or {}
        for t in [t for t, at in acks.items() if now - at > ACK_TTL]:
            acks.pop(t, None)
        return str(token).strip() in acks


def consume(file, token: str) -> None:
    key = drawing_key(file)
    with _lock:
        (_acks.get(key) or {}).pop(str(token).strip(), None)
