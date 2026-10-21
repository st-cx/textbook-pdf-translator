"""Raster source ownership regressions; no PDF text layer or paid service."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from textbook_pdf.scan_quality import audit_scan_page


class ScanQualityTests(unittest.TestCase):
    @staticmethod
    def image(gray=255, width=240, height=160):
        return np.full((height, width, 3), gray, np.uint8)

    @staticmethod
    def region(bbox, owner="element-1", **extra):
        return {"bbox": bbox, "owner": owner, **extra}

    @staticmethod
    def word(bbox, text="source", confidence=.99):
        return {"bbox": bbox, "text": text, "confidence": confidence}

    @staticmethod
    def codes(report):
        return {i["code"] for i in report["issues"]}

    def test_omitted_whole_block_has_bbox_area_and_blocking_issue(self):
        image = self.image()
        image[20:35, 20:90] = 0
        image[85:125, 145:205] = 0
        result = audit_scan_page(image, [self.region([20,20,90,35])],
                                 [self.word([20,20,90,35])])
        self.assertIn("scan_unassigned_ink", self.codes(result))
        self.assertEqual(result["unassigned_ink_pixels"], 2400)
        block = result["unassigned_components"][0]
        self.assertEqual(block["bbox"], [145,85,205,125])
        self.assertEqual(block["area"], 2400)
        self.assertTrue(block["meaningful"])
        self.assertFalse(result["raster_coverage_complete"])

    def test_low_confidence_unit_survives_complete_region_coverage(self):
        image = self.image(); image[25:40,25:100] = 0
        result = audit_scan_page(image,[self.region([20,20,105,45])],
                                 [self.word([25,25,100,40],confidence=.52)])
        self.assertEqual(result["ink_coverage_ratio"],1)
        self.assertEqual(result["low_confidence_units"][0]["confidence"],.52)
        self.assertIn("scan_low_confidence_ocr", self.codes(result))
        self.assertFalse(result["optical_recognition_verified"])
        self.assertIsNone(result["recognition_accuracy"])

    def test_no_original_text_layer_is_needed_but_zero_ocr_never_proves_recognition(self):
        image = self.image(); cv2.putText(image,"SCAN",(15,60),cv2.FONT_HERSHEY_SIMPLEX,1,(0,0,0),2)
        result = audit_scan_page(image,[self.region([10,20,120,70])],[])
        self.assertGreater(result["foreground_pixels"],0)
        self.assertEqual(result["unassigned_ink_pixels"],0)
        self.assertIn("scan_no_ocr_units",self.codes(result))
        self.assertFalse(result["optical_recognition_verified"])
        self.assertEqual(result["coverage_basis"],"raster-ink-and-ocr-units")

    def test_formula_and_diagram_are_owned_as_original_raster_regions(self):
        image = self.image()
        cv2.putText(image,"a",(20,35),cv2.FONT_HERSHEY_SIMPLEX,.5,(0,0,0),1)
        cv2.line(image,(15,42),(55,42),(0,0,0),1)
        cv2.putText(image,"b",(20,62),cv2.FONT_HERSHEY_SIMPLEX,.5,(0,0,0),1)
        cv2.rectangle(image,(120,25),(155,55),(0,0,0),1)
        cv2.arrowedLine(image,(155,40),(205,40),(0,0,0),1)
        result = audit_scan_page(image,[self.region([10,10,65,68],"formula",kind="formula_display"),
                                       self.region([115,20,212,62],"diagram",kind="figure")],[])
        self.assertEqual(result["unassigned_components"],[])
        self.assertEqual(result["covered_ink_pixels"],result["foreground_pixels"])
        self.assertTrue(result["raster_coverage_complete"])
        self.assertFalse(result["optical_recognition_verified"])

    def test_gray_gradient_background_is_not_counted_as_whole_page_ink(self):
        gradient = np.tile(np.linspace(155,225,240,dtype=np.uint8),(160,1))
        image = np.repeat(gradient[:,:,None],3,axis=2)
        cv2.putText(image,"Gray scan",(10,80),cv2.FONT_HERSHEY_SIMPLEX,.75,(25,25,25),2)
        result = audit_scan_page(image,[self.region([5,45,170,90])],
                                 [self.word([10,50,160,85],"Gray scan")])
        self.assertGreater(result["foreground_pixels"],300)
        self.assertLess(result["foreground_pixels"],image.shape[0]*image.shape[1]*.2)
        self.assertEqual(result["unassigned_ink_pixels"],0)

    def test_uniform_gray_background_and_missing_text_block(self):
        image = self.image(gray=190)
        cv2.putText(image,"lost",(90,90),cv2.FONT_HERSHEY_SIMPLEX,1,(30,30,30),2)
        result = audit_scan_page(image,[],[])
        self.assertGreater(result["unassigned_ink_pixels"],100)
        self.assertLess(result["foreground_pixels"],2000)
        self.assertIn("scan_unassigned_ink",self.codes(result))

    def test_bbox_tolerance_exactly_two_pixels_not_arbitrary_growth(self):
        image=self.image();image[30:50,30:50]=0
        accepted=audit_scan_page(image,[self.region([32,32,48,48])],[])
        self.assertEqual(accepted["unassigned_ink_pixels"],0)
        missed=audit_scan_page(image,[self.region([35,32,48,48])],[])
        self.assertGreater(missed["unassigned_ink_pixels"],0)
        self.assertIn("scan_unassigned_ink",self.codes(missed))
        self.assertEqual(missed["thresholds"]["source_bbox_tolerance_pixels"],2)

    def test_isolated_tiny_noise_is_explicit_and_retained_in_raw_counts(self):
        image=self.image();image[20:40,20:60]=0;image[120,200]=0
        result=audit_scan_page(image,[self.region([20,20,60,40])],[])
        self.assertEqual(result["unassigned_ink_pixels"],1)
        self.assertEqual(result["unassigned_noise_pixels"],1)
        self.assertEqual(result["unassigned_meaningful_ink_pixels"],0)
        self.assertTrue(result["unassigned_components"][0]["tiny_isolated_noise"])
        self.assertIn("scan_tiny_unassigned_noise",self.codes(result))
        self.assertLess(result["ink_coverage_ratio"],1)

    def test_small_nearby_marks_are_not_silently_classified_as_noise(self):
        image=self.image();image[70,80]=0;image[70,83]=0
        result=audit_scan_page(image,[],[])
        self.assertEqual(result["unassigned_noise_pixels"],0)
        self.assertEqual(result["unassigned_meaningful_component_count"],2)
        self.assertIn("scan_unassigned_ink",self.codes(result))

    def test_bbox_without_owner_does_not_establish_coverage(self):
        image=self.image();image[40:60,40:80]=0
        result=audit_scan_page(image,[{"bbox":[0,0,240,160]}],[])
        self.assertEqual(result["covered_ink_pixels"],0)
        self.assertIn("scan_region_owner_missing",self.codes(result))
        self.assertFalse(result["raster_coverage_complete"])

    def test_invalid_geometry_is_not_normalized_into_coverage(self):
        image=self.image();image[40:60,40:80]=0
        result=audit_scan_page(image,[self.region([100,90,10,20])],[])
        self.assertIn("scan_region_bbox_invalid",self.codes(result))
        self.assertEqual(result["covered_ink_pixels"],0)

    def test_unknown_and_percentage_confidence_are_not_inferred(self):
        image=self.image();image[30:50,30:100]=0
        result=audit_scan_page(image,[self.region([30,30,100,50])],
                               [self.word([30,30,65,50],confidence=95),self.word([65,30,100,50],confidence=None)])
        self.assertEqual(result["ocr_units"]["unknown_confidence"],2)
        self.assertIn("scan_ocr_confidence_unavailable",self.codes(result))
        self.assertEqual(result["low_confidence_units"],[])

    def test_blank_image_is_not_one_hundred_percent_recognition(self):
        result=audit_scan_page(self.image(),[],[])
        self.assertEqual(result["foreground_pixels"],0)
        self.assertIsNone(result["ink_coverage_ratio"])
        self.assertFalse(result["raster_coverage_complete"])
        self.assertIn("scan_no_visible_ink",self.codes(result))
        self.assertIsNone(result["recognition_accuracy"])

    def test_high_confidence_wrong_word_does_not_certify_optical_accuracy(self):
        image=self.image();cv2.putText(image,"ADTs",(20,60),cv2.FONT_HERSHEY_SIMPLEX,1,(0,0,0),2)
        result=audit_scan_page(image,[self.region([10,20,120,70])],[self.word([15,25,110,65],text="apts",confidence=1)])
        self.assertEqual(result["ink_coverage_ratio"],1)
        self.assertFalse(result["optical_recognition_verified"])
        self.assertIsNone(result["recognition_accuracy"])
        self.assertTrue(any("omitted" in s for s in result["limitations"]))

    def test_pale_colored_graphic_remains_visible_foreground(self):
        image=self.image();image[40:70,120:160]=[255,255,200]
        result=audit_scan_page(image,[],[])
        self.assertEqual(result["unassigned_ink_pixels"],1200)
        self.assertIn("scan_unassigned_ink",self.codes(result))

    def test_input_objects_are_immutable_and_owner_id_alias_is_explicit(self):
        image=self.image();image[20:40,20:70]=0
        regions=[{"bbox":[20,20,70,40],"owner_id":"e1"}]
        words=[self.word([20,20,70,40])]
        snapshot=(image.copy(),deepcopy(regions),deepcopy(words))
        result=audit_scan_page(image,regions,words)
        np.testing.assert_array_equal(image,snapshot[0]);self.assertEqual(regions,snapshot[1]);self.assertEqual(words,snapshot[2])
        self.assertEqual(result["covered_ink_pixels"],result["foreground_pixels"])

    def test_black_raster_is_not_misreported_as_a_blank_white_page(self):
        result=audit_scan_page(self.image(gray=0),[],[])
        self.assertEqual(result["foreground_pixels"],240*160)
        self.assertEqual(result["unassigned_ink_pixels"],240*160)
        self.assertIn("scan_unassigned_ink",self.codes(result))
        self.assertNotIn("scan_no_visible_ink",self.codes(result))

    def test_component_report_cap_never_turns_omitted_evidence_into_pass(self):
        image=self.image();image[10:20,10:20]=0;image[50:60,80:90]=0;image[100:110,180:190]=0
        with patch("textbook_pdf.scan_quality.MAX_REPORTED_COMPONENTS",1):
            result=audit_scan_page(image,[],[])
        self.assertEqual(result["unassigned_component_count"],3)
        self.assertEqual(len(result["unassigned_components"]),1)
        self.assertEqual(result["omitted_component_count"],2)
        self.assertEqual(result["unassigned_meaningful_component_count"],3)
        self.assertIn("scan_component_evidence_truncated",self.codes(result))
        self.assertFalse(result["raster_coverage_complete"])

    def test_image_and_confidence_contract_is_checked(self):
        for image in [np.zeros((20,20),np.uint8),np.zeros((20,20,4),np.uint8),np.zeros((20,20,3),float)]:
            with self.assertRaises(ValueError):audit_scan_page(image,[],[])
        for confidence in [-1,1.1,float("nan"),True]:
            with self.assertRaises(ValueError):audit_scan_page(self.image(),[],[],min_confidence=confidence)


if __name__=="__main__":
    unittest.main()
