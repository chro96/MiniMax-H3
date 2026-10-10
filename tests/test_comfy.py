"""Working-workflow snapshots and ComfyUI protocol, without a GPU/model."""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from studio import media
from studio.comfy import Comfy, normalize
from studio.domain import compile_cut, template
from studio.server import Server, load_env
from studio.service import Store, Worker


def workflow(references=0):
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "working-int8.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "working-encoder.safetensors"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "working-vae.safetensors"}},
        "10": {"class_type": "MiniMaxH3ReferenceToVideo" if references else "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": "original prompt", "width": 352, "height": 608, "length": 124}},
        "20": {"class_type": "RandomNoise", "inputs": {"noise_seed": 123}},
        "30": {"class_type": "SamplerCustomAdvanced", "inputs": {"model": ["1", 0], "positive": ["10", 0], "latent": ["10", 1], "noise": ["20", 0], "steps": 8}},
        "40": {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}},
        "50": {"class_type": "CreateVideo", "inputs": {"images": ["40", 0], "fps": 24}},
        "60": {"class_type": "SaveVideo", "inputs": {"video": ["50", 0], "filename_prefix": "video/test", "format": "mp4", "codec": "h264"}},
    }
    for i in range(references):
        ident = str(70 + i)
        graph[ident] = {"class_type": "LoadImage", "inputs": {"image": f"original-{i}.png"}}
        graph["10"]["inputs"][f"ref_images.ref_image_{i}"] = [ident, 0]
    return graph


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"COMFYUI_WORKFLOW_PATH": "", "COMFYUI_REF_WORKFLOW_PATH": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.comfy = Comfy(self.tmp.name)

    def test_preserves_working_model_resolution_steps_and_freezes_controls(self):
        original = workflow()
        self.comfy.install(original)
        doc = template("test", "story")
        spec = compile_cut(doc, doc["cuts"][0]["id"])["request"] | {"seed": 0}
        frozen = self.comfy.prepare(spec, "a" * 32)
        graph = frozen["workflow"]
        self.assertEqual(graph["1"], original["1"])
        self.assertEqual(graph["10"]["inputs"]["width"], 352)
        self.assertEqual(graph["10"]["inputs"]["height"], 608)
        self.assertEqual(graph["30"]["inputs"]["steps"], 8)
        self.assertEqual(graph["10"]["inputs"]["length"], 158)
        self.assertEqual(graph["10"]["inputs"]["prompt"], spec["prompt"])
        self.assertEqual(graph["20"]["inputs"]["noise_seed"], 0)
        self.assertEqual(graph["60"]["inputs"]["filename_prefix"], "minimax_studio/" + "a" * 32)
        original["10"]["inputs"]["width"] = 480
        self.comfy.install(original)
        self.assertEqual(frozen["workflow"]["10"]["inputs"]["width"], 352)
        self.assertEqual(self.comfy.prepare(spec, "b" * 32)["workflow"]["10"]["inputs"]["width"], 480)

    def test_workflow_replacement_and_restore_retain_both_versions(self):
        first = workflow()
        self.comfy.install(first)
        second = workflow()
        second["10"]["inputs"]["width"] = 480
        result = self.comfy.install(second)
        self.assertEqual(len(result["configuration"]["history"]), 1)
        previous = result["configuration"]["history"][0]["id"]
        restored = self.comfy.restore(previous)
        self.assertEqual(self.comfy.load(0)["workflow"]["10"]["inputs"]["width"], 352)
        self.assertEqual(len(restored["configuration"]["history"]), 2)
        with self.assertRaises(ValueError):
            self.comfy.restore("../t2va")

    def test_windows_env_encodings_preserve_first_key(self):
        file = Path(self.tmp.name) / ".env.studio"
        for encoding in ("utf-8", "utf-8-sig", "utf-16"):
            file.write_bytes("COMFYUI_BASE_URL=http://127.0.0.1:8189\n".encode(encoding))
            with patch.dict(os.environ, {}, clear=True):
                load_env(file)
                self.assertEqual(os.environ["COMFYUI_BASE_URL"], "http://127.0.0.1:8189")
        file.write_bytes(b"\x81invalid")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            load_env(file)

    def test_remote_id_journal_survives_database_failure_and_checks_input_hash(self):
        self.comfy.install(workflow())
        store = Store(self.tmp.name)
        p = store.create("test", "story")
        for cut in p["document"]["cuts"]:
            cut.update(visual="A robot.", action="Walking.")
        p = store.save(p["id"], p["document"], p["version"])
        queued = store.enqueue(p["id"], "generate", {"version": p["version"], "provider": "comfy", "cut_ids": [p["document"]["cuts"][0]["id"]], "auto_assemble": False})
        with store.db() as db:
            take = dict(db.execute("SELECT * FROM takes").fetchone())
            db.execute("UPDATE takes SET status='submitting'")
        folder = store.root / "takes" / take["id"]
        folder.mkdir(parents=True)
        worker = Worker(store)
        with patch.object(store, "db", side_effect=sqlite3.OperationalError("write failed")), self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "saved-remote-id"):
            worker.persist_external_id(take, "saved-remote-id")
        worker.recover_journal()
        recovered = store.detail(p["id"])["takes"][0]
        self.assertEqual(recovered["external_id"], "saved-remote-id")
        self.assertEqual(recovered["status"], "queued")
        file = folder / "remote-job.json"
        value = json.loads(file.read_text())
        value["input_sha256"] = "other-input"
        file.write_text(json.dumps(value))
        with store.db() as db:
            db.execute("UPDATE takes SET status='submitting',external_id=NULL")
        worker.recover_journal()
        ambiguous = store.detail(p["id"])["takes"][0]
        self.assertEqual(ambiguous["status"], "failed")
        self.assertIsNone(ambiguous["external_id"])

    def test_comfy_batch_assembles_30_seconds_and_partial_retake_keeps_masters(self):
        self.comfy.install(workflow())
        source = Path(self.tmp.name) / "simulated-h3.mp4"
        media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:r=24", "-frames:v", "158", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        content = source.read_bytes()
        class SimulatedComfy:
            submissions = 0
            def submit(self, spec, assets):
                self.submissions += 1
                return f"simulated-{self.submissions}"
            def collect(self, spec, external_id, output, progress, stop):
                output.write_bytes(content)
        store = Store(self.tmp.name)
        p = store.create("test", "story")
        for cut in p["document"]["cuts"]:
            cut.update(visual="A robot.", action="Walking.")
        p = store.save(p["id"], p["document"], p["version"])
        provider = SimulatedComfy()
        worker = Worker(store, comfy=provider)
        # Exercise the production assembly/approval path with smaller CPU
        # canvases. Protocol/media compatibility are tested separately above.
        with patch.dict(media.SIZES, {"9:16": (144, 256)}):
            job = store.enqueue(p["id"], "generate", {"version": p["version"], "provider": "comfy", "cut_ids": [c["id"] for c in p["document"]["cuts"]]})
            worker.process(job["job_id"])
            p = store.detail(p["id"])
            self.assertTrue(p["master_current"], p["jobs"][0]["error"])
            self.assertFalse(p["renders"][0]["manifest"]["demo"])
            old_master = store.root / p["renders"][0]["file"]
            original = hashlib.sha256(old_master.read_bytes()).hexdigest()
            self.assertEqual(int(media.probe(old_master)["streams"][0]["nb_frames"]), 720)
            edited = copy.deepcopy(p["document"])
            edited["cuts"][2]["action"] = "Waving a hand."
            p = store.save(p["id"], edited, p["version"])
            job = store.enqueue(p["id"], "generate", {"version": p["version"], "provider": "comfy", "cut_ids": [edited["cuts"][2]["id"]]})
            worker.process(job["job_id"])
            p = store.detail(p["id"])
            new_take = next(t for t in p["takes"] if t["revision"] == 2)
            self.assertFalse(new_take["selected"])
            self.assertEqual(len(p["renders"]), 1)
            p = store.choose(p["id"], new_take["id"])
            job = store.enqueue(p["id"], "assemble", {"version": p["version"]})
            worker.process(job["job_id"])
            p = store.detail(p["id"])
            self.assertTrue(p["master_current"])
            self.assertEqual(len(p["renders"]), 2)
            self.assertEqual(provider.submissions, 6)
            self.assertEqual(hashlib.sha256(old_master.read_bytes()).hexdigest(), original)
            self.assertTrue(store.set_state(p["id"], "approve")["approved"])

    def test_custom_bindings_and_primitives_without_model_link_replacement(self):
        graph = {"a": {"class_type": "CustomH3GGUF", "inputs": {"text": "old", "duration": 5, "random_seed": 123}},
                 "b": {"class_type": "CustomSaveVideo", "inputs": {"video": ["a", 0]}}}
        value = {"workflow": graph, "output_node": "b", "bindings": {
            "prompt": [["a", "text"]], "seconds": [["a", "duration"]], "seed": [["a", "random_seed"]]}}
        self.comfy.install(value)
        spec = {"references": [], "seconds": 6, "prompt": "new prompt", "seed": 0}
        prepared = self.comfy.prepare(spec, "a" * 32)
        self.assertEqual(prepared["workflow"]["a"]["inputs"], {"text": "new prompt", "duration": 6, "random_seed": 0})
        standard = workflow()
        standard["21"] = {"class_type": "PrimitiveInt", "inputs": {"value": 100}}
        standard["20"]["inputs"]["noise_seed"] = ["21", 0]
        normalized = normalize(standard)
        self.assertEqual(normalized["bindings"]["seed"], [["21", "value"]])
        standard["21"]["class_type"] = "MathExpression"
        with self.assertRaisesRegex(ValueError, "元の値"):
            normalize(standard)

    def test_selected_output_prunes_unrelated_generation_and_rejects_ambiguity(self):
        graph = workflow()
        graph["90"] = {"class_type": "OtherExpensiveModel", "inputs": {"prompt": "unrelated"}}
        graph["91"] = {"class_type": "SaveVideo", "inputs": {"video": ["90", 0]}}
        with self.assertRaisesRegex(ValueError, "output_node"):
            normalize(graph)
        normalized = normalize({"workflow": graph, "output_node": "60"})
        self.assertNotIn("90", normalized["workflow"])
        self.assertNotIn("91", normalized["workflow"])
        for value in ({"nodes": [], "links": []}, {"workflow": workflow(), "bindings": {}}, {"workflow": workflow(), "bindings": {"prompt": "bad"}}):
            with self.assertRaises(ValueError):
                normalize(value)

    def test_reference_counts_and_order_are_explicit(self):
        graph = workflow(2)
        graph["10"]["inputs"] = dict(reversed(list(graph["10"]["inputs"].items())))
        self.comfy.install(graph)
        self.assertEqual(self.comfy.configuration()["workflow_counts"], [2])
        spec = {"references": ["first", "second"], "seconds": 5, "prompt": "ref prompt", "seed": 3}
        frozen = self.comfy.prepare(spec, "a" * 32)
        self.assertEqual(frozen["workflow"]["70"]["inputs"]["image"], "studio_asset:first")
        self.assertEqual(frozen["workflow"]["71"]["inputs"]["image"], "studio_asset:second")
        self.assertEqual(frozen["workflow"]["10"]["inputs"]["length"], 124)
        with self.assertRaisesRegex(ValueError, "読み込んで"):
            self.comfy.prepare(spec | {"references": ["first"]}, "a" * 32)

    def test_restart_collects_same_id_and_keeps_queued_snapshot(self):
        self.comfy.install(workflow())
        store = Store(self.tmp.name)
        project = store.create("test", "story")
        for cut in project["document"]["cuts"]:
            cut.update(visual="A robot in a garden.", action="The robot walks.")
        project = store.save(project["id"], project["document"], project["version"])
        result = store.enqueue(project["id"], "generate", {"version": project["version"], "provider": "comfy", "cut_ids": [project["document"]["cuts"][0]["id"]], "auto_assemble": False})
        captured = []
        class FakeComfy:
            def submit(self, spec, assets):
                captured.append(copy.deepcopy(spec))
                return "saved-comfy-id"
            def collect(self, spec, external_id, output, progress, stop):
                self_id = external_id
                assert self_id == "saved-comfy-id"
                if len(captured) == 1 and not getattr(self, "resumed", False):
                    self.resumed = True
                    raise InterruptedError()
                output.write_bytes(b"completed video")
        provider = FakeComfy()
        Worker(store, comfy=provider).process(result["job_id"])
        changed = workflow()
        changed["1"]["inputs"]["unet_name"] = "different-model.safetensors"
        self.comfy.install(changed)
        restarted = Store(self.tmp.name)
        worker = Worker(restarted, comfy=provider)
        worker.recover_journal()
        with patch("studio.service.media.probe", return_value={"streams": [{"codec_type": "video", "duration": "6"}]}), patch("studio.service.media.thumbnail"):
            worker.process(result["job_id"])
        self.assertEqual(len(captured), 1)
        take = restarted.detail(project["id"])["takes"][0]
        self.assertEqual(take["status"], "succeeded")
        self.assertTrue(take["matches"])
        self.assertEqual(take["input"]["comfy"]["workflow"]["1"]["inputs"]["unet_name"], "working-int8.safetensors")
        # Manual recovery of a download failure also never submits a new job.
        with restarted.db() as db:
            db.execute("UPDATE takes SET status='failed' WHERE id=?", (take["id"],))
        p = restarted.detail(project["id"])
        job = restarted.enqueue(p["id"], "recover", {"version": p["version"], "take_id": take["id"]})
        with patch("studio.service.media.probe", return_value={"streams": [{"codec_type": "video", "duration": "6"}]}), patch("studio.service.media.thumbnail"):
            worker.process(job["job_id"])
        self.assertEqual(len(captured), 1)


