"""SGLang asynchronous video contract and optional compatible text planner."""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


def request(url, payload=None, token=None, timeout=45):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"生成先のHTTPエラー: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("生成先に接続できません。サーバーの起動とURLを確認してください") from exc


class H3:
    def __init__(self):
        self.base = os.environ.get("H3_BASE_URL", "http://127.0.0.1:30010").rstrip("/")
        self.ref_base = os.environ.get("H3_REF_BASE_URL", "http://127.0.0.1:30011").rstrip("/")
        self.token = os.environ.get("H3_API_KEY")
        self.poll_seconds = float(os.environ.get("H3_POLL_SECONDS", "3"))
        self.deadline_seconds = float(os.environ.get("H3_TIMEOUT_SECONDS", "3600"))

    def endpoint(self, task):
        return self.ref_base if task == "ref2va" else self.base

    def submit(self, spec, assets):
        payload = {k: v for k, v in spec.items() if k != "references"}
        payload["conditions"] = []
        for ident in spec["references"]:
            asset = assets[ident]
            content = asset["path"].read_bytes()
            if asset.get("sha256") and hashlib.sha256(content).hexdigest() != asset["sha256"]:
                raise ValueError("参照画像のハッシュが保存時の記録と一致しません")
            encoded = base64.b64encode(content).decode("ascii")
            payload["conditions"].append({"type": "image", "role": "reference", "uri": f"data:{asset['mime']};base64,{encoded}"})
        try:
            with request(spec.get("endpoint", self.endpoint(spec["task"])) + "/v1/videos", {k:v for k,v in payload.items() if k != "endpoint"}, self.token) as response:
                data = json.load(response)
        except Exception as exc:
            raise RuntimeError(f"{exc}。受付結果が不明な場合があります。H3サーバーのジョブ一覧を確認してから再投入してください") from exc
        ident = data.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", ident):
            raise RuntimeError("H3サーバーから有効なジョブIDが返りませんでした。再投入前にサーバーを確認してください")
        return ident

    def collect(self, spec, external_id, output, progress, stop):
        base = spec.get("endpoint", self.endpoint(spec["task"])) + "/v1/videos/" + urllib.parse.quote(external_id, safe="")
        deadline = time.monotonic() + self.deadline_seconds
        while time.monotonic() < deadline:
            if stop.is_set():
                raise InterruptedError("Studio停止。次回起動時に同じジョブIDから回収します")
            with request(base, token=self.token) as response:
                data = json.load(response)
            state = data.get("status")
            if state == "completed":
                partial = output.with_suffix(".part")
                with request(base + "/content", token=self.token, timeout=180) as response, partial.open("wb") as target:
                    while block := response.read(1024 * 1024):
                        if stop.is_set():
                            raise InterruptedError("Studio停止。次回起動時に同じジョブIDから回収します")
                        target.write(block)
                partial.replace(output)
                return
            if state in {"failed", "cancelled", "canceled"}:
                raise RuntimeError("H3生成が失敗しました。生成サーバーのジョブ情報を確認してください")
            if state not in {"queued", "pending", "in_progress", "processing", "running"}:
                raise RuntimeError(f"H3の未対応ステータス: {str(state)[:80]}")
            progress(data.get("progress", 0))
            stop.wait(self.poll_seconds)
        raise TimeoutError("H3の待機時間を超過しました。「同じジョブを回収」で生成済み結果を確認できます")


class Planner:
    @property
    def available(self):
        return bool(os.environ.get("STUDIO_LLM_BASE_URL") and os.environ.get("STUDIO_LLM_MODEL"))

    def json(self, instruction, data):
        if not self.available:
            raise ValueError("AIコマ割りにはSTUDIO_LLM_BASE_URLとSTUDIO_LLM_MODELの設定が必要です")
        payload = {"model": os.environ["STUDIO_LLM_MODEL"], "messages": [
            {"role": "system", "content": instruction + " Return a JSON object only; no Markdown."},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}], "temperature": 0.6}
        base = os.environ["STUDIO_LLM_BASE_URL"].rstrip("/")
        with request(base + "/chat/completions", payload, os.environ.get("STUDIO_LLM_API_KEY"), timeout=180) as response:
            result = json.load(response)
        content = result["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("AIの回答がJSONオブジェクトではありません")
        return result

    def storyboard(self, doc):
        return self.json(
            "You are a storyboard writer for a 30-second character short. Rewrite the supplied document into concrete filmable cuts with a clear opening, development and ending. "
            "Keep the exact cut IDs, count, seconds, aspect_ratio, references, asset IDs and audio settings. "
            "Write title, cut title and description_ja in Japanese; subject, style, world, soundscape, visual, action, camera, end_state and sound in English. "
            "Preserve original dialogue verbatim. Each cut is a single continuous shot. Describe composition, appearance, location, visible action, camera motion, lighting and ending state. "
            "Maintain character identity, props and ending-to-start continuity. No music in generated cuts; music is added to the final master. Do not invent image labels. Return the complete document.", doc)

    def revise(self, doc, cut, instruction):
        return self.json(
            "Rewrite ONLY the supplied cut to satisfy the user's correction. Return {\"cut\": {...}}. Keep id, seconds, references and mute unchanged. "
            "Write title and description_ja in Japanese, visual/action/camera/end_state/sound in English, dialogue in its original language. "
            "Describe the desired result directly rather than instructions relative to an earlier take. Preserve shared identity, continuity and all unrelated details.",
            {"document": doc, "cut": cut, "instruction": instruction})["cut"]
