"""Reuse a working local ComfyUI API workflow; never install or select models."""
import copy
import errno
import hashlib
import http.client
import json
import math
import os
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from . import media
from .files import replace


H3_NODES = {"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"}
VIDEO_NODES = {"SaveVideo", "VHS_VideoCombine"}


class ComfyHTTPError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"ComfyUI HTTP {code}: {message}")


class ComfyConnectionError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scalar_target(graph, target):
    """Follow primitive widgets without replacing conditioning/model links."""
    seen = set()
    while True:
        if not isinstance(target, list) or len(target) != 2 or not all(isinstance(x, str) for x in target):
            raise ValueError("bindingsの指定は[ノードID文字列, 入力名]です")
        node, key = target
        if node not in graph or key not in graph[node]["inputs"] or (node, key) in seen:
            raise ValueError("bindingsに存在しない入力または循環があります")
        seen.add((node, key))
        value = graph[node]["inputs"][key]
        if not isinstance(value, list):
            return target
        if len(value) != 2 or str(value[0]) not in graph or value[1] != 0:
            raise ValueError("bindingsは値を持つ入力へ指定してください")
        upstream = graph[str(value[0])]
        keys = list(upstream["inputs"])
        if not upstream["class_type"].startswith("Primitive") or len(keys) != 1:
            raise ValueError("連結された入力を変更できません。bindingsで元の値の入力を指定してください")
        target = [str(value[0]), keys[0]]


