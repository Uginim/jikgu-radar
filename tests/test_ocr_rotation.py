import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from PIL import Image
from nextask.core.ocr import RemoteOCR
from nextask.core._infra_client import ZoroUnavailable


class OCRRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'label.png'
        Image.new('RGB', (120, 80), 'white').save(self.path)
        self.adapter = RemoteOCR.__new__(RemoteOCR)
        self.adapter._orientation = Mock(return_value=(0, {}))

    def response(self, text, rid):
        return {'text': text, 'request_id': rid, 'attempts': [{'ok': True, 'request_id': rid}]}

    def test_rotates_at_most_twice_and_keeps_best_panel_with_all_attempts(self):
        call = Mock(side_effect=[self.response('WARNING: consult doctor', 'original'),
            self.response('Supplement Facts\nVitamin C 100 mg\nOther ingredients: cellulose', 'ccw'),
            self.response('WARNING with many many words', 'cw')])
        self.adapter.client = SimpleNamespace(ocr=call)
        result = self.adapter.read(str(self.path))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result['response']['request_id'], 'ccw')
        self.assertEqual(result['rotation_retry']['selected_angle'], 90)
        self.assertEqual([r['response']['request_id'] for r in result['rotation_retry']['candidates']],
                         ['original', 'ccw'])
        self.assertEqual(result['attempt_count'], 2)
        self.assertEqual(base64.b64decode(call.call_args_list[0].args[0]), self.path.read_bytes())
        for args in call.call_args_list[1:]:
            with Image.open(io.BytesIO(base64.b64decode(args.args[0]))) as im:
                self.assertEqual(im.size, (80, 120))

    def test_clear_panel_does_not_rotate(self):
        call = Mock(return_value=self.response('Supplement Facts\nVitamin C 100 mg', 'original'))
        self.adapter.client = SimpleNamespace(ocr=call)
        self.assertNotIn('rotation_retry', self.adapter.read(str(self.path)))
        call.assert_called_once()

    def test_failed_rotation_preserves_original_success_and_failure_attempts(self):
        call = Mock(side_effect=[self.response('WARNING', 'original'),
            ZoroUnavailable('timeout', attempts=[{'ok': False}]), self.response('WARNING', 'cw')])
        self.adapter.client = SimpleNamespace(ocr=call)
        result = self.adapter.read(str(self.path))
        self.assertEqual(result['response']['request_id'], 'original')
        self.assertEqual(result['rotation_retry']['selected_angle'], 0)
        self.assertEqual(result['attempt_count'], 3)
        self.assertFalse(result['attempts'][1]['ok'])

    def test_direction_hint_reads_rotated_panel_first_and_stops(self):
        self.adapter._orientation.return_value = (90, {'confidence': .3354})
        call = Mock(return_value=self.response('Supplement Facts\nVitamin C 100 mg', 'ccw'))
        self.adapter.client = SimpleNamespace(ocr=call)
        result = self.adapter.read(str(self.path))
        call.assert_called_once()
        self.assertEqual(result['rotation_retry']['selected_angle'], 90)
        with Image.open(io.BytesIO(base64.b64decode(call.call_args.args[0]))) as im:
            self.assertEqual(im.size, (80, 120))

    def test_same_case_same_bytes_reuses_without_claiming_new_http_attempts(self):
        import threading
        from nextask.core import caselog
        self.adapter._cache, self.adapter._cache_lock = {}, threading.Lock()
        call = Mock(return_value=self.response('Supplement Facts\nVitamin C 100 mg', 'original'))
        self.adapter.client = SimpleNamespace(ocr=call)
        previous = getattr(caselog.CURRENT, 'case_id', None)
        try:
            caselog.CURRENT.case_id = 'case-a'
            first = self.adapter.read(str(self.path))
            first['lines'].clear()
            reused = self.adapter.read(str(self.path))
            self.assertEqual(reused['attempt_count'], 0)
            self.assertTrue(reused['lines'])
            self.assertEqual(reused['cache_reuse']['request_id'], 'original')
            call.assert_called_once()
            caselog.CURRENT.case_id = 'case-b'
            self.adapter.read(str(self.path))
            self.assertEqual(call.call_count, 2)
            Image.new('RGB', (120, 80), 'black').save(self.path)
            self.adapter.read(str(self.path))
            self.assertEqual(call.call_count, 3)
        finally:
            caselog.CURRENT.case_id = previous

    def test_upright_hint_prevents_unnecessary_quarter_turn(self):
        self.adapter._orientation = RemoteOCR._orientation.__get__(self.adapter)
        decide = Mock(return_value={'answers': {
            'orientation': {'choice': '90', 'probabilities': {'90': .32}},
            'upright': {'choice': 'upright'}}})
        self.adapter.client = SimpleNamespace(decide=decide)
        angle, metadata = self.adapter._orientation(self.path.read_bytes())
        self.assertEqual(angle, 0)
        self.assertIn('response', metadata)
        decide.return_value['answers']['upright']['choice'] = 'sideways'
        angle, _ = self.adapter._orientation(self.path.read_bytes())
        self.assertEqual(angle, 90)
