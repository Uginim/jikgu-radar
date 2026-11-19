"""Read-only seller batch screening. The browser owns navigation; the engine owns verdicts."""
import hashlib
import math
import re
import secrets
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from . import altsearch, caselog
from .matching import norm


class Money(BaseModel):
    value: float = Field(ge=0, allow_inf_nan=False)
    currency: Literal['USD', 'KRW']


class PriceBound(Money):
    exclusive: bool = False


class Conditions(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    use: str
    form: str | None = None
    exclude: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=20)
    price_min: PriceBound | None = None
    price_max: PriceBound | None = None

    @model_validator(mode='after')
    def known_choices(self):
        if self.use not in altsearch.CLS['use'] or self.form is not None and self.form not in altsearch.CLS['form']:
            raise ValueError('알 수 없는 용도 또는 제형')
        return self


class GalleryImage(BaseModel):
    url: str = Field(max_length=8000)
    data: str | None = None


class Product(BaseModel):
    url: str = Field(max_length=4000)
    source_url: str | None = Field(default=None, max_length=4000)
    title: str = Field(default='', max_length=1000)
    brand: str = Field(default='', max_length=300)
    brand_source: str | None = None
    barcode: str = Field(default='', max_length=100)
    barcode_source: str | None = None
    page_text: str = Field(default='', max_length=200000)
    ingredient_html: str = Field(default='', max_length=50000)
    images: list[str | GalleryImage] = Field(default_factory=list, max_length=8)
    price: Money | None = None
    read_error: str | None = Field(default=None, max_length=500)
    label_confirmation: str | None = Field(default=None, max_length=100)

    @field_validator('url')
    @classmethod
    def public_page_url(cls, value):
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None:
            raise ValueError('HTTP(S) 상품 주소가 필요합니다')
        return value


class Batch(BaseModel):
    conditions: Conditions | None = None
    candidates: list[Product] = Field(min_length=1, max_length=5)

    @model_validator(mode='after')
    def unique_products(self):
        urls = [p.url.split('#')[0] for p in self.candidates]
        if len(set(urls)) != len(urls):
            raise ValueError('중복 상품 주소')
        return self


# Single-process seller-local API. Park only original engine label questions;
# bounded lifetime/count, never store payload images or let a token be replayed.
_pending = {}
_pending_lock = threading.Lock()


class _ExpiredConfirmation(ValueError):
    pass


def _evidence_key(p):
    return hashlib.sha256(p.model_dump_json(exclude={'label_confirmation'}).encode()).digest()


def _remember(case, p):
    # Keep the actual engine state at the question. Continue an independent
    # no-answer branch for the initial row so partial block evidence is not lost.
    import copy
    q = case.pending[1]
    fork_id = secrets.token_hex(12)
    token = secrets.token_urlsafe(24)
    key = _evidence_key(p)
    memo = {id(case.plugin): case.plugin, **{id(it.spec): it.spec for it in case.items}}
    branch = copy.deepcopy(case, memo)
    branch.id = f'{case.id}-pending-{fork_id}'
    now = time.monotonic()
    with _pending_lock:
        for old in [k for k, data in _pending.items() if data[0] <= now]:
            del _pending[old]
        if len(_pending) >= 32:
            del _pending[next(iter(_pending))]
        _pending[token] = (now + 1800, key, q.id, branch)
    return {'id': token, 'text': q.text}


def _resume(engine, p):
    key = _evidence_key(p)
    with _pending_lock:
        state = _pending.get(p.label_confirmation)
        if state is None or state[0] <= time.monotonic() or state[1] != key:
            raise _ExpiredConfirmation()
        del _pending[p.label_confirmation]
    _, _, qid, case = state
    q = case.pending[1]
    if q.kind != 'label_confirm' or q.id != qid:
        raise _ExpiredConfirmation()
    caselog.log_event(case.id, 'batch_resume', parent_case=case.id.split('-pending-')[0], question_id=qid)
    engine.answer(case, True)  # Engine rechecks its exact evidence digest and readiness.
    return _finish(engine, case, p)


