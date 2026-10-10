"""Validated storyboards and deterministic H3 prompts (no model dependency)."""
import copy
import hashlib
import json
import math
import re
import uuid

ASPECTS = {"9:16", "16:9", "1:1", "4:3", "3:4", "21:9"}
LAYOUTS = {"5x6": [6] * 5, "6x5": [5] * 6, "3x10": [10] * 3, "2x15": [15] * 2}
FIELDS = ("visual", "action", "camera", "end_state", "sound", "dialogue")
LANGUAGES = {"Auto", "Japanese", "English", "Chinese", "Korean", "Spanish", "French", "German"}


def new_id():
    return uuid.uuid4().hex


def text(value, label, limit=6000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label}は{limit}文字以内の文字列にしてください")
    return value.strip()


def validate_document(doc):
    if not isinstance(doc, dict):
        raise ValueError("コマ割りはJSONオブジェクトで指定してください")
    result = copy.deepcopy(doc)
    for key in ("title", "brief", "subject", "style", "world", "soundscape"):
        result[key] = text(result.get(key, ""), key)
    if not result["title"]:
        raise ValueError("タイトルが必要です")
    if result.get("aspect_ratio") not in ASPECTS:
        raise ValueError("対応していないアスペクト比です")
    cuts = result.get("cuts")
    if not isinstance(cuts, list) or not 2 <= len(cuts) <= 7:
        raise ValueError("コマ割りは2〜7カットです")
    ids, total = set(), 0
    for i, cut in enumerate(cuts):
        if not isinstance(cut, dict):
            raise ValueError("カットの形式が不正です")
        ident = cut.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident) or ident in ids:
            raise ValueError("カットIDが重複または不正です")
        ids.add(ident)
        seconds = cut.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 4 <= seconds <= 15 or abs(seconds * 24 - round(seconds * 24)) > 0.0001:
            raise ValueError(f"カット{i+1}の尺は4〜15秒、1/24秒単位で指定してください")
        frames = round(seconds * 24)
        cut["seconds"] = frames // 24 if frames % 24 == 0 else frames / 24
        total += cut["seconds"]
        for key in ("title", "description_ja", *FIELDS):
            cut[key] = text(cut.get(key, ""), key)
        refs = cut.get("references", [])
        if not isinstance(refs, list) or len(refs) > 9 or any(not isinstance(r, str) or not re.fullmatch(r"[a-f0-9]{32}", r) for r in refs) or len(refs) != len(set(refs)):
            raise ValueError("カットの参照画像は重複しない9枚までです")
        cut["references"] = refs
        cut["mute"] = bool(cut.get("mute", False))
        cut["dialogue_language"] = cut.get("dialogue_language", "Auto")
        if cut["dialogue_language"] not in LANGUAGES:
            raise ValueError("セリフの言語が不正です")
    if abs(total - 30) > 0.0001:
        raise ValueError(f"動画の合計尺は30秒にしてください（現在{total:g}秒）")
    for key in ("character_reference", "bgm"):
        asset = result.get(key)
        if asset is not None and (not isinstance(asset, str) or not re.fullmatch(r"[a-f0-9]{32}", asset)):
            raise ValueError("素材IDが不正です")
        result[key] = asset
    gain = result.get("bgm_volume", 0.2)
    if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or not 0 <= gain <= 1:
        raise ValueError("BGM音量は0〜1です")
    result["bgm_volume"] = gain
    return result


def template(title, brief, layout="5x6", aspect="9:16"):
    if layout not in LAYOUTS:
        raise ValueError("構成が不正です")
    names = ["導入", "きっかけ", "展開", "転換", "結末", "余韻", "締め"]
    durations = LAYOUTS[layout]
    return validate_document({
        "title": title, "brief": brief, "subject": "", "style": "3D animation",
        "world": "", "soundscape": "Natural ambience and subtle sounds synchronized to visible actions. No music.",
        "aspect_ratio": aspect, "cuts": [
            {"id": new_id(), "title": names[i] if i < len(durations)-1 else "結末",
             "description_ja": "映像・動き・終端状態を入力してください", "seconds": seconds,
             "visual": "", "action": "", "camera": "The camera holds a static medium shot.",
             "end_state": "", "sound": "", "dialogue": "", "references": [], "mute": False}
            for i, seconds in enumerate(durations)
        ], "character_reference": None, "bgm": None, "bgm_volume": 0.2,
    })