def normalize(value):
    """Validate API format and infer only known H3/video node controls."""
    if not isinstance(value, dict):
        raise ValueError("ComfyUIのAPI形式JSONが必要です")
    graph = copy.deepcopy(value.get("workflow", value))
    if not graph or len(graph) > 500 or not all(isinstance(k, str) and isinstance(v, dict) and
            isinstance(v.get("class_type"), str) and isinstance(v.get("inputs"), dict) for k, v in graph.items()):
        raise ValueError("ComfyUIの「Export (API)」で保存したJSONを指定してください。通常の画面用JSONは使えません")
    # UI metadata is not needed for execution and may contain unrelated notes.
    graph = {k: {"class_type": v["class_type"], "inputs": v["inputs"]} for k, v in graph.items()}
    outputs = [k for k, v in graph.items() if v["class_type"] in VIDEO_NODES]
    output = value.get("output_node") if "workflow" in value else None
    if output is None and len(outputs) == 1:
        output = outputs[0]
    if not isinstance(output, str) or output not in graph:
        raise ValueError("保存動画ノードを一つにするか、output_nodeで対象ノードIDを指定してください")
    # ComfyUI executes every output node in the submitted graph. Submit only
    # the selected video's ancestors, so unrelated outputs cannot generate.
    def dependencies(value):
        if isinstance(value, list):
            if len(value) == 2 and isinstance(value[0], (str, int)) and str(value[0]) in graph and isinstance(value[1], int):
                yield str(value[0])
            else:
                for item in value:
                    yield from dependencies(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from dependencies(item)
    needed, pending = set(), [output]
    while pending:
        ident = pending.pop()
        if ident not in needed:
            needed.add(ident)
            pending.extend(dependencies(graph[ident]["inputs"]))
    graph = {ident: node for ident, node in graph.items() if ident in needed}
    supplied = value.get("bindings") if "workflow" in value else None
    if supplied is None:
        bindings = {"prompt": [], "frames": [], "seed": [], "fps": [], "output_prefix": [], "references": []}
        for ident, node in graph.items():
            kind, inputs = node["class_type"], node["inputs"]
            if kind in H3_NODES:
                bindings["prompt"].append([ident, "prompt"])
                bindings["frames"].append([ident, "length"])
            if kind == "EmptyMiniMaxH3LatentAV":
                bindings["frames"].append([ident, "length"])
            if kind in {"RandomNoise", "KSampler", "KSamplerAdvanced"}:
                key = "noise_seed" if "noise_seed" in inputs else "seed"
                bindings["seed"].append([ident, key])
            if kind == "CreateVideo":
                bindings["fps"].append([ident, "fps"])
            if ident == output:
                if "filename_prefix" in inputs:
                    bindings["output_prefix"].append([ident, "filename_prefix"])
                if kind == "VHS_VideoCombine":
                    bindings["fps"].append([ident, "frame_rate"])
            # Only direct LoadImage inputs can be mapped unambiguously. Other
            # reference preprocessing/custom nodes use explicit bindings.
            if kind == "MiniMaxH3ReferenceToVideo":
                image_inputs = [(int(match[1]), link) for key, link in inputs.items()
                                if (match := re.fullmatch(r"(?:ref_images\.)?ref_image_(\d+)", key))]
                # Autogrow expands numbered slots in schema order, regardless
                # of the ordering of keys in the exported JSON.
                for _, link in sorted(image_inputs, key=lambda item: item[0]):
                    if not isinstance(link, list) or len(link) != 2:
                        raise ValueError("参照画像のbindingsを指定してください")
                    loader = graph.get(str(link[0]), {})
                    if loader.get("class_type") != "LoadImage" or link[1] != 0:
                        raise ValueError("参照画像のbindingsを指定してください")
                    bindings["references"].append([str(link[0]), "image"])
    elif isinstance(supplied, dict):
        bindings = copy.deepcopy(supplied)
    else:
        raise ValueError("bindingsはJSONオブジェクトです")
    allowed = {"prompt", "frames", "seconds", "seed", "fps", "output_prefix", "references"}
    if set(bindings) - allowed:
        raise ValueError("未対応のbindingsがあります")
    normalized = {}
    used = set()
    for key in allowed:
        targets = bindings.get(key, [])
        if not isinstance(targets, list):
            raise ValueError("bindingsの各項目は入力指定の配列です")
        normalized[key] = []
        for target in targets:
            resolved = scalar_target(graph, target)
            pair = tuple(resolved)
            if pair in used:
                if resolved in normalized[key] and key != "references":
                    continue
                raise ValueError("同じ入力へ複数のbindingsを指定できません")
            used.add(pair)
            normalized[key].append(resolved)
    if not normalized["prompt"] or not (normalized["frames"] or normalized["seconds"]) or not normalized["seed"]:
        raise ValueError("プロンプト・尺・seedを自動検出できません。カスタムノードにはbindingsを指定してください")
    if len(normalized["references"]) > 9:
        raise ValueError("参照画像は9枚までです")
    return {"workflow": graph, "bindings": normalized, "output_node": output}


class Comfy:
    def __init__(self, root):
        self.root = Path(root) / "comfy"
        self.base = os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188").rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("COMFYUI_BASE_URLには認証情報を含まないhttp(s)のURLを指定してください")
        self.token = os.environ.get("COMFYUI_API_KEY")
        self.poll_seconds = float(os.environ.get("COMFYUI_POLL_SECONDS", "3"))
        self.deadline_seconds = float(os.environ.get("COMFYUI_TIMEOUT_SECONDS", "7200"))
        if not all(math.isfinite(value) and value > 0 for value in (self.poll_seconds, self.deadline_seconds)):
            raise ValueError("ComfyUIのポーリング間隔と待機時間は正の有限値で指定してください")

    def path(self, count):
        saved = self.root / (f"ref2va-{count}.json" if count else "t2va.json")
        if saved.is_file():
            return saved
        variable = "COMFYUI_REF_WORKFLOW_PATH" if count else "COMFYUI_WORKFLOW_PATH"
        configured = os.environ.get(variable)
        if configured:
            return Path(configured.replace("{count}", str(count)))
        return saved

    def load(self, count):
        path = self.path(count)
        if not path.is_file():
            raise ValueError(f"ComfyUIの{'参照画像'+str(count)+'枚用' if count else 'テキスト生成用'}ワークフローを「生成環境の設定」から読み込んでください")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("ComfyUIワークフローは2MBまでです")
        normalized = normalize(json.loads(path.read_text(encoding="utf-8-sig")))
        if len(normalized["bindings"]["references"]) != count:
            raise ValueError("ComfyUIワークフローの参照画像数とカットの参照画像数が一致しません")
        return normalized

    def install(self, value):
        normalized = normalize(value)
        count = len(normalized["bindings"]["references"])
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / (f"ref2va-{count}.json" if count else "t2va.json")
        partial = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        try:
            previous = self.path(count)
            if previous.is_file() and previous.stat().st_size <= 2 * 1024 * 1024:
                history = self.root / "history"
                history.mkdir(exist_ok=True)
                archive = history / f"{count}-{uuid4().hex}.json"
                staging = archive.with_suffix(".tmp")
                try:
                    staging.write_bytes(previous.read_bytes())
                    replace(staging, archive)
                finally:
                    staging.unlink(missing_ok=True)
            partial.write_text(json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
            replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return {"reference_count": count, "configuration": self.configuration()}

    def restore(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9]-[a-f0-9]{32}", ident):
            raise ValueError("ワークフロー履歴のIDが不正です")
        file = self.root / "history" / (ident + ".json")
        if not file.is_file():
            raise ValueError("ワークフロー履歴がありません")
        return self.install(json.loads(file.read_text(encoding="utf-8-sig")))

    def configuration(self):
        configured = []
        errors = []
        for count in range(10):
            if self.path(count).is_file():
                try:
                    if self.path(count).stat().st_size > 2 * 1024 * 1024:
                        raise ValueError("ComfyUIワークフローは2MBまでです")
                    value = normalize(json.loads(self.path(count).read_text(encoding="utf-8-sig")))
                    if len(value["bindings"]["references"]) == count:
                        configured.append(count)
                except (ValueError, OSError) as exc:
                    errors.append(str(exc))
        history = self.root / "history"
        files = sorted(history.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20] if history.is_dir() else []
        versions = [{"id": p.stem, "reference_count": int(p.stem[0])} for p in files if re.fullmatch(r"[0-9]-[a-f0-9]{32}", p.stem)]
        return {"base_url": self.base, "workflow_counts": configured, "errors": list(dict.fromkeys(errors)), "history": versions}

    def open(self, url, payload=None, *, body=None, content_type="application/json", timeout=45):
        headers = {"Content-Type": content_type}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if body is None and payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read(65536)
            messages = []
            try:
                data = json.loads(raw)
                error = data.get("error", {})
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    messages.append(error["message"][:400])
                for node, detail in data.get("node_errors", {}).items():
                    for error in detail.get("errors", [])[:3]:
                        if isinstance(error, dict) and isinstance(error.get("message"), str):
                            messages.append(f"node {str(node)[:50]}: {error['message'][:300]}")
                    if len(messages) >= 5:
                        break
            except (ValueError, AttributeError, TypeError):
                pass
            raise ComfyHTTPError(exc.code, " / ".join(messages)[:1500] or "接続先とワークフローを確認してください") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ComfyConnectionError("ComfyUIに接続できません。サーバーの起動とURLを確認してください") from exc

    def json(self, url, payload=None, *, body=None, content_type="application/json"):
        try:
            with self.open(url, payload, body=body, content_type=content_type) as response:
                result = json.load(response)
        except (OSError, http.client.HTTPException) as exc:
            raise ComfyConnectionError("ComfyUIの応答を読み取れませんでした") from exc
        except ValueError as exc:
            raise RuntimeError("ComfyUIから有効なJSONが返りませんでした") from exc
        if not isinstance(result, dict):
            raise RuntimeError("ComfyUIの応答はJSONオブジェクトである必要があります")
        return result

    def download(self, base, item, output, stop):
        query = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": "output"})
        for attempt in range(5):
            suffix = uuid4().hex
            partial = output.with_name(f"raw.{suffix}.part")
            converted = output.with_name(f"raw.{suffix}.convert.mp4")
            try:
                if stop.is_set():
                    raise InterruptedError("Studio停止。次回起動時に同じComfyUIジョブを回収します")
                with self.open(base + "/view?" + query, timeout=180) as response, partial.open("wb") as target:
                    length = response.headers.get("Content-Length", "")
                    expected = int(length) if length.isdigit() else None
                    received = 0
                    while block := response.read(1024 * 1024):
                        if stop.is_set():
                            raise InterruptedError("Studio停止。次回起動時に同じComfyUIジョブを回収します")
                        received += len(block)
                        target.write(block)
                    if received == 0 or (expected is not None and received != expected):
                        raise ComfyConnectionError("ComfyUIの動画ダウンロードが途中で終了しました")
                media.browser_video(partial, converted, stop)
                replace(converted, output)
                return
            except InterruptedError:
                raise
            except (ComfyConnectionError, ComfyHTTPError, OSError, http.client.HTTPException) as exc:
                if isinstance(exc, ComfyHTTPError) and exc.code < 500:
                    raise
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise RuntimeError("動画の保存先に空き容量がありません") from exc
                if attempt == 4:
                    raise RuntimeError("ComfyUIの動画回収が繰り返し失敗しました。保存先を確認し「同じジョブを回収」で再確認してください") from exc
                stop.wait(min(self.poll_seconds * 2 ** (attempt + 1), 30))
            finally:
                for file in (partial, converted):
                    try:
                        file.unlink(missing_ok=True)
                    except OSError:
                        logging.warning("Studio ComfyUI temporary file cleanup deferred: %s", file.name)

    def check(self):
        nodes = self.json(self.base + "/object_info")
        if not isinstance(nodes, dict):
            raise ValueError("ComfyUIのノード情報が不正です")
        config = self.configuration()
        missing = {}
        for count in config["workflow_counts"]:
            required = {v["class_type"] for v in self.load(count)["workflow"].values()}
            absent = sorted(required - nodes.keys())
            if absent:
                missing[str(count)] = absent
        return {"connected": True, "missing_nodes": missing, "configuration": config}

    def prepare(self, spec, take_id):
        template = self.load(len(spec["references"]))
        result = {**template, "endpoint": self.base, "template_sha256": digest(template),
                  "reference_ids": list(spec["references"])}
        # H3 outputs 17k+5 frames at 24fps. Round up so ffmpeg can trim each cut
        # to its exact requested duration. Keep resolution/steps/model intact.
        frames = round(spec["seconds"] * 24)
        frames += (5 - frames) % 17
        values = {"prompt": spec["prompt"], "frames": frames, "seconds": spec["seconds"],
                  "seed": spec["seed"], "fps": 24, "output_prefix": f"minimax_studio/{take_id}"}
        for key, value in values.items():
            for node, field in result["bindings"][key]:
                result["workflow"][node]["inputs"][field] = value
        for target, ident in zip(result["bindings"]["references"], result["reference_ids"], strict=True):
            node, field = target
            result["workflow"][node]["inputs"][field] = "studio_asset:" + ident
        result["prepared_workflow_sha256"] = digest(result["workflow"])
        return result

    def upload(self, base, asset):
        content = asset["path"].read_bytes()
        if hashlib.sha256(content).hexdigest() != asset["sha256"]:
            raise ValueError("参照画像のハッシュが保存時の記録と一致しません")
        boundary = "studio" + uuid4().hex
        filename = uuid4().hex + asset["path"].suffix.lower()
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                f'Content-Type: {asset["mime"]}\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
        try:
            uploaded = self.json(base + "/upload/image", body=body, content_type=f"multipart/form-data; boundary={boundary}")
        except ComfyConnectionError as exc:
            raise RuntimeError("ComfyUIへの参照画像アップロードに失敗しました") from exc
        name = uploaded.get("name")
        subfolder = uploaded.get("subfolder", "")
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or not isinstance(subfolder, str):
            raise ValueError("ComfyUIのアップロード結果が不正です")
        return subfolder + "/" + name if subfolder else name

    def submit(self, spec, assets):
        graph = copy.deepcopy(spec["workflow"])
        for target, ident in zip(spec["bindings"]["references"], spec["reference_ids"], strict=True):
            node, field = target
            graph[node]["inputs"][field] = self.upload(spec["endpoint"], assets[ident])
        try:
            result = self.json(spec["endpoint"] + "/prompt", {"prompt": graph, "client_id": uuid4().hex})
        except ComfyHTTPError as exc:
            if exc.code < 500:
                raise RuntimeError(f"ワークフローが拒否されました（受付されていません）。{exc}") from exc
            raise RuntimeError(f"{exc}。受付結果が不明です。ComfyUIのキューを確認してください") from exc
        except Exception as exc:
            raise RuntimeError(f"{exc}。受付結果が不明な場合があります。ComfyUIのキューを確認してから再投入してください") from exc
        ident = result.get("prompt_id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", ident):
            raise RuntimeError("ComfyUIから有効なprompt_idが返りませんでした。キューとワークフローを確認してください")
        return ident

    def collect(self, spec, external_id, output, progress, stop):
        base = spec["endpoint"]
        deadline = time.monotonic() + self.deadline_seconds
        ident = urllib.parse.quote(external_id, safe="")
        missing_since = None
        errors = 0
        while time.monotonic() < deadline:
            if stop.is_set():
                raise InterruptedError("Studio停止。次回起動時に同じComfyUIジョブを回収します")
            try:
                entry = self.json(base + "/history/" + ident).get(external_id)
                queue = self.json(base + "/queue") if not entry else None
                errors = 0
            except (ComfyConnectionError, ComfyHTTPError) as exc:
                if isinstance(exc, ComfyHTTPError) and exc.code < 500:
                    raise
                errors += 1
                missing_since = None
                if errors >= 5:
                    raise RuntimeError("ComfyUIとの通信が繰り返し失敗しました。「同じジョブを回収」で再確認できます") from exc
                progress(0)
                stop.wait(min(self.poll_seconds * 2 ** errors, 30))
                continue
            if entry:
                if not isinstance(entry, dict) or not isinstance(entry.get("status", {}), dict) or not isinstance(entry.get("outputs", {}), dict):
                    raise RuntimeError("ComfyUIの実行履歴の形式が不正です")
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError("ComfyUI生成が失敗しました。ComfyUIの実行履歴を確認してください")
                saved = entry.get("outputs", {}).get(spec["output_node"], {})
                if not isinstance(saved, dict) or any(not isinstance(saved.get(key, []), list) for key in ("videos", "images", "gifs")):
                    raise RuntimeError("ComfyUIの動画出力の形式が不正です")
                videos = [item for key in ("videos", "images", "gifs") for item in saved.get(key, [])
                          if isinstance(item, dict) and isinstance(item.get("filename"), str) and
                          Path(item["filename"]).suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"} and item.get("type") == "output"]
                if status.get("completed") or status.get("status_str") == "success":
                    if len(videos) != 1:
                        raise RuntimeError("ComfyUIの対象保存ノードから動画を一つ取得できません。バッチ数とoutput_nodeを確認してください")
                    self.download(base, videos[0], output, stop)
                    return
            else:
                if any(not isinstance(queue.get(key, []), list) for key in ("queue_running", "queue_pending")):
                    raise RuntimeError("ComfyUIのキュー情報の形式が不正です")
                present = any(isinstance(row, list) and len(row) > 1 and row[1] == external_id
                              for key in ("queue_running", "queue_pending") for row in queue.get(key, []))
                if present:
                    missing_since = None
                elif missing_since is None:
                    missing_since = time.monotonic()
                elif time.monotonic() - missing_since >= 30:
                    raise RuntimeError("ComfyUIのキューと履歴にジョブがありません。履歴が消えていないか確認してください")
            progress(0)
            stop.wait(self.poll_seconds)
        raise TimeoutError("ComfyUIの待機時間を超過しました。「同じジョブを回収」で確認できます")
