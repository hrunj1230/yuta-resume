import http.client
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest

from scripts import serve
from test_resumelib import temporary_root


class ResumeServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(self.root))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, route, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            request_headers.update(headers)
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, route, body=payload, headers=request_headers)
        response = connection.getresponse()
        status = response.status
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return status, data

    def test_ping_and_get_versions(self):
        self.assertEqual(self.request("GET", "/api/ping"), (200, {"edit": True}))
        status, data = self.request("GET", "/api/versions")
        self.assertEqual(status, 200)
        self.assertEqual(data["versions"][0]["version"], "1.0.0")

    def test_post_creates_version_and_manifest(self):
        status, data = self.request("POST", "/api/versions", {
            "bump": "minor", "note": "새 경력", "content": "<h1>Test</h1><p>Body</p>"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["version"], "1.1.0")
        self.assertEqual(data["path"], "resumes/v1.1.0/")
        self.assertTrue((self.root / data["path"] / "content.html").is_file())
        self.assertTrue((self.root / data["path"] / "resume.docx").is_file())
        manifest = json.loads((self.root / "resumes" / "versions.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["versions"][0]["version"], "1.1.0")
        self.assertEqual(manifest["versions"][0]["note"], "새 경력")

    def test_rejects_bad_host_and_origin(self):
        status, _ = self.request("GET", "/api/ping", headers={"Host": "example.com"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/versions", {
            "bump": "patch", "note": "test", "content": "<h1>Test</h1>"},
            headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        self.assertFalse((self.root / "resumes" / "v1.0.1").exists())

    def test_rejects_large_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/api/versions")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(serve.MAX_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn("error", json.loads(response.read().decode("utf-8")))
        finally:
            connection.close()

    def test_rejects_empty_content_and_bad_bump(self):
        for payload in (
            {"bump": "patch", "note": "test", "content": "<p> </p>"},
            {"bump": "invalid", "note": "test", "content": "<h1>Test</h1>"},
        ):
            with self.subTest(payload=payload):
                status, _ = self.request("POST", "/api/versions", payload)
                self.assertEqual(status, 400)
        self.assertFalse((self.root / "resumes" / "v1.0.1").exists())

    def test_post_other_path_is_not_allowed(self):
        status, _ = self.request("POST", "/resume.html", {
            "bump": "patch", "note": "test", "content": "<h1>Test</h1>"})
        self.assertEqual(status, 405)


    def status_of(self, route):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", route, headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def test_serves_site_files_only(self):
        (self.root / "resume.html").write_text("ok", encoding="utf-8")
        (self.root / ".git").mkdir(exist_ok=True)
        (self.root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        (self.root / "resumes" / "outside-link").symlink_to(outside.name)
        self.assertEqual(self.status_of("/resume.html"), 200)
        self.assertEqual(self.status_of("/resumes/versions.json"), 200)
        self.assertEqual(self.status_of("/.git/HEAD"), 404)
        self.assertEqual(self.status_of("/%2Egit/HEAD"), 404)
        self.assertEqual(self.status_of("/scripts/serve.py"), 404)
        self.assertEqual(self.status_of("/resumes/outside-link"), 404)


if __name__ == "__main__":
    unittest.main()
