"""No external AI calls or GPU allocation. Real ffmpeg and loopback HTTP."""
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from studio import media
from studio.domain import compile_cut, new_id, template, validate_document
from studio.providers import H3
from studio.server import Server
from studio.service import Conflict, Store, Worker


def filled(doc):
    doc = copy.deepcopy(doc)
    doc["subject"] = "A small blue robot."
    doc["world"] = "A quiet garden in morning light."
    for i, cut in enumerate(doc["cuts"]):
        cut.update(visual="The robot stands beside a flower pot.", action=f"The robot takes step {i+1} toward the flower.", end_state=f"The robot ends at position {i+1}.")
    return doc


class DomainTests(unittest.TestCase):
    def test_numeric_seconds_are_canonical_frames(self):
        doc = filled(template("story", "brief"))
        original = compile_cut(doc, doc["cuts"][0]["id"])["fingerprint"]
        for cut in doc["cuts"]:
            cut["seconds"] = 6.0
        self.assertEqual(compile_cut(doc, doc["cuts"][0]["id"])["fingerprint"], original)
        normalized = validate_document(doc)
        self.assertIsInstance(normalized["cuts"][0]["seconds"], int)
        doc["cuts"][0]["seconds"] = 6.0416666683
        doc["cuts"][1]["seconds"] = 5.9583333317
        normalized = validate_document(doc)
        self.assertEqual(normalized["cuts"][0]["seconds"], 145/24)
        self.assertEqual(normalized["cuts"][1]["seconds"], 143/24)

    def test_dialogue_language_can_be_explicit_without_guessing_han(self):
        doc = filled(template("story", "brief"))
        cut = doc["cuts"][0]
        cut["dialogue"] = "你好"
        self.assertIn("<d>你好</d>", compile_cut(doc,cut["id"])["request"]["prompt"])
        cut["dialogue_language"] = "Chinese"
        self.assertIn("<d>[Chinese] 你好</d>", compile_cut(doc,cut["id"])["request"]["prompt"])
    def test_layouts_are_30_seconds(self):
        for layout in ("5x6", "6x5", "3x10", "2x15"):
            doc = template("story", "brief", layout)
            self.assertEqual(sum(c["seconds"] for c in doc["cuts"]), 30)

    def test_rejects_invalid_duration_and_ids(self):
        for bad in (None, "6", True, 3, 16, float("nan"), 6.02):
            doc = template("story", "brief")
            doc["cuts"][0]["seconds"] = bad
            with self.assertRaises(ValueError):
                validate_document(doc)
        doc = template("story", "brief")
        doc["cuts"][1]["id"] = doc["cuts"][0]["id"]
        with self.assertRaises(ValueError):
            validate_document(doc)

    def test_h3_sections_dialogue_and_continuity(self):
        doc = filled(template("story", "brief"))
        doc["cuts"][1]["dialogue"] = "こんにちは！"
        cut = compile_cut(doc, doc["cuts"][1]["id"])
        prompt = cut["request"]["prompt"]
        self.assertIn("integrated_multimodal_description:", prompt)
        self.assertIn("The robot ends at position 1.", prompt)
        self.assertIn("<d>[Japanese] こんにちは！</d>", prompt)
        self.assertTrue(prompt.endswith("non_diegetic_music: N/A"))
        self.assertEqual(cut["request"]["task"], "t2va")
        doc["character_reference"] = new_id()
        doc["cuts"][1]["references"] = [new_id()]
        ref = compile_cut(doc, doc["cuts"][1]["id"])
        self.assertEqual(ref["request"]["task"], "ref2va")
        self.assertEqual(len(ref["request"]["references"]), 2)
        self.assertIn("<Picture 2>", ref["request"]["prompt"])
        sections = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"]
        positions = [ref["request"]["prompt"].index(x+":") for x in sections]
        self.assertEqual(positions, sorted(positions))

    def test_local_edit_and_continuity_changes_are_visible(self):
        doc = filled(template("story", "brief"))
        old = [compile_cut(doc, c["id"])["fingerprint"] for c in doc["cuts"]]
        doc["cuts"][1]["visual"] = "The robot holds a flower."
        changed = [i for i,c in enumerate(doc["cuts"]) if compile_cut(doc,c["id"])["fingerprint"] != old[i]]
        self.assertEqual(changed, [1])
        doc["cuts"][1]["end_state"] = "The robot holds a flower beside the pot."
        changed = [i for i,c in enumerate(doc["cuts"]) if compile_cut(doc,c["id"])["fingerprint"] != old[i]]
        self.assertEqual(changed, [1, 2])
        # Playback-only mute and editorial Japanese notes do not cause regeneration.
        current = compile_cut(doc, doc["cuts"][0]["id"])["fingerprint"]
        doc["cuts"][0]["mute"] = True
        doc["cuts"][0]["description_ja"] = "日本語のメモ"
        self.assertEqual(compile_cut(doc,doc["cuts"][0]["id"])["fingerprint"], current)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.project = self.store.create("test", "story")
        self.project = self.store.save(self.project["id"], filled(self.project["document"]), self.project["version"])

    def tearDown(self):
        self.tmp.cleanup()


