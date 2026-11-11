import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


@pytest.fixture(scope="session")
def payload_text():
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture()
def payload(payload_text):
    return json.loads(payload_text)


# A local page that behaves like the Ad Library: first batch embedded in the
# HTML, the rest fetched via POST to a graphql endpoint as the user scrolls.
PAGE = """<!doctype html><html><body style="margin:0">
<div id="filler" style="height:4000px"></div>
<script type="application/json" data-sjs>__FIRST__</script>
<script>
let page = 0, busy = false;
addEventListener('scroll', async () => {
  if (busy || innerHeight + scrollY < document.body.scrollHeight - 200) return;
  busy = true;
  const r = await fetch('/api/graphql/', {method: 'POST', body: 'page=' + (++page)});
  if (r.ok) {
    await r.text();
    document.getElementById('filler').style.height = (4000 * (page + 1)) + 'px';
  }
  busy = false;
});
</script></body></html>"""


def _page(payload, edges, has_next):
    doc = json.loads(json.dumps(payload))
    connection = doc["data"]["ad_library_main"]["search_results_connection"]
    connection["edges"] = edges
    connection["page_info"]["has_next_page"] = has_next
    return json.dumps(doc)


@pytest.fixture()
def fake_library(payload):
    edges = payload["data"]["ad_library_main"]["search_results_connection"]["edges"]
    first = _page(payload, edges[:10], True)
    later = [_page(payload, edges[10:20], True), _page(payload, edges[20:], False)]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self._send("text/html", PAGE.replace("__FIRST__", first))

        def do_POST(self):
            number = int(self.rfile.read(int(self.headers["Content-Length"])).split(b"=")[1])
            if number > len(later):
                self.send_error(404)
                return
            self._send("application/json", later[number - 1])

        def _send(self, content_type, body):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/ads/library/", requests
    server.shutdown()


@pytest.fixture(autouse=True)
def no_network_previews(monkeypatch):
    """Tests never contact Meta's CDN; preview downloads fail unless a test
    provides its own fetcher."""
    from ads_scrapper import media

    def offline(url):
        raise OSError("sin red en los tests")

    monkeypatch.setattr(media, "_fetch", offline)
