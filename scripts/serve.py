"""Serve the resume locally with a same-origin version creation endpoint."""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import unquote, urlsplit

try:
    from .resumelib import create_version, load_versions
except ImportError:
    from resumelib import create_version, load_versions


MAX_BODY_BYTES = 1024 * 1024
STATIC_FILES = {"", "index.html", "resume.html"}
STATIC_DIRS = {"assets", "resumes"}


def make_handler(root, lock=None):
    root = Path(root).resolve()
    write_lock = lock if lock is not None else threading.Lock()

    class ResumeHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _allowed_origin(self):
            port = self.server.server_address[1]
            host = self.headers.get("Host", "").lower()
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self._json(403, {"error": "허용되지 않은 Host입니다."})
                return False
            origin = self.headers.get("Origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    allowed = (parsed.scheme == "http" and parsed.netloc.lower() == host
                               and not (parsed.path or parsed.query or parsed.fragment))
                except ValueError:
                    allowed = False
                if not allowed:
                    self._json(403, {"error": "다른 출처의 요청은 허용되지 않습니다."})
                    return False
            return True

        def _static_allowed(self):
            """Serve only the published site files, never dotfiles or paths outside root."""
            parts = [part for part in unquote(urlsplit(self.path).path).split("/") if part]
            if any(part.startswith(".") for part in parts):
                return False
            if not (len(parts) <= 1 and "/".join(parts) in STATIC_FILES
                    or parts and parts[0] in STATIC_DIRS):
                return False
            target = Path(self.translate_path(self.path)).resolve()
            return target == root or root in target.parents

        def _serve_static(self, head=False):
            if not self._static_allowed():
                self.send_error(404)
                return
            if head:
                super().do_HEAD()
            else:
                super().do_GET()

        def do_GET(self):
            if not self._allowed_origin():
                return
            route = urlsplit(self.path).path
            if route == "/api/ping":
                self._json(200, {"edit": True})
            elif route == "/api/versions":
                try:
                    self._json(200, load_versions(root))
                except Exception:
                    self._json(500, {"error": "버전 목록을 읽지 못했습니다."})
            else:
                self._serve_static()

        def do_HEAD(self):
            if not self._allowed_origin():
                return
            route = urlsplit(self.path).path
            if route in {"/api/ping", "/api/versions"}:
                self._json(200, {})
            else:
                self._serve_static(head=True)

        def do_POST(self):
            if not self._allowed_origin():
                return
            if urlsplit(self.path).path != "/api/versions":
                self._json(405, {"error": "지원하지 않는 POST 경로입니다."})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._json(400, {"error": "Content-Type은 application/json이어야 합니다."})
                return
            length_text = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]+", length_text):
                self._json(400, {"error": "Content-Length가 올바르지 않습니다."})
                return
            if len(length_text) > 10:
                self._json(413, {"error": "본문은 1MB를 넘을 수 없습니다."})
                return
            length = int(length_text)
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "본문은 1MB를 넘을 수 없습니다."})
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "JSON 본문이 올바르지 않습니다."})
                return
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                self._json(400, {"error": "본문 HTML이 필요합니다."})
                return
            try:
                with write_lock:
                    result = create_version(root, bump=data.get("bump"), note=data.get("note"),
                                            content_html=data["content"])
            except ValueError as error:
                self._json(400, {"error": str(error)})
                return
            except Exception:
                self._json(500, {"error": "버전을 저장하지 못했습니다."})
                return
            self._json(201, result)

    return ResumeHandler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="이력서 로컬 편집 서버")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.root))
    print(f"편집 모드: http://127.0.0.1:{server.server_address[1]}/resume.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
