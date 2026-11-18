import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from event_receiver.event_connection import EventClient


class EventTransportTests(unittest.TestCase):
    def test_export_uses_configured_origin_and_bearer_header(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"events": []}).encode())

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = EventClient("fixture-token", f"http://127.0.0.1:{server.server_port}")
                self.addCleanup(client.close)
                self.assertEqual(client.get({"after": 10}), {"events": []})
                self.assertEqual(requests, [("/wardogs/v1/events?after=10", "Bearer fixture-token")])
            finally:
                server.shutdown()
                thread.join()

    def test_rejects_non_origin_or_embedded_credentials(self):
        for origin in ("ftp://localhost", "https://user:pass@localhost", "https://localhost/custom"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                EventClient("fixture-token", origin)