def compile_cut(doc, cut_id):
    cut = next((c for c in doc["cuts"] if c["id"] == cut_id), None)
    if cut is None:
        raise ValueError("カットがありません")
    i = doc["cuts"].index(cut)
    refs = list(dict.fromkeys(([doc["character_reference"]] if doc.get("character_reference") else []) + cut["references"]))
    if len(refs) > 9:
        raise ValueError("キャラクター参照を含めて画像は9枚までです")
    previous = doc["cuts"][i-1]["end_state"] if i else ""
    continuity = f"Cut {i+1} of {len(doc['cuts'])}. One continuous shot lasting {cut['seconds']:g} seconds."
    if previous:
        continuity += f" The previous cut ended with: {previous}. Begin consistently from that state."
    dialogue = ""
    if cut["dialogue"]:
        language = cut.get("dialogue_language", "Auto")
        if language == "Auto":
            language = "Japanese" if re.search(r"[\u3040-\u30ff]", cut["dialogue"]) else "English" if re.fullmatch(r"[\x00-\x7f]*", cut["dialogue"]) else None
        tag = f"[{language}] " if language else ""
        dialogue = f" The stated main character (S1) says: <d>{tag}{cut['dialogue']}</d> with synchronized lip movement."
    visual = f"{doc['style']}. {doc['subject']} {doc['world']} {cut['visual']} {cut['action']} {cut['camera']} {dialogue}"
    if cut["end_state"]:
        visual += f" By the end: {cut['end_state']}."
    sounds = " ".join(x for x in (doc["soundscape"], cut["sound"]) if x) or "Natural quiet ambience."
    if refs:
        definitions = []
        for n, asset in enumerate(refs, 1):
            if asset == doc.get("character_reference"):
                definitions.append(f"<Subject 1> is the main character in <Picture {n}>. Retain its identity, proportions, colors and attached accessories; poses and handheld props follow this shot's action.")
            else:
                definitions.append(f"<Picture {n}> is the storyboard composition reference for [Shot 1]. Follow its viewpoint and subject placement; do not reproduce panels, labels or sheet borders.")
        prompt = ("subject_definitions:\n" + "\n".join(definitions) +
                  f"\n\nsummary:\n[reference generation] Generate one {cut['seconds']:g}-second continuous shot in {doc['aspect_ratio']}. {cut['visual']}\n\n"
                  "retention_analysis:\nRetain the referenced identity and scene design throughout [Shot 1]. Poses and expressions move naturally according to the stated action.\n\n"
                  f"detailed_description:\n[Shot 1] {visual}\n{continuity}\n\noverall_soundscape:\n{sounds}\n\nnon_diegetic_music:\nN/A")
    else:
        prompt = f"integrated_multimodal_description: [Shot 1] {visual}\n{continuity}\n\noverall_soundscape: {sounds}\n\nnon_diegetic_music: N/A"
    request = {"model": "MiniMaxAI/MiniMax-H3", "task": "ref2va" if refs else "t2va", "prompt": prompt,
               "target": {"short_edge": 768, "aspect_ratio": doc["aspect_ratio"], "duration_seconds": cut["seconds"]},
               "references": refs}
    if float(cut["seconds"]).is_integer():
        request["seconds"] = int(cut["seconds"])
    return {"request": request, "fingerprint": fingerprint(request), "snapshot": copy.deepcopy(cut)}


def fingerprint(request):
    canonical = copy.deepcopy({k:v for k,v in request.items() if k not in {"seed", "endpoint"}})
    canonical["target"]["frames"] = round(canonical["target"].pop("duration_seconds") * 24)
    canonical.pop("seconds", None)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