class StoreTests(StoreFixture):
    def test_existing_float_take_fingerprint_migrates_without_rewriting_input(self):
        p = self.project
        result = self.store.enqueue(p["id"], "generate", {"version":p["version"],"provider":"demo","cut_ids":[p["document"]["cuts"][0]["id"]]})
        take = result["project"]["takes"][0]
        original = copy.deepcopy(take["input"])
        original["request"]["target"]["duration_seconds"] = 6.0
        request = {k:v for k,v in original["request"].items() if k != "seed"}
        legacy = hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        encoded = json.dumps(original,ensure_ascii=False)
        with self.store.db() as db:
            db.execute("UPDATE takes SET fingerprint=?,input=? WHERE id=?",(legacy,encoded,take["id"]))
        restarted = Store(self.tmp.name)
        self.assertTrue(restarted.detail(p["id"])["takes"][0]["matches"])
        with restarted.db() as db:
            self.assertEqual(db.execute("SELECT input FROM takes WHERE id=?",(take["id"],)).fetchone()[0],encoded)

    def test_save_is_not_generation_and_optimistic_conflict(self):
        project = self.project
        self.assertEqual(project["takes"], [])
        self.assertEqual(project["jobs"], [])
        with self.assertRaises(Conflict):
            self.store.save(project["id"], project["document"], project["version"]-1)
        restarted = Store(self.tmp.name)
        self.assertEqual(restarted.detail(project["id"])["document"], project["document"])

    def test_queue_serializes_edits_and_retains_snapshot(self):
        p = self.project
        result = self.store.enqueue(p["id"], "generate", {"version":p["version"],"provider":"demo","cut_ids":[p["document"]["cuts"][0]["id"]],"seed":0})
        self.assertEqual(result["project"]["takes"][0]["input"]["request"]["seed"], 0)
        with self.assertRaises(Conflict):
            self.store.save(p["id"], p["document"], p["version"])
        with self.assertRaises(Conflict):
            self.store.enqueue(p["id"], "generate", {"version":p["version"],"provider":"demo","cut_ids":[p["document"]["cuts"][0]["id"]]})
        with self.assertRaises(Conflict):
            self.store.set_state(p["id"], "archive")

    def test_generation_transaction_rolls_back_invalid_cut(self):
        p = self.project
        doc = copy.deepcopy(p["document"])
        doc["cuts"][1]["visual"] = ""
        p = self.store.save(p["id"], doc, p["version"])
        with self.assertRaises(ValueError):
            self.store.enqueue(p["id"], "generate", {"version":p["version"],"provider":"demo","cut_ids":[c["id"] for c in doc["cuts"]]})
        self.assertEqual(self.store.detail(p["id"])["takes"], [])

    def test_archive_restore_no_data_loss(self):
        p = self.store.set_state(self.project["id"], "archive")
        self.assertEqual(p["status"], "archived")
        with self.assertRaises(Conflict):
            self.store.save(p["id"], p["document"], p["version"])
        p = self.store.set_state(p["id"], "restore")
        self.assertEqual(p["document"], self.project["document"])

    def test_foreign_reference_rejected(self):
        doc = copy.deepcopy(self.project["document"])
        doc["character_reference"] = new_id()
        with self.assertRaises(ValueError):
            self.store.save(self.project["id"], doc, self.project["version"])

    def test_ai_proposal_is_reviewed_before_apply(self):
        class FakePlanner:
            def storyboard(self, doc):
                result = copy.deepcopy(doc)
                result["cuts"][0]["visual"] = "A changed camera view."
                result["cuts"][0]["seconds"] = 100
                result["cuts"][0]["id"] = new_id()
                result["cuts"][0]["dialogue"] = "unrequested words"
                return result
        p = self.project
        with patch.dict(os.environ, {"STUDIO_LLM_BASE_URL":"http://localhost:8000/v1","STUDIO_LLM_MODEL":"fake"}):
            result = self.store.enqueue(p["id"], "plan", {"version":p["version"]})
        Worker(self.store, planner=FakePlanner()).process(result["job_id"])
        before = self.store.detail(p["id"])
        self.assertEqual(before["document"], p["document"])
        after = self.store.apply_proposal(p["id"], result["job_id"], p["version"])
        self.assertEqual(after["document"]["cuts"][0]["visual"], "A changed camera view.")
        self.assertEqual(after["document"]["cuts"][0]["seconds"], 6)
        self.assertEqual(after["document"]["cuts"][0]["id"], p["document"]["cuts"][0]["id"])
        self.assertEqual(after["takes"], [])
        self.assertEqual(after["document"]["cuts"][0]["dialogue"], "")
        with self.assertRaises(Conflict):
            self.store.apply_proposal(p["id"], result["job_id"], p["version"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class MediaLifecycleTests(StoreFixture):
    def test_fractional_cuts_assemble_at_exact_frame_boundaries(self):
        doc = copy.deepcopy(self.project["document"])
        doc["cuts"][0]["seconds"] = 6.0416666683
        doc["cuts"][1]["seconds"] = 5.9583333317
        self.project = self.store.save(self.project["id"],doc,self.project["version"])
        p = self.generate([c["id"] for c in doc["cuts"]])
        manifest = p["renders"][0]["manifest"]
        self.assertEqual([round(c["seconds"]*24) for c in manifest["takes"]], [145,143,144,144,144])
        video = next(s for s in manifest["metadata"]["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(video["nb_frames"]),720)

    def test_batch_attempts_other_cuts_after_collection_failure(self):
        class FakeH3:
            submissions = 0
            def submit(self, *_):
                self.submissions += 1
                return str(self.submissions)
            def collect(self,spec,ident,output,*_):
                if ident == "1":
                    raise TimeoutError("first cut timed out")
                media.demo(output,spec["target"]["duration_seconds"],0,5)
        p = self.project
        result = self.store.enqueue(p["id"],"generate",{"version":p["version"],"cut_ids":[c["id"] for c in p["document"]["cuts"]]})
        h3 = FakeH3()
        Worker(self.store,h3=h3).process(result["job_id"])
        p = self.store.detail(p["id"])
        self.assertEqual(h3.submissions,5)
        self.assertEqual(sum(t["status"] == "succeeded" for t in p["takes"]),4)
        self.assertEqual(p["jobs"][0]["status"],"failed")
        self.assertIn("カット1:",p["jobs"][0]["error"])

    def test_audio_duration_cannot_hide_short_video(self):
        source = self.store.root / "short.mp4"
        media.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","color=s=144x256:r=24:d=1", "-f","lavfi","-i","anullsrc=r=48000:cl=stereo", "-t","6","-c:v","libx264","-c:a","aac",str(source)])
        metadata = media.probe(source)
        self.assertGreater(float(metadata["format"]["duration"]),5.9)
        self.assertAlmostEqual(media.video_duration(metadata),1,delta=0.1)
        with self.assertRaisesRegex(ValueError,"カット1.*短すぎ"):
            media.assemble([(source,self.project["document"]["cuts"][0])],self.project["document"],self.store.root/"bad-render",demo_mode=True)
    def generate(self, ids, seed=12):
        p = self.store.detail(self.project["id"])
        result = self.store.enqueue(p["id"], "generate", {"version":p["version"],"provider":"demo","cut_ids":ids,"seed":seed})
        Worker(self.store).process(result["job_id"])
        p = self.store.detail(p["id"])
        self.assertEqual(p["jobs"][0]["status"], "succeeded", p["jobs"][0]["error"])
        return p

    def test_exact_30_seconds_partial_retake_and_master_history(self):
        cut_ids = [c["id"] for c in self.project["document"]["cuts"]]
        p = self.generate(cut_ids)
        self.assertEqual(p["ready_count"], 5)
        self.assertTrue(p["master_current"])
        self.assertEqual(len(p["renders"]), 1)
        master = p["renders"][0]
        output = self.store.root / master["file"]
        old_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        metadata = media.probe(output)
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 30, delta=0.1)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(video["nb_frames"]), 720)
        original = {t["cut_id"]:t["id"] for t in p["takes"] if t["selected"]}
        doc = copy.deepcopy(p["document"])
        doc["cuts"][2]["action"] = "The robot waves one hand slowly."
        p = self.store.save(p["id"], doc, p["version"])
        self.assertFalse(p["master_current"])
        self.assertEqual(p["ready_count"], 4)
        p = self.generate([cut_ids[2]], seed=16)
        self.assertEqual(len(p["takes"]), 6)
        self.assertEqual(len(p["renders"]), 1)
        take = next(t for t in p["takes"] if t["cut_id"] == cut_ids[2] and t["revision"] == 2)
        self.assertFalse(take["selected"])
        p = self.store.choose(p["id"], take["id"])
        result = self.store.enqueue(p["id"], "assemble", {"version":p["version"]})
        Worker(self.store).process(result["job_id"])
        p = self.store.detail(p["id"])
        self.assertEqual(p["jobs"][0]["status"], "succeeded", p["jobs"][0]["error"])
        self.assertTrue(p["master_current"])
        self.assertEqual(len(p["renders"]), 2)
        latest = p["renders"][0]["manifest"]["takes"]
        self.assertEqual(latest[2]["take_id"], take["id"])
        self.assertTrue(all(t["take_id"] == original[t["cut_id"]] for i,t in enumerate(latest) if i != 2))
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), old_hash)
        with self.assertRaises(Conflict):
            self.store.set_state(p["id"], "approve")
        # Selecting an old mismatched take prevents accidental approval/assembly.
        p = self.store.choose(p["id"], original[cut_ids[2]])
        with self.assertRaises(Conflict):
            self.store.enqueue(p["id"], "assemble", {"version":p["version"]})

    def test_bgm_mute_and_assembly_only_changes(self):
        ids = [c["id"] for c in self.project["document"]["cuts"]]
        p = self.generate(ids)
        audio = self.store.root / "tone.wav"
        media.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","sine=frequency=440:duration=1",str(audio)])
        asset = self.store.upload(p["id"], "tone.wav", "audio/wav", audio.read_bytes())
        doc = copy.deepcopy(p["document"])
        doc["bgm"], doc["bgm_volume"] = asset["id"], 0.5
        for c in doc["cuts"]:
            c["mute"] = True
        p = self.store.save(p["id"], doc, p["version"])
        self.assertEqual(p["ready_count"], 5)
        result = self.store.enqueue(p["id"], "assemble", {"version":p["version"]})
        Worker(self.store).process(result["job_id"])
        p = self.store.detail(p["id"])
        self.assertEqual(p["jobs"][0]["status"], "succeeded", p["jobs"][0]["error"])
        self.assertEqual(len(p["takes"]), 5)
        output = self.store.root / p["renders"][0]["file"]
        import array
        samples = array.array("h", media.run(["ffmpeg","-v","error","-i",str(output),"-t","1","-f","s16le","-ac","1","-ar","48000","pipe:1"]))
        self.assertGreater(max(abs(x) for x in samples), 100)

    def test_failed_remote_job_recovers_without_new_submission(self):
        class FakeH3:
            submissions = 0
            fail = True

            def submit(self, *_):
                self.submissions += 1
                return "existing_remote_id"

            def collect(self, spec, ident, output, progress, stop):
                if self.fail:
                    raise TimeoutError("temporary collection failure")
                media.demo(output, spec["target"]["duration_seconds"], 0, 5)

        p = self.project
        result = self.store.enqueue(p["id"], "generate", {"version":p["version"], "cut_ids":[p["document"]["cuts"][0]["id"]], "auto_assemble":False})
        provider = FakeH3()
        worker = Worker(self.store, h3=provider)
        worker.process(result["job_id"])
        p = self.store.detail(p["id"])
        take = p["takes"][0]
        self.assertEqual(take["status"], "failed")
        self.assertEqual(take["external_id"], "existing_remote_id")
        provider.fail = False
        result = self.store.enqueue(p["id"], "recover", {"version":p["version"], "take_id":take["id"]})
        worker.process(result["job_id"])
        p = self.store.detail(p["id"])
        self.assertEqual(p["takes"][0]["status"], "succeeded")
        self.assertEqual(len(p["takes"]), 1)
        self.assertEqual(provider.submissions, 1)