def _inspect(engine, p, save_image):
    inp = p.model_dump(include={'url', 'title', 'brand', 'brand_source', 'barcode', 'barcode_source', 'page_text', 'ingredient_html'})
    # Do not dereference image URLs or open retailer pages on the server. Only use
    # actual label pixels supplied by the seller browser, validated as bounded PNG/JPEG.
    gallery = [im.model_dump() for im in p.images if isinstance(im, GalleryImage)]
    observations = []
    if gallery:
        from .sources import choose_gallery_image
        label = engine.plugin.LABEL
        image_path = choose_gallery_image(gallery, zoro=engine.zoro, photo_options=label.PHOTO, threshold=label.theta_hi,
                                          save_image=lambda data: save_image(data, seller=True), observe=observations.append)
    else:
        # A single explicit image, not an arbitrary first image from a gallery.
        im = p.images[0] if len(p.images) == 1 else None
        image_path = save_image(im, seller=True) if isinstance(im, str) and im.startswith(
            ('data:image/png;base64,', 'data:image/jpeg;base64,')) else None
    inp['role'] = 'consumer'  # Existing no-ledger action path. Search never changes a listing.
    c = engine.start(text=p.title, inputs=inp, image_path=image_path)
    for observation in observations:
        caselog.log_event(c.id, 'batch_gallery_selection', **observation)
    return _finish(engine, c, p)


def _finish(engine, c, p):
    confirmation = None
    # Several different permitted engine actions may reuse a question ID. Its
    # own action budgets apply; never turn an exhausted/stalled case into a pass.
    for _ in range(24):
        if c.status != 'waiting_user' or not c.pending:
            break
        q = c.pending[1]
        answer = True if q.kind == 'capture_auto' and c.image_path else None
        if q.kind == 'label_confirm':
            confirmation = _remember(c, p)
        engine.answer(c, answer)
    return c, confirmation


def _summary(c, confirmation):
    items = [it for it in c.items if it.spec.kind == 'ingredient']
    guard = next((it.data['review_requirements'] for it in c.items if it.data.get('review_requirements')), {})
    verdict = c.verdict or {}
    complete = bool(c.status == 'done' and guard.get('complete') and items and all(it.status == 'resolved' for it in items))
    group = 'hold' if verdict.get('key') == 'blocked' else 'review'
    reason = verdict.get('why') or '검수 미완료: 상품에서 성분자료를 직접 확인하세요.'
    if group != 'hold' and verdict.get('key') == 'not_found' and complete:
        group = 'candidate'
        reason = '확인한 라벨 성분에서 차단 근거를 찾지 못함. 판매·통관 승인 아님.'
    return {'group': group, 'reason': reason, 'case_id': c.id, 'engine_done': c.status == 'done', 'checked': complete,
            'verdict': verdict, 'n_ingredients': len(items), 'n_compared': sum(it.status == 'resolved' for it in items),
            'missing': guard.get('missing', []), 'confirmation': confirmation if group == 'review' else None}


def _word(term, name):
    if re.search(r'(?i)\b(?:no|without|free of)\b|[- ]free\b|무첨가|미함유|불포함|제외|없는|없음', name):
        return False
    t = norm(term)
    return bool(t and re.search(r'(?:^| )' + re.escape(t) + r'(?:$| )', norm(name)))


def _apply_conditions(engine, conditions, p, case, row):
    # A fit score cannot veto an engine block or override missing label evidence.
    if row['group'] == 'hold':
        return
    ingredients = [it.data['name'] for it in case.items if it.spec.kind == 'ingredient']
    # Unconfirmed label text is never clearance. It can still establish a positive
    # seller exclusion, e.g. caffeine explicitly declared on a Korean product page.
    label = case.data.get('label') or {}
    if label.get('source') == 'dom' and label.get('source_url') == p.url:
        from .plugins.supplement import parse_ingredients
        ingredients = list(dict.fromkeys([*ingredients, *parse_ingredients(label.get('lines', []))]))
    for term in conditions.exclude:
        if any(_word(alias, name) for alias in altsearch.exclusion_names(term) for name in ingredients):
            row.update(group='hold', reason=f'판매자 제외 조건과 일치: {term} (법적 차단 판정 아님). ' + row['reason'], confirmation=None)
            return
    needs_evidence = row['group'] != 'candidate'
    for low, bound in ((True, conditions.price_min), (False, conditions.price_max)):
        if bound is None:
            continue
        if p.price is None or p.price.currency != bound.currency:
            row.update(group='review', reason='판매자가 지정한 통화의 상품 가격 미확인. ' + row['reason'])
            return
        outside = p.price.value < bound.value if low else p.price.value > bound.value
        if outside or bound.exclusive and p.price.value == bound.value:
            row.update(group='hold', reason='판매자 가격 조건 불일치 (법적 차단 판정 아님). ' + row['reason'], confirmation=None)
            return
    classification = case.data.get('classification') or {}
    req = {'state': {'conditions': conditions.model_dump(exclude_none=True), 'product': p.title,
                     'classification': {k: (classification.get(k) or {}).get('label') for k in ('product_type', 'form', 'use')},
                     'ingredients': ingredients, 'price': p.price.model_dump() if p.price else None},
           'questions': {'fit': {'type': 'choice', 'instructions': 'Use only the supplied product evidence. Does it meet every explicit seller requirement? Missing evidence is not a match.',
                                 'criteria': {**altsearch.MATCH_OPTS, 'unknown': 'Insufficient evidence to assess a requested condition.'}}}}
    def mock_fit(r):
        fit = altsearch._fit_mock(r, conditions.model_dump(), classification)['fit']
        return {'fit': {**fit, 'unknown': 0.0}}
    res, meta = engine.zoro.decide(req, mock_fn=mock_fit)
    probs = res['answers']['fit']['probabilities']
    if (set(probs) != set(req['questions']['fit']['criteria']) or
            any(not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values()) or abs(sum(probs.values()) - 1) > .01):
        raise ValueError('invalid fit probabilities')
    top = max(probs, key=probs.get)
    row['fit'] = {'choice': top, 'probabilities': probs, 'source': meta['source']}
    caselog.log_event(case.id, 'batch_fit', **row['fit'], conditions=conditions.model_dump())
    if needs_evidence:
        return  # Even a confident fit is only contextual; engine review requirements remain authoritative.
    if top == 'matches' and probs[top] >= .85 and meta['source'] == 'remote':
        row['reason'] += ' 요청 조건 일치.'
    elif top == 'no' and probs[top] >= .85 and meta['source'] == 'remote':
        row.update(group='hold', reason='요청 조건 불일치 (법적 차단 판정 아님). ' + row['reason'])
    else:
        row.update(group='review', reason='요청 조건 전체 일치 여부를 직접 확인해야 합니다. ' + row['reason'])


