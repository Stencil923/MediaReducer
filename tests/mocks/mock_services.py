"""A tiny combined mock for the three media-server integrations the engine talks
to over HTTP — Plex (protected collections), Jellyfin (favorites + BoxSets), and
Radarr (post-delete cleanup). One server routes by path prefix, so a test points
engine.PLEX_URL / JELLYFIN_URL / RADARR_URL all at the same base.

The movie set is configured by the caller: pass protected/favorite/radarr paths
so the mock reports exactly the movies a test seeded on disk. Every request is
recorded in `.calls` (and DELETEs in `.deletes`) so a test can assert the engine
actually hit the endpoint, not just that the decision logic returned the right
thing.

start_mock_services(...) runs it in a daemon thread on an ephemeral port and
returns the instance (with .base_url); call .stop() when done. Also runnable as
`python3 mock_services.py <port>` for the e2e harness.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


class MockServices:
    def __init__(self, *, plex_collections=None, jellyfin_favorites=None,
                 jellyfin_boxsets=None, radarr_movies=None):
        # plex_collections: {collection_title: [{"ratingKey": str, "file": path}, ...]}
        self.plex_collections = plex_collections or {}
        # jellyfin_favorites: [path, ...]
        self.jellyfin_favorites = list(jellyfin_favorites or [])
        # jellyfin_boxsets: {boxset_name: [path, ...]}
        self.jellyfin_boxsets = jellyfin_boxsets or {}
        # radarr_movies: [{"id": int, "tmdbId": int, "path": str, "rootFolderPath": str}, ...]
        self.radarr_movies = list(radarr_movies or [])
        self.calls = []          # every (method, path) served
        self.deletes = []        # radarr movie ids deleted
        self._server = None
        self._thread = None
        self.base_url = ""

    # ── request handlers, one per service ────────────────────────────────────
    def _plex(self, path):
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": [
                {"key": "1", "type": "movie", "title": "Movies"}]}}
        if path.endswith("/collections"):
            return {"MediaContainer": {"Directory": [
                {"title": t, "ratingKey": f"col-{i}"}
                for i, t in enumerate(self.plex_collections)]}}
        if path.startswith("/library/collections/") and path.endswith("/children"):
            idx = int(path.split("/library/collections/col-")[1].split("/")[0])
            title = list(self.plex_collections)[idx]
            return {"MediaContainer": {"Metadata": [
                {"ratingKey": m["ratingKey"],
                 "Media": [{"Part": [{"file": m["file"]}]}]}
                for m in self.plex_collections[title]]}}
        return {"MediaContainer": {}}

    def _jellyfin(self, path, q):
        if path == "/Users":
            return [{"Id": "user-1", "Name": "tester"}]
        if "/Items" in path:
            filters = (q.get("Filters") or [""])[0]
            if "IsFavorite" in filters:
                return {"Items": [{"Path": p} for p in self.jellyfin_favorites]}
            if "BoxSet" in (q.get("IncludeItemTypes") or [""])[0]:
                return {"Items": [{"Id": f"box-{i}", "Name": n}
                                  for i, n in enumerate(self.jellyfin_boxsets)]}
            parent = (q.get("ParentId") or [""])[0]
            if parent.startswith("box-"):
                name = list(self.jellyfin_boxsets)[int(parent.split("box-")[1])]
                return {"Items": [{"Id": f"m-{j}", "Path": p}
                                  for j, p in enumerate(self.jellyfin_boxsets[name])]}
            return {"Items": []}
        return {}

    def _radarr_list(self, q):
        want = (q.get("tmdbId") or [None])[0]
        if want is None:
            return list(self.radarr_movies)
        return [m for m in self.radarr_movies if str(m.get("tmdbId")) == str(want)]

    def stop(self):
        if self._server:
            self._server.shutdown()


def _make_handler(mock: MockServices):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            mock.calls.append(("GET", u.path))
            if u.path.startswith("/library/"):
                return self._send(mock._plex(u.path))
            if u.path == "/Users" or "/Items" in u.path:
                return self._send(mock._jellyfin(u.path, q))
            if u.path.startswith("/api/v3/movie"):
                return self._send(mock._radarr_list(q))
            self._send({}, 404)

        def do_DELETE(self):
            u = urlparse(self.path)
            mock.calls.append(("DELETE", u.path))
            if u.path.startswith("/api/v3/movie/"):
                mid = u.path.split("/api/v3/movie/")[1].split("?")[0]
                mock.deletes.append(mid)
                return self._send({}, 200)
            self._send({}, 404)
    return Handler


def start_mock_services(**kwargs) -> MockServices:
    mock = MockServices(**kwargs)
    server = HTTPServer(("127.0.0.1", 0), _make_handler(mock))
    mock._server = server
    mock.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    mock._thread = threading.Thread(target=server.serve_forever, daemon=True)
    mock._thread.start()
    return mock


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    m = MockServices()
    HTTPServer(("0.0.0.0", port), _make_handler(m)).serve_forever()
