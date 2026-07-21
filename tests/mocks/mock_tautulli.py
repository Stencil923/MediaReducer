"""Tiny mock Tautulli server for the app/engine tests and browser e2e runs."""
import json
import random
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

random.seed(42)

MOVIES = []
for i in range(1, 401):
    # Movies 301-400 live OUTSIDE the monitored path (/library/movies).
    folder = "movies" if i <= 300 else "other"
    MOVIES.append({
        "rating_key": str(1000 + i),
        "title": f"Test Movie {i}",
        "year": 1980 + (i % 45),
        "play_count": random.choice([0, 0, 1, 2, 5, 12]),
        "last_played": random.choice([0, 1600000000 + i * 10000]),
        "added_at": 1500000000 + i * 50000,
        "file_size": 700_000_000 + i * 10_000_000,
        "file": f"/{folder}/Test Movie {i}/Test Movie {i}.mkv",
    })

CALLS = {"get_libraries": 0, "get_library_media_info": 0, "get_metadata": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        cmd = (q.get("cmd") or [""])[0]
        CALLS[cmd] = CALLS.get(cmd, 0) + 1
        open("/tmp/mock_calls.log", "a").write(cmd + "\n")  # CALLLOG
        if cmd == "get_libraries":
            data = [{"section_id": "1", "section_name": "Movies",
                     "section_type": "movie", "is_active": 1}]
        elif cmd == "get_library_media_info":
            start = int((q.get("start") or ["0"])[0])
            length = int((q.get("length") or ["25"])[0])
            data = {"recordsFiltered": len(MOVIES), "data": MOVIES[start:start + length]}
        elif cmd == "get_metadata":
            rk = (q.get("rating_key") or [""])[0]
            idx = int(rk) - 1000
            # Give ~80% of movies an IMDb guid; leave the rest unrated.
            guids = [f"imdb://tt{7000000 + idx}"] if idx % 5 != 0 else []
            # Every 7th movie sits in the "Protected" collection.
            collections = ["Protected"] if idx % 7 == 0 else []
            data = {"rating_key": rk, "guids": guids, "collections": collections}
        else:
            data = {}
        body = json.dumps({"response": {"result": "success", "data": data}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
