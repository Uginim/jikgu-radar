"""주제별 부분을 끼우는 플러그인 규격.

주제 하나 = 파일 하나(nextask/core/plugins/<topic>.py)에 `PLUGIN = MyPlugin()`.

케이스 하나는 판정 항목(Item) 여러 개로 쪼개진다(예: 성분 N개 + 문구 문장 M개). 항목마다 규격(ItemSpec)이
선택지·기준 문장·되묻기·도구·고쳐 쓰기를 정하고, 엔진이 항목마다
    판정 → (확정 | 도구 → 재판정 | 고쳐 쓰기 → 재판정 | 되묻기 → 갱신 | 사람 인계)
를 돌린다. 모든 항목이 끝나면 plugin.verdict / generate 가 케이스 결론과 결과물을 만든다.
항목 하나짜리 주제는 Plugin 자체를 ItemSpec 으로 쓰면 된다(기본 make_items).
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Question:
    """사용자에게 되묻는 질문(예/아니오/모름). 문장은 코드가 쓴다.

    predicate(option_key) -> bool 이 있으면 답을 베이즈 갱신으로 반영(재판정 없음).
    없으면 fact_yes/fact_no 를 항목 사실로 붙이고 재판정. on_answer(case, item, ans) 가 있으면 그것을 먼저 부른다
    (on_answer(engine, case, item, ans); 예: 재촬영 사진으로 OCR 다시 돌리기). on_answer 가 "handoff" 를 돌려주면 그 항목은 사람 인계.
    """
    id: str
    text: str
    predicate: Optional[Callable[[str], bool]] = None
    fact_yes: str = ""
    fact_no: str = ""
    on_answer: Optional[Callable] = None
    kind: str = "yesno"             # yesno | capture_auto(확장이 바로 캡처) | capture_region(사용자가 영역 선택)


@dataclass
class Tool:
    """외부 조회 도구. fn(case, item) -> dict. 실패하면 retries 번 재시도, 그다음 fallback(case, item)(있으면).
    모두 실패하면 on_fail: "handoff"(항목을 사람 검토로) | "continue"(도구 없이 진행)."""
    name: str
    fn: Callable
    fallback: Optional[Callable] = None
    retries: int = 1
    on_fail: str = "handoff"
    when: Callable = field(default=lambda case, item: True)


@dataclass
class Item:
    id: str
    spec: Any                       # ItemSpec
    title: str                      # 화면 표시 (예: "성분: Garcinia cambogia", "문구 2")
    data: dict = field(default_factory=dict)   # 항목 고유 데이터 (성분명, 후보, 문장 원문/현재본 ...)
    facts: list = field(default_factory=list)
    probs: Optional[dict] = None
    asked: list = field(default_factory=list)
    tools_done: list = field(default_factory=list)
    rewrites: list = field(default_factory=list)   # [{from, to, by}]
    status: str = "open"            # open | waiting_user | resolved | handoff | skipped
    result: Optional[str] = None    # 확정된 결정 키
    reason: str = ""
    n_zoro: int = 0

    def decision_probs(self):
        return self.spec.aggregate(self, self.probs) if self.probs else {}

    def top(self):
        d = self.decision_probs()
        if not d:
            return None, 0.0
        k = max(d, key=d.get)
        return k, d[k]


class ItemSpec:
    kind = "main"
    instructions = ""
    theta_hi = 0.85                 # 자동 확정 (요구사항 v1 기본 초안, 우리 데이터로 재측정 예정)
    theta_lo = 0.50                 # 이 미만이면 사람 인계
    max_questions = 3
    max_rewrites = 0
    answer_noise = 0.05
    questions: list = []
    tools: list = []
    agentic_next = False            # True 면 확정하지 못할 때 '다음 행동'을 Zoro-1 결정 노드로 고른다(next_* 메서드)
    next_budget = 4                 # 다음 행동 최대 횟수(넘으면 '확인 필요')
    system2 = True                  # 0.5 ≤ p < θ_hi 이면 Zoro-1 생각 모드(System 2)로 같은 질문을 재결정
    counts_toward_cap = True        # 상품당 질문 한도(plugin.max_questions_per_case)에 셈하는가
    use_zoro = True                 # False 면 score(case, item) 가 확률을 낸다 (예: OCR 판독 가능성)

    def questions_for(self, item):
        return list(self.questions)

    def options(self, item):
        return {}

    def labels(self, item):
        return {}

    def decision_labels(self, item):
        return self.labels(item)

    def build_request(self, case, item):
        state = {"task": getattr(case.plugin, "title", ""), "input_text": case.text}
        if item.facts:
            state["facts"] = list(item.facts)
        req = {"state": state, "questions": {"main": {"type": "choice", "instructions": self.instructions,
                                                       "criteria": dict(self.options(item))}}}
        if case.image_b64:
            req["image"] = case.image_b64
        return req

    def read_probs(self, item, response):
        return dict(response["answers"]["main"]["probabilities"])

    def aggregate(self, item, probs):
        return dict(probs)

    def score(self, case, item):
        raise NotImplementedError

    def apply_tool(self, case, item, tool_name, result):
        """도구 결과 반영. 기본: result['fact'] 를 항목 사실로 붙인다. 재판정이 필요하면 True."""
        if result.get("fact"):
            item.facts.append(result["fact"])
            return True
        return False

    def resolvable(self, item, top):
        """확신이 높아도 이 결정으로는 끝낼 수 없으면 False (예: '판독 불가'는 확정이 아니라 재촬영 요청)."""
        return True

    def can_resolve(self, case, item, top):
        """Context-aware completion gate; label acquisition also checks required evidence."""
        return self.resolvable(item, top)

    def next_code_action(self, case, item, options):
        """Optional mandatory action chosen before consulting a model."""
        return None

    # 고쳐 쓰기: rewrite_when 이 참이고 rewrites < max_rewrites 면 rewrite → 재판정. 소진되면 사람 인계.
    def rewrite_when(self, item):
        return False

    def rewrite(self, case, item):
        """-> {"text": 새 문장, "by": 누가 썼는지, ...}"""
        raise NotImplementedError

    def mock_probs(self, request):
        n = len(request["questions"]["main"]["criteria"])
        return {"main": {k: 1 / n for k in request["questions"]["main"]["criteria"]}}


class Plugin(ItemSpec):
    name = "base"
    title = "기본"
    pre_tools: list = []            # 항목을 만들기 전에 도는 케이스 단위 도구 (예: OCR). fn(case, None)
    options_map = {}                # 항목 하나짜리 주제용
    labels_map = {}

    def options(self, item):
        return dict(self.options_map)

    def labels(self, item):
        return dict(self.labels_map)

    def apply_pre_tool(self, case, tool_name, result):
        case.data[tool_name] = result

    def make_items(self, case):
        return [Item("main", self, self.title)]

    def mock_dispatch(self, request):
        """mock 모드 합성 확률. 요청의 _spec 표시로 항목 규격을 찾는다."""
        return self.mock_probs(request)

    def plan_actions(self, case):
        """결론 뒤 행동 목록 [{name, label, auto, args, reason}]. auto=False 면 셀러 승인 대기."""
        return []

    def run_action(self, case, action):
        raise NotImplementedError

    def product_status(self, case):
        return case.status

    def verdict(self, case):
        """-> (결론 키, 결론 표시, 이유)."""
        it = case.items[0] if case.items else None
        if it is None or it.status == "handoff":
            return "handoff", "사람 검토", it.reason if it else ""
        return it.result, self.decision_labels(it).get(it.result, it.result), it.reason

    def generate(self, case):
        key, label, why = self.verdict(case)
        return {"title": f"{self.title} 결과", "markdown": f"# {self.title} 결과\n\n결론: **{label}**\n\n{why}\n"}
