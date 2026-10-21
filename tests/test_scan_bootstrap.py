"""Explicit provisioning tests; all HTTP responses are mocked."""
from contextlib import redirect_stdout
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import bootstrap as b, ocr_engine as ocr, scan_detectors as detectors


class ScanBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.parent=Path(__file__).resolve().parent/'_tmp_scan_bootstrap'
        self.scratch=self.parent/uuid.uuid4().hex
        self.scratch.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.wheel=self.scratch/'wheel';self.output=self.scratch/'models'
        self.payload=b'pinned-model-test-data'
        self.sha=hashlib.sha256(self.payload).hexdigest()

    def cleanup(self):
        resolved=self.scratch.resolve()
        assert resolved.is_relative_to(self.parent.resolve()) and resolved!=self.parent.resolve()
        shutil.rmtree(resolved)

    def formula_patches(self):
        return patch.multiple(b,MFD_BYTES=len(self.payload)),patch.object(detectors,'MFD_SHA256',self.sha)

    def fake_wheel(self):
        specs=[]
        for role in ('det','cls','rec'):
            name=role+'.onnx';content=(role+'-model').encode()
            dest=self.wheel/'rapidocr/models'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content)
            specs.append({'role':role,'filename':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()})
        notices=self.wheel/'rapidocr-3.10.0.dist-info/licenses';notices.mkdir(parents=True)
        for name in ('LICENSE','MODEL_LICENSES.md'):(notices/name).write_text('Upstream attribution '+name,encoding='utf8')
        return tuple(specs),SimpleNamespace(version='3.10.0',locate_file=lambda p:self.wheel/p)

    def test_ocr_copies_verified_wheel_models_and_notices_without_http(self):
        specs,dist=self.fake_wheel()
        with patch.object(ocr,'MODEL_SPECS',specs),patch.object(b.metadata,'distribution',return_value=dist),patch.object(b,'urlopen',side_effect=AssertionError('HTTP must not be used')):
            result=b.prepare_ocr_models(self.output)
            self.assertTrue(result['ready']);self.assertFalse(result['downloaded'])
            self.assertTrue(all(r['copied'] for r in result['models']))
            self.assertTrue(all((self.output/s['filename']).read_bytes()==(self.wheel/'rapidocr/models'/s['filename']).read_bytes() for s in specs))
            self.assertEqual(len(result['attribution_files']),2)
            again=b.prepare_ocr_models(self.output)
            self.assertTrue(all(not r['copied'] for r in again['models']))

    def test_bad_wheel_model_fails_before_replacing_any_ocr_file(self):
        specs,dist=self.fake_wheel();self.output.mkdir()
        preserved=self.output/specs[0]['filename'];preserved.write_bytes(b'previous model')
        (self.wheel/'rapidocr/models'/specs[-1]['filename']).write_bytes(b'corrupt')
        with patch.object(ocr,'MODEL_SPECS',specs),patch.object(b.metadata,'distribution',return_value=dist):
            with self.assertRaisesRegex(b.ModelPreparationError,'checksum/size'):b.prepare_ocr_models(self.output)
        self.assertEqual(preserved.read_bytes(),b'previous model');self.assertEqual(len(list(self.output.iterdir())),1)

    def test_wrong_rapidocr_version_does_not_copy(self):
        specs,dist=self.fake_wheel();dist.version='3.9.0'
        with patch.object(b.metadata,'distribution',return_value=dist):
            with self.assertRaisesRegex(b.ModelPreparationError,'Expected rapidocr==3.10.0'):b.prepare_ocr_models(self.output)
        self.assertFalse(self.output.exists())

    def test_missing_attribution_prevents_ocr_copy(self):
        specs,dist=self.fake_wheel();(self.wheel/'rapidocr-3.10.0.dist-info/licenses/MODEL_LICENSES.md').unlink()
        with patch.object(ocr,'MODEL_SPECS',specs),patch.object(b.metadata,'distribution',return_value=dist):
            with self.assertRaisesRegex(b.ModelPreparationError,'attribution'):b.prepare_ocr_models(self.output)
        self.assertFalse(self.output.exists())

    def test_ocr_local_environment_destination(self):
        specs,dist=self.fake_wheel()
        with patch.object(ocr,'MODEL_SPECS',specs),patch.object(b.metadata,'distribution',return_value=dist),patch.dict('os.environ',{'TEXTBOOK_OCR_MODELS':str(self.output)}):
            self.assertEqual(Path(b.prepare_ocr_models()['model_dir']),self.output.resolve())

    def test_missing_formula_download_uses_only_fixed_revision_and_validates(self):
        size_patch,sha_patch=self.formula_patches()
        with size_patch,sha_patch,patch.object(b,'urlopen',return_value=BytesIO(self.payload)) as request:
            result=b.prepare_formula_model(self.output/'formula.onnx')
        self.assertEqual((self.output/'formula.onnx').read_bytes(),self.payload)
        url=request.call_args.args[0].full_url
        self.assertIn(detectors.MFD_REVISION,url);self.assertTrue(url.startswith('https://huggingface.co/breezedeus/pix2text-mfd-1.5/resolve/'))
        self.assertTrue(result['downloaded']);self.assertEqual(result['sha256'],self.sha)
        self.assertTrue(result['license_evidence']['declarations_differ']);self.assertFalse(list(self.output.glob('*.part')))

    def test_correct_formula_is_reused_without_request(self):
        self.output.mkdir();target=self.output/'formula.onnx';target.write_bytes(self.payload)
        size_patch,sha_patch=self.formula_patches()
        with size_patch,sha_patch,patch.object(b,'urlopen',side_effect=AssertionError('Valid local model must not download')):
            self.assertFalse(b.prepare_formula_model(target)['downloaded'])

    def test_bad_download_preserves_previous_model_and_cleans_temporary(self):
        self.output.mkdir();target=self.output/'formula.onnx';target.write_bytes(b'previous model')
        size_patch,sha_patch=self.formula_patches()
        with size_patch,sha_patch,patch.object(b,'urlopen',return_value=BytesIO(b'incorrect bytes')):
            with self.assertRaises(b.ModelPreparationError):b.prepare_formula_model(target)
        self.assertEqual(target.read_bytes(),b'previous model');self.assertEqual(list(self.output.iterdir()),[target])

    def test_oversized_download_preserves_previous_model(self):
        self.output.mkdir();target=self.output/'formula.onnx';target.write_bytes(b'previous model')
        size_patch,sha_patch=self.formula_patches()
        with size_patch,sha_patch,patch.object(b,'urlopen',return_value=BytesIO(self.payload+b'too much')):
            with self.assertRaisesRegex(b.ModelPreparationError,'exceeds pinned size'):b.prepare_formula_model(target)
        self.assertEqual(target.read_bytes(),b'previous model');self.assertEqual(len(list(self.output.iterdir())),1)

    def test_network_failure_preserves_previous_model(self):
        self.output.mkdir();target=self.output/'formula.onnx';target.write_bytes(b'previous model')
        with patch.object(b,'urlopen',side_effect=OSError('offline')):
            with self.assertRaisesRegex(b.ModelPreparationError,'previous file preserved'):b.prepare_formula_model(target)
        self.assertEqual(target.read_bytes(),b'previous model')

    def test_atomic_replace_failure_preserves_previous_model(self):
        self.output.mkdir();target=self.output/'formula.onnx';target.write_bytes(b'previous model')
        with patch.object(Path,'replace',side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):b._install_stream(BytesIO(self.payload),target,len(self.payload),self.sha)
        self.assertEqual(target.read_bytes(),b'previous model');self.assertEqual(len(list(self.output.iterdir())),1)

    def test_formula_destination_must_not_be_url(self):
        with patch.object(b,'urlopen',side_effect=AssertionError('Must reject before request')):
            with self.assertRaisesRegex(b.ModelPreparationError,'local filesystem'):b.prepare_formula_model('https://example.invalid/model.onnx')

    def test_formula_local_environment_destination(self):
        self.output.mkdir();target=self.output/'from-env.onnx';target.write_bytes(self.payload)
        size_patch,sha_patch=self.formula_patches()
        with size_patch,sha_patch,patch.dict('os.environ',{'TEXTBOOK_MFD_MODEL':str(target)}),patch.object(b,'urlopen',side_effect=AssertionError('No request')):
            self.assertEqual(Path(b.prepare_formula_model()['path']),target.resolve())

    def test_existing_layout_route_still_checks_pinned_sha3(self):
        from textbook_pdf import babel_adapter,paragraph_adapter
        target=self.scratch/'doclayout.onnx';target.write_bytes(self.payload)
        module=SimpleNamespace(get_doclayout_onnx_model_path=lambda:target)
        with patch.object(babel_adapter,'available',return_value=(True,'0.6.4')),patch.object(babel_adapter,'_configure_cache') as cache,patch.object(paragraph_adapter,'MODEL_SHA3_256',hashlib.sha3_256(self.payload).hexdigest()),patch.dict('sys.modules',{'babeldoc.assets.assets':module}):
            self.assertTrue(b.prepare_layout_model()['ready']);cache.assert_called_once()

    def test_prepare_all_reports_partial_failures_and_main_returns_nonzero(self):
        with patch.object(b,'prepare_layout_model',return_value={'ready':True}),patch.object(b,'prepare_ocr_models',side_effect=b.ModelPreparationError('Missing local OCR wheel')),patch.object(b,'prepare_formula_model',return_value={'ready':True}):
            result=b.prepare_models();self.assertTrue(result['layout_model_ready']);self.assertFalse(result['ocr_models_ready']);self.assertTrue(result['formula_model_ready'])
            output=StringIO()
            with redirect_stdout(output):self.assertEqual(b.main(),1)
            self.assertEqual(json.loads(output.getvalue())['errors'][0]['model_group'],'ocr')
            self.assertFalse(result['book_data_sent'])


if __name__=='__main__':unittest.main()
