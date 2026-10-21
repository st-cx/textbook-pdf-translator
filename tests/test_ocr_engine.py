"""Local OCR contract tests. Model tests skip when optional OCR is absent.

No APIs or document upload. Actual inference blocks network requests, uses
workspace-only probe images/models, and never modifies original books.
"""
from __future__ import annotations

import copy
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
import uuid

import numpy as np

from textbook_pdf import ocr_engine as ocr

PROBE = Path(__file__).resolve().parent / "_tmp_ocr_engine"


def quad(x0=1, y0=2, x1=9, y1=8):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def output():
    return N(boxes=[quad()], txts=("A test",), scores=(.07,),
             word_results=((("A", .02, quad(x1=3)), ("test", .1, quad(x0=4))),))


class OCRContractTests(unittest.TestCase):
    def setUp(self):
        self.old_cache = ocr._CACHED
        ocr._CACHED = None
        self.scratch = PROBE / "test-work" / uuid.uuid4().hex
        self.scratch.mkdir(parents=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        ocr._CACHED = self.old_cache
        # Explicit bounded path; normal mkdir avoids Windows temp ACL failures.
        assert self.scratch.resolve().is_relative_to((PROBE / "test-work").resolve())
        shutil.rmtree(self.scratch)

    def fake_models(self):
        specs = []
        for role in ("det", "cls", "rec"):
            payload = (role + "-local-model").encode()
            filename = role + ".onnx"
            (self.scratch / filename).write_bytes(payload)
            specs.append({"role": role, "filename": filename, "bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest(),
                          "ocr_version": "PP-OCRv6", "model_type": "small"})
        return tuple(specs)

    def test_paths_urls_bytes_wrong_dtype_and_shapes_rejected_before_engine(self):
        bad = ["https://example.invalid/page.png", "page.png", Path("page.png"), b"PNG",
               np.zeros((2, 2), np.uint8), np.zeros((0, 2, 3), np.uint8),
               np.zeros((2, 2, 4), np.uint8), np.zeros((2, 2, 3), np.float32)]
        with patch.object(ocr, "_engine") as build:
            for value in bad:
                with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                    ocr.recognize(value)
            build.assert_not_called()

    def test_oversized_raster_rejected_without_materializing_copy(self):
        # A zero-stride view uses no additional large backing allocation.
        huge = np.broadcast_to(np.zeros((1, 1, 3), np.uint8), (7000, 7000, 3))
        with self.assertRaisesRegex(ValueError, "40 megapixel"):
            ocr.recognize(huge)

    def test_rgb_becomes_owned_bgr_and_does_not_mutate_original(self):
        rgb = np.zeros((10, 12, 3), np.uint8)
        rgb[:] = [11, 22, 33]
        snapshot = rgb.copy()
        seen = []
        def backend(value, **kwargs):
            seen.append((value[0, 0].tolist(), kwargs))
            value[:] = 0
            return output()
        with patch.object(ocr, "_engine", return_value=(backend, {"signature": "x"})):
            result = ocr.recognize(rgb)
        np.testing.assert_array_equal(rgb, snapshot)
        self.assertEqual(seen[0][0], [33, 22, 11])
        self.assertEqual(seen[0][1]["text_score"], 0.0)
        self.assertTrue(seen[0][1]["return_word_box"])
        self.assertEqual(result["lines"][0]["confidence"], .07)
        self.assertEqual(result["lines"][0]["words"][0]["confidence"], .02)

    def test_low_confidence_and_zero_scores_retained(self):
        value = output()
        value.scores = (0.,)
        value.word_results = ((("A", 0., quad()),),)
        rows = ocr._lines(value, 100, 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confidence"], 0.)
        self.assertEqual(rows[0]["words"][0]["confidence"], 0.)

    def test_original_frame_coordinates_are_not_rescaled(self):
        value = output()
        value.boxes = [quad(3500, 2500, 4800, 3500)]
        value.word_results = ((("A", .5, quad(3500, 2500, 3600, 2600)),),)
        rows = ocr._lines(value, 5000, 4000)
        self.assertEqual(rows[0]["bbox"], [3500., 2500., 4800., 3500.])
        self.assertEqual(rows[0]["words"][0]["bbox"], [3500., 2500., 3600., 2600.])

    def test_bad_output_counts_confidences_and_geometry_block_whole_result(self):
        variants = []
        v = output(); v.scores = (); variants.append(v)
        v = output(); v.scores = (float("nan"),); variants.append(v)
        v = output(); v.scores = (1.1,); variants.append(v)
        v = output(); v.word_results = (); variants.append(v)
        v = output(); v.word_results = ((),); variants.append(v)
        v = output(); v.boxes = [quad(-3, 2, 9, 8)]; variants.append(v)
        v = output(); v.boxes = [quad(1, 2, 1, 8)]; variants.append(v)
        v = output(); v.boxes = [quad(1, 2, 9, float("inf"))]; variants.append(v)
        v = output(); v.word_results = ((("A", .4, None),),); variants.append(v)
        v = output(); v.boxes = None; variants.append(v)
        for i, value in enumerate(variants):
            with self.subTest(index=i), self.assertRaises(ocr.OCREngineUnavailable):
                ocr._lines(value, 100, 100)

    def test_edge_rounding_is_explicitly_reported(self):
        result = ocr._box(quad(-.2, 0, 100.2, 20), 100, 30, "edge")
        self.assertEqual(result["bbox"], [0., 0., 100., 20.])
        self.assertTrue(result["bbox_clipped_at_image_edge"])
        self.assertEqual(result["unclipped_bbox"][0], -.2)

    def test_documented_blank_sentinel_returns_no_lines(self):
        self.assertEqual(ocr._lines(N(boxes=None, txts=None, scores=None), 10, 10), [])

    def test_missing_or_corrupt_model_fails_without_backend_or_directory_creation(self):
        missing = self.scratch / "missing"
        with patch.object(ocr, "_build_backend") as build:
            with self.assertRaisesRegex(ocr.OCREngineUnavailable, "does not download"):
                ocr._engine(missing)
            self.assertFalse(missing.exists())
            specs = self.fake_models()
            (self.scratch / "rec.onnx").write_bytes(b"corrupt")
            with patch.object(ocr, "MODEL_SPECS", specs):
                with self.assertRaisesRegex(ocr.OCREngineUnavailable, "mismatch"):
                    ocr._engine(self.scratch)
            build.assert_not_called()

    def test_actual_model_identity_and_upstream_config_bind_cache(self):
        specs = self.fake_models()
        providers = {role: ["CPUExecutionProvider"] for role in ("det", "cls", "rec")}
        with patch.object(ocr, "MODEL_SPECS", specs), \
             patch.object(ocr, "_installed_versions", return_value={"rapidocr": "3.10.0", "onnxruntime": "test"}), \
             patch.object(ocr, "_upstream_config_hash", return_value="config-A") as config_hash, \
             patch.object(ocr, "_build_backend", return_value=(object(), providers, "config-A")) as build:
            _, first = ocr._engine(self.scratch)
            _, same = ocr._engine(self.scratch)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(first["signature"], same["signature"])
            self.assertEqual(first["models"][2]["sha256"], specs[2]["sha256"])
            # Returned metadata is independent from the cached identity.
            same["models"][0]["sha256"] = "mutated-by-caller"
            _, unchanged = ocr._engine(self.scratch)
            self.assertEqual(unchanged["signature"], first["signature"])
            self.assertEqual(unchanged["models"][0]["sha256"], specs[0]["sha256"])
            config_hash.return_value = "config-B"
            build.return_value = (object(), providers, "config-B")
            _, changed = ocr._engine(self.scratch)
            self.assertEqual(build.call_count, 2)
            self.assertNotEqual(changed["signature"], first["signature"])
            # A cache hit must still hash current model contents.
            (self.scratch / "rec.onnx").write_bytes(b"altered-after-first-call")
            with self.assertRaisesRegex(ocr.OCREngineUnavailable, "mismatch"):
                ocr._engine(self.scratch)

    def test_default_environment_and_explicit_directory_precedence(self):
        with patch.dict(os.environ, {"TEXTBOOK_OCR_MODELS": str(self.scratch)}):
            self.assertEqual(ocr._model_dir(), self.scratch.resolve())
            self.assertEqual(ocr._model_dir(self.scratch / "explicit"), (self.scratch / "explicit").resolve())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ocr._model_dir(), Path(ocr.__file__).resolve().parents[1] / ".cache" / "scan" / "ocr")
        with self.assertRaises(ValueError):
            ocr._model_dir("https://example.invalid/model")

    def test_identity_getter_does_not_run_image_inference(self):
        from unittest.mock import Mock
        backend = Mock(side_effect=AssertionError("No image inference permitted"))
        identity = {"signature": "content-bound", "models": [{"sha256": "actual"}]}
        with patch.object(ocr, "_engine", return_value=(backend, copy.deepcopy(identity))) as get:
            got = ocr.engine_identity(model_dir=self.scratch)
        self.assertEqual(got, identity)
        get.assert_called_once_with(self.scratch.resolve())
        backend.assert_not_called()

    def test_wrong_optional_library_version_rejected(self):
        with patch.object(ocr.metadata, "version", return_value="wrong"):
            with self.assertRaisesRegex(ocr.OCREngineUnavailable, "Expected rapidocr"):
                ocr._installed_versions()


class ActualOfflineOCRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get("TEXTBOOK_OCR_TEST_MODELS")
        if not value:
            raise unittest.SkipTest("Set TEXTBOOK_OCR_TEST_MODELS to opt into actual local OCR inference")
        cls.models = Path(value).expanduser()
        try:
            if metadata.version("rapidocr") != ocr.RAPIDOCR_VERSION:
                raise unittest.SkipTest("Pinned optional RapidOCR not installed")
            if not all((cls.models / m["filename"]).is_file() for m in ocr.MODEL_SPECS):
                raise unittest.SkipTest("Local probe OCR models are absent")
        except metadata.PackageNotFoundError:
            raise unittest.SkipTest("Optional RapidOCR is not installed")

    def offline(self):
        from contextlib import ExitStack
        stack = ExitStack()
        def reject(*args, **kwargs):
            raise AssertionError("OCR inference attempted network access")
        stack.enter_context(patch("socket.socket.connect", side_effect=reject))
        stack.enter_context(patch("urllib.request.urlopen", side_effect=reject))
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=reject))
        return stack

    def test_actual_blank_image_and_cpu_only_models(self):
        with self.offline():
            identity = ocr.engine_identity(model_dir=self.models)
            result = ocr.recognize(np.full((160, 300, 3), 255, np.uint8), model_dir=self.models)
        self.assertEqual(identity, result["engine"])
        self.assertEqual(result["lines"], [])
        self.assertEqual(len(result["engine"]["models"]), 3)
        self.assertEqual(result["engine"]["execution_providers"]["det"], ["CPUExecutionProvider"])
        self.assertEqual(len(result["engine"]["signature"]), 64)

    def test_actual_two_weiss_rasters_return_original_frame_words_and_low_scores(self):
        from PIL import Image
        identity = None
        images_value = os.environ.get("TEXTBOOK_OCR_TEST_IMAGES")
        if not images_value:
            self.skipTest("Set TEXTBOOK_OCR_TEST_IMAGES to opt into private raster regression fixtures")
        images = Path(images_value).expanduser()
        for number, expected in ((300, 53), (25, 39)):
            path = images / f"weiss-source-p{number:04d}-240dpi.png"
            if not path.is_file():
                self.skipTest("Local Weiss raster probe images are absent")
            image = np.array(Image.open(path).convert("RGB"))
            snapshot = image.copy()
            with self.offline():
                result = ocr.recognize(image, model_dir=self.models)
            np.testing.assert_array_equal(image, snapshot)
            self.assertEqual(len(result["lines"]), expected)
            self.assertGreater(sum(len(row["words"]) for row in result["lines"]), 200)
            self.assertTrue(any(row["confidence"] < .9 for row in result["lines"]))
            for row in result["lines"]:
                for obj in [row] + row["words"]:
                    x0, y0, x1, y1 = obj["bbox"]
                    self.assertTrue(0 <= x0 < x1 <= image.shape[1])
                    self.assertTrue(0 <= y0 < y1 <= image.shape[0])
            if identity is not None:
                self.assertEqual(identity, result["engine"]["signature"])
            identity = result["engine"]["signature"]
            # Plain prose is recognized, but formula transcription is expressly
            # outside this module's semantic guarantees (caller must mask it).
            self.assertIn("estimate", result["engine"]["geometry"])


if __name__ == "__main__":
    unittest.main()