class RecoveryTests(StoreFixture):
    def test_shutdown_waits_for_submitted_id_to_be_saved(self):
        submitted, release = threading.Event(), threading.Event()
        class SlowH3:
            def submit(self,*_):
                submitted.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release submission")
                return "saved_on_shutdown"
            def collect(self,*_):
                raise InterruptedError()
        p = self.project
        result = self.store.enqueue(p["id"],"generate",{"version":p["version"],"cut_ids":[p["document"]["cuts"][0]["id"]]})
        worker = Worker(self.store,h3=SlowH3())
        worker.start()
        closing = threading.Thread(target=worker.close)
        try:
            self.assertTrue(submitted.wait(3))
            closing.start()
            self.assertTrue(worker.stop.wait(1))
            self.assertTrue(closing.is_alive())
            release.set()
            closing.join(3)
            self.assertFalse(closing.is_alive())
            take = self.store.detail(p["id"])["takes"][0]
            self.assertEqual(take["external_id"],"saved_on_shutdown")
            self.assertEqual(self.store.detail(p["id"])["jobs"][0]["status"],"queued")
        finally:
            release.set()
            if closing.ident:
                closing.join(3)
            worker.close()

    def test_media_stop_terminates_and_reaps_child(self):
        pidfile = self.store.root / "child.pid"
        stop = threading.Event()
        timer = threading.Timer(0.4,stop.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                media.run([sys.executable,"-c","import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(20)",str(pidfile)],stop=stop)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()),0)
        finally:
            timer.cancel()

    def test_orphan_renders_cleaned_and_duplicate_worker_rejected(self):
        directory = self.store.root / "renders"
        orphan = directory / new_id()
        orphan.mkdir(parents=True)
        (orphan/"unfinished.mp4").write_bytes(b"partial")
        other = directory / "manual-backup"
        other.mkdir()
        first,second = Worker(self.store),Worker(self.store)
        first.start()
        try:
            self.assertFalse(orphan.exists())
            self.assertTrue(other.exists())
            with self.assertRaisesRegex(RuntimeError,"すでに起動"):
                second.start()
        finally:
            first.close()
    def test_restart_resumes_known_id_and_fails_ambiguous_submission(self):
        p = self.project
        result = self.store.enqueue(p["id"], "generate", {"version":p["version"], "cut_ids":[c["id"] for c in p["document"]["cuts"][:2]], "auto_assemble":False})
        takes = result["project"]["takes"]
        with self.store.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (result["job_id"],))
            db.execute("UPDATE takes SET status='running',external_id='known_id' WHERE id=?", (takes[0]["id"],))
            db.execute("UPDATE takes SET status='submitting' WHERE id=?", (takes[1]["id"],))
        Worker(self.store).recover_journal()
        with self.store.db() as db:
            resumed = db.execute("SELECT * FROM takes WHERE id=?", (takes[0]["id"],)).fetchone()
            unknown = db.execute("SELECT * FROM takes WHERE id=?", (takes[1]["id"],)).fetchone()
            job = db.execute("SELECT * FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["external_id"], "known_id")
        self.assertEqual(unknown["status"], "failed")
        self.assertIn("受付結果が不明", unknown["error"])
        self.assertEqual(job["status"], "queued")


class MockVideoAPI(BaseHTTPRequestHandler):
    submissions = []
    output = b""

    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.submissions.append(payload)
        self.reply({"id":"video_1", "status":"queued"})

    def do_GET(self):
        if self.path.endswith("/content"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.output)
        else:
            self.reply({"id":"video_1","status":"completed","progress":100})

    def reply(self, value):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.server = Server(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_local_api_csrf_media_range_and_no_database_download(self):
        payload = json.dumps({"title":"HTTP test", "brief":"story"}).encode()
        req = urllib.request.Request(self.base+"/api/projects",data=payload,headers={"Content-Type":"application/json","X-Studio-Request":"1"})
        with urllib.request.urlopen(req) as response:
            p = json.load(response)
        self.assertEqual(p["status"], "draft")
        for headers in ({"Content-Type":"application/json"}, {"Content-Type":"application/json","X-Studio-Request":"1","Origin":"https://evil.example"}, {"Content-Type":"application/json","X-Studio-Request":"1","Host":"evil.example"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(self.base+"/api/projects",data=payload,headers=headers))
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        for path in ("/media/studio.sqlite3", "/media/../studio.sqlite3", "/.env.studio"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base+path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        folder = Path(self.tmp.name) / "renders" / new_id()
        folder.mkdir(parents=True)
        (folder / "master.mp4").write_bytes(b"0123456789")
        url = self.base+f"/media/renders/{folder.name}/master.mp4"
        with urllib.request.urlopen(urllib.request.Request(url,headers={"Range":"bytes=2-5"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
        with urllib.request.urlopen(urllib.request.Request(url,headers={"Range":"bytes=-3"})) as response:
            self.assertEqual(response.read(), b"789")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url,headers={"Range":"bytes=15-20"}))
        self.assertEqual(error.exception.code, 416)
        error.exception.close()

    def test_h3_contract_and_no_resubmit_on_recovery(self):
        MockVideoAPI.submissions, MockVideoAPI.output = [], b"test mp4 contents"
        remote = ThreadingHTTPServer(("127.0.0.1", 0), MockVideoAPI)
        remote_thread = threading.Thread(target=remote.serve_forever, daemon=True)
        remote_thread.start()
        root = f"http://127.0.0.1:{remote.server_port}"
        try:
            with patch.dict(os.environ, {"H3_BASE_URL":root,"H3_REF_BASE_URL":root,"H3_POLL_SECONDS":"0.01"}):
                provider = H3()
            doc = filled(template("test", "story"))
            ref = new_id()
            doc["character_reference"] = ref
            asset = Path(self.tmp.name) / "image.png"
            asset.write_bytes(b"image bytes")
            spec = compile_cut(doc,doc["cuts"][0]["id"])["request"] | {"seed":0}
            ident = provider.submit(spec, {ref:{"mime":"image/png", "path":asset}})
            self.assertEqual(len(MockVideoAPI.submissions), 1)
            sent = MockVideoAPI.submissions[0]
            self.assertEqual(sent["task"], "ref2va")
            self.assertEqual(sent["target"]["duration_seconds"], 6)
            self.assertEqual(sent["seed"], 0)
            self.assertTrue(sent["conditions"][0]["uri"].startswith("data:image/png;base64,"))
            dest = Path(self.tmp.name) / "raw.mp4"
            provider.collect(spec, ident, dest, lambda _:None, threading.Event())
            provider.collect(spec, ident, dest, lambda _:None, threading.Event())
            self.assertEqual(dest.read_bytes(), MockVideoAPI.output)
            self.assertEqual(len(MockVideoAPI.submissions), 1)
        finally:
            remote.shutdown()
            remote.server_close()
            remote_thread.join()


if __name__ == "__main__":
    unittest.main()
