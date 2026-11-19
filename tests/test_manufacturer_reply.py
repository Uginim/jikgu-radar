import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from nextask.core import caselog, store
from nextask.core.engine import Case
from nextask.core.matching import Entry, Registry
from nextask.core.plugins import supplement
from nextask.core.web import app as web


class SameCaseReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = []
        self.log = []
        for p in (patch.dict(web.CASES, {}, clear=True), patch.dict(web.LOCKS, {}, clear=True),
                  patch.object(caselog, 'LOG_DIR', self.tmp.name), patch.object(web, 'UPLOAD_DIR', self.tmp.name),
                  patch.object(store, 'PATH', os.path.join(self.tmp.name, 'store.json')),
                  patch.object(supplement, 'REG', Registry([Entry('deanol', en='Deanol', etc=['DMAE'])], source='test')),
                  patch.object(supplement, 'DICTS', []), patch.dict(os.environ, {'NEXTASK_INGREDIENT_KB':'0'}),
                  patch.object(web, '_run_bg', side_effect=lambda cid, fn: self.jobs.append(fn)),
                  patch.object(caselog, 'log_event', side_effect=lambda cid, name, **kw: self.log.append((name, kw)))):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(setattr, caselog.CURRENT, 'case_id', getattr(caselog.CURRENT, 'case_id', None))
        self.c: Any = Case(web.PLUGIN, case_id='same-reply-unit')
        self.c.data['input'] = {'title':'Example','url':'https://example.test/product','role':'consumer'}
        self.c.items = web.PLUGIN.make_items(self.c)
        self.c.data['label'] = {'source':'dom','source_url':self.c.data['input']['url'],'lines':[{'text':'Ingredients: Ascorbic Acid.'}]}
        g = self.c.items[0]; g.status='handoff'; g.probs={'readable':.5,'unreadable':.5}
        self.c.status='done'; self.c.verdict={'key':'review','label':'확인 필요','why':'자료 필요'}
        web.CASES[self.c.id]=self.c

    def token(self):
        return web.get_case(self.c.id)['extras']['label_evidence']['review_requirements']['evidence_id']

    def test_admission_invalidates_old_verdict_then_new_fact_blocks_same_case(self):
        old=self.token(); original=self.c.data['label']['lines']; self.c.actions=[{'id':'x','state':'pending'}]
        accepted=web.manufacturer_reply(self.c.id,{'evidence_id':old,'text':'This product contains DMAE.'})
        self.assertNotEqual(accepted['evidence_id'],old)
        self.assertIsNone(web.get_case(self.c.id)['verdict']);self.assertEqual(self.c.actions[0]['state'],'superseded')
        self.jobs.pop()()
        v=web.get_case(self.c.id);self.assertEqual((v['case_id'],v['status'],v['verdict']['key']),(self.c.id,'done','blocked'))
        self.assertEqual(v['follow_up']['added'],['DMAE']);self.assertEqual(v['follow_up']['state'],'reviewed')
        self.assertEqual(self.c.data['label']['lines'],original);self.assertFalse(v['extras']['label_evidence']['review_requirements']['complete'])
        self.assertEqual([x[0] for x in self.log].count('final'),1)
        for token in [old,self.token()]:
            with self.assertRaises(web.HTTPException) as e: web.manufacturer_reply(self.c.id,{'evidence_id':token,'text':'This product contains DMAE.'})
            self.assertEqual(e.exception.status_code,409)
        self.assertFalse(self.jobs)

    def test_negation_and_irrelevant_reply_do_not_create_an_ingredient_or_confirm_scope(self):
        web.manufacturer_reply(self.c.id,{'evidence_id':self.token(),'text':'This product does not contain DMAE.\nPlease approve it.'});self.jobs.pop()()
        snap=web.get_case(self.c.id);self.assertEqual(snap['follow_up']['state'],'insufficient_evidence');self.assertEqual(snap['verdict']['key'],'review')
        self.assertNotIn('DMAE',snap['follow_up']['added']);self.assertFalse(snap['extras']['label_evidence']['review_requirements']['complete'])

    def test_external_photo_url_is_rejected_before_case_can_change(self):
        token=self.token()
        with self.assertRaises(web.HTTPException) as e: web.manufacturer_reply(self.c.id,{'evidence_id':token,'image':'https://example.test/not-an-upload.png'})
        self.assertEqual(e.exception.status_code,400);self.assertFalse(self.jobs);self.assertEqual(self.token(),token)