def review(engine, batch, save_image):
    """NDJSON events, stable 1-based indices. Retry a failed stage once, after other rows."""
    started = time.monotonic()
    pending = list(enumerate(batch.candidates, 1))
    counts = {'candidate': 0, 'hold': 0, 'review': 0}
    checked = engine_done = 0
    inspected = {}
    for attempt in (1, 2):
        retry = []
        for index, p in pending:
            base = {'index': index, 'title': p.title or p.url, 'attempt': attempt}
            yield {'event': 'progress', **base, 'status': '검수 중' if attempt == 1 else '다시 시도 중'}
            row = {'url': p.url, 'title': p.title or p.url, 'checked': False, 'engine_done': False,
                   'group': 'review', 'case_id': None, 'confirmation': None, 'attempt': attempt}
            if p.read_error:
                # Browser already retried the page read, or stopped at a login/challenge.
                row['reason'] = '상품 페이지 미확인: ' + p.read_error
            elif p.source_url is not None and p.source_url.split('#')[0] != p.url.split('#')[0]:
                row['reason'] = '상품 주소와 판독한 페이지 출처가 달라 검수하지 않았습니다.'
            else:
                try:
                    if index not in inspected:
                        inspected[index] = _resume(engine, p) if p.label_confirmation else _inspect(engine, p, save_image)
                    case, confirmation = inspected[index]
                    row.update(_summary(case, confirmation))
                    if row['group'] != 'hold' and any(it.data.get('server_error') for it in case.items) and attempt == 1:
                        del inspected[index]
                        raise RuntimeError('engine unavailable')
                    if batch.conditions:
                        _apply_conditions(engine, batch.conditions, p, case, row)
                    if row['group'] == 'candidate' and engine.zoro.mode not in ('remote', 'record'):
                        row.update(group='review', reason='실제 모델로 검수하지 않은 모의·녹화 결과입니다.')
                except _ExpiredConfirmation:
                    row['reason'] = '확인이 만료되었거나 이미 처리되었거나 자료가 달라졌습니다. 이 상품을 다시 검수하세요.'
                except Exception as ex:  # Isolate all product-level errors. Never discard another product's result.
                    if attempt == 1:
                        retry.append((index, p))
                        yield {'event': 'progress', **base, 'status': '오류 · 나머지 상품 뒤 재시도', 'error': type(ex).__name__}
                        continue
                    row.update(group='review', reason=f'재시도 후 검수 또는 조건 판정 실패 ({type(ex).__name__}). 직접 확인 필요.')
            checked += int(row['checked'])
            engine_done += int(row['engine_done'])
            counts[row['group']] += 1
            yield {'event': 'row', 'index': index, 'row': row}
        pending = retry
    yield {'event': 'done', 'found': len(batch.candidates), 'checked': checked, 'engine_done': engine_done,
           'counts': counts, 'elapsed_ms': round((time.monotonic() - started) * 1000)}
