"""Loopback-only HTTP UI, JSON API and seekable local media."""
import argparse
import base64
import json
import os
import re
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .domain import new_id, validate_document
from .comfy import Comfy
from .providers import Planner
from .service import Conflict, NotFound, Store, Worker, encode
from . import media

STATIC = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".mp4": "video/mp4", ".jpg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp", ".mp3": "audio/mpeg",
                 ".wav": "audio/wav", ".json": "application/json; charset=utf-8"}


class Forbidden(Exception):
    pass


def load_env(path):
    if not path.is_file():
        return
    content = path.read_bytes()
    try:
        text = content.decode("utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("環境設定ファイルをUTF-8で保存してください") from exc
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            os.environ.setdefault(key, value.strip().strip("\"'"))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

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
            raise Forbidden("localhostからアクセスしてください")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            raise Forbidden("別のサイトからの操作は受け付けません")
        if write and self.headers.get("X-Studio-Request") != "1":
            raise Forbidden("Studioの画面から操作してください")

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
                    raise NotFound("素材がありません")
                target = (store.root / relative).resolve()
                if not target.is_relative_to(store.root):
                    raise Forbidden("不正な素材パスです")
                return self.file(target, head=head)
            if not write and path == "/api/config":
                try:
                    comfy = Comfy(store.root).configuration()
                except (ValueError, OSError):
                    comfy = {"base_url": "", "workflow_counts": [], "errors": ["ComfyUIの接続URL・ポーリング間隔・待機時間の設定を確認してください"]}
                default = os.environ.get("STUDIO_DEFAULT_PROVIDER") or ("comfy" if 0 in comfy["workflow_counts"] else "h3")
                return self.json_response({"llm_available": Planner().available, "default_provider": default if default in {"comfy", "h3", "demo"} else "h3", "duration": 30,
                                           "comfy": comfy,
                                           "h3_base_url": os.environ.get("H3_BASE_URL", "http://127.0.0.1:30010"),
                                           "h3_ref_base_url": os.environ.get("H3_REF_BASE_URL", "http://127.0.0.1:30011")})
            if not write and path == "/api/comfy/check":
                try:
                    return self.json_response(Comfy(store.root).check())
                except RuntimeError as exc:
                    return self.json_response({"error": str(exc)}, 503)
                except OSError:
                    return self.json_response({"error": "ComfyUIに接続できません。起動と接続先を確認してください"}, 503)
            if write and path == "/api/comfy/workflow":
                value = self.body().get("workflow")
                if len(encode(value).encode()) > 2 * 1024 * 1024:
                    raise ValueError("ComfyUIワークフローは2MBまでです")
                return self.json_response(Comfy(store.root).install(value))
            if write and path == "/api/comfy/restore":
                return self.json_response(Comfy(store.root).restore(self.body().get("history_id")))
            if path == "/api/projects":
                if not write:
                    return self.json_response(store.library())
                data = self.body()
                return self.json_response(store.create(data.get("title", ""), data.get("brief", ""), data.get("layout", "5x6"), data.get("aspect_ratio", "9:16")), 201)
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(?:/([a-z-]+))?", path)
            if not match:
                raise NotFound("ページがありません")
            ident, action = match.groups()
            if not write:
                if action == "export":
                    project = store.detail(ident)
                    # Export every input, take and master provenance; no config secrets.
                    return self.json_response({"format": "minimax-studio-v1", "project": project})
                if action is None:
                    return self.json_response(store.detail(ident))
                raise NotFound("ページがありません")
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
                raise NotFound("操作がありません")
            self.json_response(result)
        except Conflict as exc:
            self.json_response({"error": str(exc)}, 409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)[:2000]}, 400)
        except NotFound as exc:
            self.json_response({"error": str(exc.args[0])}, 404)
        except Forbidden as exc:
            self.json_response({"error": str(exc)}, 403)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"Studio error: {type(exc).__name__}", flush=True)
            self.json_response({"error": "処理に失敗しました。入力とサーバーの状態を確認してください"}, 500)

    def file(self, path, head=False):
        if not path.is_file():
            raise NotFound("ファイルがありません")
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
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
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
    try:
        load_env(Path(args.env_file))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    store = Store(args.data_dir)
    server = Server(("127.0.0.1", args.port), store)
    worker = Worker(store)
    worker.start()
    shutdown_requested = False
    def shutdown(*_):
        nonlocal shutdown_requested
        if shutdown_requested:
            media.kill_children()
            try:
                print("Studioを強制終了します。次回起動時にジョブ記録から回収します。受付IDが未保存の場合は生成先のキューを確認してください。", flush=True)
            finally:
                os._exit(130)
        shutdown_requested = True
        worker.stop.set()
        print("Studioを停止中です。受付IDの保存を待ちます。強制終了はもう一度Ctrl+C / Ctrl+Break。", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, shutdown)
    print(f"MiniMax Local Studio: http://127.0.0.1:{args.port}", flush=True)
    print(f"保存先: {store.root}\nAIコマ割り: {'利用可能' if Planner().available else '未設定（テンプレート・手動編集は利用可能）'}", flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        worker.close()
        media.kill_children()
        server.server_close()


if __name__ == "__main__":
    main()
