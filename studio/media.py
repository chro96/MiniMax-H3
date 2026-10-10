"""Normalize and assemble immutable cuts into an exact 720-frame master."""
import json
import subprocess
import time
import math
import os
import threading
from pathlib import Path

SIZES = {"9:16": (768, 1366), "16:9": (1366, 768), "1:1": (768, 768),
         "4:3": (1024, 768), "3:4": (768, 1024), "21:9": (1792, 768)}
PROCESS_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
_children = set()
_children_lock = threading.RLock()
_terminating = False


def kill_children():
    global _terminating
    with _children_lock:
        _terminating = True
        for process in list(_children):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass


def run(args, timeout=600, stop=None):
    process = None
    try:
        if stop is not None and stop.is_set():
            raise InterruptedError("Studio停止")
        with _children_lock:
            if _terminating:
                raise InterruptedError("Studio停止")
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **PROCESS_GROUP)
            _children.add(process)
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError("動画処理を停止しました。次回起動時に再開します")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            if stop is not None and stop.is_set():
                raise InterruptedError("動画処理を停止しました。次回起動時に再開します")
            raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg / ffprobeが必要です。導入してPATHから実行できる状態にしてください") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.decode(errors="replace")[-1600:]) from exc
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
            finally:
                with _children_lock:
                    _children.discard(process)
    return stdout


def probe(path, stop=None):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], 30, stop))


def video_duration(metadata):
    video = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("動画ストリームがありません")
    candidates = [video.get("duration")]
    if video.get("nb_frames") and video.get("avg_frame_rate"):
        a, b = map(float, video["avg_frame_rate"].split("/"))
        if a and b:
            candidates.append(float(video["nb_frames"]) * b / a)
    if video.get("duration_ts") and video.get("time_base"):
        a, b = map(float, video["time_base"].split("/"))
        if b:
            candidates.append(float(video["duration_ts"]) * a / b)
    for value in candidates:
        try:
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                return duration
        except (ValueError, TypeError):
            pass
    raise ValueError("映像の尺を確認できません")


def demo(path, seconds, index, seed, stop=None):
    colors = ["0x345d64", "0xb48051", "0x576b4d", "0x96758d", "0x4e6d99", "0xa95f52", "0x625981"]
    color = colors[(index + seed % 7) % len(colors)]
    font = ""
    if os.name == "nt":
        fonts = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
        candidate = next((fonts / name for name in ("arial.ttf", "segoeui.ttf") if (fonts / name).is_file()), None)
        if candidate:
            location = candidate.as_posix().replace(":", "\\:")
            if "'" in location:
                raise ValueError("Windowsフォントのパスに引用符が含まれています")
            font = f"fontfile='{location}':"
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=144x256:r=24",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf",
         f"drawtext={font}text='DEMO  CUT {index+1}':fontcolor=white:fontsize=14:x=(w-tw)/2:y=(h-th)/2",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], stop=stop)


def thumbnail(source, dest, stop=None):
    run(["ffmpeg", "-v", "error", "-y", "-ss", "0.2", "-i", str(source), "-frames:v", "1", "-vf", "scale=256:-2", str(dest)], 60, stop)


def browser_video(source, dest, stop=None):
    """ComfyUI may save AV1/WebM. Materialize a playable H.264/AAC MP4."""
    streams = probe(source, stop)["streams"]
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("生成結果に動画ストリームがありません")
    audio = [s for s in streams if s["codec_type"] == "audio"]
    compatible = video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p" and all(s.get("codec_name") == "aac" for s in audio)
    args = ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?"]
    if compatible:
        args += ["-c", "copy"]
    else:
        args += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    run(args + ["-movflags", "+faststart", str(dest)], stop=stop)


def assemble(cuts, doc, directory, bgm=None, demo_mode=False, stop=None):
    directory.mkdir(parents=True, exist_ok=True)
    w, h = (144, 256) if demo_mode else SIZES[doc["aspect_ratio"]]
    if demo_mode and doc["aspect_ratio"] != "9:16":
        a, b = map(int, doc["aspect_ratio"].split(":"))
        h, w = 144, max(2, round(144*a/b/2)*2)
    files = []
    for i, (source, cut) in enumerate(cuts):
        metadata = probe(source, stop)
        duration = video_duration(metadata)
        if duration < cut["seconds"] - 0.5:
            raise ValueError(f"カット{i+1}の動画が指定尺より短すぎます（{duration:g}秒）")
        has_audio = any(s["codec_type"] == "audio" for s in metadata["streams"]) and not cut["mute"]
        dest = directory / f"cut-{i+1}.mp4"
        frames = round(cut["seconds"] * 24)
        seconds = frames / 24
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
        if not has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        args += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf",
                 f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,tpad=stop_mode=clone:stop_duration=0.5",
                 "-af", f"aresample=48000,apad,atrim=duration={seconds},asetpts=PTS-STARTPTS", "-frames:v", str(frames), "-t", str(seconds), "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dest)]
        run(args, stop=stop)
        files.append(dest)
    # Generated names only; no user-controlled concat paths.
    listing = directory / "concat.txt"
    listing.write_text("\n".join(f"file '{p.name}'" for p in files) + "\n")
    output = directory / "master.mp4"
    bed = directory / "bgm.wav"
    # Materialize the loop before mixing. An infinite second input mixed with
    # copied concat video can stall ffmpeg's interleaving queues.
    if bgm:
        run(["ffmpeg", "-v", "error", "-y", "-stream_loop", "-1", "-i", str(bgm),
             "-t", "30", "-af", f"volume={doc['bgm_volume']},afade=t=out:st=29:d=1",
             "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(bed)], stop=stop)
    args = ["ffmpeg", "-v", "error", "-y", "-filter_complex_threads", "1", "-f", "concat", "-safe", "1", "-i", str(listing)]
    if bgm:
        args += ["-i", str(bed), "-filter_complex",
                 "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]",
                 "-map", "0:v:0", "-map", "[a]", "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-c:a", "aac"]
    args += ["-c:v", "copy", "-t", "30", "-movflags", "+faststart", str(output)]
    run(args, stop=stop)
    metadata = probe(output, stop)
    video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    if abs(float(metadata["format"]["duration"]) - 30) > 0.1 or int(video.get("nb_frames", 0)) != 720:
        raise ValueError("合成結果が30秒・720フレームになっていません")
    thumbnail(output, directory / "cover.jpg", stop)
    for file in files:
        file.unlink()
    listing.unlink()
    bed.unlink(missing_ok=True)
    return output, metadata
