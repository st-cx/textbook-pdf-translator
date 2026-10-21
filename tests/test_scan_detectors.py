import json
from pathlib import Path
import shutil
import uuid
import unittest
from unittest.mock import patch

import numpy as np
from textbook_pdf import scan_detectors as d


class FakeDetector:
    def __init__(self, name, boxes):
        self.identity = {"name": name, "version": "test", "sha256": "test-content"}
        self.boxes = boxes; self.received = None
    def predict(self, rgb):
        self.received = rgb.copy()
        return self.boxes, {"fake": True}


def box(label, bounds=(10, 10, 50, 30), score=.9):
    return {"bbox": list(bounds), "class_name": label, "confidence": score}


class ScanDetectorTests(unittest.TestCase):
    def setUp(self):
        self.rgb = np.zeros((100, 200, 3), np.uint8)
        self.layout = FakeDetector("layout", [box("plain text")])
        self.formula = FakeDetector("mfd", [])

    def run_detect(self, **kwargs):
        return d.detect_regions(self.rgb, layout_model=kwargs.pop("layout", self.layout),
                                formula_model=kwargs.pop("formula", self.formula), **kwargs)

    def test_pixel_coordinates_and_overlapping_evidence_survive(self):
        self.formula.boxes = [box("embedding"), box("isolated", (12, 12, 49, 29), .8)]
        result = self.run_detect()
        self.assertEqual([r["kind"] for r in result["regions"]], ["text", "formula_inline", "formula_display"])
        self.assertEqual(result["regions"][0]["bbox"], [10., 10., 50., 30.])
        self.assertEqual(result["input_pixel_size"], [200, 100])
        self.assertTrue(result["overlapping_region_evidence_preserved"])
        np.testing.assert_array_equal(self.layout.received, self.rgb)
        self.assertEqual(result["models"]["formula"]["sha256"], "test-content")

    def test_identity_matches_result_without_page_inference(self):
        identity=d.detector_identity(layout_model=self.layout,formula_model=self.formula)
        self.assertIsNone(self.layout.received);self.assertIsNone(self.formula.received)
        self.assertEqual(identity,self.run_detect()["models"])
        self.assertEqual(identity["adapter"]["module_sha256"],d.file_sha256(Path(d.__file__)))
        self.assertEqual(identity["adapter"]["configuration"]["mfd_input_size"],768)

    def test_identity_returns_detached_model_metadata(self):
        self.layout.identity["nested"]={"hash":"original"}
        identity=d.detector_identity(layout_model=self.layout,formula_model=self.formula)
        identity["layout"]["nested"]["hash"]="mutated"
        self.assertEqual(self.layout.identity["nested"]["hash"],"original")

    def test_ambiguous_abandon_is_not_invented_page_number(self):
        self.layout.boxes = [box("abandon"), box("table"), box("figure_caption"), box("code"), box("future_type")]
        self.assertEqual([r["kind"] for r in self.run_detect()["regions"]], ["other", "table", "caption", "code", "other"])

    def test_boundary_clip_keeps_original_detector_evidence(self):
        self.formula.boxes = [box("embedding", (-2, 90, 205, 105)), box("isolated", (-20, 10, -1, 20))]
        result = self.run_detect()
        r = result["regions"][1]
        self.assertEqual(r["bbox"], [0., 90., 200., 100.])
        self.assertEqual(r["bbox_before_image_clip"], [-2., 90., 205., 105.])
        self.assertEqual(result["diagnostics"][0]["code"], "detector_region_outside_image")

    def test_invalid_results_fail_instead_of_becoming_valid_source(self):
        for bounds, score in [((1, 2, 1, 3), .8), ((1, 2, float("nan"), 3), .8), ((1, 2, 3, 4), 1.2)]:
            with self.subTest(bounds=bounds, score=score):
                self.formula.boxes = [box("embedding", bounds, score)]
                with self.assertRaises(d.DetectorUnavailable): self.run_detect()

    def test_input_rejects_paths_float_gray_and_empty(self):
        for invalid in ["https://example.com/page.png", np.zeros((5, 5), np.uint8),
                        np.zeros((5, 5, 3), np.float32), np.zeros((0, 5, 3), np.uint8)]:
            with self.subTest(type=str(type(invalid))):
                with self.assertRaises(ValueError): d.detect_regions(invalid)

    def test_large_virtual_input_fails_before_allocation(self):
        large=np.broadcast_to(np.zeros((1, 1, 3), np.uint8), (1, 40_000_001, 3))
        with self.assertRaisesRegex(ValueError, "40 megapixel"): d._image(large)

    def test_letterbox_preserves_rgb_and_inverse_aspect_coordinates(self):
        rgb=np.zeros((101, 200, 3), np.uint8);rgb[:] = [255, 12, 6]
        tensor, t=d._letterbox(rgb)
        self.assertEqual(tensor.shape, (1, 3, 768, 768));self.assertEqual(tensor.dtype, np.float32)
        np.testing.assert_allclose(tensor[0, :, t['top']+10, t['left']+10], [1,12/255,6/255], atol=1e-6)
        source=np.array([30., 10., 80., 40.]);p=source*t['gain']+[t['left'],t['top'],t['left'],t['top']]
        center=(p[:2]+p[2:])/2;wh=p[2:]-p[:2]
        raw=np.array([[*center,*wh,.9,.1]],np.float32).T[None]
        records,e=d._decode_mfd(raw,t,rgb.shape,{0:'embedding',1:'isolated'})
        np.testing.assert_allclose(records[0]['bbox'],source,atol=1e-4)
        self.assertEqual(records[0]['class_name'],'embedding')

    def test_mfd_raw_class_scores_have_no_extra_objectness(self):
        raw=np.array([[100,100,20,20,.1,.92]],np.float32).T[None]
        boxes,e=d._decode_mfd(raw,{'gain':1.,'left':0,'top':0},self.rgb.shape,{0:'embedding',1:'isolated'})
        self.assertEqual(len(boxes),1);self.assertEqual(boxes[0]['class_name'],'isolated')
        self.assertAlmostEqual(boxes[0]['confidence'],.92,places=6)

    def test_nms_only_deduplicates_same_class(self):
        rows=np.array([[100,100,20,20,.95,.01],[100,100,20,20,.9,.01],[100,100,20,20,.01,.94]],np.float32)
        boxes,e=d._decode_mfd(rows.T[None],{'gain':1.,'left':0,'top':0},self.rgb.shape,{0:'embedding',1:'isolated'})
        self.assertEqual(len(boxes),2);self.assertEqual(e['same_class_duplicate_anchors_suppressed'],1)
        self.assertEqual({b['class_name'] for b in boxes},{'embedding','isolated'})

    def test_unexpected_or_nonfinite_model_output_rejected(self):
        for raw in [np.zeros((1,7,10)),np.full((1,6,1),np.nan),np.zeros((2,6,10))]:
            with self.assertRaises(d.DetectorUnavailable):d._decode_mfd(raw,{'gain':1.,'left':0,'top':0},self.rgb.shape,{0:'embedding',1:'isolated'})

    def test_missing_model_never_downloads(self):
        with patch('socket.socket.connect',side_effect=AssertionError('Network attempted')):
            with self.assertRaises(d.DetectorUnavailable): self.run_detect(formula=Path('missing-local-mfd.onnx'))

    def test_unpinned_model_fails_before_runtime_load(self):
        temp_root=Path(__file__).resolve().parent/'_tmp_scan_detectors'/uuid.uuid4().hex
        temp_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,temp_root)
        p=temp_root/'model.onnx';p.write_bytes(b'invalid model')
        with self.assertRaisesRegex(d.DetectorUnavailable,'hash differs'):d.FormulaDetector(p)


if __name__=='__main__': unittest.main()
