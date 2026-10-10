"""Loopback-only HTTP UI, JSON API and seekable local media."""
import argparse
import base64
import json
import mimetypes
import os
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .domain import new_id, validate_document
from .providers import Planner
from .service import Conflict, Store, Worker, encode

STATIC = Path(__file__).parent / "static"


def load_env(path):
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            os.environ.setdefault(key, value.strip().strip("\"'"))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        super().__init__(address, Handler)
        self.store = store


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Request paths only; never log bodies, tokens or prompts.
        print(f"Studio {self.address_string()} {fmt % args}", flush=True)

    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def json_response(self, result, status=200):
        data = encode(result).encode()
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def guard(self, write=False):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise PermissionError("localhostからアクセスしてください")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            raise PermissionError("別のサイトからの操作は受け付けません")
        if write and self.headers.get("X-Studio-Request") != "1":
            raise PermissionError("Studioの画面から操作してください")

    def body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("JSONで送信してください")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 22 * 1024 * 1024:
            raise ValueError("送信内容が大きすぎるか空です")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("JSONオブジェクトが必要です")
        return value

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def do_HEAD(self):
        self.dispatch(False, head=True)

    def dispatch(self, write, head=False):
        try:
            self.guard(write)
            path = unquote(urlsplit(self.path).path)
            store = self.server.store
            if not write and path in {"/", "/app.js", "/style.css"}:
                return self.file(STATIC / ("index.html" if path == "/" else path[1:]), head=head)
            if not write and path.startswith("/media/"):
                relative = path[len("/media/"):]
                if not re.fullmatch(r"(?:assets/[a-f0-9]{32}\.(?:png|jpg|webp|mp3|wav)|takes/[a-f0-9]{32}/(?:raw\.mp4|thumb\.jpg)|renders/[a-f0-9]{32}/(?:master\.mp4|cover\.jpg|manifest\.json))", relative):
                    raise KeyError("素材がありません")
                target = (store.root / relative).resolve()
                if not target.is_relative_to(store.root):
                    raise PermissionError("不正な素材パスです")
                return self.file(target, head=head)
            if not write and path == "/api/config":
                return self.json_response({"llm_available": Planner().available, "default_provider": "h3", "duration": 30,
                                           "h3_base_url": os.environ.get("H3_BASE_URL", "http://127.0.0.1:30010"),
                                           "h3_ref_base_url": os.environ.get("H3_REF_BASE_URL", "http://127.0.0.1:30011")})
            if path == "/api/projects":
                if not write:
                    return self.json_response(store.library())
                data = self.body()
                return self.json_response(store.create(data.get("title", ""), data.get("brief", ""), data.get("layout", "5x6"), data.get("aspect_ratio", "9:16")), 201)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(?:/([a-z-]+))?", path)
            if not match:
                raise KeyError("ページがありません")
            ident, action = match.groups()
            if not write:
                if action == "export":
                    project = store.detail(ident)
                    # Export every input, take and master provenance; no config secrets.
                    return self.json_response({"format": "minimax-studio-v1", "project": project})
                if action is None:
                    return self.json_response(store.detail(ident))
                raise KeyError("ページがありません")
            data = self.body()
            if action == "save":
                result = store.save(ident, data.get("document"), data.get("version"))
            elif action == "import":
                doc = data.get("document")
                if isinstance(doc, dict) and isinstance(doc.get("cuts"), list):
                    for cut in doc["cuts"]:
                        if isinstance(cut, dict) and not cut.get("id"):
                            cut["id"] = new_id()
                result = store.save(ident, doc, data.get("version"))
            elif action == "upload":
                try:
                    content = base64.b64decode(data.get("content", ""), validate=True)
                except Exception as exc:
                    raise ValueError("素材データが不正です") from exc
                result = store.upload(ident, data.get("name", "asset"), data.get("mime", ""), content)
            elif action in {"generate", "assemble", "plan", "revise", "recover"}:
                result = store.enqueue(ident, action, data)
            elif action == "choose":
                result = store.choose(ident, data.get("take_id"))
            elif action == "apply-proposal":
                result = store.apply_proposal(ident, data.get("job_id"), data.get("version"))
            elif action in {"approve", "reopen", "archive", "restore"}:
                result = store.set_state(ident, action)
            else:
                raise KeyError("操作がありません")
            self.json_response(result)
        except Conflict as exc:
            self.json_response({"error": str(exc)}, 409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)[:2000]}, 400)
        except KeyError as exc:
            self.json_response({"error": str(exc.args[0])}, 404)
        except PermissionError as exc:
            self.json_response({"error": str(exc)}, 403)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"Studio error: {type(exc).__name__}", flush=True)
            self.json_response({"error": "処理に失敗しました。入力とサーバーの状態を確認してください"}, 500)

    def file(self, path, head=False):
        if not path.is_file():
            raise KeyError("ファイルがありません")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or not any(match.groups()):
                return self.range_error(size)
            a, b = match.groups()
            if a:
                start, end = int(a), min(int(b), size-1) if b else size-1
            else:
                start = max(0, size-int(b))
            if start > end or start >= size or start < 0:
                return self.range_error(size)
            status = 206
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end-start+1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = end-start+1
            while remaining:
                block = source.read(min(256 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def range_error(self, size):
        self.send_response(416)
        self.headers_common()
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="MiniMax Local Studio — 30秒動画制作")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--data-dir", default="data/studio")
    parser.add_argument("--env-file", default=".env.studio")
    args = parser.parse_args()
    load_env(Path(args.env_file))
    store = Store(args.data_dir)
    server = Server(("127.0.0.1", args.port), store)
    worker = Worker(store)
    worker.start()
    def shutdown(*_):
        worker.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"MiniMax Local Studio: http://127.0.0.1:{args.port}", flush=True)
    print(f"保存先: {store.root}\nAIコマ割り: {'利用可能' if Planner().available else '未設定（テンプレート・手動編集は利用可能）'}", flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        worker.close()
        server.server_close()


if __name__ == "__main__":
    main()
