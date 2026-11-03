"""Ask the download client what is unfinished. `check` is read-only: it never changes a torrent.

A real run moved episodes Transmission was still writing, and it re-created one at
its old path. So a whole torrent is off limits until it finishes, including files in
it that are already complete, and a paused or stalled torrent counts as unfinished.

File age cannot answer this: partial files keep their normal names, are preallocated
to full size, and stall for hours. If the client cannot be reached the answer is
unknown, not "nothing is downloading", so callers must treat .error as a stop.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

FIELDS = ["name", "downloadDir", "files", "leftUntilDone", "percentDone",
          "metadataPercentComplete", "status"]
CHECKING = (1, 2)  # queued to verify, verifying
# torrent-get reads. torrent-stop (pause) is the one change MyLibrary may make, and only
# torrentdupes asks for it. Removing a torrent or its data is never allowed.
METHODS = {"torrent-get", "torrent-stop"}


@dataclass
class Active:
    paths: set[str] = field(default_factory=set)
    torrents: list[str] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)  # every torrent's files: path -> torrent
    error: str | None = None


def _post(cfg, session: str, opener, method="torrent-get", arguments=None):
    if method not in METHODS:
        raise ValueError(f"RPC method not allowed: {method}")
    headers = {"Content-Type": "application/json", "X-Transmission-Session-Id": session}
    if cfg.transmission_user or cfg.transmission_password:
        login = f"{cfg.transmission_user}:{cfg.transmission_password}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(login).decode()
    body = json.dumps({"method": method,
                       "arguments": {"fields": FIELDS} if arguments is None else arguments}).encode()
    req = urllib.request.Request(cfg.transmission_url, data=body, headers=headers)
    with opener(req, timeout=10) as resp:
        return json.loads(resp.read())


def call(cfg, opener, method="torrent-get", arguments=None) -> dict:
    try:
        data = _post(cfg, "", opener, method, arguments)
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 hands over the session id to retry with
            raise
        data = _post(cfg, e.headers.get("X-Transmission-Session-Id", ""), opener, method, arguments)
    if data.get("result") != "success":
        raise ValueError(str(data.get("result"))[:200])
    return data.get("arguments", {})


def _torrents(cfg, opener, fields=None) -> list[dict]:
    args = None if fields is None else {"fields": fields}
    return call(cfg, opener, "torrent-get", args).get("torrents", [])


def describe(e: Exception) -> str:
    """A short reason for a failed call, never including the login."""
    if isinstance(e, urllib.error.HTTPError):
        why = "login refused: check [transmission] user and password" if e.code == 401 \
            else f"HTTP {e.code}"
        return f"cannot check downloads ({why})"
    reason = getattr(e, "reason", e)
    return f"cannot check downloads ({type(e).__name__}: {reason})"[:200]


def _unfinished(t: dict) -> bool:
    return (t.get("leftUntilDone", 0) > 0 or t.get("percentDone", 1) < 1
            or t.get("metadataPercentComplete", 1) < 1 or t.get("status") in CHECKING
            or any(f.get("bytesCompleted", 0) < f.get("length", 0) for f in t.get("files", [])))


def check(cfg, opener=urllib.request.urlopen) -> Active:
    """Return what the client holds and what is unfinished. Disabled means nothing to report."""
    if not cfg.transmission_enabled:
        return Active()
    try:
        torrents = _torrents(cfg, opener)
    except (urllib.error.URLError, OSError, ValueError) as e:  # HTTPError is a URLError
        return Active(error=describe(e))
    out = Active()
    for t in torrents:
        unfinished = _unfinished(t)
        if unfinished:
            out.torrents.append(t.get("name", "?"))
        for f in t.get("files", []):
            path = os.path.normpath(os.path.join(t.get("downloadDir", ""), f["name"]))
            out.held[path] = t.get("name", "?")
            if unfinished:
                out.paths.add(path)
    return out
