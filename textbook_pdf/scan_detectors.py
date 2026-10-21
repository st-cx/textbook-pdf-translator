"""Local, image-only scan detectors; no OCR, image URL or model downloads.

DocLayout supplies document regions. Pix2Text MFD 1.5 supplies inline/display
formula boxes. Their overlaps remain separate evidence for scan_adapter.
MFD's ONNX export declares raw YOLO11 xywh/class scores (nms=False); this
module performs RGB letterboxing and class-aware duplicate NMS with NumPy.
No Pix2Text, Ultralytics or Torch package is imported.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from importlib.metadata import version
import math
import os
from pathlib import Path
import threading
import time

import numpy as np

from .models import file_sha256

MFD_REVISION = "f470a885e0fca1d3d2bfa2a54991db7ae01f1861"
MFD_SHA256 = "40d4fc852d99bcbf25a9478897d2f49fbbb8f7fdd6569c088cd1c31386293bd7"
MFD_FILENAME = "pix2text-mfd-1.5.onnx"
MFD_SIZE = 768
MFD_CONFIDENCE = .25
MFD_NMS_IOU = .7
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_ANCHORS = 100_000
_LOCK = threading.RLock()
_MODELS = {}
_LAYOUT_KINDS = {
    "title": "heading", "plain text": "text", "text": "text",
    "figure": "figure", "table": "table", "code": "code",
    "figure_caption": "caption", "table_caption": "caption",
    "formula_caption": "caption", "table_footnote": "caption",
    "isolate_formula": "formula_display", "abandon": "other",
}
_FORMULA_KINDS = {"embedding": "formula_inline", "isolated": "formula_display"}


class DetectorUnavailable(RuntimeError):
    pass


def _image(value):
    if not isinstance(value, np.ndarray) or value.dtype != np.uint8:
        raise ValueError("Expected a local uint8 RGB array, not a path or URL")
    if value.ndim != 3 or value.shape[2] != 3 or min(value.shape[:2]) < 1:
        raise ValueError("Expected nonempty HxWx3 RGB pixels")
    if value.shape[0] * value.shape[1] > _MAX_IMAGE_PIXELS:
        raise ValueError("Scan raster exceeds the bounded 40 megapixel budget")
    return np.ascontiguousarray(value)


def _letterbox(rgb, size=MFD_SIZE):
    import cv2
    h, w = rgb.shape[:2]
    gain = min(size / h, size / w)
    nh, nw = max(1, round(h * gain)), max(1, round(w * gain))
    top, left = (size - nh) // 2, (size - nw) // 2
    resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[top:top + nh, left:left + nw] = resized
    tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32) / 255
    return tensor, {"gain": gain, "left": left, "top": top, "size": size}


def _nms(boxes, scores, classes, iou=MFD_NMS_IOU):
    """Remove duplicate anchors within a class; conflicting classes survive."""
    keep = []
    for cls in sorted(set(classes.tolist())):
        order = np.flatnonzero(classes == cls)
        order = order[np.argsort(-scores[order], kind="stable")]
        while len(order):
            current = int(order[0]); keep.append(current)
            if len(order) == 1:
                break
            rest = order[1:]
            lo = np.maximum(boxes[current, :2], boxes[rest, :2])
            hi = np.minimum(boxes[current, 2:], boxes[rest, 2:])
            overlap = np.prod(np.maximum(hi - lo, 0), axis=1)
            area = np.prod(boxes[current, 2:] - boxes[current, :2])
            areas = np.prod(boxes[rest, 2:] - boxes[rest, :2], axis=1)
            union = area + areas - overlap
            order = rest[overlap / np.maximum(union, 1e-12) <= iou]
    return sorted(keep, key=lambda i: (-float(scores[i]), i))


def _decode_mfd(raw, transform, shape, names):
    raw = np.asarray(raw)
    if raw.ndim != 3 or raw.shape[0] != 1 or raw.shape[1] != 6 or raw.shape[2] > _MAX_ANCHORS:
        raise DetectorUnavailable("MFD output differs from pinned [1,6,anchors] schema")
    if not np.isfinite(raw).all():
        raise DetectorUnavailable("MFD output contains nonfinite values")
    rows = raw[0].T
    scores = rows[:, 4:].max(axis=1)
    selected = scores > MFD_CONFIDENCE
    rows, scores = rows[selected], scores[selected]
    classes = rows[:, 4:].argmax(axis=1)
    if np.any(scores > 1):
        raise DetectorUnavailable("MFD class scores are outside the probability range")
    boxes = np.column_stack((rows[:, :2] - rows[:, 2:4] / 2, rows[:, :2] + rows[:, 2:4] / 2)).astype(float)
    boxes -= [transform["left"], transform["top"], transform["left"], transform["top"]]
    boxes /= transform["gain"]
    # Degenerate inactive anchors do not become source regions.
    positive = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes, scores, classes = boxes[positive], scores[positive], classes[positive]
    keep = _nms(boxes, scores, classes)
    detections = [{"bbox": boxes[i].tolist(), "confidence": float(scores[i]),
                   "class_name": names[int(classes[i])]} for i in keep]
    return detections, {"raw_anchors": int(raw.shape[2]), "candidates_above_confidence": int(selected.sum()),
                        "positive_candidates": len(boxes), "same_class_duplicate_anchors_suppressed": len(boxes)-len(keep),
                        "confidence_threshold": MFD_CONFIDENCE, "same_class_nms_iou": MFD_NMS_IOU,
                        "cross_class_and_cross_model_conflicts_preserved": True}


class FormulaDetector:
    def __init__(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise DetectorUnavailable("Pinned MFD model is missing; run the explicit model provisioning step")
        sha = file_sha256(path)
        if sha != MFD_SHA256:
            raise DetectorUnavailable("MFD model hash differs from the pinned 1.5 model")
        import onnxruntime as ort
        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        metadata = self.session.get_modelmeta().custom_metadata_map
        try:
            self.names = ast.literal_eval(metadata["names"])
        except (KeyError, ValueError, SyntaxError) as error:
            raise DetectorUnavailable("MFD model is missing its declared class labels") from error
        if self.names != {0: "embedding", 1: "isolated"}:
            raise DetectorUnavailable("MFD model class labels differ from the pinned contract")
        inputs = self.session.get_inputs()
        if len(inputs) != 1 or inputs[0].name != "images" or inputs[0].type != "tensor(float)":
            raise DetectorUnavailable("MFD input differs from the pinned float RGB contract")
        self.identity = {"name": "Pix2Text-MFD", "version": "1.5", "revision": MFD_REVISION,
                         "path": str(path), "sha256": sha, "onnxruntime_version": version("onnxruntime"),
                         "providers": self.session.get_providers(), "model_card_license": "MIT",
                         "embedded_onnx_license": metadata.get("license"),
                         "license_declarations_differ": "AGPL" in metadata.get("license", ""),
                         "onnx_metadata": metadata, "model_downloads": False}

    def predict(self, rgb):
        tensor, geometry = _letterbox(rgb)
        raw = self.session.run(None, {"images": tensor})[0]
        return _decode_mfd(raw, geometry, rgb.shape, self.names)


class LayoutDetector:
    def __init__(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise DetectorUnavailable("Pinned DocLayout model is missing; run the explicit model provisioning step")
        from .babel_adapter import _configure_cache
        from .paragraph_adapter import _load_model
        _configure_cache(path.parent)
        self.model, provenance = _load_model(path)
        self.identity = {**provenance, "name": "DocLayout-YOLO-DocStructBench", "version": "BabelDOC-0.6.4",
                         "sha256": file_sha256(path), "path": str(path)}

    def predict(self, rgb):
        result = self.model.predict(rgb[:, :, ::-1], imgsz=1024)[0]
        return [{"bbox": [float(v) for v in box.xyxy], "confidence": float(box.conf),
                 "class_name": result.names[int(box.cls)]} for box in result.boxes], {
                     "input": "BGR view of caller RGB", "prediction_coordinates": "original image pixels",
                     "internal_input_size": 1024, "cross_model_conflicts_preserved": True}


def _load(which, value):
    if value is not None and not isinstance(value, (str, Path)):
        if not hasattr(value, "predict") or not isinstance(getattr(value, "identity", None), dict):
            raise TypeError("Injected detector must expose predict(rgb) and an identity dictionary")
        return value
    if value is None:
        if which == "layout":
            from .babel_adapter import layout_model_path
            value = layout_model_path()
        else:
            value = os.environ.get("TEXTBOOK_MFD_MODEL", str(Path(__file__).resolve().parent.parent / ".cache" / "scan" / MFD_FILENAME))
    path = Path(value).resolve()
    if not path.is_file():
        raise DetectorUnavailable(f"Local {which} model is missing: {path}; no download was attempted")
    stat = path.stat(); key = (which, str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _MODELS:
        model = (LayoutDetector if which == "layout" else FormulaDetector)(path)
        # Bounded cache: retain only the current model of each detector type.
        for old in [k for k in _MODELS if k[0] == which]:
            del _MODELS[old]
        _MODELS[key] = model
    return _MODELS[key]


def _adapter_identity():
    return {"name": "textbook_pdf.scan_detectors", "version": 1,
            "module_sha256": file_sha256(Path(__file__).resolve()),
            "configuration": {"layout_input_size": 1024, "mfd_input_size": MFD_SIZE,
                              "mfd_confidence": MFD_CONFIDENCE, "mfd_same_class_nms_iou": MFD_NMS_IOU,
                              "max_input_pixels": _MAX_IMAGE_PIXELS,
                              "input": "uint8 RGB", "output": "original image pixels, top-left xyxy",
                              "cross_class_and_cross_model_overlaps": "preserved"}}


def detector_identity(*, layout_model=None, formula_model=None):
    """Return the exact detect_regions models signature without page inference.

    Pinned local sessions may be loaded once to verify their metadata. No
    predict/run method or remote download is invoked. Wrapper source and
    effective preprocessing settings participate in the cache signature.
    """
    with _LOCK:
        result = {which: deepcopy(_load(which, value).identity)
                  for which, value in (("layout", layout_model), ("formula", formula_model))}
        result["adapter"] = _adapter_identity()
        return result


def detect_regions(image_rgb, *, layout_model=None, formula_model=None):
    """Return local detections in original RGB-image pixels, top-left xyxy.

    Model arguments are local paths or injected predict/identity detector handles.
    None discovers pinned models locally. Missing models raise; this function
    never downloads them, calls OCR, consumes a text layer, or infers page numbers
    from DocLayout's ambiguous `abandon` class.
    """
    started = time.perf_counter(); rgb = _image(image_rgb)
    h, w = rgb.shape[:2]; regions, identities, processing, diagnostics = [], {}, {}, []
    with _LOCK:
        for which, supplied, mapping in (("layout", layout_model, _LAYOUT_KINDS), ("formula", formula_model, _FORMULA_KINDS)):
            model = _load(which, supplied)
            records, evidence = model.predict(rgb)
            identities[which] = deepcopy(model.identity); processing[which] = evidence
            for index, item in enumerate(records):
                bbox = np.asarray(item["bbox"], dtype=float)
                confidence = float(item["confidence"])
                if bbox.shape != (4,) or not np.isfinite(bbox).all() or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise DetectorUnavailable(f"{which} detector returned invalid box/confidence")
                if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    raise DetectorUnavailable(f"{which} detector returned a nonpositive region")
                clipped = np.clip(bbox, [0, 0, 0, 0], [w, h, w, h])
                if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
                    diagnostics.append({"code": "detector_region_outside_image", "detector": which, "bbox": bbox.tolist()})
                    continue
                label = str(item["class_name"])
                region = {"id": f"{which}-{index:04d}", "kind": mapping.get(label, "other"),
                          "bbox": clipped.tolist(), "confidence": confidence, "detector": which,
                          "detector_class": label}
                if not np.array_equal(bbox, clipped):
                    region["bbox_before_image_clip"] = bbox.tolist()
                regions.append(region)
    identities["adapter"] = _adapter_identity()
    return {"regions": regions, "models": identities, "elapsed_seconds": time.perf_counter()-started,
            "input_pixel_size": [w, h], "coordinates": "original image pixels, top-left xyxy",
            "postprocessing": processing, "diagnostics": diagnostics,
            "overlapping_region_evidence_preserved": True}
