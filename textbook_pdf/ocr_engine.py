"""Pinned local RapidOCR CPU bridge for masked scan prose.

Only local uint8 RGB arrays are accepted. Provision the three pinned ONNX
files separately; recognition never fetches models or sends document pixels.
Word boxes come from CTC alignment and are estimates, not source glyph ink.
"""
from __future__ import annotations

import copy
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import threading
import time

import numpy as np

RAPIDOCR_VERSION = "3.10.0"
WRAPPER_REVISION = "rapidocr-local-rgb-v1"
MODEL_SPECS = (
    {"role": "det", "filename": "PP-OCRv6_det_small.onnx", "bytes": 9929594,
     "sha256": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
     "ocr_version": "PP-OCRv6", "model_type": "small"},
    {"role": "cls", "filename": "ch_ppocr_mobile_v2.0_cls_mobile.onnx", "bytes": 585532,
     "sha256": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
     "ocr_version": "PP-OCRv4", "model_type": "mobile"},
    {"role": "rec", "filename": "PP-OCRv6_rec_small.onnx", "bytes": 21234383,
     "sha256": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
     "ocr_version": "PP-OCRv6", "model_type": "small"},
)
# Include every override in the source/config identity. Upstream defaults are
# additionally bound by the actual installed config.yaml content hash.
_SETTINGS = {
    "Global.max_side_len": 4096, "Global.min_side_len": 30,
    "Global.text_score": 0.0, "Global.log_level": "warning",
    "Global.use_det": True, "Global.use_cls": True, "Global.use_rec": True,
    "Global.return_word_box": True, "Global.return_single_char_box": False,
    "Det.limit_side_len": 2048, "Det.limit_type": "max",
    "Det.lang_type": "en", "Rec.lang_type": "en",
    "EngineConfig.onnxruntime.intra_op_num_threads": 4,
    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    "EngineConfig.onnxruntime.use_cuda": False,
    "EngineConfig.onnxruntime.use_dml": False,
    "EngineConfig.onnxruntime.use_cann": False,
    "EngineConfig.onnxruntime.use_coreml": False,
}
_MAX_IMAGE_PIXELS = 40_000_000
_LOCK = threading.RLock()
_CACHED = None


class OCREngineUnavailable(RuntimeError):
    """Missing/incompatible local engine, models, or untrustworthy output."""


# Short alias is useful to callers that also catch DetectorUnavailable.
OCRUnavailable = OCREngineUnavailable


def _sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _image(value):
    if not isinstance(value, np.ndarray) or value.dtype != np.uint8:
        raise ValueError("Expected a local uint8 RGB array, not a path, URL or bytes")
    if value.ndim != 3 or value.shape[2] != 3 or min(value.shape[:2]) < 1:
        raise ValueError("Expected nonempty HxWx3 RGB pixels")
    if value.shape[0] * value.shape[1] > _MAX_IMAGE_PIXELS:
        raise ValueError("OCR raster exceeds the bounded 40 megapixel budget")
    # RapidOCR treats ndarray inputs as BGR; always own the copy to protect the
    # caller's raster from any upstream preprocessing/mutation.
    return np.ascontiguousarray(value[..., ::-1]).copy()


def _model_dir(value=None):
    chosen = value if value is not None else os.environ.get("TEXTBOOK_OCR_MODELS")
    if chosen is None:
        return Path(__file__).resolve().parent.parent / ".cache" / "scan" / "ocr"
    if not isinstance(chosen, (str, os.PathLike)) or "://" in os.fspath(chosen):
        raise ValueError("model_dir must be a local directory")
    return Path(chosen).expanduser().resolve()


def _verified_models(directory):
    records, paths = [], {}
    for spec in MODEL_SPECS:
        path = directory / spec["filename"]
        if not path.is_file():
            raise OCREngineUnavailable(
                f"Missing local OCR model {path}. Provision the pinned RapidOCR "
                f"{RAPIDOCR_VERSION} models in .cache/scan/ocr or TEXTBOOK_OCR_MODELS; "
                "recognition does not download models.")
        try:
            size, actual = path.stat().st_size, _sha256(path)
        except OSError as exc:
            raise OCREngineUnavailable(f"Cannot read local OCR model {path}: {exc}") from exc
        if size != spec["bytes"] or actual != spec["sha256"]:
            raise OCREngineUnavailable(f"OCR model checksum/size mismatch: {path}")
        records.append(dict(spec, sha256=actual, bytes=size))
        paths[spec["role"]] = path
    return records, paths


