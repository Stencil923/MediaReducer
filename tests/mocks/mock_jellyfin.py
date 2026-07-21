"""Standalone mock Jellyfin server for the browser e2e — the Jellyfin
counterpart to mock_tautulli.py. Serves the SAME 400-movie fixture library
(same on-disk paths make_fixtures.py builds) in Jellyfin's API shapes so a real
Simulate can run end to end with Jellyfin as the source.

Endpoints get_all_movies_from_jellyfin() hits:
  GET /Users                         -> the user list (play data is per-user)
  GET /Items?IncludeItemTypes=Movie  -> the base library (Path/MediaSources/
                                        ProviderIds/DateCreated/RunTimeTicks)
  GET /Users/{uid}/Items?EnableUserData=true -> per-user PlayCount/LastPlayed/
                                        IsFavorite, aggregated by the engine

No BoxSet endpoints are needed: the fixture leaves JELLYFIN_PROTECTED_COLLECTIONS
empty, so _jellyfin_protected_items() returns before querying the server.
Favorites/BoxSet paths still answer (empty) so a stray query never 404s.

Run as: python3 mock_jellyfin.py <port>
"""
import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

random.seed(42)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# Base library — mirrors mock_tautulli.py: movies 1-300 under /movies (monitored),
# 301-400 under /other. Same paths, so the engine's path translation maps them
# under MEDIAREDUCER_LIBRARY exactly as it does for the Plex/Tautulli run.
ITEMS = []
USERDATA = []  # per-user watch state for user u1, keyed to the same items
for i in range(1, 401):
    folder = "movies" if i <= 300 else "other"
    added_at = 1_500_000_000 + i * 50_000
    plays = random.choice([0, 0, 1, 2, 5, 12])
    last_played = random.choice([0, 1_600_000_000 + i * 10_000])
    prov = {"Imdb": f"tt{7000000 + i}"} if i % 5 != 0 else {}
    ITEMS.append({
        "Id": f"m{i}",
        "Name": f"Test Movie {i}",
        "ProductionYear": 1980 + (i % 45),
        "Path": f"/{folder}/Test Movie {i}/Test Movie {i}.mkv",
        "DateCreated": _iso(added_at),
        "ProviderIds": prov,
        "RunTimeTicks": 60_000_000 * 90,
        "MediaSources": [{
            "Size": 700_000_000 + i * 10_000_000,
            "Bitrate": 8_000_000,
            "MediaStreams": [{"Type": "Video", "Height": 1080 if i % 2 else 720}],
        }],
    })
    USERDATA.append({
        "Id": f"m{i}",
        "UserData": {
            "PlayCount": plays,
            "Played": plays > 0,
            "LastPlayedDate": _iso(last_played) if last_played else None,
            "IsFavorite": (i % 25 == 0),
        },
    })


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path == "/System/Info":   # the connection-health probe
            return self._send({"ServerName": "e2e-jellyfin", "Version": "10.9.0",
                               "Id": "mock-server"})
        if path == "/Users":
            return self._send([{"Id": "u1", "Name": "e2e"}])
        if "/Items" in path:
            filters = (q.get("Filters") or [""])[0]
            include = (q.get("IncludeItemTypes") or [""])[0]
            if "IsFavorite" in filters:
                return self._send({"Items": [it for it, ud in zip(ITEMS, USERDATA)
                                             if ud["UserData"]["IsFavorite"]]})
            if "BoxSet" in include:
                return self._send({"Items": []})
            if path.startswith("/Users/") and (q.get("EnableUserData") or [""])[0] == "true":
                return self._send({"Items": USERDATA})
            if "ParentId" in q:
                return self._send({"Items": []})
            return self._send({"Items": ITEMS})   # the base library scan
        self._send({})


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
