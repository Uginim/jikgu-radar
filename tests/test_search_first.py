"""Deterministic fault and safety edges. Not live retailer/model evidence."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from nextask.core import altsearch, search_first as batch


def product(i, **kw):
    return {'url': f'https://example.com/pr/{i}', 'title': f'Product {i}', **kw}


def inspected(i, key='not_found', complete=True, ingredient='Vitamin C'):
    gate = NS(spec=NS(kind='label'), data={'review_requirements': {'complete': complete, 'missing': []}})
    it = NS(spec=NS(kind='ingredient'), data={'name': ingredient}, status='resolved')
    return NS(id=str(i), items=[gate, it], status='done', verdict={'key': key, 'why': 'source verdict'}, data={}), None


class BatchTests(unittest.TestCase):
    def test_clarification_changes_search_words_without_changing_original_request(self):
        from nextask.core.zoro import Zoro
        text = '관절이나 운동용으로 카페인 없는 캡슐 찾아줘'
        result = altsearch.interpret(NS(zoro=Zoro(mode='mock')), text)
        self.assertEqual(result['conditions']['query'], text)
        self.assertEqual(result['question']['search_queries'],
                         {'pre_workout':'pre workout capsules caffeine free','joint':'joint capsules caffeine free'})

    def test_transient_failure_goes_last_without_losing_or_duplicating_rows(self):
        b = batch.Batch(candidates=[product(1), product(2), product(3, read_error='일반 로드 재시도 후 실패')]); seen=[]
        def inspect(engine, p, save):
            seen.append(p.title)
            if len(seen) == 1:
                raise TimeoutError()
            return inspected(p.title)
        with patch.object(batch, '_inspect', inspect):
            events = list(batch.review(NS(zoro=NS(mode='remote')), b, None))
        self.assertEqual(seen, ['Product 1', 'Product 2', 'Product 1'])
        self.assertEqual([(e['index'],e['row']['group']) for e in events if e['event']=='row'],
                         [(2, 'candidate'),(3,'review'),(1,'candidate')])
        self.assertEqual(events[-1]['checked'], 2)
        self.assertEqual(events[-1]['counts'], {'candidate':2,'hold':0,'review':1})

    def test_partial_positive_is_held_partial_negative_is_not_cleared(self):
        self.assertEqual(batch._summary(*inspected(1,'blocked',False))['group'], 'hold')
        row=batch._summary(*inspected(2,'not_found',False));self.assertFalse(row['checked']);self.assertEqual(row['group'],'review')

    def test_exclusion_and_money_can_only_restrict(self):
        c,_ = inspected(1,ingredient='Caffeine anhydrous')
        b = batch.Conditions(query='카페인 없는 운동 전 보충제',use='pre_workout',exclude=['카페인'])
        row = batch._summary(c,None)
        batch._apply_conditions(None,b,batch.Product(**product(1)),c,row)
        self.assertEqual(row['group'],'hold');self.assertIn('카페인',row['reason'])
        low, high = altsearch.price_bounds('1~3만원')
        assert low is not None and high is not None
        self.assertEqual((low['value'], high['value']), (10000, 30000))
        _,high = altsearch.price_bounds('under $30'); b=batch.Conditions(query='under $30',use='general',price_max=high)
        for price,group in [(None,'review'),({'value':29, 'currency':'KRW'},'review'),({'value':30,'currency':'USD'},'hold')]:
            with self.subTest(price=price):
                row=batch._summary(c,None);batch._apply_conditions(None,b,batch.Product(**product(1,price=price)),c,row)
                self.assertEqual(row['group'],group)

    def test_confirmation_resumes_original_question_once_and_rejects_changed_evidence(self):
        p = batch.Product(**product(1, page_text='original', ingredient_html='<p>original</p>'))
        question = NS(id='q_confirm_label_current', text='original evidence', kind='label_confirm')
        case = NS(id='parent', plugin=object(), image_path=None, items=[], pending=(None, question), status='waiting_user')
        answers = []
        def answer(c, value):
            answers.append((c.id, c.pending[1].id, value))
            c.status, c.pending = 'done', None
        engine = NS(answer=answer)
        with patch.object(batch, '_pending', {}), patch.object(batch.caselog, 'log_event'):
            original, confirm = batch._finish(engine, case, p)
            self.assertIsNone(original.pending)
            assert confirm is not None
            reply = p.model_copy(update={'label_confirmation':confirm['id']})
            with self.assertRaises(batch._ExpiredConfirmation):
                batch._resume(engine, reply.model_copy(update={'page_text':'different'}))
            resumed, _ = batch._resume(engine, reply)
            self.assertNotEqual(resumed.id, original.id)
            self.assertEqual(answers, [(original.id, question.id, None), (resumed.id, question.id, True)])
            with self.assertRaises(batch._ExpiredConfirmation):
                batch._resume(engine, reply)


if __name__=='__main__':
    unittest.main()