def _installed_versions():
    try:
        versions = {name: metadata.version(name) for name in ("rapidocr", "onnxruntime")}
    except metadata.PackageNotFoundError as exc:
        raise OCREngineUnavailable(
            f"Install optional rapidocr=={RAPIDOCR_VERSION} and onnxruntime in this "
            "tool's isolated environment before enabling scan OCR.") from exc
    if versions["rapidocr"] != RAPIDOCR_VERSION:
        raise OCREngineUnavailable(
            f"Expected rapidocr=={RAPIDOCR_VERSION}, found {versions['rapidocr']}")
    return versions


def _upstream_config_hash():
    try:
        path = metadata.distribution("rapidocr").locate_file("rapidocr/config.yaml")
        return _sha256(path)
    except (metadata.PackageNotFoundError, OSError) as exc:
        raise OCREngineUnavailable("Cannot verify installed RapidOCR configuration") from exc


def _build_backend(paths):
    try:
        import rapidocr
        import onnxruntime as ort
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import EngineType, OCRVersion, ModelType, LangDet, LangRec
    except ImportError as exc:
        raise OCREngineUnavailable(f"Local RapidOCR dependencies unavailable: {exc}") from exc
    params = dict(_SETTINGS)
    for spec in MODEL_SPECS:
        prefix = spec["role"].capitalize()
        params[prefix + ".model_path"] = str(paths[spec["role"]])
        params[prefix + ".ocr_version"] = OCRVersion(spec["ocr_version"])
        params[prefix + ".model_type"] = ModelType(spec["model_type"])
        params[prefix + ".engine_type"] = EngineType.ONNXRUNTIME
    params["Det.lang_type"] = LangDet(params["Det.lang_type"])
    params["Rec.lang_type"] = LangRec(params["Rec.lang_type"])
    # model_root_dir is not a download destination: all role model_path values
    # are explicit and verified. The pinned rec model embeds its CTC dictionary.
    params["Global.model_root_dir"] = str(paths["det"].parent)
    ort.disable_telemetry_events()
    try:
        backend = RapidOCR(params=params)
        # Load explicitly so providers/character metadata are checked before the
        # first image is consumed (RapidOCR normally initializes them lazily).
        components = {
            "det": backend._load_det_model(), "cls": backend._load_cls_model(),
            "rec": backend._load_rec_model(),
        }
        providers = {}
        for role, component in components.items():
            providers[role] = list(component.session.session.get_providers())
            if providers[role] != ["CPUExecutionProvider"]:
                raise OCREngineUnavailable(f"OCR {role} did not use CPU-only execution")
        if not components["rec"].session.have_key("character"):
            raise OCREngineUnavailable("Pinned OCR recognizer has no embedded character dictionary")
    except OCREngineUnavailable:
        raise
    except Exception as exc:
        raise OCREngineUnavailable(f"Cannot initialize pinned local OCR models: {exc}") from exc
    config_path = Path(rapidocr.__file__).parent / "config.yaml"
    return backend, providers, _sha256(config_path)


def _engine(directory):
    global _CACHED
    # Revalidate model contents on every call, including cache hits; an edited
    # file must not silently reuse an engine under an obsolete source identity.
    records, paths = _verified_models(directory)
    versions = _installed_versions()
    upstream_config = _upstream_config_hash()
    key = _digest({"models": records, "packages": versions,
                   "settings": _SETTINGS, "wrapper_revision": WRAPPER_REVISION,
                   "upstream_config_sha256": upstream_config})
    if _CACHED is not None and _CACHED[0] == key:
        return _CACHED[1], copy.deepcopy(_CACHED[2])
    backend, providers, loaded_config = _build_backend(paths)
    if loaded_config != upstream_config:
        raise OCREngineUnavailable("RapidOCR configuration changed during initialization")
    _verified_models(directory)  # loaded files must still match the pinned bytes
    identity = {"name": "RapidOCR", "version": versions["rapidocr"],
                "onnxruntime_version": versions["onnxruntime"], "models": records,
                "execution_providers": providers, "local_only": True,
                "wrapper_revision": WRAPPER_REVISION,
                "upstream_config_sha256": upstream_config,
                "config": dict(_SETTINGS),
                "geometry": "Original-input pixels, top-left origin. CTC word boxes "
                            "are alignment estimates, not native glyph/ink evidence.",
                "confidence_filter": "None; Global.text_score=0.0. Detector proposal "
                                     "thresholds still apply and do not prove completeness."}
    identity["config_sha256"] = _digest({"overrides": _SETTINGS,
                                          "upstream_config_sha256": upstream_config,
                                          "wrapper_revision": WRAPPER_REVISION})
    identity["signature"] = _digest(identity)
    # One cached CPU engine bounds retained model/session memory. All access is
    # serialized by recognize's lock because the upstream call mutates options.
    _CACHED = (key, backend, identity)
    return backend, copy.deepcopy(identity)