class MockComfy(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/upload/image":
            self.server.uploads.append(body)
            self.send({"name": "uploaded.png", "subfolder": "", "type": "input"})
        elif self.path == "/prompt":
            if getattr(self.server, "reject_prompt", False):
                self.send({"error": {"message": "Prompt validation failed"}, "node_errors": {"10": {"errors": [{"message": "Value not in list"}]}}}, 400)
                return
            self.server.submissions.append(json.loads(body))
            self.send({"prompt_id": "comfy-job-1", "number": 1, "node_errors": {}})

    def do_GET(self):
        if self.path.startswith("/history/"):
            if getattr(self.server, "fail_history", 0):
                self.server.fail_history -= 1
                self.send({}, 503)
                return
            self.send({"comfy-job-1": self.server.entry})
        elif self.path.startswith("/view?"):
            self.server.downloads.append(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
            if getattr(self.server, "fail_download", 0):
                self.server.fail_download -= 1
                self.send({}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.server.video)))
            self.end_headers()
            if getattr(self.server, "short_download", 0):
                self.server.short_download -= 1
                self.wfile.write(self.server.video[:3])
                return
            self.wfile.write(self.server.video)
        elif self.path == "/object_info":
            self.send({node["class_type"]: {} for node in workflow(1).values()})
        elif self.path == "/queue":
            self.send({"queue_running": [], "queue_pending": []})


class ComfyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote = ThreadingHTTPServer(("127.0.0.1", 0), MockComfy)
        self.remote.submissions, self.remote.uploads, self.remote.downloads = [], [], []
        self.remote.entry = {"status": {"completed": True, "status_str": "success"}, "outputs": {"60": {"images": [
            {"filename": "clip with spaces.webm", "subfolder": "video/folder", "type": "output"}]}}}
        thread = threading.Thread(target=self.remote.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.remote.server_close)
        self.addCleanup(self.remote.shutdown)
        self.env = patch.dict(os.environ, {"COMFYUI_BASE_URL": f"http://127.0.0.1:{self.remote.server_port}", "COMFYUI_POLL_SECONDS": "0.001", "COMFYUI_WORKFLOW_PATH": "", "COMFYUI_REF_WORKFLOW_PATH": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.comfy = Comfy(self.tmp.name)

    def test_upload_prompt_download_remux_and_no_resubmission(self):
        source = Path(self.tmp.name) / "source.webm"
        media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:r=24", "-t", "0.5", "-c:v", "libvpx-vp9", str(source)])
        self.remote.video = source.read_bytes()
        self.comfy.install(workflow(1))
        self.assertFalse(self.comfy.check()["missing_nodes"])
        image = Path(self.tmp.name) / "image.png"
        image.write_bytes(b"test image bytes")
        asset = {"path": image, "sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "mime": "image/png"}
        spec = self.comfy.prepare({"references": ["ref"], "seconds": 5, "prompt": "ref prompt", "seed": 0}, "a" * 32)
        ident = self.comfy.submit(spec, {"ref": asset})
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertIn(b"test image bytes", self.remote.uploads[0])
        sent = self.remote.submissions[0]["prompt"]
        self.assertEqual(sent["70"]["inputs"]["image"], "uploaded.png")
        self.assertIn(b'name="subfolder"\r\n\r\n\r\n', self.remote.uploads[0])
        self.assertEqual(spec["workflow"]["70"]["inputs"]["image"], "studio_asset:ref")
        output = Path(self.tmp.name) / "raw.mp4"
        for _ in range(2):
            self.comfy.collect(spec, ident, output, lambda _: None, threading.Event())
        metadata = media.probe(output)
        self.assertEqual(metadata["streams"][0]["codec_name"], "h264")
        self.assertIn("mp4", metadata["format"]["format_name"])
        self.assertEqual(len(self.remote.submissions), 1)
        self.assertEqual(self.remote.downloads[0]["filename"], ["clip with spaces.webm"])
        self.assertFalse(list(output.parent.glob("raw.*.part")))
        self.assertFalse(list(output.parent.glob("raw.*.convert.mp4")))
        image.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "ハッシュ"):
            self.comfy.submit(spec, {"ref": asset})
        self.assertEqual(len(self.remote.submissions), 1)

    def test_failure_multiple_outputs_and_stopping_leave_no_submission(self):
        self.comfy.install(workflow())
        spec = self.comfy.prepare({"references": [], "seconds": 5, "prompt": "prompt", "seed": 0}, "a" * 32)
        output = Path(self.tmp.name) / "raw.mp4"
        self.remote.entry["status"]["status_str"] = "error"
        with self.assertRaisesRegex(RuntimeError, "生成が失敗"):
            self.comfy.collect(spec, "comfy-job-1", output, lambda _: None, threading.Event())
        self.remote.entry["status"]["status_str"] = "success"
        self.remote.entry["outputs"]["60"]["images"] *= 2
        with self.assertRaisesRegex(RuntimeError, "動画を一つ"):
            self.comfy.collect(spec, "comfy-job-1", output, lambda _: None, threading.Event())
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            self.comfy.collect(spec, "comfy-job-1", output, lambda _: None, stop)
        self.assertFalse(self.remote.submissions)

    def test_transient_polling_retries_and_rejection_keeps_actionable_error(self):
        self.comfy.install(workflow())
        spec = self.comfy.prepare({"references": [], "seconds": 5, "prompt": "prompt", "seed": 0}, "a" * 32)
        self.remote.reject_prompt = True
        with self.assertRaisesRegex(RuntimeError, "受付されていません.*node 10: Value not in list"):
            self.comfy.submit(spec, {})
        self.assertFalse(self.remote.submissions)
        self.remote.reject_prompt = False
        ident = self.comfy.submit(spec, {})
        self.remote.fail_history = 2
        self.remote.video = b"video bytes"
        self.remote.fail_download = 1
        self.remote.short_download = 1
        output = Path(self.tmp.name) / "raw.mp4"
        def convert(source, dest, stop):
            dest.write_bytes(source.read_bytes())
        with patch("studio.comfy.media.browser_video", side_effect=convert):
            self.comfy.collect(spec, ident, output, lambda _: None, threading.Event())
        self.assertEqual(output.read_bytes(), b"video bytes")
        self.assertEqual(len(self.remote.submissions), 1)
        self.assertEqual(self.remote.fail_history, 0)
        self.assertEqual(len(self.remote.downloads), 3)

    def test_studio_workflow_import_csrf_and_default_configuration(self):
        server = Server(("127.0.0.1", 0), Store(self.tmp.name))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps({"workflow": workflow()}).encode()
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(base + "/api/comfy/workflow", data=body, headers={"Content-Type": "application/json"}))
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with urllib.request.urlopen(urllib.request.Request(base + "/api/comfy/workflow", data=body, headers={"Content-Type": "application/json", "X-Studio-Request": "1"})) as response:
            self.assertEqual(json.load(response)["reference_count"], 0)
        with urllib.request.urlopen(base + "/api/config") as response:
            config = json.load(response)
        self.assertEqual(config["default_provider"], "comfy")
        self.assertEqual(config["comfy"]["workflow_counts"], [0])
        with urllib.request.urlopen(base + "/api/comfy/check") as response:
            self.assertTrue(json.load(response)["connected"])
        with patch.dict(os.environ, {"COMFYUI_BASE_URL": "http://secret-user:bad-secret@localhost:8188"}):
            with urllib.request.urlopen(base + "/api/config") as response:
                bad = json.load(response)
            self.assertTrue(bad["comfy"]["errors"])
            self.assertEqual(bad["default_provider"], "h3")
            self.assertNotIn("bad-secret", json.dumps(bad))
        with patch("mimetypes.guess_type", return_value=("text/plain", None)):
            for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
                with urllib.request.urlopen(base + path) as response:
                    self.assertEqual(response.headers.get_content_type(), kind)
        with patch("studio.server.Comfy.install", side_effect=PermissionError("C:/private/user/path")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base + "/api/comfy/workflow", data=body, headers={"Content-Type": "application/json", "X-Studio-Request": "1"}))
            self.assertEqual(error.exception.code, 500)
            self.assertNotIn(b"private/user", error.exception.read())
            error.exception.close()


if __name__ == "__main__":
    unittest.main()
