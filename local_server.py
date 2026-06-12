
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PZ ComfyW Local Direct
- bez FTP
- bez PHP
- bez workeru
- běží lokálně na PC vedle ComfyUI
"""
from __future__ import annotations

import json, os, sys, time, uuid, shutil, threading, mimetypes, traceback, webbrowser, socket, re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote, urlencode, quote
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    import requests
except ImportError:
    raise SystemExit("Chybí modul requests. Spusť START_LOCAL.cmd, nebo: python -m pip install requests websocket-client")
try:
    import websocket  # websocket-client
except Exception:
    websocket = None

APP_VERSION = "2026-06-11-local-direct-v3-ftp-ui"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA = ROOT / "data"
INPUTS = DATA / "inputs"
OUTPUTS = DATA / "outputs"
TMP = DATA / "tmp"
LOGS = DATA / "logs"
JOBS_PATH = DATA / "jobs.json"
for p in (DATA, INPUTS, OUTPUTS, TMP, LOGS):
    p.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "comfy_base": "http://127.0.0.1:8000",
    "host": "127.0.0.1",
    "port": 8765,
    "open_browser": True,
    "auto_start_queue": True,
    "output_dir": "data/outputs",
    "input_dir": "data/inputs",
    "i2v_workflow": "workflows/ltx23_i2v_template.json",
    "flf2v_workflow": "workflows/ltx23_flf2v_template.json"
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            # utf-8-sig: snese i BOM, který přidává Notepad — jinak by se config potichu ignoroval
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            out = dict(DEFAULT_CONFIG)
            out.update(cfg if isinstance(cfg, dict) else {})
            return out
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

CONFIG = load_config()

def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(",", ".")))
    except Exception:
        return default

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return default

def rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")

def sanitize_filename(name: str, fallback_ext: str = ".bin") -> str:
    name = Path(name or "file").name
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in Path(name).stem)[:80].strip("._") or "file"
    ext = Path(name).suffix.lower()
    if not ext:
        ext = fallback_ext
    return stem + ext

def log_line(msg: str) -> None:
    line = f"{now_iso()} {msg}"
    print(line, flush=True)
    try:
        (LOGS / "local_server.log").open("a", encoding="utf-8").write(line + "\n")
    except Exception:
        pass


class UploadPart:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.data = data


def parse_multipart_body(content_type: str, body: bytes) -> Dict[str, List[Any]]:
    """Small dependency-free multipart parser for local uploads.
    Returns {field_name: [str | UploadPart, ...]}.
    """
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or '', re.I)
    if not m:
        raise ValueError('Chybí multipart boundary.')
    boundary = (m.group(1) or m.group(2) or '').strip().encode('latin1')
    if not boundary:
        raise ValueError('Prázdný multipart boundary.')
    marker = b'--' + boundary
    out: Dict[str, List[Any]] = {}
    for raw_part in body.split(marker):
        # Odstraň jen úvodní protokolový CRLF, ne data samotná.
        part = raw_part
        if part.startswith(b'\r\n'):
            part = part[2:]
        elif part.startswith(b'\n'):
            part = part[1:]
        # Koncový marker "--" (poslední boundary) nebo prázdná část.
        if not part or part.startswith(b'--'):
            continue
        if b'\r\n\r\n' in part:
            header_bytes, content = part.split(b'\r\n\r\n', 1)
        elif b'\n\n' in part:
            header_bytes, content = part.split(b'\n\n', 1)
        else:
            continue
        # Odstraň přesně JEDEN koncový CRLF (oddělovač před další boundary) —
        # binární data legitimně končící 0x0A/0x0D zůstanou netknutá.
        if content.endswith(b'\r\n'):
            content = content[:-2]
        elif content.endswith(b'\n'):
            content = content[:-1]
        headers = header_bytes.decode('latin1', errors='replace')
        disp = ''
        for line in headers.splitlines():
            if line.lower().startswith('content-disposition:'):
                disp = line
                break
        if not disp:
            continue
        params = {}
        for mm in re.finditer(r';\s*([^=]+)=(?:"([^"]*)"|([^;]*))', disp):
            params[mm.group(1).strip().lower()] = (mm.group(2) if mm.group(2) is not None else mm.group(3) or '')
        name = params.get('name')
        if not name:
            continue
        if 'filename' in params:
            out.setdefault(name, []).append(UploadPart(params.get('filename') or 'file', content))
        else:
            out.setdefault(name, []).append(content.decode('utf-8', errors='replace'))
    return out

jobs_lock = threading.RLock()
processing_lock = threading.Lock()
cancel_flags: set[int] = set()
active_prompt_by_job: Dict[int, str] = {}


def load_jobs() -> List[dict]:
    if not JOBS_PATH.exists():
        return []
    try:
        data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_jobs(jobs: List[dict]) -> None:
    tmp = JOBS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(JOBS_PATH)

def next_job_id(jobs: List[dict]) -> int:
    return max([int(j.get("id", 0)) for j in jobs] or [0]) + 1

def public_job(j: dict) -> dict:
    x = dict(j)
    if x.get("input_path"):
        x["input_url"] = "/file/input/" + quote(Path(x["input_path"]).name)
    if x.get("input2_path"):
        x["input2_url"] = "/file/input/" + quote(Path(x["input2_path"]).name)
    if x.get("output_path"):
        x["output_url"] = "/file/output/" + quote(Path(x["output_path"]).name)
    return x

def get_job(job_id: int) -> Optional[dict]:
    with jobs_lock:
        for j in load_jobs():
            if int(j.get("id", 0)) == int(job_id):
                return j
    return None

def update_job(job_id: int, **fields: Any) -> None:
    with jobs_lock:
        jobs = load_jobs()
        for j in jobs:
            if int(j.get("id", 0)) == int(job_id):
                j.update(fields)
                j["updated_at"] = now_iso()
                break
        save_jobs(jobs)

def add_event(job_id: int, message: str, kind: str = "info", data: Any = None) -> None:
    with jobs_lock:
        jobs = load_jobs()
        for j in jobs:
            if int(j.get("id", 0)) == int(job_id):
                ev = {"time": now_iso(), "kind": kind, "message": message}
                if data is not None: ev["data"] = data
                j.setdefault("events", []).append(ev)
                j["message"] = message
                j["updated_at"] = now_iso()
                break
        save_jobs(jobs)

def set_progress(job_id: int, status: str, progress: int, message: str, node: str = "") -> None:
    update_job(job_id, status=status, progress=max(0, min(100, int(progress))), message=message, current_node=node)
    add_event(job_id, message, status)

# ─── Comfy helpers ───────────────────────────────────────────

def comfy_base() -> str:
    return str(load_config().get("comfy_base") or "http://127.0.0.1:8000").rstrip("/")

def comfy_get(path: str, **kw):
    return requests.get(comfy_base() + path, **kw)

def comfy_post(path: str, **kw):
    return requests.post(comfy_base() + path, **kw)

def comfy_online() -> Tuple[bool, str]:
    try:
        r = comfy_get("/queue", timeout=4)
        if r.status_code < 400:
            return True, "ComfyUI API odpovídá"
        return False, f"ComfyUI HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)

def upload_image_to_comfy(path: Path) -> str:
    with path.open("rb") as f:
        files = {"image": (path.name, f, "application/octet-stream")}
        data = {"overwrite": "true", "type": "input"}
        r = comfy_post("/upload/image", files=files, data=data, timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"Comfy /upload/image HTTP {r.status_code}: {r.text[:800]}")
    js = r.json()
    return js.get("name") or path.name

_COMFY_OBJECT_INFO_CACHE: Optional[dict] = None

def get_comfy_object_info() -> dict:
    global _COMFY_OBJECT_INFO_CACHE
    if _COMFY_OBJECT_INFO_CACHE is not None:
        return _COMFY_OBJECT_INFO_CACHE
    try:
        r = comfy_get("/object_info", timeout=20)
        r.raise_for_status()
        data = r.json()
        _COMFY_OBJECT_INFO_CACHE = data if isinstance(data, dict) else {}
    except Exception as e:
        log_line(f"WARN object_info: {e}")
        _COMFY_OBJECT_INFO_CACHE = {}
    return _COMFY_OBJECT_INFO_CACHE

def get_comfy_combo_options(class_type: str, input_name: str) -> List[str]:
    try:
        info = get_comfy_object_info().get(class_type) or {}
        required = ((info.get("input") or {}).get("required") or {})
        optional = ((info.get("input") or {}).get("optional") or {})
        cfg = required.get(input_name, optional.get(input_name))
        if isinstance(cfg, (list, tuple)) and cfg:
            first = cfg[0]
            if isinstance(first, list):
                return [str(x) for x in first]
        if isinstance(cfg, dict) and isinstance(cfg.get("options"), list):
            return [str(x) for x in cfg.get("options")]
    except Exception:
        pass
    return []

def _wildcard_match(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())

def resolve_comfy_combo_value(class_type: str, input_name: str, current: str, exact: List[str], wild: List[str], strict=False) -> str:
    options = get_comfy_combo_options(class_type, input_name)
    if not options:
        return current
    for o in options:
        if o.lower() == str(current).lower():
            return o
    for pref in exact:
        for o in options:
            if o.lower() == pref.lower():
                return o
    for pat in wild:
        for o in options:
            if _wildcard_match(o, pat):
                return o
    if strict:
        raise RuntimeError(f"Nenalezen vhodný model pro {class_type}.{input_name}. Ve workflow je {current!r}. Dostupné: {', '.join(options[:30])}")
    return current

def repair_ltx_model_names(wf: dict) -> None:
    preferred_ckpt = ["ltx-2.3-22b-dev-fp8.safetensors", "ltx-2-19b-distilled.safetensors", "ltx-2-19b-dev-fp8.safetensors"]
    preferred_ckpt_wild = ["*ltx-2.3*dev*fp8*.safetensors", "*ltx-2.3*.safetensors", "*ltx-2*dev*fp8*.safetensors", "*ltx-2*distilled*.safetensors", "*ltx*.safetensors"]
    preferred_text = ["gemma_3_12B_it_fp4_mixed.safetensors"]
    preferred_text_wild = ["*gemma*12b*fp4*.safetensors", "*gemma*.safetensors", "*.safetensors"]
    for node_id, node in list(wf.items()):
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        cls = str(node.get("class_type") or "")
        if "ckpt_name" in inputs and isinstance(inputs.get("ckpt_name"), str):
            cur = str(inputs.get("ckpt_name") or "")
            if cls in {"CheckpointLoaderSimple", "LTXVAudioVAELoader", "LTXAVTextEncoderLoader"} and cur.lower().startswith("ltx-"):
                new = resolve_comfy_combo_value(cls, "ckpt_name", cur, preferred_ckpt, preferred_ckpt_wild, True)
                if new != cur:
                    inputs["ckpt_name"] = new
        if "text_encoder" in inputs and isinstance(inputs.get("text_encoder"), str):
            cur = str(inputs.get("text_encoder") or "")
            new = resolve_comfy_combo_value(cls, "text_encoder", cur, preferred_text, preferred_text_wild, False)
            if new != cur:
                inputs["text_encoder"] = new

# ─── Workflow patching ───────────────────────────────────────

def deep_replace(obj: Any, repl: Dict[str, Any]) -> Any:
    if isinstance(obj, dict): return {k: deep_replace(v, repl) for k, v in obj.items()}
    if isinstance(obj, list): return [deep_replace(v, repl) for v in obj]
    if isinstance(obj, str):
        if obj in repl: return repl[obj]
        s = obj
        for k, v in repl.items():
            if isinstance(v, (str, int, float)):
                s = s.replace(k, str(v))
        return s
    return obj

def workflow_contains_value(obj: Any, needle: str) -> bool:
    if isinstance(obj, dict): return any(workflow_contains_value(v, needle) for v in obj.values())
    if isinstance(obj, list): return any(workflow_contains_value(v, needle) for v in obj)
    if isinstance(obj, str): return obj == needle or needle in obj
    return False

def _get_node(wf: dict, node_id: Any) -> Optional[dict]:
    if isinstance(node_id, (list, tuple)) and node_id: node_id = node_id[0]
    return wf.get(str(node_id)) or wf.get(node_id)

def set_node_input(wf: dict, node_id: str, input_name: str, value: Any) -> bool:
    node = _get_node(wf, node_id)
    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
        node["inputs"][input_name] = value
        return True
    return False

def get_node_input(wf: dict, node_id: str, input_name: str) -> Any:
    node = _get_node(wf, node_id)
    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
        return node["inputs"].get(input_name)
    return None

def _node_title(node: dict) -> str:
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    return str(meta.get("title") or node.get("title") or "")

def _set_linked_numeric(wf: dict, link_value: Any, value: Any, label: str, patched: List[str], kind: str) -> bool:
    if not isinstance(link_value, (list, tuple)) or not link_value:
        return False
    src = _get_node(wf, link_value[0])
    if not isinstance(src, dict) or not isinstance(src.get("inputs"), dict):
        return False
    for k in ("value", "int", "integer", "number", "float"):
        if k in src["inputs"] and isinstance(src["inputs"].get(k), (int, float)):
            old = src["inputs"][k]
            src["inputs"][k] = value
            patched.append(f"{kind} linked {label} -> node {link_value[0]}.{k}: {old} -> {value}")
            return True
    return False

def auto_patch_workflow_nodes(wf: dict, values: Dict[str, Any]) -> List[str]:
    patched: List[str] = []
    new_image = str(values.get("image") or "")
    prompt = str(values.get("positive_prompt") or "")
    negative = str(values.get("negative_prompt") or "").strip()
    width = int(values.get("width") or 0); height = int(values.get("height") or 0)
    seed = int(values.get("seed") or 0); steps = int(values.get("steps") or 0)
    cfg = float(values.get("cfg") or 0); fps = int(values.get("fps") or 0)
    duration = float(values.get("duration") or 0); frame_count = int(values.get("frame_count") or 0)
    image_exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
    text_candidates: List[tuple] = []
    positive_patched = False; negative_patched = False
    for node_id, node in list(wf.items()):
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict): continue
        inputs = node["inputs"]; class_type = str(node.get("class_type") or ""); cls = class_type.lower()
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = str(meta.get("title") or node.get("title") or "").lower()
        label = f"{node_id}:{class_type}"
        is_image_loader = (("load" in cls and "image" in cls) or "load image" in title or "input image" in title or title.strip() in ("image", "input", "start image", "source image"))
        if new_image:
            for key in ("image", "image_path", "filename", "file", "path"):
                if key in inputs and isinstance(inputs.get(key), str):
                    old = inputs.get(key)
                    if is_image_loader or str(old).lower().endswith(image_exts):
                        if old != new_image:
                            inputs[key] = new_image; patched.append(f"image {label}.{key}: {old} -> {new_image}")
            for key, val in list(inputs.items()):
                if isinstance(val, str) and val.lower().endswith(image_exts) and val != new_image:
                    inputs[key] = new_image; patched.append(f"image global {label}.{key}: {val} -> {new_image}")
        text_keys = [k for k in ("text", "prompt", "caption", "positive", "negative") if k in inputs and isinstance(inputs.get(k), str)]
        if "value" in inputs and isinstance(inputs.get("value"), str):
            value_is_prompt_text = (("primitive" in cls and "string" in cls) or "string" in cls or any(x in title for x in ("prompt", "positive", "negative", "caption", "text")))
            if value_is_prompt_text and "value" not in text_keys:
                if "prompt" in title or "caption" in title or "positive" in title or "negative" in title: text_keys.insert(0, "value")
                else: text_keys.append("value")
        if text_keys:
            key = text_keys[0]; current_text = str(inputs.get(key) or "")
            is_text_node = any(x in cls for x in ("text", "prompt", "encode", "gemma", "clip", "string")) or any(x in title for x in ("prompt", "text", "caption", "positive", "negative"))
            if is_text_node:
                negative_hint = ("negative" in title or "negative" in cls or key == "negative" or any(x in current_text.lower() for x in ("low quality", "ugly", "deformed", "blur", "flicker", "watermark", "cartoon")))
                positive_hint = (not negative_hint) and ("positive" in title or key == "positive" or "prompt" in title or "caption" in title or ("primitive" in cls and "string" in cls and key == "value"))
                text_candidates.append((node_id, node, key, label, negative_hint, positive_hint))
                if negative_hint:
                    if negative:
                        old = str(inputs[key]); inputs[key] = negative; patched.append(f"negative {label}.{key}: {old[:40]!r} -> custom"); negative_patched = True
                elif positive_hint and prompt:
                    old = str(inputs[key]); inputs[key] = prompt; patched.append(f"positive {label}.{key}: {old[:40]!r} -> prompt"); positive_patched = True
        def set_num(keys, value, kind, cast_int=False):
            if not value: return False
            for k in keys:
                if k in inputs:
                    v = inputs.get(k)
                    if isinstance(v, (int, float)):
                        old = v; inputs[k] = int(value) if cast_int else value; patched.append(f"{kind} {label}.{k}: {old} -> {inputs[k]}"); return True
                    if _set_linked_numeric(wf, v, int(value) if cast_int else value, f"{label}.{k}", patched, kind): return True
            return False
        set_num(("width", "W", "w"), width, "width", True)
        set_num(("height", "H", "h"), height, "height", True)
        set_num(("seed", "noise_seed", "random_seed"), seed, "seed", True)
        if seed and "sampling_mode.seed" in inputs and isinstance(inputs.get("sampling_mode.seed"), (int, float)):
            old = inputs.get("sampling_mode.seed"); inputs["sampling_mode.seed"] = seed; patched.append(f"seed {label}.sampling_mode.seed: {old} -> {seed}")
        set_num(("steps",), steps, "steps", True)
        set_num(("cfg", "guidance", "guidance_scale"), cfg, "cfg", False)
        set_num(("fps", "frame_rate"), fps, "fps", True)
        set_num(("duration", "seconds", "sec", "length_seconds", "video_duration"), duration, "duration", False)
        set_num(("frame_count", "frames", "frames_number", "num_frames", "length", "video_length"), frame_count, "frames", True)
        if "value" in inputs and isinstance(inputs.get("value"), (int, float)):
            val = inputs.get("value")
            if (width and title in ("width", "w")) or (width and "width" in title and "height" not in title): inputs["value"] = int(width); patched.append(f"width primitive {label}.value: {val} -> {inputs['value']}")
            elif (height and title in ("height", "h")) or (height and "height" in title and "width" not in title): inputs["value"] = int(height); patched.append(f"height primitive {label}.value: {val} -> {inputs['value']}")
            elif fps and any(x in title for x in ("frame rate", "framerate", "fps")): inputs["value"] = int(fps); patched.append(f"fps primitive {label}.value: {val} -> {inputs['value']}")
            elif duration and "duration" in title: inputs["value"] = duration; patched.append(f"duration primitive {label}.value: {val} -> {inputs['value']}")
            elif frame_count and any(x in title for x in ("frame count", "frames", "num frames", "length")) and "rate" not in title: inputs["value"] = int(frame_count); patched.append(f"frames primitive {label}.value: {val} -> {inputs['value']}")
    if prompt and not positive_patched and text_candidates:
        node_id, node, key, label, neg, pos = ([c for c in text_candidates if not c[4]] or text_candidates)[0]
        old = node["inputs"][key]; node["inputs"][key] = prompt; patched.append(f"positive fallback {label}.{key}: {str(old)[:40]!r} -> prompt")
    if negative and not negative_patched and text_candidates:
        candidates = [c for c in text_candidates if c[4]]
        if candidates:
            node_id, node, key, label, neg, pos = candidates[0]
            old = node["inputs"][key]; node["inputs"][key] = negative; patched.append(f"negative fallback {label}.{key}: {str(old)[:40]!r} -> custom")
    return patched

CAMERA_PRESETS = {
    "Decentní nájezd dopředu": "the camera pushes in only slightly toward the subject in a restrained and minimal slow dolly forward, the framing tightens just a touch over the duration, smooth, stabilized and continuous",
    "Pomalý nájezd dopředu": "the camera slowly pushes in toward the subject in a smooth dolly forward, gradually tightening the framing, stabilized and continuous",
    "Pomalý odjezd dozadu": "the camera slowly pulls back from the subject in a smooth dolly out, gradually revealing more of the surrounding environment, stabilized and continuous",
    "Obíhání kolem objektu": "the camera circles slowly around the subject in a smooth orbital motion, the subject stays centered in frame, steady continuous parallax",
    "Půlkruhový oblouk": "the camera arcs around the subject in a controlled half-circle, smooth and stabilized, gradually revealing the subject from a new angle",
    "Stoupání kamery (dron nahoru)": "the camera rises upward in a smooth aerial drone movement, gradually revealing the wider landscape below, stabilized and continuous",
    "Klesání kamery (pohled dolů)": "the camera descends slowly from a high overhead view looking straight down at the scene, smooth aerial motion, stabilized",
    "Jeřáb nahoru": "the camera cranes upward in a slow controlled vertical rise, the subject remains in frame, smooth and continuous",
    "Jeřáb dolů": "the camera cranes downward in a slow controlled vertical descent, smooth and stabilized, gradually framing the subject from a lower angle",
    "Pomalý posun do strany": "the camera tracks slowly to the side in a smooth horizontal dolly parallel to the subject, stabilized and continuous",
    "Statická kamera (stativ)": "the camera holds completely still on a locked-off tripod, no camera movement, only the subject and the environment evolve over time",
    "Jemný posun (drobný drift)": "the camera drifts with very subtle, almost imperceptible motion, minimal parallax, breathing-like and stabilized",
    "Z ruky (dokumentární)": "the camera follows in a natural handheld documentary style, slight organic motion, observational and credible, lightly stabilized but not locked",
}

def join_prompt_parts_once(*parts: str) -> str:
    out: List[str] = []
    def norm(x): return " ".join(str(x or "").lower().replace(";", ",").split())
    for p in parts:
        p = str(p or "").strip().strip(",")
        if not p: continue
        if norm(p) and norm(p) in norm(", ".join(out)): continue
        out.append(p)
    return ", ".join(out)

def restore_ltx_official_image_hold(wf: dict) -> None:
    fixed_ok = set_node_input(wf, "320:288", "strength", 1.0) and set_node_input(wf, "320:296", "strength", 0.85)
    if fixed_ok:
        if _get_node(wf, "320:302") is not None:
            set_node_input(wf, "320:288", "bypass", ["320:302", 0]); set_node_input(wf, "320:296", "bypass", ["320:302", 0])
        else:
            set_node_input(wf, "320:288", "bypass", False); set_node_input(wf, "320:296", "bypass", False)
        return
    ltx_nodes = []
    for nid, node in wf.items():
        if isinstance(node, dict) and str(node.get("class_type") or "") == "LTXVImgToVideoInplace" and isinstance(node.get("inputs"), dict):
            ltx_nodes.append((str(nid), node))
    if len(ltx_nodes) >= 2:
        ltx_nodes.sort(key=lambda x: x[0])
        ltx_nodes[0][1]["inputs"]["strength"] = 1.0
        ltx_nodes[1][1]["inputs"]["strength"] = 0.85
        for _, node in ltx_nodes[:2]:
            if node["inputs"].get("bypass") is True: node["inputs"]["bypass"] = False

def assert_ltx_frame_hold_protected(wf: dict, comfy_image_name: str) -> None:
    if not (_get_node(wf, "320:288") and _get_node(wf, "320:296")):
        return
    if comfy_image_name and not workflow_contains_value(wf, comfy_image_name):
        raise RuntimeError(f"LTX ochrana: vstupní obrázek není ve workflow. Comfy image={comfy_image_name}")
    s1 = get_node_input(wf, "320:288", "strength"); s2 = get_node_input(wf, "320:296", "strength")
    try:
        if not (float(s1) == 1.0 and abs(float(s2) - 0.85) < 0.001):
            raise ValueError()
    except Exception:
        raise RuntimeError(f"LTX ochrana: image-hold strength byl přepsán. 320:288={s1} 320:296={s2}")

def patch_ltx_prompt_enhance(wf: dict, enable: bool, tokens: int, seed: int = 0) -> List[str]:
    patched: List[str] = []
    tokens = max(64, min(512, int(tokens or 128)))
    for node_id, node in wf.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict): continue
        cls = str(node.get("class_type") or ""); title = _node_title(node).lower(); inputs = node["inputs"]
        if cls == "TextGenerateLTX2Prompt":
            old = inputs.get("max_length"); inputs["max_length"] = tokens; patched.append(f"prompt tokens {node_id}: {old}->{tokens}")
            if seed and "sampling_mode.seed" in inputs and isinstance(inputs.get("sampling_mode.seed"), (int, float)):
                inputs["sampling_mode.seed"] = int(seed)
        if cls == "PrimitiveBoolean" and "value" in inputs:
            if "prompt enhance" in title or ("enhance" in title and "prompt" in title):
                old = inputs.get("value"); inputs["value"] = bool(enable); patched.append(f"prompt enhance {node_id}: {old}->{enable}")
    return patched

def set_flf2v_images(wf: dict, first_image: str, last_image: str) -> None:
    if _get_node(wf, "31") and _get_node(wf, "39"):
        set_node_input(wf, "31", "image", first_image); set_node_input(wf, "39", "image", last_image); return
    load_nodes = []
    for node_id, node in wf.items():
        if isinstance(node, dict) and str(node.get("class_type") or "").lower() == "loadimage":
            load_nodes.append(str(node_id))
    if len(load_nodes) >= 2:
        set_node_input(wf, load_nodes[0], "image", first_image); set_node_input(wf, load_nodes[1], "image", last_image); return
    raise RuntimeError("2 PICT workflow nemá dva LoadImage nody pro první a poslední frejm.")

def sanitize_workflow(wf: Any, source: str) -> dict:
    if isinstance(wf, dict):
        wf = dict(wf)
        for k in list(wf.keys()):
            if str(k).startswith("_"): wf.pop(k, None)
    if not isinstance(wf, dict) or not wf:
        raise ValueError("Workflow JSON je prázdný nebo neplatný: " + source)
    if "nodes" in wf and "links" in wf:
        raise RuntimeError("Soubor je UI workflow, ne API workflow. Exportuj z ComfyUI přes Save (API format).")
    return wf

def load_workflow(input_mode: str) -> dict:
    cfg = load_config()
    rel = cfg.get("flf2v_workflow") if input_mode == "2pict" else cfg.get("i2v_workflow")
    path = ROOT / str(rel or "")
    with path.open("r", encoding="utf-8") as f:
        return sanitize_workflow(json.load(f), str(path))

def node_stage_label(wf: Optional[dict], node_id: str) -> str:
    node = _get_node(wf or {}, node_id) if wf else None
    cls = str((node or {}).get("class_type") or ""); title = _node_title(node or {})
    if not node_id: return "Čekám na Comfy"
    import re
    def has(p): return re.search(p, cls or "") is not None
    if cls == "SaveVideo": return "Ukládám video"
    if cls == "CreateVideo": return "Skládám video"
    if has("VAEDecode|AudioVAEDecode|SeparateAVLatent|CropGuides|LatentUpsampler"): return "Dekóduji výstup"
    if has("SamplerCustomAdvanced|KSampler|SamplerEuler|ManualSigmas|RandomNoise|CFGGuider"): return "Generuji snímky"
    if has("ImgToVideo|AddGuide|EmptyLatent|EmptyLTXV|ConcatAVLatent|LTXVConditioning"): return "Připravuji latent"
    if has("CLIPTextEncode|TextGenerate|PrimitiveString|ComfySwitch"): return "Kóduji prompt"
    if has("Preprocess|Resize|GetImageSize|LoadImage"): return "Zpracovávám obrázek"
    if has("Checkpoint|TextEncoder|AudioVAELoader|LoraLoader|ModelLoader"): return "Načítám model"
    return title or cls or str(node_id)

def build_workflow(job: dict, comfy_img: str, comfy_img2: Optional[str]) -> dict:
    s = job.get("settings") or {}
    fps = safe_int(s.get("fps"), 25); duration = safe_float(s.get("duration"), 5.0)
    frame_count = safe_int(s.get("frame_count"), max(1, round(fps * duration)))
    width = safe_int(s.get("width"), 1280); height = safe_int(s.get("height"), 720)
    seed = safe_int(s.get("seed"), 1); steps = safe_int(s.get("steps"), 30)
    cfg = safe_float(s.get("cfg"), 3.5); motion = safe_float(s.get("motion_strength"), 0.75)
    camera_motion = str(s.get("camera_motion") or "").strip() or CAMERA_PRESETS.get(str(job.get("preset") or "Statická kamera (stativ)"), "")
    style = str(s.get("style") or "").strip()
    user_prompt = str(job.get("prompt") or "").strip()
    tech_quality = "smooth motion, stable footage, sharp details, high quality, natural motion blur, 180-degree shutter"
    prompt = join_prompt_parts_once(user_prompt, camera_motion, style, tech_quality)
    negative = str(job.get("negative_prompt") or "").strip()
    input_mode = str(s.get("input_mode") or "1pict").lower()
    is_two = bool(comfy_img2) or input_mode in ("2pict", "2 pict", "flf2v")
    wf = load_workflow("2pict" if is_two else "1pict")
    repl = {
        "__POSITIVE_PROMPT__": prompt, "__NEGATIVE_PROMPT__": negative, "__IMAGE_FILENAME__": comfy_img,
        "__WIDTH__": width, "__HEIGHT__": height, "__FPS__": fps, "__DURATION__": duration,
        "__FRAME_COUNT__": frame_count, "__SEED__": seed, "__STEPS__": steps,
        "__CFG__": cfg, "__GUIDANCE__": cfg, "__MOTION_STRENGTH__": motion,
        "__CAMERA_MOTION__": camera_motion, "__OUTPUT_PREFIX__": f"pz_local_{job['id']}"
    }
    wf = deep_replace(wf, repl)
    values = {"positive_prompt": prompt, "negative_prompt": negative, "image": comfy_img, "width": width, "height": height, "fps": fps, "duration": duration, "frame_count": frame_count, "seed": seed, "steps": steps, "cfg": cfg, "motion_strength": motion, "camera_motion": camera_motion, "output_prefix": f"pz_local_{job['id']}"}
    if is_two:
        values["image"] = ""
    patched = auto_patch_workflow_nodes(wf, values)
    if is_two:
        if not comfy_img2: raise RuntimeError("2 PICT potřebuje druhý obrázek / poslední frejm.")
        set_flf2v_images(wf, comfy_img, comfy_img2)
        if not workflow_contains_value(wf, comfy_img) or not workflow_contains_value(wf, comfy_img2):
            raise RuntimeError("2 PICT ochrana: první nebo poslední frejm se nedostal do workflow.")
    else:
        restore_ltx_official_image_hold(wf)
        if comfy_img and not workflow_contains_value(wf, comfy_img):
            raise RuntimeError("Nový obrázek se nepodařilo vložit do workflow.")
        assert_ltx_frame_hold_protected(wf, comfy_img)
    enh = patch_ltx_prompt_enhance(wf, bool(s.get("prompt_enhance", False)), safe_int(s.get("enhance_tokens"), 128), seed)
    repair_ltx_model_names(wf)
    add_event(int(job["id"]), "Workflow patch: " + ("; ".join((patched + enh)[:10]) or "OK"), "workflow")
    return wf

# ─── Render / monitoring ─────────────────────────────────────

def submit_prompt(workflow: dict, client_id: str) -> str:
    r = comfy_post("/prompt", json={"prompt": workflow, "client_id": client_id}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"ComfyUI /prompt HTTP {r.status_code}: {r.text[:2000]}")
    data = r.json()
    pid = data.get("prompt_id")
    if not pid: raise RuntimeError(f"ComfyUI nevrátil prompt_id: {data}")
    return str(pid)

def interrupt_comfy() -> None:
    try: comfy_post("/interrupt", timeout=5)
    except Exception: pass

def get_history(prompt_id: str, allow_empty=True) -> Optional[dict]:
    r = comfy_get("/history/" + quote(prompt_id), timeout=30)
    if r.status_code >= 400: r.raise_for_status()
    data = r.json()
    if prompt_id in data: return data[prompt_id]
    return None if allow_empty else (_ for _ in ()).throw(RuntimeError(f"History neobsahuje {prompt_id}"))

def extract_history_error(history: dict) -> str:
    status = history.get("status") if isinstance(history, dict) else None
    if not isinstance(status, dict): return ""
    msgs = status.get("messages") or []
    for msg in reversed(msgs if isinstance(msgs, list) else []):
        typ, data = "", None
        if isinstance(msg, (list, tuple)) and len(msg) >= 2: typ, data = str(msg[0] or ""), msg[1]
        elif isinstance(msg, dict): typ, data = str(msg.get("type") or ""), msg.get("data") or msg
        if typ not in ("execution_error", "execution_interrupted", "error"): continue
        if isinstance(data, dict):
            node = data.get("node_id") or data.get("node"); cls = data.get("class_type") or ""; exc = data.get("exception_message") or data.get("message") or data.get("exception_type") or ""
            return f"{typ} na node {node or '?'} {cls}: {exc}"[:1800]
        return f"{typ}: {data}"[:1800]
    if status.get("completed") is False or str(status.get("status_str") or "").lower() in ("error", "failed", "interrupted"):
        return f"status={status.get('status_str')}, completed={status.get('completed')}"
    return ""

def raise_if_history_failed(history: dict) -> None:
    err = extract_history_error(history)
    if err: raise RuntimeError("ComfyUI render spadl: " + err)

def find_output_files(history: dict) -> List[dict]:
    outputs = history.get("outputs") or {}
    found: List[dict] = []
    video_ext = {"mp4", "webm", "mov", "mkv", "gif"}
    for node_id, out in outputs.items():
        for bucket in ("videos", "gifs", "images"):
            for item in out.get(bucket, []) or []:
                fn = item.get("filename") or ""
                ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                if bucket in ("videos", "gifs") or ext in video_ext:
                    found.append({"filename": fn, "subfolder": item.get("subfolder", ""), "type": item.get("type", "output"), "bucket": bucket, "node_id": node_id})
    return found

def download_comfy_output(item: dict, dst_dir: Path) -> Path:
    params = {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")}
    url = comfy_base() + "/view?" + urlencode(params)
    clean = sanitize_filename(item["filename"], ".mp4")
    dst = dst_dir / f"{uuid.uuid4().hex[:8]}_{clean}"
    with requests.get(url, stream=True, timeout=900) as r:
        r.raise_for_status()
        with dst.open("wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk: f.write(chunk)
    return dst

def check_cancel(job_id: int) -> None:
    if int(job_id) in cancel_flags:
        interrupt_comfy()
        raise RuntimeError("Zrušeno uživatelem")

def watch_prompt(job_id: int, prompt_id: str, client_id: str, workflow: dict) -> None:
    if websocket is not None:
        try:
            return watch_prompt_ws(job_id, prompt_id, client_id, workflow)
        except Exception as e:
            add_event(job_id, f"WebSocket nedostupný, přecházím na polling: {e}", "warn")
    return watch_prompt_poll(job_id, prompt_id, workflow)

def watch_prompt_ws(job_id: int, prompt_id: str, client_id: str, workflow: dict) -> None:
    ws_url = comfy_base().replace("http://", "ws://").replace("https://", "wss://") + "/ws?" + urlencode({"clientId": client_id})
    ws = websocket.create_connection(ws_url, timeout=8)
    ws.settimeout(1)
    last_hist = 0.0
    try:
        set_progress(job_id, "generating", 8, "ComfyUI generuje", "queued")
        while True:
            check_cancel(job_id)
            try:
                raw = ws.recv()
            except Exception:
                raw = None
            if raw:
                try:
                    msg = json.loads(raw) if isinstance(raw, str) else None
                except Exception:
                    msg = None
                if isinstance(msg, dict):
                    typ = msg.get("type"); data = msg.get("data") or {}
                    if typ == "progress":
                        value = safe_float(data.get("value"), 0); maxv = max(1.0, safe_float(data.get("max"), 1))
                        pct = 10 + int(min(1.0, value / maxv) * 82)
                        set_progress(job_id, "generating", pct, f"Generuji snímky {int(value)}/{int(maxv)}", "progress")
                    elif typ == "executing" and data.get("prompt_id") == prompt_id:
                        node = data.get("node")
                        if node is None:
                            set_progress(job_id, "downloading", 94, "ComfyUI dokončilo render, načítám výsledek", "done")
                            return
                        label = node_stage_label(workflow, str(node))
                        set_progress(job_id, "generating", max(10, get_job(job_id).get("progress", 10)), label, str(node))
                    elif typ in ("execution_error", "execution_interrupted"):
                        raise RuntimeError(f"ComfyUI {typ}: {data}")
            if time.time() - last_hist > 5:
                hist = get_history(prompt_id, allow_empty=True)
                if hist:
                    raise_if_history_failed(hist)
                    set_progress(job_id, "downloading", 94, "ComfyUI dokončilo render, načítám výsledek", "history")
                    return
                last_hist = time.time()
    finally:
        try: ws.close()
        except Exception: pass

def watch_prompt_poll(job_id: int, prompt_id: str, workflow: dict) -> None:
    progress = 10
    set_progress(job_id, "generating", progress, "ComfyUI generuje / čekám na history", "poll")
    while True:
        check_cancel(job_id)
        hist = get_history(prompt_id, allow_empty=True)
        if hist:
            raise_if_history_failed(hist)
            set_progress(job_id, "downloading", 94, "ComfyUI dokončilo render, načítám výsledek", "history")
            return
        progress = min(92, progress + 1)
        set_progress(job_id, "generating", progress, "ComfyUI generuje / polling", "poll")
        time.sleep(8)

def process_job(job: dict) -> None:
    job_id = int(job["id"])
    work_dir = TMP / f"job_{job_id}_{uuid.uuid4().hex[:6]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        ok, msg = comfy_online()
        if not ok:
            raise RuntimeError(f"ComfyUI není dostupné na {comfy_base()}: {msg}")
        set_progress(job_id, "processing", 2, "Připravuji vstupní obrázek", "input")
        input_path = ROOT / str(job.get("input_path") or "")
        if not input_path.exists(): raise RuntimeError("Vstupní obrázek neexistuje")
        set_progress(job_id, "uploading", 5, f"Nahrávám obrázek do ComfyUI: {input_path.name}", "upload_image")
        comfy_img = upload_image_to_comfy(input_path)
        comfy_img2 = None
        if job.get("input2_path"):
            p2 = ROOT / str(job.get("input2_path"))
            if not p2.exists(): raise RuntimeError("Druhý obrázek neexistuje")
            set_progress(job_id, "uploading", 6, f"Nahrávám poslední frejm do ComfyUI: {p2.name}", "upload_image2")
            comfy_img2 = upload_image_to_comfy(p2)
        set_progress(job_id, "queued", 7, "Sestavuji workflow", "workflow")
        wf = build_workflow(job, comfy_img, comfy_img2)
        client_id = str(uuid.uuid4())
        prompt_id = submit_prompt(wf, client_id)
        active_prompt_by_job[job_id] = prompt_id
        update_job(job_id, comfy_prompt_id=prompt_id)
        set_progress(job_id, "queued", 8, f"Odesláno do ComfyUI: {prompt_id}", "queued")
        watch_prompt(job_id, prompt_id, client_id, wf)
        hist = get_history(prompt_id, allow_empty=False)
        raise_if_history_failed(hist)
        outputs = find_output_files(hist)
        if not outputs:
            raise RuntimeError("V ComfyUI history není video výstup. Zkontroluj SaveVideo/CreateVideo ve workflow.")
        set_progress(job_id, "downloading", 95, f"Stahuji výsledek z ComfyUI ({len(outputs)} souborů)", "download")
        chosen: Optional[Path] = None
        for item in outputs:
            out = download_comfy_output(item, OUTPUTS)
            if out.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv", ".gif"):
                chosen = out; break
        if chosen is None:
            chosen = download_comfy_output(outputs[0], OUTPUTS)
        set_progress(job_id, "done", 100, "Hotovo – výsledek je uložen lokálně", "done")
        update_job(job_id, output_path=rel_path(chosen), status="done", progress=100)
    except Exception as e:
        err = str(e)
        if "Zrušeno uživatelem" in err:
            set_progress(job_id, "cancelled", 0, "Job zrušen", "cancelled")
            update_job(job_id, error="Zrušeno uživatelem")
        else:
            log_line("ERROR job #%s: %s\n%s" % (job_id, err, traceback.format_exc()))
            update_job(job_id, status="error", error=err, message=err, current_node="error")
            add_event(job_id, err, "error")
    finally:
        active_prompt_by_job.pop(job_id, None)
        cancel_flags.discard(job_id)
        shutil.rmtree(work_dir, ignore_errors=True)

def worker_loop() -> None:
    while True:
        try:
            with processing_lock:
                job = None
                with jobs_lock:
                    jobs = load_jobs()
                    for j in jobs:
                        if j.get("status") == "pending":
                            j["status"] = "processing"; j["progress"] = 1; j["message"] = "Job převzat lokální frontou"; j["updated_at"] = now_iso()
                            job = dict(j)
                            save_jobs(jobs)
                            break
                if job:
                    process_job(job)
            time.sleep(1)
        except Exception as e:
            log_line(f"worker_loop error: {e}")
            time.sleep(3)

# ─── Translation ─────────────────────────────────────────────

def translate_google_gtx(text: str, source="cs", target="en") -> str:
    text = str(text or "").strip()
    if not text: return ""
    url = "https://translate.googleapis.com/translate_a/single?" + urlencode({"client":"gtx", "sl":source, "tl":target, "dt":"t", "q":text})
    r = requests.get(url, timeout=12, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    return "".join(part[0] for part in data[0] if part and part[0]).strip() or text

# ─── HTTP API ────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "PZComfyWLocal/1.0"

    def log_message(self, fmt, *args):
        log_line("HTTP " + (fmt % args))

    def send_json(self, data: Any, status=200):
        b = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def send_text(self, text: str, status=200, ctype="text/plain; charset=utf-8"):
        b = text.encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0: return {}
        raw = self.rfile.read(n)
        try: return json.loads(raw.decode("utf-8"))
        except Exception: return {}

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path == "/" or path == "/index.html" or path == "/app.php" or path == "/comfy.php":
                return self.serve_file(ROOT / "web" / "index.html")
            if path == "/api.php":
                return self.ftp_api_get(qs)
            if path.startswith("/api/"):
                return self.handle_api_get(path[5:], qs)
            if path.startswith("/file/input/"):
                name = Path(unquote(path.split("/file/input/",1)[1])).name
                return self.serve_file(INPUTS / name, download=False)
            if path.startswith("/file/output/"):
                name = Path(unquote(path.split("/file/output/",1)[1])).name
                return self.serve_file(OUTPUTS / name, download=False)
            if path.startswith("/web/"):
                rel = path.split("/web/",1)[1]
                return self.serve_file(ROOT / "web" / rel)
            return self.send_text("Not found", 404)
        except Exception as e:
            return self.send_json({"success":False,"error":str(e)},500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path == "/api.php":
                return self.ftp_api_post(qs)
            if path.startswith("/api/"):
                return self.handle_api_post(path[5:], qs)
            return self.send_text("Not found", 404)
        except Exception as e:
            log_line(traceback.format_exc())
            return self.send_json({"success":False,"error":str(e)},500)

    # ── api.php kompatibilní endpointy (FTP UI) ──
    def ftp_api_get(self, qs: dict):
        action = str((qs.get("action") or [""])[0])
        if action in ("me", "has_users"):
            return self.send_json({"success": True, "authenticated": True, "has_users": True,
                                   "title": "PZ COMFY VIDEO REMOTE", "role": "admin", "username": "local",
                                   "user_id": 1, "is_admin": True, "csrf": "local"})
        if action == "projects":
            cfg = load_config()
            return self.send_json({"success": True, "projects": [
                {"id": 1, "name": "LTX 2.3 nový model i2v / 1 PICT",
                 "description": "LTX 2.3 image-to-video workflow pro jednu vstupní fotku.",
                 "input_type": "image", "workflow_file": str(cfg.get("i2v_workflow") or ""), "active": 1, "sort_order": 1},
                {"id": 2, "name": "LTX 2.3 první + poslední frejm / 2 PICT",
                 "description": "LTX 2.3 FLF2V workflow pro první a poslední frejm / dvě vstupní fotky.",
                 "input_type": "image", "workflow_file": str(cfg.get("flf2v_workflow") or ""), "active": 1, "sort_order": 2},
            ]})
        if action in ("dashboard", "dashboard_cached"):
            status = str((qs.get("status") or [""])[0])
            limit = safe_int((qs.get("limit") or [200])[0], 200)
            detail_id = safe_int((qs.get("detail_id") or [0])[0], 0)
            return self.send_json(ftp_dashboard_payload(status, limit, detail_id))
        if action == "jobs":
            with jobs_lock:
                all_jobs = load_jobs()
            rows = sorted(all_jobs, key=lambda x: int(x.get("id", 0)), reverse=True)
            return self.send_json({"success": True, "jobs": [ftp_job_public(j) for j in rows],
                                   "queue_counts": ftp_queue_counts(all_jobs)})
        if action == "job_detail":
            jid = safe_int((qs.get("id") or [0])[0])
            j = get_job(jid)
            if not j:
                return self.send_json({"success": False, "error": "Job nenalezen."}, 404)
            return self.send_json({"success": True, "job": ftp_job_public(j), "events": ftp_events(j)})
        if action == "job_file":
            return self.ftp_send_job_file(qs)
        if action == "stats":
            with jobs_lock:
                all_jobs = load_jobs()
            return self.send_json({"success": True, "data": None, "workers": ftp_workers_state(),
                                   "queue_counts": ftp_queue_counts(all_jobs)})
        if action == "diagnostics":
            d = self.diagnostics()
            d["workers"] = [{"id": LOCAL_WORKER_ID, "version": LOCAL_WORKER_VERSION, "state": "online"}]
            return self.send_json(d)
        if action in ("default_workflow", "project_workflow"):
            cfg = load_config()
            rel = cfg.get("i2v_workflow")
            if action == "project_workflow" and safe_int((qs.get("id") or [0])[0]) == 2:
                rel = cfg.get("flf2v_workflow")
            p = ROOT / str(rel or "")
            if not p.is_file():
                return self.send_json({"success": False, "error": "Workflow neexistuje."}, 404)
            return self.serve_file(p)
        if action == "logout":
            return self.send_json({"success": True})
        return self.send_json({"success": False, "error": "Neznámá akce: " + action}, 404)

    def ftp_send_job_file(self, qs: dict):
        jid = safe_int((qs.get("id") or [0])[0])
        kind = str((qs.get("kind") or ["output"])[0])
        j = get_job(jid)
        if not j:
            return self.send_text("Job nenalezen", 404)
        rel = {"input": j.get("input_path"), "input2": j.get("input2_path"), "output": j.get("output_path")}.get(kind)
        if not rel:
            return self.send_text("Soubor není k dispozici", 404)
        return self.serve_file(ROOT / str(rel), download=False)

    def read_multipart(self) -> Dict[str, List[Any]]:
        content_type = self.headers.get("Content-Type", "")
        n = int(self.headers.get("Content-Length", "0") or "0")
        if "multipart/form-data" not in content_type.lower() or n <= 0:
            raise ValueError("Čekám multipart/form-data.")
        return parse_multipart_body(content_type, self.rfile.read(n))

    @staticmethod
    def _field(fields: Dict[str, List[Any]], name: str, default: str = "") -> str:
        v = (fields.get(name) or [default])[0]
        if isinstance(v, UploadPart):
            return v.data.decode("utf-8", errors="replace")
        return str(v if v is not None else default)

    def ftp_api_post(self, qs: dict):
        action = str((qs.get("action") or [""])[0])
        if action == "login":
            return self.send_json({"success": True})
        if action == "logout":
            return self.send_json({"success": True})
        if action == "translate_prompt":
            body = self.read_json()
            text = str(body.get("text") or "")
            tr, ok, prov = _ftp_translate(text)
            return self.send_json({"success": True, "translated": tr if ok else "",
                                   "provider": prov or "none"})
        if action in ("create_job", "create_jobs_batch"):
            fields = self.read_multipart()
            prompt = self._field(fields, "prompt").strip()
            if not prompt:
                return self.send_json({"success": False, "error": "Prompt je prázdný."}, 400)
            negative = self._field(fields, "negative_prompt").strip()
            preset = self._field(fields, "preset", "Statická kamera (stativ)")
            if action == "create_job":
                try:
                    settings = json.loads(self._field(fields, "settings_json", "{}")) or {}
                except Exception:
                    settings = {}
                imgs = [x for x in (fields.get("image") or []) if isinstance(x, UploadPart)]
                img2s = [x for x in (fields.get("image2") or []) if isinstance(x, UploadPart) and x.filename]
                if not imgs:
                    return self.send_json({"success": False, "error": "Chybí vstupní obrázek."}, 400)
                job = ftp_create_one(prompt, negative, preset, settings, imgs[0], img2s[0] if img2s else None)
                return self.send_json({"success": True, "id": job["id"], "job": ftp_job_public(job)})
            # create_jobs_batch
            try:
                settings_list = json.loads(self._field(fields, "settings_jsons", "[]")) or []
            except Exception:
                settings_list = []
            files = [x for x in (fields.get("images") or []) + (fields.get("images[]") or []) if isinstance(x, UploadPart)]
            if not files:
                return self.send_json({"success": False, "error": "Chybí vstupní obrázky."}, 400)
            created, failed = [], []
            for i, f in enumerate(files):
                try:
                    s = settings_list[i] if i < len(settings_list) and isinstance(settings_list[i], dict) else {}
                    job = ftp_create_one(prompt, negative, preset, s, f, None)
                    created.append({"id": job["id"], "name": f.filename})
                except Exception as e:
                    failed.append({"name": f.filename, "error": str(e)})
            return self.send_json({"success": len(created) > 0, "created": created,
                                   "ids": [c["id"] for c in created], "failed": failed,
                                   "created_count": len(created), "failed_count": len(failed)})
        if action == "cancel_job":
            body = self.read_json()
            jid = safe_int(body.get("id"))
            if not jid:
                return self.send_json({"success": False, "error": "ID chybí."}, 400)
            cancel_flags.add(jid)
            if jid in active_prompt_by_job:
                interrupt_comfy()
            j = get_job(jid)
            if j and j.get("status") == "pending":
                update_job(jid, status="cancelled", error="Zrušeno uživatelem", message="Job zrušen")
            add_event(jid, "Job zrušen uživatelem", "cancel")
            return self.send_json({"success": True})
        if action == "delete_job":
            body = self.read_json()
            jid = safe_int(body.get("id"))
            deleted = 0
            with jobs_lock:
                jobs = load_jobs(); keep = []
                for j in jobs:
                    if int(j.get("id", 0)) == jid:
                        deleted += 1
                        for key in ("input_path", "input2_path", "output_path"):
                            if j.get(key):
                                try: (ROOT / str(j[key])).unlink(missing_ok=True)
                                except Exception: pass
                    else:
                        keep.append(j)
                save_jobs(keep)
            return self.send_json({"success": True, "deleted": deleted})
        if action == "clear_finished":
            with jobs_lock:
                jobs = load_jobs(); keep = []; deleted = 0
                for j in jobs:
                    if j.get("status") in ("done", "error", "cancelled"):
                        deleted += 1
                        for key in ("input_path", "input2_path", "output_path"):
                            if j.get(key):
                                try: (ROOT / str(j[key])).unlink(missing_ok=True)
                                except Exception: pass
                    else:
                        keep.append(j)
                save_jobs(keep)
            return self.send_json({"success": True, "deleted": deleted})
        if action == "rerun_job":
            body = self.read_json()
            jid = safe_int(body.get("id"))
            new_seed = bool(body.get("new_seed", True))
            src = get_job(jid)
            if not src:
                return self.send_json({"success": False, "error": "Zdrojový job nenalezen."}, 404)
            src_in = ROOT / str(src.get("input_path") or "")
            if not src_in.is_file():
                return self.send_json({"success": False, "error": "Původní obrázek už neexistuje."}, 400)
            s = dict(src.get("settings") or {})
            if new_seed:
                s["seed"] = int.from_bytes(os.urandom(4), "big") % 2147483647 or 1
            with jobs_lock:
                jobs = load_jobs()
                nid = next_job_id(jobs)
                new_in = INPUTS / f"job_{nid}_{uuid.uuid4().hex[:8]}_{src_in.name.split('_', 3)[-1]}"
                shutil.copyfile(src_in, new_in)
                new_in2_rel = None
                if src.get("input2_path"):
                    src_in2 = ROOT / str(src["input2_path"])
                    if src_in2.is_file():
                        new_in2 = INPUTS / f"job_{nid}_last_{uuid.uuid4().hex[:8]}_{src_in2.name.split('_', 4)[-1]}"
                        shutil.copyfile(src_in2, new_in2)
                        new_in2_rel = rel_path(new_in2)
                job = dict(src)
                job.update(id=nid, status="pending", progress=0, created_at=now_iso(), updated_at=now_iso(),
                           settings=s, input_path=rel_path(new_in), input2_path=new_in2_rel,
                           output_path=None, message="Čeká ve frontě (rerun)", error="", current_node="",
                           comfy_prompt_id=None,
                           events=[{"time": now_iso(), "kind": "create", "message": f"Rerun jobu #{jid}"}])
                jobs.append(job)
                save_jobs(jobs)
            return self.send_json({"success": True, "id": nid, "new_seed": s.get("seed")})
        if action == "update_pending_job":
            body = self.read_json()
            jid = safe_int(body.get("id"))
            j = get_job(jid)
            if not j:
                return self.send_json({"success": False, "error": "Job nenalezen."}, 404)
            if str(j.get("status")) != "pending":
                return self.send_json({"success": False, "error": "Editovat lze jen pending job."}, 409)
            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                return self.send_json({"success": False, "error": "Prompt je prázdný."}, 400)
            negative = str(body.get("negative_prompt") or "").strip()
            preset = str(body.get("preset") or j.get("preset") or "custom")
            s = dict(j.get("settings") or {})
            incoming = body.get("settings") if isinstance(body.get("settings"), dict) else {}
            s.update(incoming)
            s = _ftp_normalize_settings(s, preset)
            update_job(jid, prompt=prompt, negative_prompt=negative, preset=preset, settings=s,
                       message="Pending job upraven")
            add_event(jid, "Pending job upraven", "update")
            return self.send_json({"success": True, "job": ftp_job_public(get_job(jid) or {})})
        if action == "update_pending_image":
            fields = self.read_multipart()
            jid = safe_int(self._field(fields, "id"))
            j = get_job(jid)
            if not j:
                return self.send_json({"success": False, "error": "Job nenalezen."}, 404)
            if str(j.get("status")) != "pending":
                return self.send_json({"success": False, "error": "Fotku lze změnit jen u pending jobu."}, 409)
            imgs = [x for x in (fields.get("image") or []) if isinstance(x, UploadPart)]
            if not imgs:
                return self.send_json({"success": False, "error": "Nový obrázek se nenahrál."}, 400)
            old = j.get("input_path")
            dst = _ftp_save_upload(imgs[0], f"job_{jid}_replace")
            update_job(jid, input_path=rel_path(dst))
            if old:
                try: (ROOT / str(old)).unlink(missing_ok=True)
                except Exception: pass
            add_event(jid, "Vstupní obrázek vyměněn", "update")
            return self.send_json({"success": True})
        if action in ("request_comfy_start", "request_comfyui_start", "start_comfy"):
            ps1 = ROOT / "tools" / "START_COMFY_SAFE.ps1"
            if ps1.is_file() and sys.platform.startswith("win"):
                try:
                    import subprocess
                    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                                     creationflags=0x08000008)  # DETACHED + NO_WINDOW
                    return self.send_json({"success": True, "message": "Startuji ComfyUI lokálně (START_COMFY_SAFE.ps1)."})
                except Exception as e:
                    return self.send_json({"success": False, "error": f"Start ComfyUI selhal: {e}"})
            return self.send_json({"success": False, "error": "tools/START_COMFY_SAFE.ps1 nenalezen."})
        if action == "request_worker_restart":
            return self.send_json({"success": True, "message": "Lokální režim nemá vzdálený worker — vše běží v tomto okně."})
        if action in ("cleanup_uploads", "worker_control", "sync_stats", "worker_ping"):
            return self.send_json({"success": True})
        return self.send_json({"success": False, "error": "Neznámá akce: " + action}, 404)

    def serve_file(self, path: Path, download=False):
        path = path.resolve()
        try:
            root_ok = str(path).startswith(str(ROOT.resolve()))
            if not root_ok or not path.exists() or not path.is_file():
                return self.send_text("Not found", 404)
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(path.stat().st_size))
            if download:
                self.send_header("Content-Disposition", f"attachment; filename={path.name}")
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except BrokenPipeError:
            pass

    def handle_api_get(self, action: str, qs: dict):
        if action == "config":
            cfg = load_config(); ok,msg = comfy_online()
            return self.send_json({"success":True,"config":cfg,"comfy_online":ok,"comfy_message":msg,"version":APP_VERSION})
        if action == "jobs":
            with jobs_lock:
                jobs = [public_job(j) for j in sorted(load_jobs(), key=lambda x:int(x.get("id",0)), reverse=True)]
            return self.send_json({"success":True,"jobs":jobs})
        if action == "job":
            job_id = safe_int((qs.get("id") or [0])[0])
            j = get_job(job_id)
            return self.send_json({"success":bool(j),"job":public_job(j) if j else None,"error":None if j else "Job nenalezen"}, 200 if j else 404)
        if action == "diagnostics":
            return self.send_json(self.diagnostics())
        if action == "stats":
            return self.send_json(self.stats())
        if action == "open_outputs":
            try:
                if sys.platform.startswith("win"): os.startfile(str(OUTPUTS.resolve()))  # type: ignore
                elif sys.platform == "darwin": os.system(f"open {quote(str(OUTPUTS.resolve()))}")
                else: os.system(f"xdg-open {quote(str(OUTPUTS.resolve()))}")
                return self.send_json({"success":True})
            except Exception as e:
                return self.send_json({"success":False,"error":str(e)})
        return self.send_json({"success":False,"error":"Neznámá akce"},404)

    def handle_api_post(self, action: str, qs: dict):
        if action == "config":
            data = self.read_json(); cfg = load_config()
            if "comfy_base" in data:
                cfg["comfy_base"] = str(data["comfy_base"]).strip().rstrip("/") or cfg["comfy_base"]
            save_config(cfg)
            return self.send_json({"success":True,"config":cfg})
        if action == "translate":
            data = self.read_json(); text = str(data.get("text") or "")
            try:
                tr = translate_google_gtx(text, data.get("source") or "cs", data.get("target") or "en")
                return self.send_json({"success":True,"translated":tr})
            except Exception as e:
                return self.send_json({"success":False,"error":str(e),"translated":text})
        if action == "create":
            return self.create_jobs_multipart()
        if action == "delete":
            data = self.read_json(); ids = data.get("ids") or [data.get("id")]
            ids = [safe_int(x) for x in ids if safe_int(x)]
            deleted = 0
            with jobs_lock:
                jobs = load_jobs(); keep=[]
                for j in jobs:
                    if int(j.get("id",0)) in ids:
                        deleted += 1
                        for key in ("input_path","input2_path","output_path"):
                            if j.get(key):
                                try: (ROOT / str(j[key])).unlink(missing_ok=True)
                                except Exception: pass
                    else: keep.append(j)
                save_jobs(keep)
            return self.send_json({"success":True,"deleted":deleted})
        if action == "cancel":
            data = self.read_json(); ids = data.get("ids") or [data.get("id")]
            ids = [safe_int(x) for x in ids if safe_int(x)]
            for i in ids:
                cancel_flags.add(i)
                if i in active_prompt_by_job: interrupt_comfy()
                j = get_job(i)
                if j and j.get("status") == "pending": update_job(i, status="cancelled", error="Zrušeno uživatelem", message="Zrušeno")
            return self.send_json({"success":True,"cancelled":ids})
        if action == "clear_finished":
            with jobs_lock:
                jobs = load_jobs(); keep=[]; deleted=0
                for j in jobs:
                    if j.get("status") in ("done","error","cancelled"):
                        deleted += 1
                        for key in ("input_path","input2_path","output_path"):
                            if j.get(key):
                                try: (ROOT / str(j[key])).unlink(missing_ok=True)
                                except Exception: pass
                    else: keep.append(j)
                save_jobs(keep)
            return self.send_json({"success":True,"deleted":deleted})
        return self.send_json({"success":False,"error":"Neznámá akce"},404)

    def create_jobs_multipart(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type.lower():
            return self.send_json({"success":False,"error":"Čekám multipart/form-data"},400)
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return self.send_json({"success":False,"error":"Prázdný upload."},400)
        fields = parse_multipart_body(content_type, self.rfile.read(n))
        settings_raw = (fields.get("settings") or ["{}"]) [0]
        if isinstance(settings_raw, UploadPart): settings_raw = settings_raw.data.decode("utf-8", errors="replace")
        try: settings = json.loads(str(settings_raw or "{}"))
        except Exception: settings = {}
        prompt = str(settings.get("prompt") or "").strip()
        negative = str(settings.get("negative_prompt") or "").strip()
        input_lang = str(settings.get("input_language") or "cs")
        translate = bool(settings.get("translate_prompt")) and input_lang == "cs"
        original_prompt = prompt
        original_negative = negative
        if translate:
            try:
                prompt = translate_google_gtx(prompt, "cs", "en")
                if negative: negative = translate_google_gtx(negative, "cs", "en")
            except Exception as e:
                log_line(f"Translate failed: {e}")
        files = [x for x in fields.get("images", []) if isinstance(x, UploadPart) and x.filename]
        file2_candidates = [x for x in fields.get("image2", []) if isinstance(x, UploadPart) and x.filename]
        file2 = file2_candidates[0] if file2_candidates else None
        if not files:
            return self.send_json({"success":False,"error":"Vyber aspoň jeden obrázek."},400)
        input_mode = str(settings.get("input_mode") or "1pict").lower()
        if input_mode == "2pict" and file2 is None:
            return self.send_json({"success":False,"error":"Režim 2 PICT potřebuje poslední frejm."},400)
        created=[]
        with jobs_lock:
            jobs = load_jobs()
            start_id = next_job_id(jobs)
            for idx, item in enumerate(files):
                jid = start_id + len(created)
                fname = f"job_{jid}_{uuid.uuid4().hex[:8]}_" + sanitize_filename(item.filename, ".png")
                dst = INPUTS / fname
                dst.write_bytes(item.data)
                dst2_rel = None
                if input_mode == "2pict" and file2 is not None:
                    fname2 = f"job_{jid}_last_{uuid.uuid4().hex[:8]}_" + sanitize_filename(file2.filename, ".png")
                    dst2 = INPUTS / fname2
                    dst2.write_bytes(file2.data)
                    dst2_rel = rel_path(dst2)
                s = dict(settings)
                base_seed = safe_int(s.get("seed"), int(time.time()) % 2147483647)
                seed_mode = str(s.get("seed_mode") or "increment_batch")
                if seed_mode == "increment_batch": s["seed"] = base_seed + idx
                elif seed_mode == "random_each": s["seed"] = int.from_bytes(os.urandom(4), "big") % 2147483647 or 1
                else: s["seed"] = base_seed
                fps = safe_int(s.get("fps"),25); duration=safe_float(s.get("duration"),5)
                s["frame_count"] = max(1, round(fps * duration))
                s["original_prompt"] = original_prompt; s["original_negative_prompt"] = original_negative
                job = {"id":jid,"status":"pending","progress":0,"created_at":now_iso(),"updated_at":now_iso(),"prompt":prompt,"negative_prompt":negative,"preset":s.get("preset") or "Statická kamera (stativ)","settings":s,"input_path":rel_path(dst),"input2_path":dst2_rel,"output_path":None,"message":"Čeká ve frontě","error":"","events":[{"time":now_iso(),"kind":"created","message":"Job vytvořen lokálně"}]}
                jobs.append(job); created.append(public_job(job))
            save_jobs(jobs)
        return self.send_json({"success":True,"jobs":created})

    def diagnostics(self) -> dict:
        checks=[]
        def add(name,status,msg): checks.append({"name":name,"status":status,"message":msg})
        ok,msg = comfy_online(); add("ComfyUI API", "ok" if ok else "bad", comfy_base() + " · " + msg)
        add("Python", "ok", sys.version.split()[0])
        add("requests", "ok", requests.__version__)
        add("websocket-client", "ok" if websocket is not None else "warn", "OK" if websocket is not None else "není nainstalovaný, použije se polling")
        for p in (INPUTS, OUTPUTS, TMP, ROOT/"workflows"):
            add(str(p.relative_to(ROOT)), "ok" if p.exists() and os.access(p, os.W_OK) else "bad", "zapisovatelné" if os.access(p, os.W_OK) else "není zapisovatelné")
        for f in (ROOT/"workflows/ltx23_i2v_template.json", ROOT/"workflows/ltx23_flf2v_template.json"):
            try:
                js=json.loads(f.read_text(encoding="utf-8")); sanitize_workflow(js, str(f)); add(str(f.relative_to(ROOT)), "ok", f"API workflow, nodes: {len(js)}")
            except Exception as e: add(str(f.relative_to(ROOT)), "bad", str(e))
        return {"success":True,"checks":checks,"version":APP_VERSION}

    def stats(self) -> dict:
        with jobs_lock:
            jobs = load_jobs()
        counts={}
        for j in jobs: counts[j.get("status","?")] = counts.get(j.get("status","?"),0)+1
        return {"success":True,"counts":counts,"active_prompts":active_prompt_by_job}


def recover_stuck_jobs() -> None:
    """Joby rozdělané při pádu/zavření serveru vrátí do fronty,
    jinak by ve stavu 'processing' visely navždy (fronta bere jen 'pending')."""
    with jobs_lock:
        jobs = load_jobs()
        changed = 0
        for j in jobs:
            if j.get("status") in ("processing", "uploading", "queued", "generating", "downloading"):
                j["status"] = "pending"; j["progress"] = 0; j["current_node"] = ""
                j["message"] = "Obnoveno po restartu serveru — čeká ve frontě"
                j["updated_at"] = now_iso()
                changed += 1
        if changed:
            save_jobs(jobs)
            log_line(f"Obnoveno {changed} rozdělaných jobů zpět do fronty.")

# ─── api.php KOMPATIBILNÍ VRSTVA pro FTP UI ──────────────────
# Web UI je 1:1 převzaté z FTP verze (app.php). Tahle vrstva překládá
# jeho volání api.php?action=... na lokální frontu, aby vzhled i chování
# byly stejné jako na webu, jen bez přihlášení, tokenů a FTP.

LOCAL_WORKER_VERSION = "2026-06-10-v7-polish-diagnostics"  # musí sedět s EXPECTED_WORKER_VERSION v UI
LOCAL_WORKER_ID = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "LOCAL-PC"

_comfy_state_cache: Dict[str, Any] = {"ts": 0.0, "ok": False, "msg": ""}

def comfy_online_cached(max_age: float = 8.0) -> Tuple[bool, str]:
    now = time.time()
    if now - float(_comfy_state_cache.get("ts") or 0) > max_age:
        ok, msg = comfy_online()
        _comfy_state_cache.update(ts=now, ok=ok, msg=msg)
    return bool(_comfy_state_cache["ok"]), str(_comfy_state_cache["msg"])

_gpu_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

def local_gpu_stats() -> Optional[dict]:
    now = time.time()
    if now - float(_gpu_cache.get("ts") or 0) < 8.0:
        return _gpu_cache.get("data")
    data = None
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=(0x08000000 if sys.platform.startswith("win") else 0))
        line = (out.stdout or "").strip().splitlines()
        if line:
            parts = [p.strip() for p in line[0].split(",")]
            if len(parts) >= 4:
                data = {"util_pct": safe_int(parts[0]), "mem_used_mb": safe_int(parts[1]),
                        "mem_total_mb": safe_int(parts[2]), "temp_c": safe_int(parts[3])}
    except Exception:
        data = None
    _gpu_cache.update(ts=now, data=data)
    return data

def ftp_workers_state() -> dict:
    ok, _msg = comfy_online_cached()
    active = 0
    try:
        active = next(iter(active_prompt_by_job.keys()), 0)
    except Exception:
        active = 0
    return {LOCAL_WORKER_ID: {
        "updated_at": gmdate_iso(),
        "worker": {"id": LOCAL_WORKER_ID, "version": LOCAL_WORKER_VERSION, "state": "online", "active_job": int(active)},
        "comfy": {"online": ok, "state": "ready" if ok else "offline"},
        "gpu": local_gpu_stats(),
    }}

def gmdate_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def ftp_queue_counts(jobs: List[dict]) -> dict:
    counts = {"pending": 0, "processing": 0, "queued": 0, "generating": 0, "uploading": 0,
              "downloading": 0, "done_today": 0, "active_total": 0, "finished_total": 0}
    today = time.strftime("%Y-%m-%d")
    for j in jobs:
        st = str(j.get("status") or "")
        if st in counts:
            counts[st] += 1
        if st in ("pending", "processing", "queued", "generating", "uploading", "downloading"):
            counts["active_total"] += 1
        if st in ("done", "error", "cancelled"):
            counts["finished_total"] += 1
        if st == "done" and str(j.get("created_at") or "").startswith(today):
            counts["done_today"] += 1
    return counts

def ftp_job_public(j: dict) -> dict:
    jid = int(j.get("id") or 0)
    s = dict(j.get("settings") or {})
    if j.get("input2_path"):
        s["input_image_2"] = j.get("input2_path")
        s.setdefault("input_mode", "2pict")
    out_rel = j.get("output_path")
    out = {
        "id": jid,
        "prompt": j.get("prompt") or "",
        "negative_prompt": j.get("negative_prompt") or "",
        "preset": j.get("preset") or "",
        "input_image": j.get("input_path") or "",
        "input_original_name": Path(str(j.get("input_path") or "")).name or None,
        "output_video": out_rel,
        "output_files_list": ([{"rel": out_rel, "name": Path(str(out_rel)).name}] if out_rel else []),
        "settings": s,
        "comfy_prompt_id": j.get("comfy_prompt_id"),
        "status": j.get("status") or "pending",
        "progress": int(j.get("progress") or 0),
        "current_node": j.get("current_node") or "",
        "error": j.get("error") or None,
        "message": j.get("message") or "",
        "worker_id": LOCAL_WORKER_ID,
        "target_worker": None,
        "username": "local",
        "created_at": j.get("created_at") or "",
        "updated_at": j.get("updated_at") or "",
        "started_at": j.get("started_at"),
        "finished_at": j.get("finished_at"),
        "duration_seconds": j.get("duration_seconds"),
        "input_url": (f"api.php?action=job_file&id={jid}&kind=input" if j.get("input_path") else None),
        "input2_url": (f"api.php?action=job_file&id={jid}&kind=input2" if j.get("input2_path") else None),
        "output_url": (f"api.php?action=job_file&id={jid}&kind=output" if out_rel else None),
    }
    return out

def ftp_events(j: dict) -> List[dict]:
    evs = []
    for e in (j.get("events") or []):
        evs.append({"type": str(e.get("kind") or "info"), "message": str(e.get("message") or ""),
                    "data_json": None, "created_at": str(e.get("time") or "")})
    evs.reverse()  # nejnovější první, jako FTP (ORDER BY id DESC)
    return evs

def ftp_dashboard_payload(status: str, limit: int, detail_id: int) -> dict:
    with jobs_lock:
        all_jobs = load_jobs()
    if status:
        rows = sorted([j for j in all_jobs if str(j.get("status")) == status], key=lambda x: int(x.get("id", 0)))
    else:
        rows = sorted(all_jobs, key=lambda x: int(x.get("id", 0)), reverse=True)
    rows = rows[:max(1, min(500, limit))]
    out = {
        "success": True,
        "jobs": [ftp_job_public(j) for j in rows],
        "workers": ftp_workers_state(),
        "queue_counts": ftp_queue_counts(all_jobs),
        "generated_at": gmdate_iso(),
        "cached": False,
    }
    if detail_id > 0:
        dj = next((j for j in all_jobs if int(j.get("id", 0)) == detail_id), None)
        if dj:
            out["detail"] = {"job": ftp_job_public(dj), "events": ftp_events(dj)}
        else:
            out["detail_error"] = "Job nenalezen."
    return out

def _ftp_translate(text: str) -> Tuple[str, bool, Optional[str]]:
    text = str(text or "").strip()
    if not text:
        return "", False, None
    try:
        tr = translate_google_gtx(text, "cs", "en")
        if tr.strip():
            return tr, True, "google_gtx"
    except Exception as e:
        log_line(f"Translate failed: {e}")
    return text, False, None

def _ftp_save_upload(part: "UploadPart", prefix: str) -> Path:
    fname = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:10]}_" + sanitize_filename(part.filename, ".png")
    dst = INPUTS / fname
    dst.write_bytes(part.data)
    return dst

def _ftp_normalize_settings(s: dict, preset: str) -> dict:
    s = dict(s or {})
    s["width"] = max(256, min(4096, safe_int(s.get("width"), 1280)))
    s["height"] = max(256, min(4096, safe_int(s.get("height"), 720)))
    s["fps"] = max(1, min(60, safe_int(s.get("fps"), 25)))
    s["duration"] = max(1.0, min(60.0, safe_float(s.get("duration"), 5.0)))
    s["frame_count"] = max(1, min(3600, int(round(s["fps"] * s["duration"]))))
    s["steps"] = max(1, min(200, safe_int(s.get("steps"), 30)))
    s["cfg"] = max(0.0, min(30.0, safe_float(s.get("cfg"), 3.5)))
    s["motion_strength"] = max(0.0, min(2.0, safe_float(s.get("motion_strength"), 0.75)))
    s["prompt_enhance"] = bool(s.get("prompt_enhance"))
    s["enhance_tokens"] = max(64, min(512, safe_int(s.get("enhance_tokens"), 128)))
    seed = safe_int(s.get("seed"), 0)
    s["seed"] = seed if seed > 0 else (int.from_bytes(os.urandom(4), "big") % 2147483647 or 1)
    sm = str(s.get("seed_mode") or "increment_batch")
    s["seed_mode"] = sm if sm in ("increment_batch", "locked", "random_each") else "increment_batch"
    cam = str(s.get("camera_motion") or "").strip()
    if not cam:
        cam = CAMERA_PRESETS.get(preset, "")
    s["camera_motion"] = cam
    s["style"] = str(s.get("style") or "")
    return s

def ftp_create_one(prompt: str, negative: str, preset: str, settings: dict,
                   image: "UploadPart", image2: Optional["UploadPart"]) -> dict:
    s = _ftp_normalize_settings(settings, preset)
    s["original_prompt"] = str(s.get("original_prompt") or prompt)
    s["original_negative_prompt"] = str(s.get("original_negative_prompt") or negative)
    translate_enabled = bool(s.get("translate_prompt", True)) and str(s.get("input_language") or "cs") == "cs"
    if translate_enabled and not s.get("translated"):
        tr, ok, prov = _ftp_translate(prompt)
        if ok:
            prompt = tr
            s["translated"] = True
            s["translation_provider"] = prov
        if negative:
            ntr, nok, _ = _ftp_translate(negative)
            if nok:
                negative = ntr
    dst = _ftp_save_upload(image, "job")
    dst2_rel = None
    if image2 is not None:
        dst2 = _ftp_save_upload(image2, "job_last")
        dst2_rel = rel_path(dst2)
        s["input_mode"] = "2pict"
    with jobs_lock:
        jobs = load_jobs()
        jid = next_job_id(jobs)
        job = {"id": jid, "status": "pending", "progress": 0,
               "created_at": now_iso(), "updated_at": now_iso(),
               "prompt": prompt, "negative_prompt": negative,
               "preset": preset or "Statická kamera (stativ)", "settings": s,
               "input_path": rel_path(dst), "input2_path": dst2_rel,
               "output_path": None, "message": "Čeká ve frontě", "error": "",
               "events": [{"time": now_iso(), "kind": "create",
                           "message": "Job vytvořen lokálně" + (" + prompt přeložen do EN" if s.get("translated") else "")}]}
        jobs.append(job)
        save_jobs(jobs)
    return job

def main() -> None:
    cfg = load_config()
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 8765)
    recover_stuck_jobs()
    threading.Thread(target=worker_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    log_line("="*68)
    log_line(" PZ ComfyW Local Direct")
    log_line(f" Web:   {url}")
    log_line(f" Comfy: {comfy_base()}")
    log_line(" Bez FTP / bez workeru / lokální data ve složce data/")
    log_line("="*68)
    if cfg.get("open_browser", True):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    httpd.serve_forever()

if __name__ == "__main__":
    main()
