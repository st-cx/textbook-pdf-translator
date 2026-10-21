"""Explicit pinned model provisioning; never imported by ordinary inference.

Only this initialization entry point may download the public MFD weights.
No PDF path, document bytes, image URL or credentials are accepted.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import uuid
from urllib.request import Request, urlopen


MFD_BYTES = 80_311_115


class ModelPreparationError(RuntimeError):
    pass


def _checksum(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _matches(path, size, checksum):
    path = Path(path)
    return path.is_file() and path.stat().st_size == size and _checksum(path) == checksum


def _local_path(value):
    if not isinstance(value, (str, os.PathLike)) or "://" in os.fspath(value):
        raise ModelPreparationError("Model destination must be a local filesystem path")
    return Path(value).expanduser().resolve()


def _install_stream(stream, destination, size, checksum):
    """Validate a same-directory temporary file before replacing any old file."""
    destination = _local_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    created = False
    try:
        count = 0
        with temporary.open("xb") as target:
            created = True
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > size:
                    raise ModelPreparationError(f"Model exceeds pinned size: {destination.name}")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not _matches(temporary, size, checksum):
            raise ModelPreparationError(f"Model checksum/size mismatch: {destination.name}; previous file preserved")
        temporary.replace(destination)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def prepare_layout_model():
    """Retain the existing explicitly invoked BabelDOC provisioning route."""
    from .babel_adapter import _configure_cache, available
    from .paragraph_adapter import MODEL_SHA3_256
    ok, detail = available()
    if not ok:
        raise ModelPreparationError(detail)
    _configure_cache(Path(__file__).resolve().parent.parent)
    from babeldoc.assets.assets import get_doclayout_onnx_model_path
    model = Path(get_doclayout_onnx_model_path())
    checksum = _checksum(model, "sha3_256")
    if checksum != MODEL_SHA3_256:
        raise ModelPreparationError("Prepared DocLayout model differs from the pinned checksum")
    return {"ready": True, "path": str(model.resolve()), "bytes": model.stat().st_size,
            "sha3_256": checksum, "sha256": _checksum(model), "provisioning": "explicit BabelDOC bootstrap"}


def prepare_ocr_models(model_dir=None):
    """Copy only the three verified models bundled in rapidocr==3.10.0."""
    from .ocr_engine import MODEL_SPECS, RAPIDOCR_VERSION, _model_dir
    try:
        dist = metadata.distribution("rapidocr")
    except metadata.PackageNotFoundError as error:
        raise ModelPreparationError(f"Install rapidocr=={RAPIDOCR_VERSION} in this tool's environment before preparing scan OCR") from error
    if dist.version != RAPIDOCR_VERSION:
        raise ModelPreparationError(f"Expected rapidocr=={RAPIDOCR_VERSION}, found {dist.version}; no OCR model copied")
    destination = _model_dir(model_dir)
    sources = []
    # Verify the complete wheel model set before modifying its destination.
    for spec in MODEL_SPECS:
        source = Path(dist.locate_file("rapidocr/models/" + spec["filename"]))
        if not _matches(source, spec["bytes"], spec["sha256"]):
            raise ModelPreparationError(f"Bundled RapidOCR model checksum/size mismatch: {source}; no OCR model copied")
        sources.append((source, spec))
    notices = []
    for name in ("LICENSE", "MODEL_LICENSES.md"):
        source = Path(dist.locate_file(f"rapidocr-{RAPIDOCR_VERSION}.dist-info/licenses/{name}"))
        if not source.is_file():
            raise ModelPreparationError(f"RapidOCR attribution file missing: {source}; no OCR model copied")
        notices.append((source, name))
    records = []
    for source, spec in sources:
        target = destination / spec["filename"]
        copied = not _matches(target, spec["bytes"], spec["sha256"])
        if copied:
            with source.open("rb") as stream:
                _install_stream(stream, target, spec["bytes"], spec["sha256"])
        records.append({**spec, "path": str(target), "copied": copied, "source": "rapidocr==" + RAPIDOCR_VERSION + " wheel"})
    notice_records = []
    for source, name in notices:
        target = destination / name
        size, checksum = source.stat().st_size, _checksum(source)
        if not _matches(target, size, checksum):
            with source.open("rb") as stream:
                _install_stream(stream, target, size, checksum)
        notice_records.append({"path": str(target), "sha256": checksum})
    return {"ready": True, "rapidocr_version": RAPIDOCR_VERSION, "model_dir": str(destination),
            "models": records, "attribution_files": notice_records, "downloaded": False}


def prepare_formula_model(model_path=None):
    """Explicit fixed-revision MFD download; valid local files are reused."""
    from .scan_detectors import MFD_FILENAME, MFD_REVISION, MFD_SHA256
    model = _local_path(model_path if model_path is not None else os.environ.get(
        "TEXTBOOK_MFD_MODEL", str(Path(__file__).resolve().parent.parent / ".cache" / "scan" / MFD_FILENAME)))
    size = MFD_BYTES
    url = f"https://huggingface.co/breezedeus/pix2text-mfd-1.5/resolve/{MFD_REVISION}/{MFD_FILENAME}"
    downloaded = not _matches(model, size, MFD_SHA256)
    if downloaded:
        request = Request(url, headers={"User-Agent": "textbook-pdf-explicit-model-bootstrap/1"})
        try:
            with urlopen(request, timeout=45) as response:
                _install_stream(response, model, size, MFD_SHA256)
        except (OSError, TimeoutError) as error:
            raise ModelPreparationError(f"MFD preparation failed; previous file preserved. Retry the explicit initialization step: {error}") from error
    return {"ready": True, "path": str(model), "bytes": size, "sha256": MFD_SHA256,
            "revision": MFD_REVISION, "source_url": url, "downloaded": downloaded,
            "license_evidence": {"model_card": "MIT", "embedded_onnx": "AGPL-3.0 (Ultralytics)",
                                 "declarations_differ": True,
                                 "model_card_url": f"https://huggingface.co/breezedeus/pix2text-mfd-1.5/blob/{MFD_REVISION}/README.md"}}


def prepare_models():
    models, errors = {}, []
    for name, prepare in (("layout", prepare_layout_model), ("ocr", prepare_ocr_models), ("formula", prepare_formula_model)):
        try:
            models[name] = prepare()
        except Exception as error:
            models[name] = {"ready": False}
            errors.append({"model_group": name, "detail": str(error)})
    return {"layout_model_ready": models["layout"]["ready"], "ocr_models_ready": models["ocr"]["ready"],
            "formula_model_ready": models["formula"]["ready"], "models": models, "errors": errors,
            "book_data_sent": False, "explicit_preparation_only": True}


def main():
    report = prepare_models()
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
