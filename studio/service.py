"""Durable local library and single-worker queue.

No generation is submitted on edit, selection, approval or assembly. Each take
owns an immutable input snapshot. Failed remote jobs may be collected without
submitting another request, and completed masters always remain accessible.
"""
import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import media
from .domain import FIELDS, compile_cut, fingerprint, new_id, template, validate_document
from .providers import H3, Planner


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "assets").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    approved INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                    mime TEXT NOT NULL, file TEXT NOT NULL, sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS takes (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, cut_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, provider TEXT NOT NULL, input TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT,
                    file TEXT, thumb TEXT, error TEXT, created TEXT NOT NULL, output_sha256 TEXT,
                    UNIQUE(project_id, cut_id, revision)
                );
                CREATE TABLE IF NOT EXISTS selections (
                    project_id TEXT NOT NULL, cut_id TEXT NOT NULL, take_id TEXT NOT NULL,
                    PRIMARY KEY(project_id, cut_id)
                );
                CREATE TABLE IF NOT EXISTS renders (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    file TEXT NOT NULL, cover TEXT NOT NULL, manifest TEXT NOT NULL,
                    created TEXT NOT NULL, UNIQUE(project_id, revision)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
                CREATE INDEX IF NOT EXISTS takes_project ON takes(project_id, cut_id);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(takes)")}
            if "output_sha256" not in columns:
                db.execute("ALTER TABLE takes ADD COLUMN output_sha256 TEXT")
            # Migrate hashes from numeric seconds to canonical frame counts.
            # Use each original request, preserving what was actually generated.
            for row in db.execute("SELECT id,input,fingerprint FROM takes").fetchall():
                digest = fingerprint(json.loads(row["input"])["request"])
                if digest != row["fingerprint"]:
                    db.execute("UPDATE takes SET fingerprint=? WHERE id=?", (digest, row["id"]))
            for row in db.execute("SELECT id,file FROM takes WHERE status='succeeded' AND output_sha256 IS NULL").fetchall():
                file = self.root / row["file"] if row["file"] else None
                if file and file.is_file():
                    db.execute("UPDATE takes SET output_sha256=? WHERE id=?", (hashlib.sha256(file.read_bytes()).hexdigest(), row["id"]))

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.root / "studio.sqlite3", timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def project(self, db, ident):
        row = db.execute("SELECT * FROM projects WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError("プロジェクトがありません")
        result = dict(row)
        result["document"] = validate_document(json.loads(result["document"]))
        return result

    def editable(self, db, ident):
        project = self.project(db, ident)
        if project["archived"] or project["approved"]:
            raise Conflict("編集するにはアーカイブを解除し、承認済みの場合は編集へ戻してください")
        if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running')", (ident,)).fetchone():
            raise Conflict("このプロジェクトの処理中です。完了後に操作してください")
        return project

    def assets(self, db, ident):
        return {r["id"]: {**dict(r), "path": self.root / r["file"]} for r in db.execute("SELECT * FROM assets WHERE project_id=?", (ident,))}

    def validate_assets(self, db, ident, doc):
        assets = self.assets(db, ident)
        images = ([doc["character_reference"]] if doc.get("character_reference") else [])
        images += [r for c in doc["cuts"] for r in c["references"]]
        for ref in images:
            if ref not in assets or not assets[ref]["mime"].startswith("image/"):
                raise ValueError("参照画像がありません。このプロジェクトの画像を指定してください")
        if doc.get("bgm") and (doc["bgm"] not in assets or not assets[doc["bgm"]]["mime"].startswith("audio/")):
            raise ValueError("BGMがありません。このプロジェクトの音声を指定してください")
        for cut in doc["cuts"]:
            compile_cut(doc, cut["id"])

    def create(self, title, brief, layout="5x6", aspect="9:16"):
        doc = template(title, brief, layout, aspect)
        ident, date = new_id(), now()
        with self.db() as db:
            db.execute("INSERT INTO projects(id,document,created,updated) VALUES(?,?,?,?)", (ident, encode(doc), date, date))
        return self.detail(ident)

    def save(self, ident, doc, version):
        doc = validate_document(doc)
        with self.db() as db:
            project = self.editable(db, ident)
            if project["version"] != version:
                raise Conflict("別の画面で更新されています。再読み込みしてから編集してください")
            self.validate_assets(db, ident, doc)
            old_ids = {c["id"] for c in project["document"]["cuts"]}
            new_ids = {c["id"] for c in doc["cuts"]}
            for removed in old_ids - new_ids:
                db.execute("DELETE FROM selections WHERE project_id=? AND cut_id=?", (ident, removed))
            db.execute("UPDATE projects SET document=?,version=version+1,updated=? WHERE id=?", (encode(doc), now(), ident))
        return self.detail(ident)

    def upload(self, ident, name, mime, content):
        formats = {"image/png": (".png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
                   "image/jpeg": (".jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
                   "image/webp": (".webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
                   "audio/mpeg": (".mp3", lambda b: b.startswith(b"ID3") or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")),
                   "audio/wav": (".wav", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WAVE")}
        if mime not in formats or not content or len(content) > 15 * 1024 * 1024 or not formats[mime][1](content):
            raise ValueError("素材はPNG/JPEG/WebP画像またはMP3/WAV音声（15MBまで）です")
        with self.db() as db:
            self.editable(db, ident)
            asset = new_id()
            relative = f"assets/{asset}{formats[mime][0]}"
            path = self.root / relative
            path.write_bytes(content)
            try:
                if mime.startswith("audio/"):
                    if not any(s["codec_type"] == "audio" for s in media.probe(path)["streams"]):
                        raise ValueError("音声ストリームがありません")
                else:
                    metadata = media.probe(path)
                    stream = next(s for s in metadata["streams"] if s["codec_type"] == "video")
                    if stream["width"] * stream["height"] > 40_000_000:
                        raise ValueError("画像サイズが大きすぎます")
                db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?)", (asset, ident, str(name)[:200], mime, relative, hashlib.sha256(content).hexdigest()))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return {"id": asset, "name": name, "mime": mime, "url": "/media/" + relative}

    def selected(self, db, ident):
        return {r["cut_id"]: r["take_id"] for r in db.execute("SELECT * FROM selections WHERE project_id=?", (ident,))}

    def assembly_manifest(self, db, ident, allow_demo=True):
        project = self.project(db, ident)
        doc = project["document"]
        selected = self.selected(db, ident)
        takes, kinds = [], set()
        for cut in doc["cuts"]:
            row = db.execute("SELECT * FROM takes WHERE id=? AND project_id=? AND cut_id=? AND status='succeeded'", (selected.get(cut["id"]), ident, cut["id"])).fetchone()
            if not row or not row["file"] or not (self.root / row["file"]).is_file():
                raise Conflict("すべてのカットで完了したテイクを選択してください")
            if row["fingerprint"] != compile_cut(doc, cut["id"])["fingerprint"]:
                raise Conflict("本文と不一致のカットがあります。本文を戻すか再生成してから合成してください")
            kinds.add(row["provider"])
            if not row["output_sha256"]:
                raise Conflict("素材動画のハッシュがありません。保存先を確認してください")
            if not allow_demo and hashlib.sha256((self.root / row["file"]).read_bytes()).hexdigest() != row["output_sha256"]:
                raise Conflict("素材動画のハッシュが一致しません")
            takes.append({"cut_id": cut["id"], "take_id": row["id"], "revision": row["revision"],
                          "seconds": cut["seconds"], "mute": cut["mute"], "sha256": row["output_sha256"]})
        if len(kinds) != 1:
            raise Conflict("デモとH3のテイクを混ぜて合成できません")
        demo = "demo" in kinds
        if demo and not allow_demo:
            raise Conflict("デモ映像は承認できません。H3で生成した映像を使用してください")
        return {"document": copy.deepcopy(doc), "takes": takes, "demo": demo, "frames": 720,
                "aspect_ratio": doc["aspect_ratio"], "bgm": doc.get("bgm"), "bgm_volume": doc["bgm_volume"]}

    def matches_master(self, manifest, latest):
        if not latest:
            return False
        if not (self.root / latest["file"]).is_file():
            return False
        old = json.loads(latest["manifest"]) if isinstance(latest["manifest"], str) else latest["manifest"]
        return all(old.get(k) == manifest.get(k) for k in ("takes", "aspect_ratio", "bgm", "bgm_volume", "demo"))

    def detail(self, ident):
        with self.db() as db:
            project = self.project(db, ident)
            doc = project["document"]
            selected = self.selected(db, ident)
            takes = []
            live = {c["id"]: compile_cut(doc, c["id"])["fingerprint"] for c in doc["cuts"]}
            for row in db.execute("SELECT * FROM takes WHERE project_id=? ORDER BY revision DESC", (ident,)):
                take = dict(row)
                take["input"] = json.loads(take["input"])
                take["matches"] = live.get(take["cut_id"]) == take["fingerprint"]
                take["selected"] = selected.get(take["cut_id"]) == take["id"]
                take["url"] = "/media/" + take["file"] if take["file"] else None
                take["thumb_url"] = "/media/" + take["thumb"] if take["thumb"] else None
                takes.append(take)
            renders = []
            for row in db.execute("SELECT * FROM renders WHERE project_id=? ORDER BY revision DESC", (ident,)):
                render = dict(row)
                render["manifest"] = json.loads(render["manifest"])
                render["url"], render["cover_url"] = "/media/" + render["file"], "/media/" + render["cover"]
                renders.append(render)
            project["takes"], project["renders"] = takes, renders
            project["assets"] = [{k: v for k, v in a.items() if k != "path"} | {"url": "/media/" + a["file"]} for a in self.assets(db, ident).values()]
            project["jobs"] = [dict(r) | {"payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 20", (ident,))]
            project["prompts"] = {c["id"]: compile_cut(doc, c["id"])["request"]["prompt"] for c in doc["cuts"]}
            project["master_current"] = False
            try:
                manifest = self.assembly_manifest(db, ident)
                project["master_current"] = self.matches_master(manifest, renders[0] if renders else None)
            except Conflict:
                pass
            active = any(j["status"] in {"queued", "running"} for j in project["jobs"])
            count = sum(1 for t in takes if t["selected"] and t["matches"] and t["status"] == "succeeded")
            project["ready_count"] = count
            project["status"] = ("archived" if project["archived"] else "approved" if project["approved"] else "generating" if active else
                                 "review" if project["master_current"] else "assembly" if count == len(doc["cuts"]) else "draft")
            return project

    def library(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM projects ORDER BY updated DESC")]
        result = []
        for ident in ids:
            p = self.detail(ident)
            result.append({"id": ident, "title": p["document"]["title"], "brief": p["document"]["brief"],
                           "status": p["status"], "updated": p["updated"], "ready_count": p["ready_count"], "cut_count": len(p["document"]["cuts"]),
                           "cover_url": p["renders"][0]["cover_url"] if p["renders"] else None,
                           "demo": p["renders"][0]["manifest"]["demo"] if p["renders"] else False})
        return result

    def choose(self, ident, take_id):
        with self.db() as db:
            project = self.editable(db, ident)
            take = db.execute("SELECT * FROM takes WHERE id=? AND project_id=? AND status='succeeded'", (take_id, ident)).fetchone()
            if not take or take["cut_id"] not in {c["id"] for c in project["document"]["cuts"]}:
                raise ValueError("このプロジェクトの完了テイクを指定してください")
            db.execute("INSERT OR REPLACE INTO selections VALUES(?,?,?)", (ident, take["cut_id"], take_id))
            db.execute("UPDATE projects SET version=version+1,updated=? WHERE id=?", (now(), ident))
        return self.detail(ident)

    def set_state(self, ident, action):
        with self.db() as db:
            project = self.project(db, ident)
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running')", (ident,)).fetchone():
                raise Conflict("処理が完了してから状態を変更してください")
            if action == "approve":
                if project["archived"]:
                    raise Conflict("アーカイブを解除してください")
                manifest = self.assembly_manifest(db, ident, allow_demo=False)
                latest = db.execute("SELECT * FROM renders WHERE project_id=? ORDER BY revision DESC LIMIT 1", (ident,)).fetchone()
                if not self.matches_master(manifest, latest):
                    raise Conflict("現在の構成で合成してから承認してください")
                db.execute("UPDATE projects SET approved=1 WHERE id=?", (ident,))
            elif action == "reopen":
                db.execute("UPDATE projects SET approved=0 WHERE id=?", (ident,))
            elif action in {"archive", "restore"}:
                db.execute("UPDATE projects SET archived=? WHERE id=?", (int(action == "archive"), ident))
            else:
                raise ValueError("状態変更が不正です")
            db.execute("UPDATE projects SET version=version+1,updated=? WHERE id=?", (now(), ident))
        return self.detail(ident)

    def enqueue(self, ident, kind, options):
        with self.db() as db:
            project = self.editable(db, ident)
            if options.get("version") != project["version"]:
                raise Conflict("画面の内容が更新されています。再読み込みしてください")
            doc = project["document"]
            payload = {"version": project["version"]}
            if kind == "generate":
                provider = options.get("provider", "h3")
                if provider not in {"h3", "demo"}:
                    raise ValueError("生成先はh3またはdemoです")
                cut_ids = options.get("cut_ids")
                if not isinstance(cut_ids, list) or not cut_ids or len(cut_ids) != len(set(cut_ids)) or any(c not in {s["id"] for s in doc["cuts"]} for c in cut_ids):
                    raise ValueError("生成するカットが不正です")
                self.validate_assets(db, ident, doc)
                seed = options.get("seed")
                if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2147483647):
                    raise ValueError("seedは0〜2147483647です")
                take_ids = []
                for cut_id in cut_ids:
                    compiled = compile_cut(doc, cut_id)
                    if not compiled["snapshot"]["visual"] or not compiled["snapshot"]["action"]:
                        raise ValueError("各カットの映像と動きを入力して保存してください")
                    compiled["request"]["seed"] = seed if seed is not None else int.from_bytes(os.urandom(4), "big") % 2147483648
                    if provider == "h3":
                        compiled["request"]["endpoint"] = H3().endpoint(compiled["request"]["task"])
                    revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM takes WHERE project_id=? AND cut_id=?", (ident, cut_id)).fetchone()[0]
                    take_id = new_id()
                    compiled["document"] = copy.deepcopy(doc)
                    assets = self.assets(db, ident)
                    compiled["reference_assets"] = [{"id": ref, "sha256": assets[ref]["sha256"], "name": assets[ref]["name"], "mime": assets[ref]["mime"]} for ref in compiled["request"]["references"]]
                    db.execute("INSERT INTO takes(id,project_id,cut_id,revision,provider,input,fingerprint,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
                               (take_id, ident, cut_id, revision, provider, encode(compiled), compiled["fingerprint"], "queued", now()))
                    take_ids.append(take_id)
                payload.update({"take_ids": take_ids, "auto_assemble": bool(options.get("auto_assemble", True))})
            elif kind == "assemble":
                payload["manifest"] = self.assembly_manifest(db, ident)
            elif kind == "recover":
                take = db.execute("SELECT * FROM takes WHERE id=? AND project_id=?", (options.get("take_id"), ident)).fetchone()
                if not take or take["status"] != "failed" or not take["external_id"] or take["provider"] != "h3":
                    raise ValueError("回収可能なH3ジョブがありません")
                db.execute("UPDATE takes SET status='queued',error=NULL WHERE id=?", (take["id"],))
                payload.update({"take_ids": [take["id"]], "auto_assemble": False})
            elif kind in {"plan", "revise"}:
                if not Planner().available:
                    raise ValueError("AI機能にはSTUDIO_LLM_BASE_URLとSTUDIO_LLM_MODELを設定してください")
                payload["document"] = copy.deepcopy(doc)
                if kind == "revise":
                    cut = next((c for c in doc["cuts"] if c["id"] == options.get("cut_id")), None)
                    instruction = options.get("instruction", "")
                    if not cut or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 2000:
                        raise ValueError("対象カットと修正指示が必要です（2000文字まで）")
                    payload.update({"cut": cut, "instruction": instruction})
            else:
                raise ValueError("ジョブ種別が不正です")
            ident_job, date = new_id(), now()
            db.execute("INSERT INTO jobs(id,project_id,kind,payload,status,stage,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                       (ident_job, ident, kind, encode(payload), "queued", "待機中", date, date))
            db.execute("UPDATE projects SET updated=? WHERE id=?", (date, ident))
        return {"job_id": ident_job, "project": self.detail(ident)}

    def apply_proposal(self, ident, job_id, version):
        with self.db() as db:
            project = self.editable(db, ident)
            job = db.execute("SELECT * FROM jobs WHERE id=? AND project_id=? AND kind IN ('plan','revise') AND status='succeeded'", (job_id, ident)).fetchone()
            if not job:
                raise ValueError("反映できるAI提案がありません")
            payload = json.loads(job["payload"])
            if project["version"] != version or payload["version"] != version:
                raise Conflict("AI提案後に内容が更新されました。現在の本文で再提案してください")
            doc = validate_document(payload["proposal"])
            self.validate_assets(db, ident, doc)
            db.execute("UPDATE projects SET document=?,version=version+1,updated=? WHERE id=?", (encode(doc), now(), ident))
        return self.detail(ident)


class Worker:
    def __init__(self, store, h3=None, planner=None):
        self.store, self.h3, self.planner = store, h3 or H3(), planner or Planner()
        self.stop = threading.Event()
        self.thread = None
        self.lease = None

    def start(self):
        self.lease = (self.store.root / "worker.lock").open("a+")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lease.close()
            raise RuntimeError("この保存先のStudioはすでに起動しています") from exc
        self.recover_journal()
        self.cleanup_renders()
        self.thread = threading.Thread(target=self.loop, daemon=False, name="studio-worker")
        self.thread.start()

    def cleanup_renders(self):
        with self.store.db() as db:
            known = {r[0] for r in db.execute("SELECT id FROM renders")}
        directory = self.store.root / "renders"
        if directory.is_dir():
            for folder in directory.iterdir():
                if folder.is_dir() and not folder.is_symlink() and len(folder.name) == 32 and all(c in "0123456789abcdef" for c in folder.name) and folder.name not in known:
                    shutil.rmtree(folder)

    def recover_journal(self):
        with self.store.db() as db:
            # A saved external ID is resumed, never resubmitted. A crash between
            # POST and persisting its response is ambiguous and requires review.
            for row in db.execute("SELECT * FROM takes WHERE status IN ('running','submitting')").fetchall():
                if row["status"] == "submitting" and not row["external_id"]:
                    db.execute("UPDATE takes SET status='failed',error=? WHERE id=?", ("受付結果が不明です。H3サーバーのジョブ一覧を確認してから撮り直してください", row["id"]))
                else:
                    db.execute("UPDATE takes SET status='queued' WHERE id=?", (row["id"],))
            db.execute("UPDATE jobs SET status='queued',stage='再開待機',updated=? WHERE status='running'", (now(),))

    def close(self):
        self.stop.set()
        if self.thread:
            # A POST must finish and persist its ID before the process exits.
            # Media subprocesses observe stop and are terminated/reaped.
            self.thread.join()
        if self.lease:
            self.lease.close()

    def update(self, ident, **fields):
        fields["updated"] = now()
        with self.store.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), ident))

    def loop(self):
        while not self.stop.is_set():
            try:
                with self.store.db() as db:
                    row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    self.process(row["id"])
                else:
                    self.stop.wait(0.5)
            except Exception as exc:
                logging.error("Studio worker failure: %s", type(exc).__name__)
                self.stop.wait(2)
                if not self.stop.is_set():
                    try:
                        self.recover_journal()
                    except Exception as recovery_error:
                        logging.error("Studio recovery failure: %s", type(recovery_error).__name__)

    def process(self, ident):
        with self.store.db() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())
            if job["status"] not in {"queued", "running"}:
                return
        payload = json.loads(job["payload"])
        self.update(ident, status="running", stage="準備中", error=None)
        try:
            if job["kind"] in {"generate", "recover"}:
                failures = []
                for i, take_id in enumerate(payload["take_ids"]):
                    if self.stop.is_set():
                        raise InterruptedError()
                    try:
                        self.generate(ident, take_id, i, len(payload["take_ids"]))
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        error = str(exc)[:1500] or type(exc).__name__
                        with self.store.db() as db:
                            db.execute("UPDATE takes SET status='failed',error=? WHERE id=?", (error, take_id))
                        failures.append(f"カット{i+1}: {error}")
                assembly_skipped = False
                if payload["auto_assemble"]:
                    try:
                        with self.store.db() as db:
                            manifest = self.store.assembly_manifest(db, job["project_id"])
                    except Conflict:
                        manifest = None
                        assembly_skipped = True
                    if manifest:
                        self.render(ident, job["project_id"], manifest)
                if failures:
                    self.update(ident, status="failed", stage="一部のカットが失敗（完了したテイクは保持）", error="\n".join(failures)[:2000], progress=100)
                    return
                if assembly_skipped:
                    self.update(ident, status="succeeded", stage="生成完了・本文と一致するテイクを選択して合成してください", progress=100)
                    return
            elif job["kind"] == "assemble":
                self.render(ident, job["project_id"], payload["manifest"])
            else:
                self.update(ident, stage="AI提案を作成中")
                original = payload["document"]
                if job["kind"] == "plan":
                    proposal = self.planner.storyboard(original)
                    if not isinstance(proposal, dict) or not isinstance(proposal.get("cuts"), list) or len(proposal["cuts"]) != len(original["cuts"]):
                        raise ValueError("AIがカット数を変更しました。再提案してください")
                    # Structural fields are controlled here, never by the model.
                    for proposed, cut in zip(proposal["cuts"], original["cuts"]):
                        for key in ("id", "seconds", "references", "mute", "dialogue", "dialogue_language"):
                            proposed[key] = copy.deepcopy(cut[key])
                    for key in ("aspect_ratio", "character_reference", "bgm", "bgm_volume", "brief"):
                        proposal[key] = copy.deepcopy(original[key])
                else:
                    revised = self.planner.revise(original, payload["cut"], payload["instruction"])
                    for key in ("id", "seconds", "references", "mute", "dialogue_language"):
                        revised[key] = copy.deepcopy(payload["cut"][key])
                    proposal = copy.deepcopy(original)
                    proposal["cuts"] = [revised if c["id"] == revised["id"] else c for c in proposal["cuts"]]
                payload["proposal"] = validate_document(proposal)
                with self.store.db() as db:
                    self.store.validate_assets(db, job["project_id"], payload["proposal"])
                self.update(ident, payload=encode(payload))
            self.update(ident, status="succeeded", stage="完了（AI提案は内容を確認して反映）" if job["kind"] in {"plan", "revise"} else "完了", progress=100)
        except InterruptedError:
            self.update(ident, status="queued", stage="停止・次回起動で回収")
        except Exception as exc:
            error = str(exc)[:2000] or exc.__class__.__name__
            self.update(ident, status="failed", stage="失敗", error=error)
            with self.store.db() as db:
                for take_id in payload.get("take_ids", []):
                    db.execute("UPDATE takes SET status='failed',error=? WHERE id=? AND status IN ('queued','running','submitting')", (error, take_id))

    def generate(self, job_id, take_id, index, count):
        with self.store.db() as db:
            take = dict(db.execute("SELECT * FROM takes WHERE id=?", (take_id,)).fetchone())
            if take["status"] == "succeeded":
                return
            if take["status"] == "failed":
                raise RuntimeError(take["error"] or "テイクが失敗しています")
            assets = self.store.assets(db, take["project_id"])
        compiled = json.loads(take["input"])
        folder = self.store.root / "takes" / take_id
        folder.mkdir(parents=True, exist_ok=True)
        output, thumb = folder / "raw.mp4", folder / "thumb.jpg"
        def progress(value=0):
            value = value if isinstance(value, (int, float)) and math.isfinite(value) else 0
            self.update(job_id, stage=f"カット{index+1}/{count}を生成中", progress=(index + max(0, min(value, 100))/100) / count * 90)
        progress()
        if take["provider"] == "demo":
            with self.store.db() as db:
                db.execute("UPDATE takes SET status='running' WHERE id=?", (take_id,))
            cut_index = next(i for i, c in enumerate(compiled["document"]["cuts"]) if c["id"] == take["cut_id"])
            media.demo(output, compiled["snapshot"]["seconds"], cut_index, compiled["request"]["seed"], self.stop)
        else:
            external_id = take["external_id"]
            if not external_id:
                with self.store.db() as db:
                    db.execute("UPDATE takes SET status='submitting' WHERE id=?", (take_id,))
                external_id = self.h3.submit(compiled["request"], assets)
                with self.store.db() as db:
                    db.execute("UPDATE takes SET status='running',external_id=? WHERE id=?", (external_id, take_id))
            self.h3.collect(compiled["request"], external_id, output, progress, self.stop)
        metadata = media.probe(output, self.stop)
        if media.video_duration(metadata) < compiled["snapshot"]["seconds"] - 0.5:
            raise ValueError("生成結果に有効な指定尺の映像がありません")
        media.thumbnail(output, thumb, self.stop)
        with self.store.db() as db:
            db.execute("UPDATE takes SET status='succeeded',file=?,thumb=?,output_sha256=?,error=NULL WHERE id=?",
                       (str(output.relative_to(self.store.root)), str(thumb.relative_to(self.store.root)), hashlib.sha256(output.read_bytes()).hexdigest(), take_id))
            # The first completed take is selected automatically; a retake
            # never silently replaces an already selected take or master.
            db.execute("INSERT OR IGNORE INTO selections VALUES(?,?,?)", (take["project_id"], take["cut_id"], take_id))
            db.execute("UPDATE projects SET version=version+1,updated=? WHERE id=?", (now(), take["project_id"]))

    def render(self, job_id, project_id, manifest):
        self.update(job_id, stage="30秒動画を合成中", progress=92)
        with self.store.db() as db:
            current = self.store.assembly_manifest(db, project_id)
            if current != manifest:
                raise Conflict("合成の構成が変更されています。再度合成してください")
            latest = db.execute("SELECT * FROM renders WHERE project_id=? ORDER BY revision DESC LIMIT 1", (project_id,)).fetchone()
            if self.store.matches_master(manifest, latest):
                return
            revision = (latest["revision"] if latest else 0) + 1
            assets = self.store.assets(db, project_id)
            inputs = []
            for part, cut in zip(manifest["takes"], manifest["document"]["cuts"]):
                take = db.execute("SELECT * FROM takes WHERE id=?", (part["take_id"],)).fetchone()
                source = self.store.root / take["file"]
                if hashlib.sha256(source.read_bytes()).hexdigest() != part["sha256"]:
                    raise ValueError("素材動画のハッシュが一致しません")
                inputs.append((source, cut))
        ident = new_id()
        folder = self.store.root / "renders" / ident
        bgm = assets[manifest["bgm"]]["path"] if manifest.get("bgm") else None
        try:
            output, metadata = media.assemble(inputs, manifest["document"], folder, bgm, manifest["demo"], self.stop)
            manifest = {**manifest, "metadata": metadata, "created": now()}
            (folder / "manifest.json").write_text(encode(manifest), encoding="utf-8")
            with self.store.db() as db:
                db.execute("INSERT INTO renders VALUES(?,?,?,?,?,?,?)", (ident, project_id, revision, str(output.relative_to(self.store.root)),
                           str((folder / "cover.jpg").relative_to(self.store.root)), encode(manifest), now()))
                db.execute("UPDATE projects SET version=version+1,updated=? WHERE id=?", (now(), project_id))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