def _confidence(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OCREngineUnavailable(f"Invalid OCR {label} confidence") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise OCREngineUnavailable(f"OCR {label} confidence is not a finite probability")
    return result


def _box(quad, width, height, label):
    try:
        points = np.asarray(quad, dtype=float)
    except (TypeError, ValueError) as exc:
        raise OCREngineUnavailable(f"Invalid OCR {label} quadrilateral") from exc
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise OCREngineUnavailable(f"OCR {label} has nonfinite/invalid quadrilateral geometry")
    area = abs(float(np.dot(points[:, 0], np.roll(points[:, 1], -1))
                     - np.dot(points[:, 1], np.roll(points[:, 0], -1)))) / 2
    if area <= 0:
        raise OCREngineUnavailable(f"OCR {label} has a degenerate quadrilateral")
    raw = [float(points[:, 0].min()), float(points[:, 1].min()),
           float(points[:, 0].max()), float(points[:, 1].max())]
    if raw[0] < -1 or raw[1] < -1 or raw[2] > width + 1 or raw[3] > height + 1:
        raise OCREngineUnavailable(f"OCR {label} coordinates exceed the original image frame")
    box = [max(0., raw[0]), max(0., raw[1]), min(float(width), raw[2]),
           min(float(height), raw[3])]
    if box[2] <= box[0] or box[3] <= box[1]:
        raise OCREngineUnavailable(f"OCR {label} has degenerate geometry")
    evidence = {"bbox": box}
    if raw != box:
        evidence.update(unclipped_bbox=raw, bbox_clipped_at_image_edge=True)
    return evidence


def _lines(result, width, height):
    boxes, texts, scores = (getattr(result, name, None) for name in ("boxes", "txts", "scores"))
    if boxes is None and texts is None and scores is None:
        return []  # upstream's documented blank-image sentinel
    if any(value is None for value in (boxes, texts, scores)):
        raise OCREngineUnavailable("OCR returned a partial line result")
    try:
        count = len(texts)
        if len(boxes) != count or len(scores) != count:
            raise OCREngineUnavailable("OCR line text/box/confidence counts differ")
        if count == 0:
            return []
        words = result.word_results
        if words is None or len(words) != count:
            raise OCREngineUnavailable("OCR word-box coverage differs from line count")
    except (TypeError, AttributeError) as exc:
        raise OCREngineUnavailable("OCR output differs from the pinned word-box schema") from exc
    lines = []
    for i, (quad, text, score, units) in enumerate(zip(boxes, texts, scores, words)):
        if not isinstance(text, str):
            raise OCREngineUnavailable("OCR line text is not a string")
        line = {"text": text, "confidence": _confidence(score, f"line {i}"),
                **_box(quad, width, height, f"line {i}"), "words": []}
        if not isinstance(units, (list, tuple)):
            raise OCREngineUnavailable(f"OCR line {i} has no valid word sequence")
        if not units and text.strip():
            raise OCREngineUnavailable(f"OCR line {i} has text without word alignment")
        for j, unit in enumerate(units):
            if not isinstance(unit, (tuple, list)) or len(unit) != 3 or not isinstance(unit[0], str):
                raise OCREngineUnavailable(f"Invalid OCR word result at line {i}, unit {j}")
            line["words"].append({"text": unit[0],
                                  "confidence": _confidence(unit[1], f"word {i}:{j}"),
                                  **_box(unit[2], width, height, f"word {i}:{j}")})
        lines.append(line)
    return lines


def engine_identity(*, model_dir=None):
    """Verify local models/config and return recognize's identical identity.

    The first call initializes CPU sessions to verify their actual providers;
    it performs no image inference, directory creation or model downloads.
    Returned metadata is detached from the cached identity.
    """
    with _LOCK:
        _, identity = _engine(_model_dir(model_dir))
    return identity


def recognize(image_rgb, *, model_dir=None):
    """Recognize local RGB pixels, returning every line and estimated word box.

    The caller masks formulas/code/figures first. No OCR confidence, including
    1.0, establishes formula correctness or complete scan-source coverage.
    Missing/modified models and invalid output raise OCREngineUnavailable.
    """
    started = time.perf_counter()
    bgr = _image(image_rgb)
    directory = _model_dir(model_dir)
    height, width = bgr.shape[:2]
    with _LOCK:
        backend, identity = _engine(directory)
        try:
            result = backend(bgr, return_word_box=True, return_single_char_box=False,
                             text_score=0.0)
        except Exception as exc:
            raise OCREngineUnavailable(f"Local OCR inference failed: {exc}") from exc
        lines = _lines(result, width, height)
    return {"lines": lines, "engine": identity,
            "elapsed_seconds": time.perf_counter() - started}
