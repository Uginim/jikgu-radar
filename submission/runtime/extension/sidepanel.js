// 현재 상품 한 건의 작업 대화. HTTP 접수, 서버 실행 상태, 상품 판정은 서로 다른 상태다.
// SSE는 메모리 이벤트 전체를 재생한다. 재연결 시 기존 prefix를 비교하고 같은 사건의 부작용을 반복하지 않는다.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (v) => (v * 100).toFixed(0) + "%";
let caseId = null, es = null, lastPage = null, refreshTimer = null, run = null;
let capabilities = {}, entrySession = null, entryPhoto = null, entryBusy = false;
let batch = null;
let entryTextRequested = false;
let serverReady = false;

// ---- 설정 ----------------------------------------------------------------------------------------
const settings = chrome.storage?.local;
$("scan").disabled = true;
Promise.resolve(settings?.get(["server", "role"])).then((v = {}) => {
  $("server").value = QS.get("server") || v.server || "http://127.0.0.1:8000";
  if (v.role) $("role").value = v.role;
}).catch((e) => feed("설정 읽기 실패", e.message, "error")).finally(() => { $("scan").disabled = false; void initializeEntry(); });
$("server").addEventListener("change", () => { settings?.set({ server: $("server").value }); void initializeEntry(); });
$("role").addEventListener("change", () => settings?.set({ role: $("role").value }));
const server = () => ($("server").value || "http://127.0.0.1:8000").replace(/\/$/, "");

// ---- 탭·페이지 조작 --------------------------------------------------------------------------------
// 시험용 훅: sidepanel.html?tab=<탭 id>&server=<주소> 로 열면 그 탭을 대상으로 한다(E2E 자동화에서 패널을 일반 탭으로 열 때)
const QS = new URLSearchParams(location.search);
async function activeTab(target = run) {
  if (target?.tabId != null) {
    const tab = await chrome.tabs.get(target.tabId);
    if (tab.url?.split("#")[0] !== target.url) throw new Error("대상 상품 페이지가 바뀌었습니다. 원래 상품을 복원한 뒤 다시 확인하세요.");
    return tab;
  }
  if (QS.get("tab")) return chrome.tabs.get(Number(QS.get("tab")));
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("검사할 상품 탭을 찾지 못했습니다.");
  return tab;
}
async function toPage(msg, target = run) {
  const tab = await activeTab(target);
  try {
    return await chrome.tabs.sendMessage(tab.id, msg);
  } catch (e) { // 확장 설치 전에 열린 탭이면 콘텐츠 스크립트를 넣고 다시
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ["content.css"] });
    return chrome.tabs.sendMessage(tab.id, msg);
  }
}
async function captureTab(target = run) {
  const tab = await activeTab(target);
  const [visible] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
  if (visible?.id !== tab.id) throw new Error("대상 상품 탭이 표시되지 않아 캡처하지 않았습니다.");
  await toPage({ type: "capture_prepare" }, target);
  let captured = false;
  try {
    const r = await chrome.runtime.sendMessage({ type: "capture", windowId: tab.windowId });
    if (!r?.ok) throw new Error(r?.error || "캡처 실패");
    const after = await chrome.tabs.get(tab.id);
    const [active] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
    if (after.url !== tab.url || active?.id !== tab.id) throw new Error("캡처 중 대상 상품이 바뀌었습니다.");
    captured = true;
    return { image: r.dataUrl, source_url: tab.url?.split("#")[0] };
  } finally {
    await toPage({ type: "capture_finish", captured }, target).catch(() => {});
  }
}
async function cropDataUrl(dataUrl, rect, maxSize = Infinity) {
  const bmp = await createImageBitmap(await (await fetch(dataUrl)).blob());
  const s = rect.dpr || 1, scale = Math.min(1, maxSize / (rect.w * s), maxSize / (rect.h * s));
  const c = new OffscreenCanvas(Math.max(1, Math.round(rect.w * s * scale)), Math.max(1, Math.round(rect.h * s * scale)));
  c.getContext("2d").drawImage(bmp, rect.x * s, rect.y * s, rect.w * s, rect.h * s, 0, 0, c.width, c.height);
  bmp.close();
  const blob = await c.convertToBlob({ type: "image/png" });
  return new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob); });
}
async function captureQuestionRegion(q, ctx) {
  const context = q.context;
  if (!context?.target_name || context.source_url?.split("#")[0] !== ctx.url) throw new Error("질문 원문의 상품 위치를 확인하지 못했습니다.");
  const limit = Math.min(448, context.max_width || 448, context.max_height || 448);
  const response = await toPage({ type: "reveal_question", term: context.target_name, max_width: limit, max_height: limit }, ctx);
  const rect = response?.result?.rect;
  if (!response?.ok || !response.result?.shown || !rect?.w || !rect?.h || rect.w > limit || rect.h > limit)
    throw new Error("요청한 성분 행의 좁은 영역을 확보하지 못했습니다.");
  if (run !== ctx || ctx.pending?.question_id !== q.question_id) throw new Error("캡처 전에 질문이 바뀌었습니다.");
  const captured = await captureTab(ctx);
  const after = await toPage({ type: "question_rect" }, ctx);
  if (JSON.stringify(after?.result) !== JSON.stringify(rect)) throw new Error("캡처 중 질문 행 위치가 바뀌었습니다.");
  return { ...captured, image: await cropDataUrl(captured.image, rect, limit), region: rect };
}
async function selectRegion(target = run) {
  const rect = (await toPage({ type: "select_region" }, target))?.result;
  if (!rect) return null;
  await new Promise((ok) => setTimeout(ok, 150)); // 선택 상자가 사라진 뒤 캡처
  const captured = await captureTab(target);
  return { ...captured, image: await cropDataUrl(captured.image, rect), region: rect };
}

// ---- 서버 ---------------------------------------------------------------------------------------
async function api(path, body, ctx = run, method) {
  if (path !== "/health" && !serverReady) throw new Error("실제 검사 서버에 연결한 뒤 시작하세요.");
  const r = await fetch((ctx?.server || server()) + path, { method: method || (body ? "POST" : "GET"),
    ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(typeof j.detail === "string" ? j.detail : `요청 실패 (${r.status})`), { status: r.status });
  if (path !== "/health" && j.ui_fixture === true) throw new Error("실제 검사 결과가 아니어서 표시하지 않았습니다.");
  return j;
}
function atBottom() {
  const el = $("conversation");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 56;
}
function follow(wasAtBottom) {
  if (!$("history-details").open) return;
  if (wasAtBottom) $("conversation").scrollTop = $("conversation").scrollHeight;
  else $("latest").hidden = false;
}
function feed(k, v, kind = "agent") {
  const bottom = atBottom(), el = document.createElement("article");
  el.className = `message ${kind}`;
  el.innerHTML = `<div class="message-label">${esc(plainText(k))}</div><div class="message-body">${esc(kind === "user" ? v : kind === "error" ? errorText(v) : plainText(v))}</div>`;
  $("log").append(el); follow(bottom);
  return el;
}
async function busy(fn) {
  try { await fn(); } catch (e) { feed("오류 · 완료되지 않음", e.message, "error"); }
}
function setWorkState(ctx, state, hint) {
  if (run !== ctx) return;
  ctx.state = state;
  $("workstate").textContent = { starting: "시작 요청", running: "실행 중", waiting: "답변 대기", sending: "전송 중",
    accepted: "접수 확인 중", disconnected: "연결 확인", held: "직접 확인 필요", done: "검사 종료", error: "실행 확인 필요" }[state];
  $("composerhint").textContent = ["error", "disconnected"].includes(state) ? plainText(hint) : "";
  const locked = !ctx.terminal || ctx.chatBusy || ctx.altBusy;
  $("scan").hidden = !ctx.terminal;
  $("new-work").hidden = !ctx.terminal && !ctx.executionError;
  $("scan").disabled = locked;
  $("scan").textContent = "다시 검사";
  $("role").disabled = locked; $("server").disabled = locked;
  $("chat").hidden = !ctx.terminal || !ctx.snapshot?.verdict;
  $("reconnect").hidden = !["disconnected", "error", "accepted"].includes(state) || !ctx.id;
  $("batch-back").hidden = !ctx.batchOrigin;
  $("batch-back").disabled = entryBusy || !!ctx.chatBusy || !!ctx.altBusy || (!ctx.terminal && state !== "waiting");
  $("ask").querySelectorAll("button[data-a]").forEach((b) => b.disabled = state !== "waiting");
  const heading = $("ask").querySelector(".answer-heading > span");
  if (heading && ctx.pending) heading.textContent = { sending: "전송 중", accepted: "답변 받음 · 재개 대기",
    error: "전송 결과 확인 필요", disconnected: "연결 확인 필요",
    waiting: ["page_reread", "capture_auto"].includes(ctx.pending.kind) ? "확인 중" : "답변 필요" }[state] || heading.textContent;
  syncEntryControls();
  renderWork(ctx);
}
function scheduleRefresh(ctx = run) {
  clearTimeout(refreshTimer);
  if (!ctx?.id) return;
  refreshTimer = setTimeout(() => refresh(ctx).catch((e) => connectionError(ctx, e)), 300);
}
function connectionError(ctx, error) {
  if (run !== ctx || ctx.terminal) return;
  if (ctx.state !== "disconnected") feed("연결 확인 필요", error.message, "error");
  setWorkState(ctx, "disconnected", "결과를 추정하지 않습니다. 연결과 현재 질문을 다시 확인하세요.");
}
function rememberQuestion(q, ctx) {
  const id = q.question_id || q.id;
  if (!ctx.questions.has(id)) {
    const el = feed("확인이 필요합니다", q.text, "question-message");
    el.tabIndex = -1; el.dataset.questionId = id; ctx.questions.set(id, el);
  }
}
async function refresh(ctx) {
  const serial = ++ctx.refreshSerial;
  const s = await api(`/cases/${ctx.id}`, undefined, ctx);
  if (run !== ctx || serial !== ctx.refreshSerial) return;
  const bottom = atBottom();
  ctx.snapshot = s;
  if (ctx.batchOrigin && batch === ctx.batchOrigin.job) {
    const { job, index } = ctx.batchOrigin;
    (job.snapshots ||= new Map()).set(index, s);
  }
  const selected = selectedGallery(ctx);
  if (selected && ctx.labelAsset !== selected.asset) {
    ctx.labelAsset = selected.asset;
    toPage({ type: "select_label_asset", asset: selected.asset }, ctx).catch(() => {});
  }
  render(s);
  const q = s.question && { ...s.question, question_id: s.question.id || s.question.question_id };
  if (s.status === "waiting_user" && q) {
    if (ctx.pending?.question_id !== q.question_id) {
      ctx.pending = q; ctx.submission = null;
      rememberQuestion(q, ctx);
      showAsk(q, ctx);
      if (["page_reread", "capture_auto"].includes(q.kind) && !ctx.automatic.has(q.question_id)) {
        ctx.automatic.add(q.question_id);
        void submitQuestion(q, "automatic", ctx);
      }
    } else if (!ctx.submission) {
      setWorkState(ctx, "waiting", "위 질문에 답해주세요. 답변은 현재 상품의 이번 질문에만 적용됩니다.");
    }
  } else {
    if (ctx.pending && s.status === "running") feed("재개 확인", "서버가 답변 대기를 끝내고 실행 중 상태를 보냈습니다.");
    ctx.pending = null; ctx.submission = null; $("ask").hidden = true;
    if (s.verdict && s.status !== "running") {
      ctx.terminal = true; es?.close();
      $("resultbundle").hidden = false;
      const held = s.extras?.label_evidence?.review_requirements?.complete === false
        || (s.items || []).some((it) => it.status === "handoff");
      setWorkState(ctx, held ? "held" : "done", "");
      if (!ctx.highlighted && ctx.pageAvailable !== false) {
        ctx.highlighted = true;
        if (!ctx.sellerImage && s.extras?.label_evidence?.source === "dom") toPage({ type: "highlight", terms: s.highlights || [] }).catch(() => {});
        toPage({ type: "inspection_result", verdict: s.verdict.key, complete: s.extras?.label_evidence?.review_requirements?.complete === true && s.items?.length > 0 && s.items.every((it) => it.status === "resolved"),
          seller_image: ctx.sellerImage === true }).catch(() => {});
        if (s.verdict.key === "blocked" && ctx.role === "consumer")
          toPage({ type: "warn_cart", message: `식약처 해외직구 차단 성분 포함: ${s.verdict.why}` }).catch(() => {});
      }
    } else if (s.status === "running") {
      setWorkState(ctx, ctx.executionError ? "error" : "running", ctx.executionError
        ? `서버 실행 오류 · 완료되지 않음: ${ctx.executionError}` : "서버에서 확인한 실행 상태입니다. 추가 입력이 필요하면 질문을 표시합니다.");
    }
  }
  follow(bottom);
  if (ctx.terminal && bottom) $("resultbundle").scrollIntoView({ block: "start" });
}
function connect(ctx) {
  es?.close();
  let cursor = 0;
  const stream = new EventSource(`${ctx.server}/cases/${ctx.id}/events`);
  es = stream;
  stream.onopen = () => { cursor = 0; scheduleRefresh(ctx); };
  const types = ["step", "tool_call", "zoro_probs", "decision", "question", "next_action", "highlight", "result", "stats"];
  types.forEach((type) => stream.addEventListener(type, (e) => {
    if (run !== ctx || ctx.terminal) return;
    const signature = type + "\n" + e.data;
    let index;
    if (e.lastEventId !== "") {
      const id = Number(e.lastEventId);
      if (!Number.isInteger(id) || id < 0) { stream.close(); connectionError(ctx, new Error("실행 기록 순번이 올바르지 않습니다.")); return; }
      if (id < (ctx.replayFloor || 0)) return;
      ctx.eventIds ||= new Map();
      const previous = ctx.eventIds.get(id);
      if (previous !== undefined) {
        if (previous !== signature) { stream.close(); connectionError(ctx, new Error("같은 순번의 실행 기록이 바뀌었습니다.")); }
        return;
      }
      if (ctx.lastEventId != null && id < ctx.lastEventId) { stream.close(); connectionError(ctx, new Error("실행 기록의 순서가 바뀌었습니다.")); return; }
      ctx.eventIds.set(id, signature); ctx.lastEventId = id; index = id;
      ctx.events.push(signature);
    } else {
      if (ctx.replayFloor) { stream.close(); connectionError(ctx, new Error("이 서버는 기존 검사 기록의 이어받기 순번을 제공하지 않습니다.")); return; }
      index = cursor++;
      if (index < ctx.events.length) {
        if (ctx.events[index] !== signature) {
          stream.close();
          connectionError(ctx, new Error("서버의 재생 기록이 이전 기록과 다릅니다. 자동 재전송하지 않습니다."));
        }
        return;
      }
      ctx.events.push(signature);
    }
    let d;
    try { d = JSON.parse(e.data); } catch {
      stream.close(); connectionError(ctx, new Error("실행 기록을 읽지 못했습니다.")); return;
    }
    console.debug("case_event", { case_id: ctx.id, index, type, data: d });
    if (type === "question") rememberQuestion(d, ctx);
    if (type === "step" && d.event === "error") ctx.executionError = errorText(d.reason || "검사를 마치지 못했어요.");
    if (type === "step" && d.event === "agent_stage") { ctx.agentState = d; renderWork(ctx); }
    if (type === "step" && d.event === "tie_break") feed("확인 우선 규칙", tieBreakText(d));
    if (type === "step" && d.event === "code_selection") feed("정해진 순서로 확인", d.reason);
    if (type === "step" && d.reason && !/^[a-z][a-z0-9_]*$/.test(d.reason) && !["decision", "item_final", "tie_break", "code_selection"].includes(d.event))
      feed(d.event === "error" ? "실행 오류" : "실행 기록", d.reason, d.event === "error" ? "error" : "agent");
    if (type === "tool_call") {
      const result = d.result || {};
      const summary = d.ok ? acquisitionText(result) || plainText(result.reason || "조회 응답을 받았습니다. 확보된 근거는 결과에서 확인하세요.")
        : d.tool === "read_dom" ? "페이지에서 성분표 글자를 읽지 못했습니다." : "자료를 확인하지 못했습니다. 실패한 조회를 근거로 사용하지 않습니다.";
      feed(`확인한 일 · ${toolLabel(d.tool)}`, [retryText(result), summary].filter(Boolean).join("\n"), d.ok && result.status !== "ocr_failed" ? "agent" : "error");
    }
    if (type === "next_action") feed(d.code_selection || d.source === "code" || d.tie_break ? "정해진 순서로 확인" : d.policy === "fixed" ? "다음 확인" : "Zoro-1이 고른 다음 확인",
      `${plainText(d.choice_label || actionLabel(d.choice))}${!d.code_selection && d.source !== "code" && d.policy !== "fixed" && d.p != null ? ` · 모델의 선택지 점수 ${choicePct(d.p)}` : ""}`);
    if (type === "decision") feed(plainText(d.item_title || d.item || "항목 판정"), `${plainText(d.top_label || "")} · ${plainText(d.next_action || "")}`);
    if (type === "highlight" && !ctx.sellerImage) toPage({ type: "highlight", terms: d.terms || [] }).catch(() => {});
    if (type === "result") {
      void refresh(ctx).catch((error) => connectionError(ctx, error));
    } else scheduleRefresh(ctx);
  }));
  stream.onerror = () => {
    if (run === ctx && !ctx.terminal) {
      connectionError(ctx, new Error("이벤트 연결이 끊겼습니다. 서버의 기록 재생으로 재연결합니다."));
      scheduleRefresh(ctx);
    }
  };
}
async function startCase(input = null) {
  if (!serverReady || batch?.busy || batch?.awaitingChoice || (run && (!run.terminal || run.chatBusy || run.altBusy))) return;
  if (input?.context) await activeTab(input.context);
  document.body.classList.remove("access-error");
  es?.close(); clearTimeout(refreshTimer);
  run = null; lastPage = null;
  const ctx = { server: input?.context?.server || server(), role: $("role").value, events: [], questions: new Map(), automatic: new Set(), refreshSerial: 0,
    sellerImage: !!input?.image, tabId: input?.context?.tabId, url: input?.context?.url };
  run = ctx; caseId = null;
  $("batch-entry").hidden = true; $("batch-work").hidden = true;
  setWorkState(ctx, "starting", "현재 탭을 읽고 서버에 검사 작업을 요청합니다.");
  $("resultbundle").hidden = true;
  $("log").replaceChildren();
  $("items").replaceChildren(); $("ask").hidden = true; $("chatin").value = "";
  $("welcome").hidden = true; $("latest").hidden = true;
  $("entrybox").hidden = true; $("history-details").hidden = false; $("history-details").open = false;
  $("explanation-answer").textContent = ""; $("conversation").scrollTop = 0;
  $("productscope").hidden = true;
  ["verdict", "actions", "classline", "compare"].forEach((id) => $(id).hidden = true);
  $("alternative-content").replaceChildren();
  document.querySelectorAll("#resultbundle details").forEach((el) => el.open = false);
  feed("나 · 검사 요청", input?.image ? "제공한 성분표 사진 검사" : "현재 상품 페이지 검사", "user");
  try {
    const tab = await activeTab();
    ctx.tabId = tab.id; ctx.url = tab.url?.split("#")[0];
    await toPage({ type: "clear" });
    if (!input?.image) {
      const access = await toPage({ type: "page_access" }, ctx);
      if (!access?.ok || !access.result || access.result.blocked || access.result.read_error) {
        ctx.accessError = access?.result?.reason || access?.result?.read_error || "페이지 접근 상태를 확인하지 못해 검사를 시작하지 않았습니다.";
        throw new Error(ctx.accessError);
      }
    }
    const page = input?.image ? { ...input.context.page, images: [input.image], image_origin: "seller", found: "image" }
      : (await toPage({ type: "extract", site_generic: capabilities.site_generic === true }))?.result || {};
    if (run !== ctx) return;
    if (page.blocked) { ctx.accessError = page.reason || "현재 페이지를 상품 자료로 사용할 수 없습니다."; throw new Error(ctx.accessError); }
    if (page.url?.split("#")[0] !== ctx.url) throw new Error("읽는 중 대상 페이지가 바뀌어 검사를 시작하지 않았습니다.");
    lastPage = page; ctx.page = page; ctx.sourceImage = input?.image || null;
    $("producttitle").textContent = $("producttitle").title = page.title || tab.title || "상품명 미확인";
    $("productsite").textContent = productSite(ctx.url);
    if (page.expanded?.length) feed("페이지 읽기", `접힌 영역 ${page.expanded.length}곳을 펼쳤습니다.`);
    feed("확인할 성분표", input?.image ? "판매자가 제공한 사진" : page.found === "text" ? "페이지 글자" : page.found === "image" ? "페이지 사진 · 판독 전" : "페이지에서 찾지 못함");
    const result = await api("/cases", { url: ctx.url, title: page.title, brand: page.brand, page_text: page.page_text,
      brand_source: page.brand_source, barcode: page.barcode, barcode_source: page.barcode_source,
      ingredient_html: page.ingredient_html, images: page.images, image_origin: page.image_origin, role: ctx.role }, ctx);
    ctx.id = caseId = result.case_id;
    setWorkState(ctx, "accepted", "검사 요청이 접수됐습니다. 실제 실행 상태를 확인합니다.");
    connect(ctx); scheduleRefresh(ctx);
  } catch (e) {
    ctx.terminal = !ctx.id;
    if (ctx.accessError) {
      document.body.classList.add("access-error");
      setWorkState(ctx, "error", ctx.accessError);
      for (const id of ["welcome", "entrybox", "history-details", "work-summary", "ask", "new-work"]) $(id).hidden = true;
      return;
    }
    feed("검사 시작 실패", e.message, "error");
    if (!ctx.id) { $("welcome").hidden = false; if (input?.image) $("entrybox").hidden = false; }
    setWorkState(ctx, "error", pageGrantRequired(e) ? "이 탭에서 직구레이더 확장 아이콘을 눌러 페이지 읽기를 허용하세요."
      : "검사가 완료되지 않았습니다. 대상 페이지와 서버를 확인하세요.");
  }
}
function selectedGallery(ctx) {
  const selection = ctx.snapshot?.extras?.gallery_selection;
  if (ctx.snapshot?.extras?.label_evidence?.image_origin !== "gallery" || !Number.isInteger(selection?.selected_index)) return null;
  const candidate = ctx.page?.images?.[selection.selected_index], selected = selection.candidates?.find(c => c.index === selection.selected_index);
  if (!candidate?.url || !selected?.asset) return null;
  try {
    const url = new URL(candidate.url); url.search = ""; url.hash = "";
    return url.href === selected.asset ? { asset: selected.asset, candidate } : null;
  } catch { return null; }
}
function questionSource(ctx) {
  const evidence = ctx.snapshot?.extras?.label_evidence;
  if (evidence?.source === "dom") return '<div class="question-source question-dom"><button type="button" class="ghost" data-show-label>질문 위치 보기</button><div data-question-crop></div></div>';
  const image = evidence?.image_origin === "gallery" ? selectedGallery(ctx)?.candidate.data
    : ctx.sellerImage || ["ocr", "capture"].includes(evidence?.source) ? ctx.sourceImage : null;
  if (image && /^data:image\/(?:png|jpeg);base64,/.test(image))
    return `<figure class="question-source"><img src="${esc(image)}" alt="이 검사에 제공한 성분표 원본"><figcaption>${ctx.sellerImage ? "제공 사진" : "읽기에 제공한 사진"}<br>질문 전문의 판독 내용과 대조하세요</figcaption></figure>`;
  return '<p class="question-source">사진 원본 위치 미확인 · 질문 전문의 판독 내용을 확인하세요</p>';
}
async function showQuestionArea(q, ctx, activate = false) {
  const current = () => run === ctx && ctx.pending?.question_id === q.question_id;
  if (!current() || ctx.questionCaptureBusy) return;
  ctx.questionCaptureBusy = true;
  const output = $("ask").querySelector("[data-question-crop]");
  try {
    const tab = await activeTab(ctx);
    if (activate) await chrome.tabs.update(tab.id, { active: true });
    const [visible] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
    if (visible?.id !== tab.id) throw new Error("상품 탭을 열고 ‘질문 위치 보기’를 누르세요.");
    const item = ctx.snapshot?.items?.find(it => it.id === q.item);
    const term = item?.id?.startsWith("ing:") ? item.card?.ingredient || item.title.replace(/^성분[:：]\s*/, "") : "";
    const response = await toPage({ type: "reveal_question", term }, ctx);
    const rect = response?.result?.rect;
    if (!response?.ok || !response.result?.shown || !rect?.w || !rect?.h)
      throw new Error("원래 질문 행의 위치를 찾지 못했습니다. 임의 영역은 표시하지 않았습니다.");
    if (!current()) return;
    const captured = await captureTab(ctx);
    const after = await toPage({ type: "question_rect" }, ctx);
    if (JSON.stringify(after?.result) !== JSON.stringify(rect)) throw new Error("캡처 중 위치가 바뀌었습니다. 질문 위치를 다시 확인하세요.");
    const image = await cropDataUrl(captured.image, rect);
    if (!current() || !output?.isConnected) return;
    output.innerHTML = `<figure class="question-crop"><div><img src="${esc(image)}" alt="실제 페이지의 질문 행과 이웃 행 캡처"></div><figcaption>${rect.clipped ? "현재 보이는 범위만 캡처" : term ? "질문 행 · 함량 · 이웃 행" : "현재 페이지의 성분표"} · 판독 결과가 아닌 화면 원본</figcaption></figure>`;
  } catch (error) {
    if (current() && output?.isConnected) output.textContent = errorText(error);
  } finally { ctx.questionCaptureBusy = false; }
}
function showAsk(q, ctx) {
  $("ask").hidden = false;
  const automatic = ["page_reread", "capture_auto"].includes(q.kind);
  const title = automatic ? (q.kind === "capture_auto" ? "성분표 캡처 중" : "페이지 다시 읽기")
    : q.kind === "capture_region" ? "성분표는 어디에 있나요?" : q.kind === "label_confirm" ? "전체 성분표인가요?" : plainText(q.text || q.question || "추가 확인이 필요해요");
  $("ask").innerHTML = `<div class="answer-heading"><span>${automatic ? "확인 중" : "답변 필요"}</span><span class="role-tag role-${automatic ? "code" : "seller"}">${automatic ? "자동 확인" : "판매자"}</span></div>
    <h2>${esc(title)}</h2>${q.kind === "label_confirm" ? `<p class="question-scope">성분표와 기타 성분 · 현재 상품 기준</p>` : ""}
    ${!automatic && q.kind !== "capture_region" ? questionSource(ctx) : ""}
    ${(q.text || q.question) && (q.text || q.question) !== title ? `<details class="question-detail"><summary>질문 전문</summary><p>${esc(plainText(q.text || q.question))}</p></details>` : ""}
    <div class="answer-buttons">${automatic ? `<button data-a="automatic">다시 시도</button>` : q.kind === "capture_region"
      ? `<button data-a="select">위치 선택</button><button data-a="no">성분표 없음</button><button class="ghost" data-a="unknown">모름 · 나중에</button>`
      : `<button data-a="yes">예</button><button data-a="no">아니오</button><button class="ghost" data-a="unknown">모름</button>`}</div>`;
  ctx.questions.forEach((el, id) => el.classList.toggle("current-question", id === q.question_id));
  $("ask").dataset.questionId = q.question_id;
  $("ask").querySelectorAll("[data-a]").forEach((b) => b.onclick = () => void submitQuestion(q, b.dataset.a, ctx));
  const sourceButton = $("ask").querySelector("[data-show-label]");
  if (sourceButton) sourceButton.onclick = () => void showQuestionArea(q, ctx, true);
  $("ask").scrollIntoView({ block: "nearest" });
  setWorkState(ctx, "waiting", "위 질문에 답해주세요. 답변 전송은 검사 완료를 뜻하지 않습니다.");
  if (sourceButton) void showQuestionArea(q, ctx);
}
async function submitQuestion(q, answer, ctx) {
  if (run !== ctx || ctx.pending?.question_id !== q.question_id || ctx.submission) return;
  ctx.submission = "sending";
  setWorkState(ctx, "sending", "현재 질문을 확인하고 자료 또는 답변을 전송합니다.");
  let posted = false;
  let submittedPage = null;
  try {
    const current = await api(`/cases/${ctx.id}`, undefined, ctx);
    if (run !== ctx) return;
    if (current.status !== "waiting_user" || current.question?.id !== q.question_id) {
      ctx.submission = null; await refresh(ctx); return;
    }
    let body = { question_id: q.question_id }, endpoint = "answer";
    if (answer === "select") {
      const selected = await selectRegion();
      if (!selected) { ctx.submission = null; setWorkState(ctx, "waiting", "영역 선택을 취소했습니다. 아직 전송하지 않았습니다."); return; }
      body = { ...body, ...selected }; endpoint = "capture";
    } else if (q.kind === "page_reread") {
      const page = (await toPage({ type: "extract", site_generic: capabilities.site_generic === true }))?.result || {};
      if (page.blocked) throw new Error(page.reason || "페이지 접근이 제한되어 자료를 다시 읽지 않았습니다.");
      submittedPage = page;
      body = { ...body, page_text: page.page_text, ingredient_html: page.ingredient_html, images: page.images, source_url: page.url };
      endpoint = "capture";
    } else if (q.kind === "capture_auto") {
      try { body = { ...body, ...await (q.context?.purpose === "ingredient_presence" ? captureQuestionRegion(q, ctx) : captureTab(ctx)) }; }
      catch (e) { body.capture_error = e.message; }
      endpoint = "capture";
    } else body.answer = answer;
    if (run !== ctx || ctx.pending?.question_id !== q.question_id) return;
    const sent = feed(endpoint === "capture" ? "자료 전송 요청" : "나 · 답변 전송 요청",
      endpoint === "capture" ? (body.capture_error ? `캡처 실패 사실을 전송합니다: ${body.capture_error}` : "자료를 전송합니다. 판독·충분성 확인은 아직 완료되지 않았습니다.")
        : ({ yes: "예", no: q.kind === "capture_region" ? "성분표 없음" : "아니오", unknown: "모름" }[answer]), "user");
    posted = true;
    await api(`/cases/${ctx.id}/${endpoint}`, body, ctx);
    if (run !== ctx) return;
    if (body.image) ctx.sourceImage = body.image;
    if (submittedPage) ctx.page = submittedPage;
    sent.querySelector(".message-label").textContent = endpoint === "capture" ? "자료 전송 · 접수됨" : "나 · 답변 접수됨";
    if (ctx.pending?.question_id !== q.question_id) return;
    ctx.submission = "accepted";
    setWorkState(ctx, "accepted", "서버 접수 확인 · 아직 재개 완료가 아닙니다. 후속 상태를 기다립니다.");
    scheduleRefresh(ctx);
  } catch (e) {
    if (run !== ctx) return;
    feed(posted ? "전송 결과 확인 필요" : "자료 확보 실패 · 미전송", e.message, "error");
    if (ctx.pending?.question_id !== q.question_id || ctx.terminal) return;
    ctx.submission = posted ? "uncertain" : null;
    setWorkState(ctx, posted ? "error" : "waiting", posted ? "중복 전송하지 않습니다. 현재 상태를 다시 확인하세요." : "원래 상품 페이지를 확인한 뒤 질문의 버튼으로 다시 시도하세요.");
    if (posted) scheduleRefresh(ctx);
  }
}
$("latest").onclick = () => {
  if ($("log").lastElementChild === $("resultbundle")) $("resultbundle").scrollIntoView({ block: "start" });
  else $("conversation").scrollTop = $("conversation").scrollHeight;
  $("latest").hidden = true;
};
$("conversation").addEventListener("scroll", () => { if (atBottom()) $("latest").hidden = true; });
$("reconnect").onclick = () => busy(async () => {
  const ctx = run;
  if (!ctx?.id) return;
  await refresh(ctx);
  if (run === ctx && !ctx.terminal) connect(ctx);
});

// ---- 화면 ---------------------------------------------------------------------------------------
// 표시만 정리한다. 서버 snapshot, 선택 점수, 판정과 원시 이벤트는 변경하지 않는다.
function plainText(value) {
  let text = String(value ?? "");
  const words = [
    ["화면 캡처 → OCR", "화면 찍어 읽기"], ["화면 캡처", "화면 촬영"],
    ["DOM", "페이지 글자"],
    ["gravityocr-ko", "사진 글자 판독 도구"], ["System 1", "빠른 판정"], ["System 2", "추가 판정"],
    ["S2", "추가 판정"], ["OCR", "사진 글자 판독"], ["DSLD", "외부 라벨 자료"], ["I2715", "상품 차단 목록"],
    ["sidecar", "저장된 판독 자료"], ["offline and not cached", "연결할 수 없고 저장된 자료도 없음"],
    ["brand_mismatch", "브랜드 다름"], ["off_market", "판매 종료 자료"], ["candidate_unverified", "현 상품 여부 미확인"],
    ["셀러", "판매자"], ["SKU", "상품 번호"], ["캡처", "화면 촬영"],
    ["지나간 걸음", "진행 과정"], ["읽고 선택한 과정", "진행 과정"], ["걸음", "단계"],
    ["보기 점수", "선택지 점수"], ["사람에게 넘김", "직접 확인 필요"],
    ["코드의 선택 보정", "정해진 확인 순서"], ["코드 규칙", "확인 규칙"], ["코드가", "확인 규칙이"],
    ["사건", "검사"], ["엔진", "검사"], ["생성 호출", "문장 작성 요청"],
  ];
  for (const [from, to] of words) text = text.replaceAll(from, to);
  const terms = { insufficient_evidence: "추가 자료 필요", review: "직접 확인 필요", reviewed: "추가 자료 확인됨",
    scope: "확인 범위", diagnostics: "자료 확인 내역", handoff: "직접 확인 필요", resolved: "확인됨",
    waiting_user: "답변 대기", running: "확인 중", pending: "확인 전", skipped: "생략", done: "확인 종료",
    failed: "확인 실패", error: "확인 오류", blocked: "차단 성분 발견", clear: "목록 일치 없음",
    readable: "글자 판독 가능", unreadable: "글자 판독 어려움", not_found: "목록 일치 없음",
    label_confirm: "전체 성분표 확인", capture_region: "성분표 위치 선택", label: "성분표",
    code: "자동 확인", agent: "자동 검사", generation_unavailable: "초안 작성 연결 확인 필요",
    outside_grounded_grammar: "초안 확인 필요", read_dom: "페이지 읽기", ocr_label: "사진 읽기",
    ocr_capture: "화면 읽기", model_readable: "사진 글자 확인", seller_scope_confirmed: "전체 성분표 확인",
    current_source: "현재 상품 자료 확인", ingredient_coverage: "전체 성분 확인", drug_suspect: "의약품 관련 여부 확인",
    no_candidates: "관련 자료 없음", generation: "다음 확인 준비", judgment: "성분 판단",
    code_check: "확인 절차 점검", tool: "자료 확인", human: "직접 확인", seller: "판매자" };
  return text.replace(/\b[a-z][a-z0-9_]*\b/g, word => Object.hasOwn(terms, word) ? terms[word] : word)
    .replace(/https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?[^\s;]*/g, "로컬 자료 주소");
}
function errorText(error) {
  console.warn("panel_error", error);
  const message = String(error?.message || error || "");
  return /[가-힣]/.test(message) ? plainText(message) : "요청을 처리하지 못했어요. 연결을 확인한 뒤 다시 시도해 주세요.";
}
function productSite(url) {
  const host = new URL(url).hostname;
  if (host === "127.0.0.1" || host === "localhost" || host === "[::1]") return "저장 페이지 · 로컬 확인";
  if (host.endsWith(".iherb.com") || host === "iherb.com") return "아이허브";
  if (host.includes("amazon.")) return "아마존";
  return "현재 상품 페이지";
}
function toolLabel(tool) {
  return { read_dom: "페이지 읽기", i2715_product: "상품 차단 이력 확인", ocr_capture: "화면에서 글자 읽기",
    ocr_label: "라벨 사진 읽기", dsld_label: "외부 라벨 자료 확인", ingredient_alias_lookup: "성분의 다른 이름 확인" }[tool] || "추가 근거 확인";
}
function actionLabel(choice) {
  return { reread: "페이지 다시 읽기", capture: "화면 찍어 읽기", dsld: "외부 라벨 찾기",
    ask: "성분표 영역 요청", confirm: "판매자 확인", stop: "사람 검토" }[choice] || "기록된 다른 행동";
}
const choicePct = (value) => `${(value * 100).toFixed(1)}%`;
function tieBreakText(tie) {
  if (!tie) return "";
  return `점수 차가 작아 확인을 먼저 진행해요.\n원래 선택: ${actionLabel(tie.original_choice)}${
    Number.isFinite(tie.original_p) ? ` · ${choicePct(tie.original_p)}` : " · 점수 미기록"}${
    Number.isFinite(tie.p) ? ` / 확인 보기 ${choicePct(tie.p)}` : ""}`;
}
function retryText(result) {
  const attempts = result?.attempts;
  if (!Array.isArray(attempts) || attempts.length < 2) return "";
  return attempts.map((a, i) => `${i === 0 ? "1차" : `${i + 1}차 재시도`} ${a.ok === true ? "성공" : a.ok === false || a.error ? "실패" : "결과 미확인"}`).join(" → ");
}
function acquisitionText(a) {
  const status = {
    read: "자료의 글자를 읽었습니다. 전체 성분표 확보 여부는 별도로 확인합니다.", read_empty: "자료에서 성분 글자를 찾지 못했습니다.",
    capture_missing: "화면 자료가 제공되지 않아 글자를 읽지 못했습니다.",
    capture_failed: "화면을 찍지 못해 글자를 읽지 못했습니다.",
    ocr_unavailable: "사진 글자를 읽을 수 있는 도구가 준비되지 않았습니다.",
    ocr_failed: "사진에서 성분 글자를 읽지 못했습니다.",
    no_candidates: "검색 조건에 맞는 외부 라벨 자료가 없습니다. 제품이 없다는 뜻은 아닙니다.",
    lookup_failed: "외부 라벨 자료를 가져오지 못했습니다.",
    candidate_unverified: "외부 라벨이 현재 상품과 같은 자료인지 확인하지 못해 성분에 반영하지 않았습니다.",
  };
  if (a.candidate) {
    const reasons = (a.candidate.conflicts || []).map((key) => ({
      brand_mismatch: "브랜드가 다름", off_market: "판매 종료 자료", upc_mismatch: "상품 번호가 다름",
      barcode_mismatch: "상품 번호가 다름", title_mismatch: "상품명이 다름",
    }[key] || "상품 정보가 일치하지 않음"));
    return reasons.length ? `외부 라벨은 ${[...new Set(reasons)].join(" · ")}으로 현재 상품의 성분에 반영하지 않았습니다.`
      : "외부 라벨 후보의 상품 판본을 확인하지 못해 성분에 반영하지 않았습니다.";
  }
  return status[a.status] || "";
}
function sourceHtml(evidence) {
  if (!evidence) return "";
  const source = evidence.source_description || ({ dom: "페이지 글자", ocr: "사진 속 글자", capture: "캡처한 글자", dsld: "외부 라벨" }[evidence.source]) || "성분표 미확보";
  const records = (evidence.acquisitions || []).map((a) => [retryText(a), acquisitionText(a)].filter(Boolean).join(" · ")).filter(Boolean);
  const unverified = evidence.review_requirements?.checks?.current_source !== true && (evidence.acquisitions || []).some((a) => a.status === "candidate_unverified");
  return `<div class="source-line">출처 · ${esc(plainText(source))}</div>${unverified ? `<p class="source-note">외부 후보 · 현재 상품과 일치 확인 필요</p>` : ""}
    ${records.length ? `<details class="source-history"><summary>성분표 확인 기록</summary>${records.map((t) => `<p class="source-note">${esc(t)}</p>`).join("")}</details>` : ""}`;
}
const displayKey = (text) => String(text ?? "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " ").trim();
function displayGroups(items) {
  const groups = new Map();
  for (const item of items || []) {
    const ingredient = item.id?.startsWith("ing:") || item.card?.ingredient;
    const key = ingredient ? JSON.stringify([displayKey(item.card?.ingredient || item.title), item.status, item.result,
      item.card?.entry?.id || null, item.card?.decided_by || null, item.card?.why_by || null]) : item.id;
    if (groups.has(key)) groups.get(key).count++;
    else groups.set(key, { item, count: 1 });
  }
  return [...groups.values()];
}
function uniqueText(text, separator = ";") {
  const seen = new Set();
  return String(text || "").split(separator).filter((part) => {
    const key = displayKey(part);
    if (!key || seen.has(key)) return false;
    seen.add(key); return true;
  }).join(separator + " ");
}
function ingredientSummary(groups) {
  const priority = (it) => it.result === "blocked" ? 0 : it.status === "handoff" || it.result !== "clear" ? 1 : 2;
  const ingredients = groups.filter(({ item }) => item.id?.startsWith("ing:") || item.card?.ingredient)
    .sort((a, b) => priority(a.item) - priority(b.item));
  if (!ingredients.length) return `<h3>읽은 성분</h3><p class="muted">아직 성분별 판정을 할 수 있는 자료가 없습니다.</p>`;
  return `<h3>읽은 성분 · ${ingredients.length}개 표기</h3><ul class="ingredient-list">${ingredients.map(({ item: it, count }) => {
    const label = it.result === "blocked" ? "차단 성분" : it.status === "handoff" ? "확인 필요" : it.result === "clear" ? "목록 일치 없음" : plainText(it.result_label || "미확인");
    return `<li><span>${esc(it.card?.ingredient || it.title.replace(/^성분:\s*/, ""))}${count > 1 ? `<small>같은 표기 ${count}건을 합쳐 표시</small>` : ""}</span>
      <b class="${it.result === "blocked" ? "ingredient-blocked" : "muted"}">${esc(label)}</b></li>`;
  }).join("")}</ul>`;
}
function blockedEvidence(groups) {
  const blocked = groups.filter(({ item }) => item.result === "blocked");
  return blocked.length ? `<h3>표기 → 식약처 목록명</h3>${blocked.map(({ item: it }) => {
    const c = it.card, full = c?.entry?.label || c?.matched_entry || "목록명 확인 필요";
    const short = full.split(/[（(]/)[0].trim() || full, label = c?.ingredient || it.title;
    const shortLabel = label.split(/[（(]/)[0].trim() || label;
    return `<div class="blocked-match"><b>${esc(shortLabel)}</b><span class="match-arrow">→</span><strong>${esc(short)}</strong></div>
      <details class="match-details"><summary>지정 근거</summary><p>표기: ${esc(label)}</p><p>목록명: ${esc(full)}</p>
      <p>${c?.entry?.id ? `지정 번호 ${esc(c.entry.id)}` : "지정 번호 미확인"}${c?.entry?.designated ? ` · ${esc(c.entry.designated)}` : ""}</p>
      <p>${esc(plainText(c?.decided_by_label || c?.why || "성분별 판정 근거 참조"))}</p>
      ${c?.link ? `<a href="${esc(c.link)}" target="_blank" rel="noopener">식약처 원료 목록</a>` : ""}</details>`;
  }).join("")}` : "";
}
function journeyHtml(s) {
  const nodes = [], decisions = [], records = [];
  const steps = s.steps || [];
  let pageShown = false;
  for (const [i, step] of steps.entries()) {
    if (step.kind === "self_answered") {
      records.push(`<div class="choice-comparison"><b>${esc(plainText(step.label))}</b>${(step.evidence_lines || []).map((line) =>
        `<p>${line.line ? `${line.line}행 · ` : ""}${esc(line.text)}</p>`).join("")}</div>`);
    }
    if (["tool", "tool_error"].includes(step.kind)) {
      const tool = (step.label || "").match(/^도구 (\S+)/)?.[1];
      const retry = retryText(step.result);
      if (!pageShown && tool === "read_dom") {
        nodes.push(`<li><b>${step.kind === "tool_error" ? "페이지 글자 없음" : "페이지 글자 읽음"}</b></li>`);
        pageShown = true;
      }
      if (["ocr_capture", "ocr_label"].includes(tool)) {
        const read = step.result?.status === "read";
        nodes.push(`<li><b>${read ? "화면 글자 읽음" : "사진 읽기 미확인"}</b>${retry ? `<small>${esc(retry)}</small>` : ""}</li>`);
      }
      records.push(`<div class="choice-comparison"><b>${esc(toolLabel(tool))}</b><div>${esc([retry, acquisitionText(step.result || {})
        || (step.kind === "tool_error" ? "자료를 확보하지 못했습니다." : "도구 응답을 받았습니다.")].filter(Boolean).join(" · "))}</div></div>`);
    }
    if (step.kind === "next_action") {
      const selected = (step.label || "").replace(/^다음 행동:\s*/, "");
      const entry = Object.entries(step.labels || {}).find(([, label]) => label === selected);
      const previous = steps[i - 1];
      const tie = step.tie_break || (previous?.kind === "tie_break" && previous.item === step.item ? previous : null);
      const code = step.code_selection || (previous?.kind === "code_selection" && previous.item === step.item ? previous : null);
      const decision = { label: entry ? actionLabel(entry[0]) : plainText(selected), score: entry && step.decision_probs?.[entry[0]],
        model: !code && step.source !== "code" && (step.reason || "").startsWith("agent"), tie, code };
      decisions.push(decision);
      records.push(`<div class="choice-comparison"><b>${code ? "정해진 순서로 확인" : tie ? "확인을 먼저 진행" : decision.model ? "Zoro-1 선택" : "정해진 순서"}: ${esc(decision.label)}</b>
        ${code ? `<p>${esc(plainText(code.reason))}</p>` : tie ? `<p>${esc(tieBreakText(tie))}</p>` : ""}
        ${Object.entries(step.labels || {}).map(([key, label]) => `<div>${key === entry?.[0] ? "선택 · " : ""}${esc(plainText(label))}
          ${decision.model && step.decision_probs?.[key] != null ? choicePct(step.decision_probs[key]) : ""}</div>`).join("")}</div>`);
    }
  }
  if (s.extras?.self_answers?.length) nodes.push(`<li><b>스스로 확인 ${s.extras.self_answers.length}건</b><small>아래 결정 기록에서 근거 줄을 볼 수 있어요.</small></li>`);
  const review = s.extras?.label_evidence?.review_requirements;
  if (review?.complete) nodes.push(`<li><b>${review.confirmation_by === "seller" ? "판매자 범위 답변" : "자료 범위 확인"}</b></li>`);
  const ingredients = (s.items || []).filter((it) => it.id?.startsWith("ing:") && it.result);
  if (ingredients.length) nodes.push(`<li><b>${ingredients.every((it) => it.card?.why_by === "code") ? "목록과 직접 대조" : "성분 대조"}</b></li>`);
  const chosen = decisions.findLast((d) => d.code || d.tie) || decisions.at(-1);
  return nodes.length || decisions.length ? `<h3>진행 과정</h3><ol class="journey-strip">${nodes.join("")}</ol>
    ${chosen ? `<div class="chosen-step">${chosen.code ? `<b>정해진 순서로 확인</b><small>${esc(plainText(chosen.code.reason))}</small>`
      : chosen.tie ? `<b>확인을 먼저 선택</b><small>${esc(tieBreakText(chosen.tie).split("\n")[1])} · 확인 규칙</small>`
      : `<b>${chosen.model ? "Zoro-1이 고른 다음 확인" : "정해진 다음 확인"} · ${esc(chosen.label)}</b>${chosen.model && chosen.score != null ? `<small>선택 점수 ${choicePct(chosen.score)}</small>` : ""}`}</div>` : ""}
    ${records.length ? `<details class="journey-comparisons"><summary>결정 기록 · 모든 선택과 도구 결과</summary>${records.join("")}</details>` : ""}
    ${chosen?.model ? `<p class="scope-note">점수는 선택지 비교값이며 정답률·통관 확률이 아닙니다.</p>` : ""}` : "";
}
const BAD = new Set(["blocked", "unreadable", "disease", "different"]), GOOD = new Set(["clear", "readable", "same"]);
// 확률 막대 + 기준선 눈금 두 개(θ_hi: System 1 즉시 결정, θ_lo: 그 아래는 사람)
function bars(probs, labels, th) {
  const t = typeof th === "number" ? { hi: th } : th || {};
  return Object.entries(probs || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<div class="bar"><span>${esc(plainText(labels[k] || k))}</span>
    <div class="track"><div class="fill ${BAD.has(k) ? "bad" : GOOD.has(k) ? "ok" : ""}" style="width:${v * 100}%"></div>
    ${t.hi ? `<div class="tick" style="left:${t.hi * 100}%" title="직접 판단 기준 ${t.hi}"></div>` : ""}
    ${t.lo ? `<div class="tick lo" style="left:${t.lo * 100}%" title="추가 확인 기준 ${t.lo}"></div>` : ""}</div><span>${pct(v)}</span></div>`).join("");
}
const SYS = { 0: "규칙", 1: "빠른 판정", 2: "추가 판정" };
const ENDED = { rule: "규칙", system1: "빠른 판정", system2: "추가 판정", tool: "자료 조회", human: "사람 확인" };
// 결정 통계 + 기준선 슬라이더(영상용): 이미 받은 확률로, 기준선을 t 로 올리면 사람에게 넘길 결정 수를 다시 센다
function statsHtml(st) {
  if (!st?.ended_by || !Array.isArray(st.finals)) return "";
  const r = st.ratio || {};
  return `<div class="stats"><div><b>판단 ${st.decisions}개</b> · AI 확인 요청 ${st.model_calls}회 ·
      ${Object.keys(ENDED).filter((k) => st.ended_by[k]).map((k) => `${ENDED[k]} ${pct(r[k])}`).join(" · ")}</div>
    <div class="muted small">추가 판정 시도 ${st.reasoning_attempts ?? "미기록"}회 · 완료 ${st.model_calls_system2 ?? "미기록"}회 ·
      완료 미확인 ${st.reasoning_incomplete ?? "미기록"}회(미채택)</div>
    <div class="muted small">평균 처리 시간 ${Object.entries(st.latency_ms || {}).map(([k, v]) => `${k === "reasoning_attempt" ? "추가 판정 시도 전체" : SYS[k.slice(-1)] || "자료 처리"} ${v} ms`).join(" · ") || "-"}</div>
    <div class="slider"><label>직접 판단 기준 <input type="range" id="thslider" min="0.5" max="0.99" step="0.01" value="0.85">
      <b id="thval">0.85</b></label><div id="thsim" class="muted small"></div></div></div>`;
}
function bindSlider(st) {
  const el = document.getElementById("thslider");
  if (!el || !st) return;
  const model = st.finals.filter((f) => f.model);
  const now = model.filter((f) => f.ended_by === "human").length;
  const upd = () => {
    const t = Number(el.value);
    document.getElementById("thval").textContent = t.toFixed(2);
    const n = model.filter((f) => f.p < t).length;
    document.getElementById("thsim").textContent =
      `자동 판단 ${model.length}개 중 기준 ${t.toFixed(2)} 미만 ${n}개 → 추가 판정·자료 확인·직접 검토 대상 (현재 직접 검토 ${now}개)`;
  };
  el.oninput = upd; upd();
}
function remainingReasons(snapshot) {
  const reasons = new Map(), items = snapshot.items || [];
  const add = (text, photo = false) => {
    text = plainText(text);
    if (text) reasons.set(text, { text, photo: photo || reasons.get(text)?.photo || false });
  };
  const label = items.find(item => item.id === "label");
  const score = label?.decision_probs?.readable, threshold = label?.thresholds?.hi;
  const reading = Number.isFinite(score) && Number.isFinite(threshold) && score < threshold
    ? `${snapshot.follow_up ? "원래 자료의 판독 점수" : "글자 판독 점수"} ${score.toFixed(2)} · 기준 ${threshold.toFixed(2)}에 못 미쳤어요.`
    : "글자가 선명한 성분표가 필요해요.";
  const scope = "이 상품의 전체 성분표인지 아직 확인하지 못했어요.";
  const answered = new Set((snapshot.extras?.label_evidence?.review_requirements?.resolved_by_followup || []).map(row => row.check));
  for (const item of items) {
    if (item.id === "label" && item.status === "handoff" && answered.has("성분표 판독 판단")) continue;
    if (item.status === "handoff") add(item.id === "label" ? reading : item.id === "scope" ? scope
      : `${item.title}: ${item.reason || item.result_label || "추가 자료가 필요해요."}`, ["label", "scope"].includes(item.id));
    for (const reason of item.card?.remaining_checks || []) add(`${item.title}: ${reason}`);
  }
  const names = {
    "현재 상품 출처 연결": "이 상품의 자료인지 확인해야 해요.",
    "성분표/원재료·기타성분 범위": "주성분과 기타 성분이 모두 나온 자료가 필요해요.",
    "성분표 판독 판단": reading,
    "셀러의 현재 상품·전체 성분영역 확인": scope,
    "제공된 성분표 전체 범위 확인": scope,
    "OCR 판독 신뢰·반복 점검": "사진에서 읽은 글을 다시 확인해야 해요.",
    "모델이 발견한 범위 누락 확인": "빠진 성분 항목이 없는지 확인해야 해요.",
    "읽은 성분 수·라벨 영역 대응 확인": "읽은 성분이 라벨 전체와 맞는지 확인해야 해요.",
  };
  for (const reason of snapshot.extras?.label_evidence?.review_requirements?.missing || []) add(names[reason] || reason, true);
  const classification = snapshot.extras?.classification?.product_type;
  if (reasons.size && classification?.key === "drug_suspect" && Number.isFinite(classification.p))
    add(`의약품 관련 분류 점수 ${pct(classification.p)} · 의약품 여부가 확정된 건 아니에요.`);
  return [...reasons.values()];
}

function render(s) {
  const finding = s.verdict || s.extras?.read_scope_result;
  $("resultbundle").hidden = !finding;
  $("resultbundle").classList.toggle("partial", !s.verdict);
  $("result-label").textContent = s.verdict ? "상품 검사 결과" : "읽은 범위의 대조 결과";
  $("explanation-details").hidden = !s.verdict;
  $("alternative-details").hidden = !s.verdict;
  const evidence = s.extras?.label_evidence;
  const groups = displayGroups(s.items);
  const review = evidence?.review_requirements;
  $("productscope").hidden = true;
  $("source-summary").hidden = !evidence;
  $("source-summary").innerHTML = sourceHtml(evidence);
  const blockedHtml = blockedEvidence(groups);
  $("blocked-evidence").hidden = !blockedHtml;
  $("blocked-evidence").innerHTML = blockedHtml;
  $("ingredient-summary").hidden = !finding;
  $("ingredient-summary").innerHTML = ingredientSummary(groups);
  const journey = journeyHtml(s);
  $("journey").hidden = !journey;
  $("journey").innerHTML = journey;
  const reviewed = review?.complete === true && (s.items || []).length > 0 && s.items.every((it) => it.status === "resolved");
  $("pstatus").className = "chip " + (reviewed ? "gray" : "warn");
  if (finding) {
    $("verdict").hidden = false;
    const title = { blocked: "등록 보류 권고", not_found: "읽은 성분에 일치 없음" }[finding.key] || "직접 확인 필요";
    $("verdict").dataset.result = finding.key;
    $("verdict").innerHTML = `<div class="big">${esc(title)}</div>
      <p class="next-guidance">${finding.key === "blocked" ? "읽은 성분에서 차단 근거 발견 · 실제 마켓 미변경"
        : finding.key === "not_found" ? (review?.complete === true ? "식약처 차단 목록 · 읽은 성분 기준" : "전체 성분표 확인 필요 · 통과 판정 아님") : "남은 확인을 마친 뒤 다시 검토하세요"}</p>`;
    $("stats").innerHTML = statsHtml(s.decision_stats);
    bindSlider(s.decision_stats);
  }
  const missing = remainingReasons(s);
  $("unresolved").hidden = !missing.length;
  $("pstatus").textContent = reviewed ? (missing.length ? "표시 성분 대조됨 · 추가 확인 남음" : "표시 성분 검토 완료") : "전체 성분 검토 미완료";
  $("unused-reasoning").hidden = !s.decision_stats?.reasoning_incomplete;
  $("unused-reasoning").textContent = `추가 판정 완료 미확인 ${s.decision_stats?.reasoning_incomplete || 0}회 · 해당 응답은 결과에 쓰지 않음`;
  const ingredients = (s.items || []).filter(it => it.id?.startsWith("ing:"));
  $("result-counts").textContent = `읽은 성분 ${ingredients.length}개 · 목록에 없음 ${ingredients.filter(it => it.status === "resolved" && it.result === "clear").length}개 · 남은 확인 ${missing.length}건`;
  $("unresolved").innerHTML = `<b>남은 확인 ${missing.length}건</b><ul>${missing.map(r => `<li><span>${esc(r.text)}</span></li>`).join("")}</ul>`;
  const resumed = s.follow_up;
  $("followup-status").hidden = !resumed;
  $("followup-status").textContent = resumed ? ({
    reviewed: `추가된 성분 ${resumed.added?.length ?? 0}개 · 다시 판단한 항목 ${resumed.rejudged?.length ?? 0}개`,
    insufficient_evidence: "추가 없음: " + ((resumed.diagnostics || []).map(d => plainText(d.reason)).filter(Boolean).join(" · ")
      || "판단할 근거가 더 필요해요."),
    failed: "추가 자료를 처리하지 못했어요. 이전 기록은 보존돼요.",
  }[resumed.state] || "추가 자료를 확인하고 있어요.") : "";
  $("result-next").replaceChildren();
  if (s.verdict) {
    const action = document.createElement("button");
    action.textContent = s.verdict.key === "blocked" ? "지정 근거" : missing.length || !reviewed ? "남은 확인 보기" : "확인한 성분 보기";
    action.onclick = () => {
      const target = s.verdict.key === "blocked" ? $("blocked-evidence").querySelector("details") || $("evidence-details")
        : missing.length ? $("unresolved") : $("evidence-details");
      if (target.tagName === "DETAILS") target.open = true;
      target.scrollIntoView({ block: "nearest" });
    };
    $("result-next").append(action);
    if (!reviewed && s.verdict.key !== "blocked" && capabilities.seller_image === true && capabilities.follow_up !== true) {
      const photo = document.createElement("button"); photo.textContent = "사진으로 새 검사"; action.className = "ghost";
      photo.onclick = () => { void openEntry(true); $("entryfile").click(); $("entrybox").scrollIntoView({ block: "start" }); };
      $("result-next").prepend(photo);
    }
  }
  renderCaseActions($("case-actions"), s, run, () => run?.id === s.case_id && run?.snapshot?.status === "done");
  renderPhotoRecovery($("photo-recovery"), s, run);
  const followup = $("case-actions").querySelector(".followup");
  const inquiry = $("case-actions").querySelector("[data-inquiry]:not([hidden])");
  if (followup || inquiry) $("unresolved").querySelectorAll("li").forEach((item, index) => {
    const actions = document.createElement("div"); actions.className = "remaining-actions";
    const add = (label, action) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "ghost";
      button.textContent = label; button.onclick = action; actions.append(button);
    };
    const open = selector => {
      followup.open = true; followup.scrollIntoView({ block: "nearest" });
      const control = followup.querySelector(selector);
      if (control.tagName === "TEXTAREA") control.focus(); else control.click();
    };
    if (followup && missing[index].photo) {
      add("사진 올리기", () => open("[data-photo]"));
      add("영역 고르기", () => open("[data-region]"));
    }
    if (inquiry) add("문의 초안", () => { inquiry.scrollIntoView({ block: "nearest" }); inquiry.click(); });
    if (followup && !missing[index].photo) add("답 붙여 넣기", () => open("textarea"));
    item.append(actions);
  });
  // 제품 분류 한 줄 + 확률, 그리고 분류로 고른 검사 세트
  const cl = s.extras?.classification, checks = s.extras?.checks || [];
  $("classline").hidden = !cl;
  if (cl) {
    const f = cl.form;
    $("classline").innerHTML = `<div><b>분류</b> ${esc(plainText(cl.product_type.label))} <span class="muted">${pct(cl.product_type.p)}</span>
      · ${esc(plainText(f.label))} <span class="muted">${f.by === "code" ? "라벨 글자" : pct(f.p)}</span>
      · ${esc(plainText(cl.use.label))} <span class="muted">${pct(cl.use.p)}</span></div>
      ${s.extras?.product_history ? `<div class="muted small">상품 차단 이력: ${esc(plainText(s.extras.product_history.reason))}</div>` : ""}
      <div class="row">${checks.filter((c) => c.id !== "blocked").map((c) => `<span class="chip ${c.result === "확인 필요" ? "warn" : c.result === "표기 있음" ? "warn" : "gray"}"
        title="${esc(plainText(c.detail))}">${esc(plainText(c.label))}: ${esc(plainText(c.result))}</span>`).join("")}</div>`;
  }
  // 단어 일치(Highlight This 방식) vs 우리 판정 — 같은 상품에서 나란히
  const cmp = s.comparison;
  $("compare").hidden = !(cmp && s.verdict);
  if (cmp) {
    const n = cmp.count;
    // 디자인 요청 9: 두 칸 비교(단어 일치 vs Zoro-1 에이전트)
    const shownRows = [...new Map(cmp.rows.map((r) => [JSON.stringify([displayKey(r.name), r.baseline, r.ours, r.entry]), r])).values()];
    const miss = shownRows.filter((r) => r.baseline === "blocked");
    const found = shownRows.filter((r) => r.ours === "blocked");
    $("compare").innerHTML = `<h3>목록 이름을 글자로 찾는 방식과 비교</h3>
      <div class="vs"><div class="vs-col miss"><h4>단어 일치</h4><div class="vs-n">${cmp.baseline_blocked}</div>
          ${miss.map((r) => `<div class="vs-row">${esc(r.name)}</div>`).join("") || `<div class="vs-row muted">표시한 성분 없음</div>`}</div>
        <div class="vs-col found"><h4>Zoro-1 에이전트</h4><div class="vs-n">${cmp.ours_blocked}</div>
          ${found.map((r) => `<div class="vs-row">${esc(r.name)} = ${esc(r.entry || "")}${r.baseline === "blocked" ? "" : " <b>(단어 일치가 놓침)</b>"}</div>`).join("")
            || `<div class="vs-row muted">차단 성분을 찾지 못함</div>`}</div></div>
      ${n.baseline_only ? `<div class="muted small">단어 일치만 막은 성분 ${n.baseline_only}개(우리 판정은 차단 근거 없음)</div>` : ""}
      <div class="muted small">숫자는 원래 검사 기록 건수이며 같은 성분 표기를 포함할 수 있습니다. 단어 일치는 목록 이름을 글자 그대로 찾는 방식입니다.</div>`;
  }
  const baseOf = (name) => cmp?.rows.find((r) => r.name === name);
  // '놓침'은 우리가 차단했는데 단어 일치가 표시 못 한 경우만. 나머지는 '표시'/'표시 안 함'
  function baseWord(r) { return r.baseline === "blocked" ? "표시" : r.category === "ours_only" ? "놓침" : "표시 안 함"; }
  $("actions").hidden = !s.actions?.length;
  $("actions").innerHTML = "<h3>앱 안 처리 기록 · 실제 마켓 미변경</h3>" + (s.actions || []).map((a) => `<div class="act"><span>${esc(
    a.name === "hold_listing" ? "연습용 장부에 보류 표시" : a.name === "notify" ? "앱 안 알림 기록" : plainText(a.label))}
      <span class="muted">${{ done: "기록됨", pending: "승인 대기", rejected: "거절", failed: "실패" }[a.state] || "상태 확인 필요"}</span></span></div>`).join("");
  // 디자인 요청 6: 바뀐 항목만 교체(같은 id·같은 내용이면 DOM 을 그대로 둬서 막대 애니메이션이 다시 돌지 않게)
  const box = $("items");
  const want = (s.items || []).map((it) => [it.id, itemHtml(it, baseOf, baseWord, it.id === "label" ? review : null)]);
  const have = new Map([...box.children].map((el) => [el.dataset.id, el]));
  want.forEach(([id, html], i) => {
    let el = have.get(id);
    if (!el || el.dataset.html !== html) {
      const n = document.createElement("div");
      n.className = "item"; n.dataset.id = id; n.dataset.html = html; n.innerHTML = html;
      if (el) el.replaceWith(n); el = n;
    }
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
    have.delete(id);
  });
  have.forEach((el) => el.remove());
  renderAltBox(s);
  $("links").innerHTML = s.case_id ? `<a target="_blank" rel="noopener" href="${esc(run.server)}/cases/${encodeURIComponent(s.case_id)}/report">실행 로그 보기</a>` : "";
}

// 문의 초안은 검사와 별도 요청이다. 고정 양식을 모델 생성 성공으로 표시하지 않는다.
function renderCaseActions(container, snapshot, ctx, current) {
  const remaining = (snapshot.extras?.label_evidence?.review_requirements?.missing || []).length
    || (snapshot.items || []).some(it => it.status === "handoff" || it.card?.remaining_checks?.length);
  const evidenceId = snapshot.extras?.label_evidence?.review_requirements?.evidence_id;
  const eligible = snapshot.status === "done" && remaining && (capabilities.inquiry_draft === true || capabilities.follow_up === true);
  const key = JSON.stringify([snapshot.case_id, eligible, evidenceId]);
  if (container.dataset.caseKey === key) return;
  container.dataset.caseKey = key; container.replaceChildren();
  if (!eligible) return;
  const request = document.createElement("button");
  request.type = "button"; request.dataset.inquiry = ""; request.textContent = "문의 초안 만들기";
  const status = document.createElement("p"); status.setAttribute("role", "status");
  const output = document.createElement("div");
  container.append(request, status, output);
  request.hidden = capabilities.inquiry_draft !== true;
  if (ctx === run && capabilities.follow_up === true && typeof evidenceId === "string" && evidenceId)
    appendFollowup(container, ctx, evidenceId, current);
  request.onclick = async () => {
    if (entryBusy || batch?.busy || !current() || !serverReady) return;
    entryBusy = true; syncEntryControls(); request.disabled = true;
    status.textContent = "초안 쓰는 중 · 아직 보내지 않음"; output.replaceChildren();
    try {
      const response = await api(`/cases/${encodeURIComponent(snapshot.case_id)}/inquiry-draft`, undefined, ctx, "POST");
      if (!current() || !container.isConnected) return;
      const generated = response.source === "zoro" && response.validation?.status === "accepted";
      const fixed = response.source === "template" && response.validation?.fallback_used === true
        && ["outside_grounded_grammar", "generation_unavailable"].includes(response.validation?.status);
      const draft = response.draft;
      if (response.case_id !== snapshot.case_id || response.status !== "ready" || (!generated && !fixed)
        || !draft || !["subject_en", "body_en", "subject_ko", "body_ko"].every(field => typeof draft[field] === "string" && draft[field].trim()))
        throw new Error("문의 초안을 받지 못했어요. 잠시 후 다시 시도해 주세요.");
      console.info("inquiry_draft", { case_id: response.case_id, source: response.source, validation: response.validation, request_id: response.meta?.request_id });
      status.textContent = "";
      output.innerHTML = `<p class="scope-note">내용을 확인한 뒤 보내세요. 자동으로 보내지 않아요.</p>
        <details open><summary>영문 문의</summary><p>${esc(draft.subject_en)}</p><textarea readonly aria-label="영문 문의 본문" rows="5">${esc(draft.body_en)}</textarea></details>
        <details open><summary>한글 문의</summary><p>${esc(draft.subject_ko)}</p><textarea readonly aria-label="한글 문의 본문" rows="5">${esc(draft.body_ko)}</textarea></details>
        <div class="result-next"><button type="button" data-copy="en">영문 복사</button><button type="button" data-copy="ko">한글 복사</button><a data-mail>메일 앱 열기</a></div>`;
      output.querySelectorAll("[data-copy]").forEach(button => button.onclick = async () => {
        if (!current()) return;
        try { await navigator.clipboard.writeText(draft[`body_${button.dataset.copy}`]); button.textContent = "복사됨"; }
        catch { status.textContent = "복사 권한을 받지 못했습니다. 위 본문을 직접 선택해 복사하세요."; }
      });
      const mail = output.querySelector("[data-mail]");
      mail.href = `mailto:?subject=${encodeURIComponent(draft.subject_en.replace(/[\r\n]+/g, " "))}&body=${encodeURIComponent(draft.body_en)}`;
      mail.onclick = event => { if (!current()) event.preventDefault(); };
      output.scrollIntoView({ block: "start" });
      request.textContent = "문의 초안 다시 만들기";
    } catch (error) {
      if (current()) { status.textContent = errorText(error); output.replaceChildren(); }
    } finally {
      entryBusy = false; syncEntryControls();
      if (container.isConnected) request.disabled = false;
    }
  };
}

function appendFollowup(container, ctx, evidenceId, current) {
  const details = document.createElement("details");
  details.className = "followup";
  details.innerHTML = `<summary>답 붙여 넣고 이어서 확인</summary><form>
    <label>받은 답변 · 추가 자료<textarea rows="4" maxlength="16000" placeholder="받은 글을 붙여 넣거나 사진을 추가하세요."></textarea></label>
    <input type="file" accept="image/png,image/jpeg" aria-label="같은 검사에 추가할 사진" hidden>
    <figure hidden><img alt="같은 검사에 추가할 실제 자료"><figcaption>선택한 사진 · 아직 보내지 않음</figcaption><button type="button" data-remove>사진 빼기</button></figure>
    <div class="result-next"><button type="button" data-photo>사진 올리기</button><button type="button" data-region>영역 고르기</button><button type="submit" disabled>같은 검사 다시 판단</button></div>
    <p role="status"></p></form>`;
  container.append(details);
  const form = details.querySelector("form"), text = form.querySelector("textarea"), file = form.querySelector('input[type="file"]');
  const send = form.querySelector('[type="submit"]'), note = form.querySelector('[role="status"]'), preview = form.querySelector("figure");
  let image = null, sending = false, uncertain = false, loading = false, photoVersion = 0;
  const sync = () => {
    form.querySelectorAll("input,textarea,button").forEach(control => { control.disabled = sending || loading || (uncertain && !control.hasAttribute("data-check")); });
    send.disabled = sending || loading || uncertain || !(text.value.trim() || image);
  };
  const stage = async value => {
    if (!current() || entryBusy || sending || uncertain || !value) return;
    if (!["image/png", "image/jpeg"].includes(value.type) || value.size > (capabilities.seller_image_max_bytes || 10485760)) {
      note.textContent = "PNG 또는 JPEG 사진 · 최대 10MB"; return;
    }
    const version = ++photoVersion; loading = true; sync();
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("사진을 읽지 못했습니다.")); reader.readAsDataURL(value);
      });
      if (!current() || sending || version !== photoVersion) return;
      image = data; preview.querySelector("img").src = image; preview.hidden = false; note.textContent = "";
    } finally { if (version === photoVersion) loading = false; sync(); }
  };
  file.onchange = () => { void stage(file.files[0]).catch(error => { note.textContent = errorText(error); }); file.value = ""; };
  form.querySelector("[data-photo]").onclick = () => { if (current() && !entryBusy && !sending && !uncertain && !loading) file.click(); };
  form.querySelector("[data-remove]").onclick = () => {
    photoVersion++; image = null; preview.hidden = true; preview.querySelector("img").removeAttribute("src"); sync();
  };
  form.addEventListener("paste", event => {
    const photo = [...(event.clipboardData?.items || [])].find(item => item.kind === "file")?.getAsFile();
    if (photo) { event.preventDefault(); event.stopPropagation(); void stage(photo).catch(error => { note.textContent = errorText(error); }); }
  });
  form.addEventListener("dragover", event => { event.preventDefault(); event.stopPropagation(); });
  form.addEventListener("drop", event => {
    event.preventDefault(); event.stopPropagation();
    void stage(event.dataTransfer?.files[0]).catch(error => { note.textContent = errorText(error); });
  });
  text.oninput = sync;
  form.querySelector("[data-region]").onclick = async () => {
    if (!current() || entryBusy || sending || uncertain || loading) return;
    photoVersion++; entryBusy = true; loading = true; sync(); syncEntryControls();
    try {
      const tab = await activeTab(ctx); await chrome.tabs.update(tab.id, { active: true });
      const selected = await selectRegion(ctx);
      if (selected && current()) { image = selected.image; preview.querySelector("img").src = image; preview.hidden = false; }
    } catch (error) { note.textContent = pageGrantRequired(error) ? "상품 탭에서 확장 아이콘을 눌러 화면 읽기를 허용해 주세요." : errorText(error); }
    finally { entryBusy = false; loading = false; syncEntryControls(); sync(); }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (!current() || entryBusy || sending || uncertain || loading || !(text.value.trim() || image)) return;
    const submittedText = text.value.trim(), submittedImage = image;
    sending = true; entryBusy = true; sync(); syncEntryControls();
    let posted = false;
    try {
      const fresh = await api(`/cases/${encodeURIComponent(ctx.id)}`, undefined, ctx);
      if (!current() || fresh.case_id !== ctx.id || fresh.status !== "done"
        || fresh.extras?.label_evidence?.review_requirements?.evidence_id !== evidenceId)
        throw new Error("검사 상태나 근거가 바뀌었습니다. 현재 상태를 다시 확인한 뒤 답변하세요.");
      posted = true;
      const accepted = await api(`/cases/${encodeURIComponent(ctx.id)}/follow-up`,
        { text: submittedText || undefined, image: submittedImage || undefined, evidence_id: evidenceId }, ctx);
      if (!current()) return;
      if (accepted.case_id !== ctx.id || accepted.status !== "accepted") throw new Error("접수 상태를 확인하지 못했습니다.");
      feed("나 · 추가 자료 접수됨", submittedText || "제공한 사진", "user");
      ctx.terminal = false; ctx.executionError = null; ctx.pending = null; ctx.submission = null; ctx.highlighted = false;
      ctx.sourceImage = submittedImage; ctx.sellerImage = !!submittedImage; ctx.snapshot = null;
      $("resultbundle").hidden = true;
      setWorkState(ctx, "accepted", "같은 검사에 답변 접수 · 서버의 재개 상태 확인 중");
      connect(ctx); scheduleRefresh(ctx);
    } catch (error) {
      uncertain = posted && !error.status;
      note.textContent = `${errorText(error)}${uncertain ? " · 재전송하지 말고 연결·상태를 다시 확인하세요." : ""}`;
      if (uncertain) {
        setWorkState(ctx, "error", note.textContent);
        const check = document.createElement("button"); check.type = "button"; check.dataset.check = ""; check.textContent = "접수 상태 다시 확인";
        check.onclick = async () => {
          if (entryBusy || run !== ctx) return;
          entryBusy = true; check.disabled = true; syncEntryControls();
          try {
            await refresh(ctx);
            if (run === ctx && ctx.snapshot?.status === "done" && ctx.snapshot.extras?.label_evidence?.review_requirements?.evidence_id === evidenceId) {
              uncertain = false; note.textContent = "현재 기록을 다시 확인했습니다. 답변을 검토한 뒤 명시적으로 다시 전송할 수 있습니다.";
            }
          } catch (failure) { check.textContent = `확인 실패 · ${errorText(failure)}`; }
          finally { entryBusy = false; syncEntryControls(); sync(); }
        };
        note.append(check);
      }
    } finally { sending = false; entryBusy = false; syncEntryControls(); sync(); }
  };
}

// 항목 한 칸(근거 카드 포함). 디자인 요청 7: 지나간 경로 노드(.path > .node.s1/.s2/.tool/.human/.rule, .on 지나감 · .now 지금)
function pathHtml(it) {
  const p = it.path || [];
  if (!p.length) return it.last_decision ? `<div class="next"><span class="sys">${esc(plainText(SYS[it.last_decision.system] || ""))}</span> ${esc(plainText(it.last_decision.next_action))}</div>` : "";
  const live = it.status === "open" || it.status === "waiting_user";
  return `<div class="path">${p.map((n, i) => `<span class="node ${n.kind} ${live && i === p.length - 1 ? "now" : "on"}">${esc(plainText(n.label))}${n.p != null ? ` <span class="p">${n.p.toFixed(2)}</span>` : ""}</span>`).join("")}
    ${!live && it.last_decision ? `<span class="node end on">${esc(plainText(it.last_decision.next_action))}</span>` : ""}</div>`;
}
function itemHtml(it, baseOf, baseWord, review) {
  const c = it.card;
  const [cls, icon] = it.result === "blocked" ? ["bad", "✕"] : it.status === "handoff" ? (it.id === "label" ? ["gray", "…"] : ["warn", "?"])
    : it.status === "resolved" ? ["ok", "–"] : ["", "·"];
  const b = it.card && baseOf(it.card.ingredient);
  return `<div class="hd">${esc(plainText(it.title))} <span class="chip ${cls}">${icon}
      ${esc(plainText(it.result_label || { open: "판정 중", waiting_user: "답변 대기", handoff: "확인 필요", skipped: "생략", resolved: "확인됨" }[it.status] || "상태 확인 필요"))}</span>
      ${b ? `<span class="chip ${b.category === "ours_only" ? "bad" : "gray"}">단어 일치: ${baseWord(b)}</span>` : ""}</div>
    ${c && c.decided_by && c.decided_by !== "zoro" ? `<div class="muted">판정 단계: ${esc(plainText(c.decided_by_label))}</div>` : bars(it.decision_probs, it.labels, it.thresholds)}
    ${pathHtml(it)}
    ${it.reason && !c ? `<div class="muted">${esc(plainText(it.reason))}</div>` : ""}
    ${review ? `<div class="card"><b>성분자료: ${review.complete ? review.confirmation_by === "code" ? "표와 기타성분 확인 완료" : "판매자 확인 완료" : "확인 필요"}</b>
      <div>${esc(plainText(review.scope))}</div><div class="muted">${review.complete ? review.confirmation_by === "code" ? "페이지의 성분표 본문과 기타성분을 대조했습니다. 실제 판매 옵션·실물 라벨의 동일성을 보증하지 않습니다." : "자료 적용·누락 여부는 판매자가 확인했으며 자동 판본 인증은 아닙니다."
        : `남은 확인: ${esc(plainText(review.missing.join(" · ")))}`}</div></div>` : ""}
    ${c ? `<div class="card"><b>근거 카드 · ${esc(plainText(c.verdict))}</b>
      ${c.label_line ? `<div>라벨 원문: <span class="muted">${esc(c.label_line)}</span></div>` : ""}
      ${c.entry ? `<div>차단 원료: ${esc(c.entry.label)} <span class="muted">· 목록 번호 ${esc(c.entry.id)} · 지정일 ${esc(c.entry.designated)}</span></div>
        ${c.entry.other_names?.length ? `<div class="muted">기타명칭: ${esc(c.entry.other_names.join(", "))}</div>` : ""}` : ""}
      <div>${esc(plainText(c.why))}</div>
      ${c.link ? `<div><a href="${esc(c.link)}" target="_blank">원문</a></div>` : ""}
      ${c.remaining_checks.map((r) => `<div class="muted">남은 확인: ${esc(plainText(r))}</div>`).join("")}
      <div class="muted small">모델 선택 점수는 정답률·통관 확률이 아닙니다.</div></div>` : ""}`;
}

// ---- (P1) 특징으로 사이트 안 검색 + 대체 제안 ----------------------------------------------------------
// 패널 입력 → 서버가 조건 해석(Zoro-1) → 백그라운드 탭으로 사이트 검색 → 후보 페이지 최대 5개를 열어 읽기 →
// 서버가 같은 검수 엔진으로 판정·조건 일치 순위 → 상위 3개 카드. 영역(#alts)은 JS 가 만든다(디자인 요청 10 마크업).
const USE_EN = { pre_workout: "pre workout", weight: "weight management", sleep: "sleep", focus: "focus",
                 sexual: "male vitality", joint: "joint", general: "vitamin", other: "" };
const FORM_EN = { capsule: "capsules", tablet: "tablets", powder: "powder", liquid: "liquid", jelly: "gummies" };
function waitTab(tabId, ms = 20000) {
  return new Promise((ok, fail) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(f); fail(new Error("tab load timeout")); }, ms);
    const f = (id, info) => { if (id === tabId && info.status === "complete") { clearTimeout(t); chrome.tabs.onUpdated.removeListener(f); ok(); } };
    chrome.tabs.onUpdated.addListener(f);
  });
}
async function inTab(url, msg) {      // 백그라운드 탭을 열고 콘텐츠 스크립트에 msg 를 보낸 뒤 닫는다
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    await waitTab(tab.id);
    await new Promise((ok) => setTimeout(ok, 800));
    let r;
    try { r = await chrome.tabs.sendMessage(tab.id, msg); }
    catch (e) {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      r = await chrome.tabs.sendMessage(tab.id, msg);
    }
    return r?.result;
  } finally { chrome.tabs.remove(tab.id).catch(() => {}); }
}
function altSection() {
  let sec = document.getElementById("alts");
  if (!sec) {
    sec = document.createElement("section");
    sec.id = "alts"; sec.className = "card-sec alts";
    $("alternative-content").append(sec);
  }
  return sec;
}
function altSeed(s) {   // 차단이면 같은 용도·제형, 차단 성분 없는 것으로 미리 채운다
  const cl = s.extras?.classification;
  const blocked = (s.items || []).filter((it) => it.result === "blocked").map((it) => it.card?.matched_entry?.split(" (")[0]).filter(Boolean);
  return [cl?.use?.label, cl?.form?.label, blocked.length ? `${blocked.join(", ")} 없는` : ""].filter(Boolean).join(" ");
}
function renderAltBox(s) {
  if (!s.verdict) return;
  const sec = altSection();
  if (sec.dataset.case === s.case_id) return;
  sec.dataset.case = s.case_id;
  sec.innerHTML = `<h3>${s.verdict.key === "blocked" ? "대체 후보 찾기" : "특징으로 찾기"}</h3>
    <div class="row"><input id="altq" aria-label="대체 후보 검색 조건" value="${esc(s.verdict.key === "blocked" ? altSeed(s) : "")}" placeholder="예: 수면 캡슐 멜라토닌 없는 3만원 이하">
    <button id="altgo">찾기</button></div><div id="altout" class="muted small">현재 사이트에서 후보를 최대 5개 찾아 같은 기준으로 성분을 확인해요.</div>`;
  document.getElementById("altgo").onclick = () => void searchAlternatives(document.getElementById("altq").value);
}
async function searchAlternatives(query, forcedUse) {
  const ctx = run;
  if (!query.trim() || !ctx?.terminal || ctx.altBusy) return;
  ctx.altBusy = true; $("altgo").disabled = true; $("scan").disabled = true;
  $("altout").querySelectorAll("button").forEach((b) => b.disabled = true);
  feed("나 · 대체 후보 검색", query, "user");
  try {
    await runAlt(query, forcedUse);
    if (run === ctx) feed("대체 후보 검색", $("altout").textContent);
  } catch (e) {
    if (run === ctx) { $("altout").textContent = `검색 실패: ${errorText(e)}`; feed("대체 후보 검색 실패", e.message, "error"); }
  } finally {
    if (run === ctx) {
      ctx.altBusy = false; $("altgo").disabled = false; $("scan").disabled = !!ctx.chatBusy;
    }
  }
}
async function runAlt(query, forcedUse) {
  const out = document.getElementById("altout");
  out.innerHTML = "조건 해석 중…";
  let { conditions, question } = await api("/alt/interpret", { query, case_id: caseId });
  if (forcedUse) { conditions.use = forcedUse; question = null; }
  if (question) {   // 용도가 애매하면 하나만 묻는다
    out.innerHTML = `<div class="q">${esc(question.text)}</div><div class="row">${Object.entries(question.choices)
      .map(([k, v]) => `<button data-use="${k}">${esc(v)}</button>`).join("")}</div>`;
    out.querySelectorAll("button").forEach((b) => b.onclick = () => void searchAlternatives(query, b.dataset.use));
    return;
  }
  out.innerHTML = `조건: ${esc(conditions.use_label)} · ${esc(conditions.form_label)}${conditions.exclude.length ? ` · 제외 ${esc(conditions.exclude.join(", "))}` : ""} — 검색 중…`;
  const kw = [USE_EN[conditions.use], FORM_EN[conditions.form]].filter(Boolean).join(" ") || query;
  const searchUrl = QS.get("search") || `https://kr.iherb.com/search?kw=${encodeURIComponent(kw)}`;
  const list = (await inTab(searchUrl, { type: "list_products", n: 5 })) || [];
  if (!list.length) { out.textContent = "검색 결과에서 상품을 찾지 못했습니다(사이트 확인 화면이면 직접 열어 주세요)."; return; }
  const cands = [];
  for (const [i, p] of list.entries()) {
    out.textContent = `후보 ${i + 1}/${list.length} 읽는 중: ${p.title}`;
    const pg = await inTab(p.url, { type: "extract", site_generic: capabilities.site_generic === true }).catch(() => null);
    if (pg?.blocked) { out.textContent = pg.reason || "페이지 접근 제한으로 후보 읽기를 중단했습니다."; return; }
    if (pg) cands.push({ ...pg, title: pg.title || p.title, image: p.image });
  }
  out.textContent = `후보 ${cands.length}개 검수 중…`;
  const r = await api("/alt/rank", { conditions, candidates: cands, case_id: caseId });
  out.innerHTML = (r.top.map((c) => `<div class="alt-card">${c.image ? `<img src="${esc(c.image)}" alt="">` : "<div></div>"}<div>
      <div class="alt-title"><a href="${esc(c.url)}" target="_blank">${esc(c.title)}</a></div>
      <div class="alt-meta">${esc(plainText([c.classification.product_type, c.classification.form, c.classification.use].filter(Boolean).join(" · ")))} · 조건 일치 ${pct(c.fit.matches || 0)}</div>
      <div class="alt-scope">${esc(plainText(c.scope))}</div></div></div>`).join("") || `<div class="muted">조건에 맞고 차단 성분이 없는 후보를 찾지 못했습니다.</div>`)
    + `<div class="muted small">후보 ${r.checked}개 검수 · 제외 ${r.excluded.length}개${r.excluded.length ? ": " + r.excluded.map((x) => `${esc(x.title.slice(0, 30))}(${esc(plainText(x.excluded_by || "조건 불일치"))})`).join(", ") : ""}</div>`;
}

// 결과 설명 질문은 /chat만 호출한다. 실행 지시·보완 답변 API로 전용하지 않는다.
async function ask() {
  const ctx = run, m = $("chatin").value.trim();
  if (!m || !ctx?.terminal || !ctx.snapshot?.verdict || ctx.chatBusy) return;
  ctx.chatBusy = true; $("chatgo").disabled = true; $("chatin").disabled = true; $("scan").disabled = true;
  feed("나 · 결과 설명 질문", m, "user");
  try {
    const r = await api(`/cases/${ctx.id}/chat`, { message: m }, ctx);
    if (run !== ctx) return;
    $("chatin").value = "";
    feed("근거 설명", r.answer);
    $("explanation-answer").textContent = plainText(r.answer);
  } catch (e) {
    if (run === ctx) { feed("설명 요청 실패 · 입력 보존", e.message, "error"); $("explanation-answer").textContent = errorText(e); }
  } finally {
    if (run === ctx) {
      ctx.chatBusy = false; $("chatgo").disabled = false; $("chatin").disabled = false; $("scan").disabled = !!ctx.altBusy;
      $("chatin").focus({ preventScroll: true });
    }
  }
}
$("chat").onsubmit = (e) => { e.preventDefault(); void ask(); };
$("chatin").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.isComposing) e.preventDefault(); });

// ---- 두 번째 탭: 국내 판매 현황 (P1) ------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
  document.querySelectorAll("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== t.dataset.tab));
});
const ML = { same: "같은 상품", variant: "용량·옵션만 다름", different: "다른 상품" };
function renderMarket(m) {
  const s = m.summary;
  $("msummary").hidden = false;
  $("msummary").innerHTML = `<div style="font-size:16px;font-weight:700">${esc(plainText(m.verdict.label))}</div><div>${esc(plainText(m.verdict.why))}</div>
    <div class="muted">후보 ${s.n_candidates} · 같은 상품 ${s.n_same} · 옵션만 다름 ${s.n_variant} · 확인 필요 ${s.n_uncertain} · 출처 ${esc(plainText(s.search_source))}</div>`;
  $("mlist").hidden = false;
  $("mlist").innerHTML = `<table><tr><th>판정</th><th>판매처·상품</th><th class="num">가격</th></tr>` + m.items.map((it, i) => {
    const L = m.listings[i];
    const top = Object.entries(it.decision_probs || {}).sort((a, b) => b[1] - a[1])[0] || ["", 0];
    const lab = it.status === "handoff" ? "확인 필요" : ML[it.result] || "결과 확인 필요";
    return `<tr><td><span class="chip ${it.result === "same" ? "ok" : it.status === "handoff" ? "warn" : ""}">${esc(lab)}</span><div class="muted">${pct(top[1])}</div></td>
      <td>${esc(L.mall)}<div class="muted">${L.link ? `<a href="${esc(L.link)}" target="_blank">${esc(L.title)}</a>` : esc(L.title)}</div></td>
      <td class="num">${(L.price || 0).toLocaleString()}원</td></tr>`;
  }).join("") + "</table>";
}
$("mscan").onclick = () => busy(async () => {
  const page = lastPage || (await toPage({ type: "extract", site_generic: capabilities.site_generic === true }))?.result || {};
  if (page.blocked) throw new Error(page.reason || "페이지 접근이 제한되어 검사를 시작하지 않았습니다.");
  lastPage = page;
  renderMarket(await api("/market", { url: page.url, title: page.title, brand: page.brand, case_id: caseId }));
});

$("scan").onclick = () => busy(startCase);

// 진입 요청은 검사 API와 분리한다. ready=false 응답은 절대 새 검사를 만들지 않는다.
const ENTRY_REQUESTS = {
  start: ["페이지 검사", "현재 상품 페이지의 성분을 검사해 주세요"],
  capture_region: ["위치 선택", "현재 페이지에서 성분표 위치를 직접 선택해 검사하고 싶어요"],
  photo: ["사진 검사", "첨부한 성분표 사진을 검사해 주세요"],
  explain: ["결과 설명", "현재 검사 결과의 근거를 설명해 주세요"],
  recheck: ["다시 검사", "현재 상품을 다시 검사해 주세요"],
};
const icon = (name) => `<svg class="ui-icon" aria-hidden="true"><use href="#ui-${name}"/></svg>`;
function syncEntryControls() {
  const working = entryBusy || !!batch?.busy || !!batch?.awaitingChoice || !!(run && (!run.terminal || run.chatBusy || run.altBusy));
  const locked = !serverReady || working;
  $("scan").disabled = locked;
  $("entry-page").disabled = locked;
  $("entry-chat").disabled = locked || capabilities.entry !== true;
  $("entry-photo").disabled = locked || capabilities.seller_image !== true;
  $("entry-chat").title = capabilities.entry === true ? "" : "요청 분류를 지원하는 서버 연결 필요";
  $("entry-photo").title = capabilities.seller_image === true ? "" : "사진 검사를 지원하는 서버 연결 필요";
  $("entrygo").disabled = locked || capabilities.entry !== true || !($("entrytext").value.trim() || entryPhoto);
  $("entrytext").hidden = !!entryPhoto && !entryTextRequested && !$("entrytext").value.trim();
  $("attach-photo").textContent = entryPhoto ? "사진 바꾸기" : "사진 넣기";
  $("inspect-photo").hidden = !entryPhoto;
  $("inspect-photo").disabled = locked || capabilities.seller_image !== true;
  $("entrygo").hidden = !!entryPhoto && !$("entrytext").value.trim();
  ["entrytext", "entryfile", "attach-photo", "remove-photo"].forEach((id) => $(id).disabled = locked);
  $("attach-photo").hidden = capabilities.seller_image !== true;
  $("entryoptions").querySelectorAll("button").forEach((b) => b.disabled = locked);
  for (const id of ["batch-list", "batch-urls-go", "batch-search-go", "batch-query", "batch-urls"]) $(id).disabled = locked;
  $("batch-clarify").querySelectorAll("button").forEach((b) => b.disabled = !serverReady || !!batch?.busy);
  $("batch-question-list").querySelectorAll("button").forEach((b) => b.disabled = !serverReady || !!batch?.busy || b.dataset.used === "true");
  $("new-work").disabled = entryBusy || !!batch?.busy || !!(run && ((!run.terminal && !run.executionError) || run.chatBusy || run.altBusy));
  $("batch-back").disabled = entryBusy || !!run?.chatBusy || !!run?.altBusy || !!(run && !run.terminal && run.state !== "waiting");
  $("role").disabled = working || !!run || !!batch; $("server").disabled = working || !!run || !!batch;
  if (!run && !batch) $("batch-entry").hidden = capabilities.search_first !== true;
}
async function initializeEntry() {
  const base = server();
  capabilities = {}; serverReady = false; $("server-notice").hidden = false; syncEntryControls();
  try {
    const health = await api("/health", undefined, { server: base });
    if (server() !== base) return;
    if (health.ok !== true || health.zoro_mode !== "remote" || health.ui_fixture === true) throw new Error("실제 검사 서버 연결 필요");
    serverReady = true;
    capabilities = health.capabilities || {};
  } catch {
    $("entry-chat").title = "서버 연결 확인 필요";
  }
  $("server-notice").hidden = serverReady;
  syncEntryControls();
  if (!run) await previewProduct();
}
async function productContext() {
  const tab = await activeTab(null);
  if (!/^https?:/.test(tab.url || "")) throw new Error("상품 웹페이지를 먼저 여세요.");
  const context = { tabId: tab.id, url: tab.url.split("#")[0], server: server(), documentTitle: tab.title };
  const response = await toPage({ type: "context" }, context);
  if (!response?.ok || !response.result?.url) throw new Error("상품 정보를 읽지 못했습니다.");
  context.page = response.result;
  await activeTab(context);
  return context;
}
function pageGrantRequired(error) {
  return /cannot access contents|missing host permission|must request permission|not allowed to access/i.test(String(error?.message || error));
}
async function previewProduct() {
  try {
    const context = await productContext();
    if (run) return;
    const title = /^\/(?:s|search)(?:\/|$)/i.test(new URL(context.url).pathname) ? context.documentTitle : context.page.title;
    $("producttitle").textContent = $("producttitle").title = title || "상품명 미확인";
    $("productsite").textContent = productSite(context.url);
  } catch (error) {
    const tab = await activeTab(null).catch(() => null);
    if (run) return;
    $("producttitle").textContent = $("producttitle").title = /^https?:/.test(tab?.url || "") ? tab.title || "현재 상품" : "상품 페이지를 여세요";
    $("productsite").textContent = pageGrantRequired(error)
      ? "이 탭에서 직구레이더 확장 아이콘을 눌러 읽기를 허용하세요." : "";
  }
}
async function openEntry(photo = false) {
  if (entryBusy || batch?.busy || batch?.awaitingChoice || (run && !run.terminal)) return;
  entryTextRequested = !photo;
  $("entrybox").hidden = false;
  $("photodrop").hidden = !photo;
  $("entryanswer").textContent = ""; $("entryoptions").replaceChildren();
  syncEntryControls();
  try {
    if (!entryPhoto) entrySession = await productContext();
    $("entrytext").focus();
  } catch (e) { $("entryanswer").textContent = errorText(e); }
  syncEntryControls();
}
async function stagePhoto(file) {
  if (!file || entryBusy || batch?.busy || batch?.awaitingChoice || (run && !run.terminal)) return;
  $("entrybox").hidden = false;
  try {
    if (capabilities.seller_image !== true) throw new Error("연결된 서버는 사진 검사를 지원하지 않습니다.");
    const allowed = capabilities.seller_image_mime || ["image/png", "image/jpeg"];
    if (!allowed.includes(file.type) || file.size > Math.min(capabilities.seller_image_max_bytes || 10485760, 10485760))
      throw new Error("PNG 또는 JPEG 사진 · 최대 10MB");
    entryBusy = true; syncEntryControls();
    const context = await productContext();
    const image = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("사진을 읽지 못했습니다."));
      reader.readAsDataURL(file);
    });
    await activeTab(context);
    entrySession = context; entryPhoto = image;
    entryTextRequested = !!$("entrytext").value.trim();
    $("photo-image").src = image; $("photo-name").textContent = file.name || "붙여넣은 사진";
    $("photo-preview").hidden = false; $("photodrop").hidden = true;
    $("entryanswer").textContent = ""; $("entryoptions").replaceChildren();
  } catch (e) { $("entryanswer").textContent = errorText(e); }
  finally { entryBusy = false; $("entryfile").value = ""; syncEntryControls(); }
}
async function submitEntry(message = $("entrytext").value.trim()) {
  if (entryBusy || (run && (!run.terminal || run.chatBusy || run.altBusy))) return;
  message ||= entryPhoto ? ENTRY_REQUESTS.photo[1] : "";
  if (!message || capabilities.entry !== true) return;
  entryBusy = true; syncEntryControls(); $("entryanswer").textContent = "요청 확인 중";
  $("entryoptions").replaceChildren();
  try {
    const context = entrySession || await productContext();
    await activeTab(context);
    if (server() !== context.server) throw new Error("연결 설정이 바뀌었습니다. 요청 창을 다시 여세요.");
    const previous = run, image = entryPhoto;
    const result = await api("/entry", { message, url: context.url, has_image: !!image,
      case_id: previous?.url === context.url && previous?.server === context.server ? previous.id : undefined }, context);
    await activeTab(context);
    if (run !== previous || entryPhoto !== image) throw new Error("요청 중 작업이 바뀌어 실행하지 않았습니다.");
    $("entryanswer").textContent = plainText(result.answer || (result.ready ? "" : "무엇을 할까요?"));
    if (result.ready !== true) {
      for (const route of [...new Set(result.options || [])]) {
        if (!ENTRY_REQUESTS[route]) continue;
        const button = document.createElement("button");
        button.type = "button"; button.className = "ghost"; button.textContent = ENTRY_REQUESTS[route][0];
        button.onclick = () => { $("entrytext").value = ENTRY_REQUESTS[route][1]; void submitEntry(); };
        $("entryoptions").append(button);
      }
      if (result.route === "photo" && !image) $("photodrop").hidden = false;
      return;
    }
    if (result.route === "explain") {
      if (!result.answer) throw new Error("설명 응답이 비어 있습니다.");
    } else if (result.route === "photo") {
      if (!image) throw new Error("검사할 사진을 먼저 넣으세요.");
      await startCase({ context, image });
    } else if (result.route === "capture_region") {
      if (capabilities.seller_image !== true) throw new Error("이 서버는 선택 영역의 사진 검사를 지원하지 않습니다.");
      $("entryanswer").textContent = "상품 페이지에서 성분표 위치를 선택하세요";
      const captured = await selectRegion(context);
      if (!captured) { $("entryanswer").textContent = "선택 취소 · 검사를 시작하지 않음"; return; }
      await startCase({ context, image: captured.image });
    } else if (["start", "recheck"].includes(result.route)) {
      await startCase({ context, image });
    } else throw new Error("지원하지 않는 실행 응답입니다. 검사를 시작하지 않았습니다.");
    if (run !== previous && run?.id) {
      entryPhoto = null; entrySession = null;
      $("photo-preview").hidden = true; $("photo-image").removeAttribute("src"); $("entrytext").value = "";
    }
  } catch (e) { $("entryanswer").textContent = errorText(e); }
  finally { entryBusy = false; syncEntryControls(); }
}
$("entry-page").onclick = () => busy(startCase);
$("entry-chat").onclick = () => void openEntry();
$("entry-photo").onclick = () => { void openEntry(true); $("entryfile").click(); };
$("inspect-photo").onclick = async () => {
  if (entryBusy || !entryPhoto || !entrySession || (run && (!run.terminal || run.chatBusy || run.altBusy))) return;
  entryBusy = true; syncEntryControls();
  try {
    await activeTab(entrySession);
    await startCase({ context: entrySession, image: entryPhoto });
    if (run?.id) { entryPhoto = null; entrySession = null; $("photo-preview").hidden = true; $("photo-image").removeAttribute("src"); }
  } catch (e) { $("entryanswer").textContent = errorText(e); }
  finally { entryBusy = false; syncEntryControls(); }
};
$("attach-photo").onclick = () => $("entryfile").click();
$("photodrop").onclick = () => { if (!entryBusy) $("entryfile").click(); };
$("photodrop").onkeydown = (e) => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); $("photodrop").click(); } };
$("entryfile").onchange = () => void stagePhoto($("entryfile").files[0]);
$("remove-photo").onclick = () => { entryPhoto = null; $("photo-preview").hidden = true; $("photo-image").removeAttribute("src"); syncEntryControls(); };
$("entrytext").oninput = syncEntryControls;
$("entryform").onsubmit = (e) => { e.preventDefault(); void submitEntry(); };
$("entrytext").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); void submitEntry(); }
};
document.addEventListener("paste", (e) => {
  const file = [...(e.clipboardData?.items || [])].find((item) => item.kind === "file")?.getAsFile();
  if (file && !entryBusy && (!run || run.terminal)) { e.preventDefault(); void stagePhoto(file); }
});
document.addEventListener("dragover", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  if (!e.dataTransfer?.files.length) return;
  e.preventDefault(); void stagePhoto(e.dataTransfer.files[0]);
});
chrome.tabs.onActivated.addListener(() => { if (!run && !entryPhoto) void previewProduct(); });

// 진행 표시: 실제 확보 기록·검토 체크만 완료로 표시한다. 모델의 다음 행동은 근거가 아니다.
function renderWork(ctx) {
  if (run !== ctx) return;
  $("work-summary").hidden = ctx.terminal || !!ctx.snapshot?.extras?.read_scope_result;
  const snapshot = ctx.snapshot || {}, evidence = snapshot.extras?.label_evidence || {};
  const checks = evidence.review_requirements?.checks || {}, acquisitions = evidence.acquisitions || [];
  const steps = snapshot.steps || [], pending = ctx.pending;
  const stage = ctx.agentState || snapshot.extras?.agent_state || [...steps].reverse().find((s) => s.kind === "agent_stage");
  const ingredients = (snapshot.items || []).filter((it) => it.id?.startsWith("ing:"));
  const pageDone = evidence.source === "dom" && checks.read_quality === true;
  const pageFailed = steps.some((s) => s.kind === "tool_error" && /read_dom/.test(s.label || ""));
  const imageDone = acquisitions.some((a) => ["ocr", "capture"].includes(a.source) && a.status === "read");
  const imageFailed = acquisitions.some((a) => ["ocr", "capture"].includes(a.source) && /failed|error/.test(a.status || ""));
  const listingDone = ingredients.length > 0 && ingredients.every((it) => it.status === "resolved");
  const active = !ctx.terminal && !["error", "disconnected"].includes(ctx.state);
  const current = pending ? (["label_confirm"].includes(pending.kind) ? "person" : "camera")
    : ctx.state === "starting" ? "page" : stage?.item?.startsWith("ing:") || ingredients.length ? "list"
    : ctx.sellerImage || imageDone || stage?.action?.includes("capture") ? "camera" : "page";
  const nodes = [
    ["page", "페이지 확인", pageDone, pageFailed],
    ["camera", "사진 읽기", imageDone, imageFailed],
    ["person", evidence.review_requirements?.confirmation_by === "code" ? "스스로 범위 확인" : "판매자 확인", checks.seller_scope_confirmed === true, false],
    ["list", "목록 대조", listingDone, ingredients.some((it) => it.status === "handoff")],
  ];
  const markup = nodes.map(([id, label, done, failed]) => {
    const status = done ? "done" : failed ? "failed" : "pending", isCurrent = active && !done && !failed && current === id;
    return `<li class="${status}${isCurrent ? " current" : ""}"${isCurrent ? ' aria-current="step"' : ""}>
      <span class="pipeline-icon">${icon(id)}${done || failed ? `<span class="stage-mark">${icon(done ? "check" : "cross")}</span>` : ""}</span>
      <span>${label}</span><span class="sr-only">${done ? "확인됨" : failed ? "미확인" : isCurrent ? "확인 중" : "확인 전"}</span></li>`;
  }).join("");
  if ($("pipeline").innerHTML !== markup) $("pipeline").innerHTML = markup;
  $("work-goal").title = plainText(stage?.goal || "현재 상품의 표시 성분과 차단 목록 대조");
  $("work-obtained").title = plainText((stage?.obtained || []).join("\n"));
  $("work-missing").title = plainText((stage?.missing || []).join("\n"));
  $("work-obtained").textContent = listingDone ? `성분 ${displayGroups(ingredients).length}종 대조`
    : imageDone ? "사진 속 글자" : pageDone ? "페이지 글자" : "아직 없음";
  $("work-missing").textContent = evidence.review_requirements?.complete && listingDone ? "없음"
    : pending?.kind === "label_confirm" || imageDone || pageDone ? "전체 성분표 확인" : "성분표 확보";
  if ((snapshot.items || []).some((it) => it.status === "handoff")) $("work-missing").textContent = "미해결 항목 확인";
  $("live-activity").hidden = ctx.terminal || !!pending;
  let label = { starting: "페이지 읽는 중", accepted: "실행 상태 확인 중", sending: "답변 전송 중",
    error: "실행 확인 필요", disconnected: "연결 확인 필요" }[ctx.state];
  if (ctx.state === "starting" && ctx.sellerImage) label = "사진 전송 중";
  const role = stage?.stage === "seller" ? "seller" : ["judgment", "generation"].includes(stage?.stage) ? stage.stage : "code";
  label ||= { judgment: "성분 확인 중", generation: stage?.action ? `${actionLabel(stage.action)} 확인 중` : "다음 방법 찾는 중",
    code_check: "선택 확인 중", seller: "답변 필요" }[stage?.stage] || "성분 검사 중";
  $("live-activity").innerHTML = `<strong>${esc(label)}</strong><span class="role-tag role-${role}">${role === "seller" ? "판매자" : role === "code" ? "자동 확인" : "Zoro-1"}</span>`;
  const retried = acquisitions.filter((a) => Array.isArray(a.attempts) && a.attempts.length > 1);
  $("retry-status").hidden = !retried.length;
  $("retry-status").innerHTML = retried.map((a) => `<span>${icon("cross")} ${a.attempts.length - 1}회 재시도 ${icon("arrow")} ${icon(a.status === "read" ? "check" : "cross")}</span><span>${a.status === "read" ? "글자 확보" : "읽기 미완료"}</span>`).join("");
}

// 묶음은 별도 작업이다. 목록 읽기/URL 입력은 조건 모델을 호출하지 않는다.
function batchUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("올바른 상품 주소를 넣으세요."); }
  const id = url.pathname.match(/\/pr\/[^?#]+\/(\d+)\/?$/i)?.[1]
    || url.pathname.match(/\/(?:dp|gp\/product)\/([a-z0-9]{10})(?:\/|$)/i)?.[1];
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || !id)
    throw new Error("지원하는 상품 주소만 넣으세요. 목록·로그인·장바구니 주소는 검사하지 않습니다.");
  url.hash = "";
  return { url: url.href, key: `${url.origin}/${id.toUpperCase()}` };
}
async function batchPermission(url) {
  if (!await chrome.permissions.contains({ origins: [new URL(url).origin + "/*"] }))
    throw new Error("이 사이트의 배경 탭 읽기 권한이 없습니다. 해당 상품에서 확장 아이콘을 눌러 개별 검사하세요.");
}
async function batchMessage(tabId, url, type, extra = {}) {
  const response = await toPage({ type, ...extra }, { tabId, url: url.split("#")[0] });
  if (!response?.ok) throw new Error(response?.error || "페이지에서 응답을 받지 못했습니다.");
  return response.result;
}
async function waitBatchTab(id) {
  await new Promise((resolve, reject) => {
    const finish = (error) => {
      clearTimeout(timer); chrome.tabs.onUpdated.removeListener(updated); chrome.tabs.onRemoved.removeListener(removed);
      error ? reject(error) : resolve();
    };
    const updated = (tabId, info) => { if (tabId === id && info.status === "complete") finish(); };
    const removed = (tabId) => { if (tabId === id) finish(new Error("읽는 중 상품 탭이 닫혔습니다.")); };
    const timer = setTimeout(() => finish(Object.assign(new Error("상품 페이지 로드 시간 초과"), { retryable: true })), 30000);
    chrome.tabs.onUpdated.addListener(updated); chrome.tabs.onRemoved.addListener(removed);
    chrome.tabs.get(id).then(tab => { if (tab.status === "complete") finish(); }, finish);
  });
  return chrome.tabs.get(id);
}
async function readBatchCandidate(candidate) {
  await batchPermission(candidate.url);
  let tab;
  try {
    tab = await chrome.tabs.create({ url: candidate.url, active: false });
    const loaded = await waitBatchTab(tab.id);
    const actual = new URL(loaded.url);
    if (actual.origin !== new URL(candidate.url).origin || /\/(?:login|signin|sign-in|cart|checkout|ap\/signin)(?:\/|$)/i.test(actual.pathname))
      throw Object.assign(new Error("다른 사이트·로그인·구매 경로로 이동해 중단했습니다."), { stop: true });
    const access = await batchMessage(tab.id, loaded.url, "batch_access");
    if (access.blocked) throw Object.assign(new Error(access.reason), { stop: true });
    if (access.read_error) throw Object.assign(new Error(access.read_error), { retryable: access.retryable === true });
    if (batchUrl(loaded.url).key !== batchUrl(candidate.url).key) throw new Error("다른 상품으로 이동해 읽은 자료를 사용하지 않았습니다.");
    const page = await batchMessage(tab.id, loaded.url, "extract", { site_generic: capabilities.site_generic === true });
    if (page.blocked) throw Object.assign(new Error(page.reason || "현재 페이지를 상품 자료로 사용할 수 없습니다."),
      { stop: ["security", "login"].includes(page.kind), retryable: false });
    const after = await chrome.tabs.get(tab.id);
    if (page.url?.split("#")[0] !== after.url?.split("#")[0] || batchUrl(page.url).key !== batchUrl(candidate.url).key)
      throw new Error("읽는 중 상품이 바뀌어 자료를 사용하지 않았습니다.");
    return { ...page, url: page.url.split("#")[0], source_url: page.url.split("#")[0], images: page.images || [] };
  } catch (error) {
    if (/ERR_|net::|loading|network|load failed|chrome-error:\/\//i.test(error.message)) error.retryable = true;
    throw error;
  } finally {
    if (tab?.id != null) await chrome.tabs.remove(tab.id).catch(() => {});
  }
}
function observedBatchRow(job, index) {
  const original = job.rows[index], snapshot = job.snapshots?.get(index);
  if (!original || !snapshot) return original;
  const items = snapshot.items || [], ingredients = items.filter(it => it.id?.startsWith("ing:"));
  const review = snapshot.extras?.label_evidence?.review_requirements;
  const checked = snapshot.status === "done" && review?.complete === true && items.length > 0 && items.every(it => it.status === "resolved");
  const verdict = snapshot.verdict || snapshot.extras?.read_scope_result;
  return { ...original, verdict, checked, engine_done: snapshot.status === "done", confirmation: null,
    n_ingredients: ingredients.length, n_compared: ingredients.filter(it => it.status === "resolved" && ["clear", "blocked"].includes(it.result)).length,
    group: verdict?.key === "blocked" ? "hold" : checked && !job.conditions ? "candidate" : "review",
    reason: checked && job.conditions ? "후속 성분 검토 완료 · 검색 조건 일치는 다시 확인 필요"
      : snapshot.status === "waiting_user" ? "같은 검사의 답변 대기 · 이 상품 검사에서 이어 확인"
      : snapshot.status === "running" ? "같은 검사 실행 중" : verdict?.why || "검사 기록의 남은 확인을 보세요",
    missing: [...(review?.missing || []), ...items.filter(it => it.status === "handoff").map(it => `${it.title}: ${it.reason || it.result_label || "확인 필요"}`)] };
}
async function openBatchCase(job, index) {
  const row = job.rows[index];
  if (batch !== job || job.busy || entryBusy || !row?.case_id) return;
  entryBusy = true; syncEntryControls();
  try {
    const snapshot = await api(`/cases/${encodeURIComponent(row.case_id)}`, undefined, { server: job.server });
    if (batch !== job || job.rows[index]?.case_id !== row.case_id) return;
    if (snapshot.case_id !== row.case_id || !Number.isInteger(snapshot.event_count))
      throw new Error("이 서버는 기존 검사 기록의 이어받기 순번을 제공하지 않습니다.");
    let ctx = job.caseViews?.get(index);
    let tab = ctx ? await activeTab(ctx).catch(() => null) : null;
    if (!tab) tab = await chrome.tabs.create({ url: row.url, active: true });
    else await chrome.tabs.update(tab.id, { active: true });
    await waitBatchTab(tab.id);
    ctx ||= { id: row.case_id, server: job.server, role: snapshot.role || $("role").value, events: [], questions: new Map(),
      automatic: new Set(), refreshSerial: 0, replayFloor: snapshot.event_count, page: job.candidates[index] };
    ctx.tabId = tab.id; ctx.url = row.url.split("#")[0]; ctx.batchOrigin = { job, index }; ctx.terminal = false;
    (job.caseViews ||= new Map()).set(index, ctx);
    es?.close(); clearTimeout(refreshTimer); run = ctx; caseId = ctx.id; lastPage = ctx.page;
    document.body.classList.remove("access-error");
    for (const id of ["batch-entry", "batch-work", "welcome", "entrybox", "work-summary", "resultbundle", "ask"]) $(id).hidden = true;
    $("log").replaceChildren(); $("items").replaceChildren();
    ctx.questions.clear(); ctx.pending = null;
    $("history-details").hidden = false; $("history-details").open = false;
    $("producttitle").textContent = $("producttitle").title = row.title || job.candidates[index].title || "기존 상품 검사";
    $("productsite").textContent = productSite(ctx.url);
    const access = await toPage({ type: "page_access" }, ctx).catch(() => null);
    ctx.pageAvailable = access?.ok === true && access.result?.blocked === false;
    if (ctx.pageAvailable) {
      const extracted = await toPage({ type: "extract", site_generic: capabilities.site_generic === true }, ctx).catch(() => null);
      ctx.pageAvailable = extracted?.ok === true && extracted.result?.blocked !== true;
    }
    feed("기존 검사 열기", "같은 사건의 실제 기록입니다. 아직 새 자료를 보내지 않았습니다.");
    await refresh(ctx);
    if (!ctx.terminal) connect(ctx);
    if (!ctx.pageAvailable) $("composerhint").textContent = "기존 검사 기록 · 현재 상품 페이지는 읽을 수 없음";
  } catch (error) { $("batch-status").textContent = errorText(error); }
  finally { entryBusy = false; syncEntryControls(); if (run?.batchOrigin) $("batch-back").disabled = !run.terminal && run.state !== "waiting"; }
}
$("batch-back").onclick = async () => {
  const ctx = run, origin = ctx?.batchOrigin;
  if (!origin || entryBusy || ctx.chatBusy || ctx.altBusy || (!ctx.terminal && ctx.state !== "waiting")) return;
  es?.close(); clearTimeout(refreshTimer); run = null; caseId = null; lastPage = null;
  for (const id of ["entrybox", "work-summary", "resultbundle", "ask", "history-details", "scan", "new-work", "reconnect", "batch-back"]) $(id).hidden = true;
  $("batch-work").hidden = false; $("new-work").hidden = false; $("composerhint").textContent = "";
  $("producttitle").textContent = origin.job.source.documentTitle || "묶음 검사 결과";
  $("workstate").textContent = "묶음 검사 결과";
  batchRows(origin.job); batchQuestions(origin.job);
  try { const tab = await activeTab(origin.job.source); await chrome.tabs.update(tab.id, { active: true }); } catch { /* Results remain available when the source tab is closed. */ }
};

function batchRows(job) {
  $("batch-found").textContent = String(job.candidates.length);
  const rows = job.candidates.map((_, index) => observedBatchRow(job, index));
  $("batch-engine").textContent = String(rows.filter(r => r?.engine_done === true).length);
  $("batch-checked").textContent = String(rows.filter(r => r?.checked === true).length);
  for (const [index, candidate] of job.candidates.entries()) {
    let tr = $("batch-rows").children[index];
    if (!tr) {
      tr = document.createElement("tr"); tr.innerHTML = '<td><a target="_blank" rel="noopener"></a></td><td></td>';
      $("batch-rows").append(tr);
    }
    const link = tr.querySelector("a"), row = rows[index], progress = job.progress[index], version = job.snapshots?.get(index);
    link.href = candidate.url; link.title = candidate.url;
    link.textContent = row?.title || candidate.title || `상품 ${batchUrl(candidate.url).key.split("/").pop()} · 제목 미확인`;
    const signature = JSON.stringify([row, progress, candidate.read_error]);
    if (tr.dataset.result === signature) continue;
    tr.dataset.result = signature;
    tr.children[1].innerHTML = row ? `<span class="chip ${row.group === "hold" ? "bad" : row.group === "candidate" ? "ok" : "warn"}">${!row.case_id ? "검사 전" : !row.verdict && !row.engine_done ? "판정 미완료" : ({
      hold: "보류", candidate: job.conditions ? "조건 맞는 후보" : "검토한 후보", review: "직접 확인 필요" }[row.group] || "직접 확인 필요")}</span>
      ${row.case_id && row.verdict ? `<p>읽은 범위 · ${esc({ blocked: "차단 성분 발견", not_found: "차단 근거 없음", review: "대조 미확정" }[row.verdict.key] || plainText(row.verdict.label || "결과 확인 필요"))}</p>` : ""}
      <p>${esc(plainText(row.reason || "근거 확인 필요"))}</p>
      ${row.case_id ? `<small>${Number.isInteger(row.n_ingredients) ? `읽은 성분 ${row.n_ingredients}개 · ` : ""}${Number.isInteger(row.n_compared) ? `${row.n_compared}개 대조 · ` : ""}${row.checked ? "전체 성분 검토 완료" : "전체 성분 검토 미완료"}</small>` : ""}
      ${Array.isArray(row.missing) && row.missing.length ? `<ul>${row.missing.filter(m => typeof m === "string").map(m => `<li>${esc(plainText(m))}</li>`).join("")}</ul>` : ""}
      ${row.case_id ? '<details data-case-detail><summary>근거 · 다음 행동</summary><div></div></details>' : ""}`
      : `<span class="muted">${esc(plainText(candidate.read_error || progress?.status || "검사 전"))}${progress?.attempt > 1 ? ` · 재시도 ${progress.attempt - 1}회` : ""}</span>`;
    const detail = tr.querySelector("[data-case-detail]");
    if (detail) detail.ontoggle = async () => {
      if (!detail.open || detail.dataset.loaded || batch !== job || job.busy) return;
      detail.dataset.loaded = "loading";
      const output = detail.querySelector("div"), ctx = { id: row.case_id, server: job.server };
      const current = () => batch === job && job.rows[index]?.case_id === row.case_id && job.snapshots?.get(index) === version && detail.isConnected;
      output.textContent = "이 상품의 검사 기록 읽는 중";
      try {
        const snapshot = await api(`/cases/${encodeURIComponent(row.case_id)}`, undefined, ctx);
        if (!current()) return;
        if (snapshot.case_id !== row.case_id) throw new Error("다른 검사의 기록이라 표시하지 않았습니다.");
        const pending = (snapshot.items || []).filter(it => it.status === "handoff");
        output.innerHTML = `<ul>${pending.map(it => `<li>${esc(plainText(`${it.title}: ${it.reason || it.result_label || "확인 필요"}`))}</li>`).join("")}</ul>
          ${snapshot.question ? '<p>현재 질문의 답변은 이 상품 검사에서 이어 확인하세요.</p>' : ""}
          <section class="case-actions"></section>
          <a href="${esc(job.server)}/cases/${encodeURIComponent(row.case_id)}/report" target="_blank" rel="noopener">이 상품의 실행 로그</a>`;
        renderCaseActions(output.querySelector(".case-actions"), snapshot, ctx, current);
        if (Number.isInteger(snapshot.event_count)) {
          const open = document.createElement("button"); open.type = "button"; open.textContent = "이 상품 검사 이어 확인";
          open.onclick = () => void openBatchCase(job, index); output.prepend(open);
        }
        detail.dataset.loaded = "ready";
      } catch (error) {
        if (current()) { output.textContent = errorText(error); delete detail.dataset.loaded; }
      }
    };
  }
}
function batchQuestions(job) {
  $("batch-question-list").replaceChildren();
  const pending = job.finished ? job.candidates.map((_, index) => ({ row: observedBatchRow(job, index), index })).filter(({ row }) => row?.confirmation?.id) : [];
  $("batch-questions").hidden = !pending.length;
  for (const { row, index } of pending) {
    const details = document.createElement("details");
    details.className = "batch-question";
    details.innerHTML = `<summary>${esc(row.title)}</summary><pre tabindex="0">${esc(plainText(row.confirmation.text))}</pre><button type="button">실제 라벨과 일치함을 직접 확인</button>`;
    const button = details.querySelector("button"), token = row.confirmation.id;
    button.dataset.used = String(job.usedTokens.has(token));
    if (job.usedTokens.has(token)) button.textContent = "재전송하지 않음 · 새 검수 필요";
    button.onclick = () => void confirmBatchRow(job, index, token);
    $("batch-question-list").append(details);
  }
  syncEntryControls();
}
async function reviewBatch(job, indices, token) {
  if (!serverReady) throw new Error("실제 검사 서버에 연결한 뒤 시작하세요.");
  const response = await fetch(job.server + "/search-first/review", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conditions: job.conditions, candidates: indices.map(index => token
      ? { ...job.candidates[index], label_confirmation: token } : job.candidates[index]) }) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `묶음 요청 실패 (${response.status})`);
  }
  if (!response.body) throw new Error("묶음 응답 본문이 없습니다.");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "", ended = false;
  const receive = (line) => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.ui_fixture === true || event.row?.ui_fixture === true) throw new Error("실제 검사 결과가 아니어서 표시하지 않았습니다.");
    if (batch !== job) throw new Error("현재 묶음 작업이 바뀌었습니다.");
    if (event.event === "done") { ended = true; job.summary = event; return; }
    if (!["row", "progress"].includes(event.event)) return;
    const index = indices[event.index - 1];
    if (!Number.isInteger(index)) throw new Error("응답의 상품 순서가 요청과 다릅니다.");
    if (event.event === "row") {
      if (event.row?.url !== job.candidates[index].url) throw new Error("다른 상품의 결과를 받아 행을 바꾸지 않았습니다.");
      job.rows[index] = event.row;
    } else {
      job.progress[index] = event;
      $("batch-status").textContent = `${index + 1}/${job.candidates.length} · ${plainText(event.status || "진행 응답")}${event.attempt > 1 ? " · 재시도" : ""}`;
    }
    batchRows(job);
  };
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let boundary;
      while ((boundary = buffer.indexOf("\n")) !== -1) { receive(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 1); }
      if (chunk.done) break;
    }
    receive(buffer);
    if (!ended) throw new Error("종료 사건 없이 연결이 끝났습니다. 받은 행만 보존했습니다.");
  } finally { await reader.cancel().catch(() => {}); }
}
async function confirmBatchRow(job, index, token) {
  if (batch !== job || job.busy || !job.finished || job.usedTokens.has(token) || job.rows[index]?.confirmation?.id !== token) return;
  job.busy = true; job.usedTokens.add(token); syncEntryControls();
  $("batch-status").textContent = `${index + 1}번 상품 · 확인 답변 전송 중`;
  try {
    await reviewBatch(job, [index], token);
    $("batch-status").textContent = `${index + 1}번 상품 · 확인 답변 후 결과 갱신`;
  } catch (error) {
    $("batch-status").textContent = `${errorText(error)} · 같은 확인 요청을 자동 재전송하지 않습니다.`;
  } finally {
    job.busy = false; batchRows(job); batchQuestions(job);
  }
}
async function collectBatch(job, candidates) {
  const seen = new Set();
  job.candidates = candidates.filter(candidate => {
    const product = batchUrl(candidate.url);
    if (seen.has(product.key)) return false;
    seen.add(product.key); candidate.url = product.url; return true;
  });
  if (!job.candidates.length) throw new Error("현재 목록에서 지원하는 상품 링크를 찾지 못했습니다.");
  if (job.candidates.length > 5) { job.candidates = []; throw new Error("한 번에 상품 주소 5개까지 넣으세요."); }
  $("batch-entry").hidden = true; $("batch-clarify").hidden = true;
  $("batch-rows").replaceChildren(); batchRows(job);
  const retry = [];
  let stopped = false;
  for (let index = 0; index < job.candidates.length; index++) {
    const candidate = job.candidates[index];
    if (stopped) { candidate.read_error = "사이트 접근 중단 · 이 상품은 읽지 않음"; continue; }
    $("batch-status").textContent = `${index + 1}/${job.candidates.length} · 상품 페이지 읽는 중`;
    try { job.candidates[index] = await readBatchCandidate(candidate); }
    catch (error) {
      candidate.read_error = error.message;
      if (error.stop) stopped = true;
      else if (error.retryable) retry.push(index);
    }
    batchRows(job);
    if (index + 1 < job.candidates.length && !stopped) await new Promise(resolve => setTimeout(resolve, 500));
  }
  if (!stopped) for (const index of retry) {
    $("batch-status").textContent = `${index + 1}번 상품 · 페이지 로드 마지막 재시도`;
    try { job.candidates[index] = await readBatchCandidate(job.candidates[index]); }
    catch (error) {
      job.candidates[index].read_error = error.message;
      if (error.stop) { stopped = true; break; }
    }
    batchRows(job);
  }
  if (stopped) $("batch-status").textContent = "사이트 접근 중단 · 이미 읽은 자료와 실패 기록만 정리합니다.";
  await reviewBatch(job, job.candidates.map((_, index) => index));
  job.finished = true;
  const missing = job.candidates.length - job.rows.filter(Boolean).length;
  $("batch-status").textContent = missing ? `서버 종료 · 응답 없는 상품 ${missing}개` : "검사 응답 종료 · 상품별 남은 확인을 보세요";
}
async function searchBatch(job) {
  if (typeof job.searchQuery !== "string" || !job.searchQuery.trim()) throw new Error("이 서버는 검색어 계획을 지원하지 않습니다. 서버 업데이트가 필요합니다.");
  const plan = await batchMessage(job.source.tabId, job.source.url, "batch_search", { query: job.searchQuery });
  if (plan.blocked || plan.error || plan.read_error) throw new Error(plan.reason || plan.error || plan.read_error);
  if (new URL(plan.url).origin !== new URL(job.source.url).origin) throw new Error("다른 사이트로 검색하지 않았습니다.");
  await batchPermission(plan.url);
  let tab;
  try {
    $("batch-status").textContent = "현재 사이트 검색 결과 읽는 중";
    tab = await chrome.tabs.create({ url: plan.url, active: false });
    const loaded = await waitBatchTab(tab.id);
    if (new URL(loaded.url).origin !== new URL(plan.url).origin) throw new Error("다른 사이트로 이동해 검색을 중단했습니다.");
    const result = await batchMessage(tab.id, loaded.url, "batch_links");
    if (result.blocked || result.read_error) throw new Error(result.reason || result.read_error);
    await collectBatch(job, result.candidates || []);
  } finally { if (tab?.id != null) await chrome.tabs.remove(tab.id).catch(() => {}); }
}
function finishBatch(job, error) {
  if (batch !== job) return;
  if (error) $("batch-status").textContent = errorText(error);
  job.busy = false; $("new-work").hidden = false;
  batchRows(job); batchQuestions(job); syncEntryControls();
}
async function beginBatch(mode) {
  if (capabilities.search_first !== true || entryBusy || batch?.busy || batch?.awaitingChoice || (run && (!run.terminal || run.chatBusy || run.altBusy))) return;
  es?.close(); clearTimeout(refreshTimer); run = null; caseId = null; lastPage = null;
  const job = { server: server(), conditions: null, candidates: [], rows: [], progress: [], usedTokens: new Set(), busy: true, finished: false };
  batch = job; syncEntryControls();
  for (const id of ["welcome", "entrybox", "work-summary", "resultbundle", "ask", "history-details", "scan", "new-work"]) $(id).hidden = true;
  $("batch-work").hidden = false; $("batch-questions").hidden = true; $("batch-rows").replaceChildren();
  $("batch-entry-error").textContent = ""; $("batch-status").textContent = "요청한 상품 확인 중"; batchRows(job);
  try {
    if (mode === "urls") {
      const urls = $("batch-urls").value.trim().split(/\s+/).filter(Boolean);
      await collectBatch(job, urls.map(url => ({ url: batchUrl(url).url, title: "" })));
    } else {
      const tab = await activeTab(null);
      job.source = { tabId: tab.id, url: tab.url?.split("#")[0] };
      if (mode === "list") {
        const result = await batchMessage(tab.id, job.source.url, "batch_links");
        if (result.blocked || result.read_error) throw new Error(result.reason || result.read_error);
        await collectBatch(job, result.candidates || []);
      } else {
        const query = $("batch-query").value.trim();
        if (!query) throw new Error("찾을 상품 조건을 넣으세요.");
        const interpreted = await api("/alt/interpret", { query }, job);
        if (!interpreted.conditions) throw new Error("상품 조건을 받지 못했습니다.");
        job.conditions = interpreted.conditions; job.searchQuery = interpreted.search_query;
        if (interpreted.question) {
          const choices = Object.entries(interpreted.question.choices || {});
          if (!choices.length) throw new Error("조건 선택지를 받지 못했습니다.");
          if (choices.some(([use]) => typeof interpreted.question.search_queries?.[use] !== "string" || !interpreted.question.search_queries[use].trim()))
            throw new Error("이 서버는 선택 후 검색어 계획을 지원하지 않습니다. 서버 업데이트가 필요합니다.");
          job.awaitingChoice = true; job.busy = false;
          $("batch-entry").hidden = false; $("batch-clarify").hidden = false;
          $("batch-clarify").innerHTML = `<p>${esc(plainText(interpreted.question.text))}</p>`;
          for (const [use, label] of choices) {
            const button = document.createElement("button"); button.type = "button"; button.textContent = label;
            button.onclick = async () => {
              if (batch !== job || !job.awaitingChoice || job.busy) return;
              job.conditions = { ...job.conditions, use, use_label: label }; job.awaitingChoice = false; job.busy = true;
              job.searchQuery = interpreted.question.search_queries[use];
              $("batch-clarify").hidden = true; syncEntryControls();
              try { await searchBatch(job); finishBatch(job); } catch (error) { finishBatch(job, error); }
            };
            $("batch-clarify").append(button);
          }
          $("batch-status").textContent = "조건 답변 대기 · 아직 검색하지 않음"; $("new-work").hidden = false; syncEntryControls(); return;
        }
        await searchBatch(job);
      }
    }
    finishBatch(job);
  } catch (error) { finishBatch(job, error); }
}
$("batch-list").onclick = () => void beginBatch("list");
$("batch-urls-go").onclick = () => void beginBatch("urls");
$("batch-search").onsubmit = (event) => { event.preventDefault(); void beginBatch("search"); };
$("batch-query").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void beginBatch("search"); } };
$("new-work").onclick = () => {
  if (entryBusy || batch?.busy || (run && ((!run.terminal && !run.executionError) || run.chatBusy || run.altBusy))) return;
  batch = null; run = null; caseId = null; lastPage = null; entrySession = null; entryPhoto = null; entryTextRequested = false;
  es?.close(); clearTimeout(refreshTimer);
  document.body.classList.remove("access-error");
  for (const id of ["batch-work", "batch-clarify", "entrybox", "work-summary", "resultbundle", "ask", "history-details", "scan", "new-work", "reconnect", "batch-back"]) $(id).hidden = true;
  $("welcome").hidden = false; $("composerhint").textContent = ""; $("photo-preview").hidden = true;
  $("photo-image").removeAttribute("src"); $("entrytext").value = ""; $("workstate").textContent = "시작 전";
  syncEntryControls(); void previewProduct(); $("conversation").scrollTop = 0;
};

function renderPhotoRecovery(container, snapshot, ctx) {
  if (!container) return;
  const evidence = snapshot.extras?.label_evidence;
  const emptyPhoto = ctx?.sellerImage && ctx.id === snapshot.case_id &&
    evidence && !(snapshot.items || []).some(item => item.id?.startsWith("ing:"));
  const visible = emptyPhoto && snapshot.status === "done" && !$("case-actions").querySelector(".followup");
  container.hidden = !visible;
  if (!visible) { container.replaceChildren(); delete container.dataset.caseId; return; }
  if (container.dataset.caseId === ctx.id) return;
  container.dataset.caseId = ctx.id;
  const read = (snapshot.steps || []).filter(step => step.result?.source === "ocr" && step.result.status === "read");
  const heading = read.some(step => (step.result.lines || []).some(line =>
    /^(?:(?:supplement|nutrition)\s+facts\b|(?:other\s+)?ingredients?\s*[:：]|(?:성분|원재료명)\s*[:：])/i.test(line.text?.trim())));
  const note = document.createElement("p");
  note.textContent = heading ? "성분표 표기는 읽었지만 대조할 성분 행을 확인하지 못했습니다."
    : read.length ? "읽은 글에서 성분표를 찾지 못함 · 성분을 대조하지 않았습니다."
    : "사진의 글자를 읽지 못함 · 성분을 대조하지 않았습니다.";
  const upload = document.createElement("button"), region = document.createElement("button");
  upload.type = region.type = "button";
  upload.textContent = "다른 사진 올리기"; region.textContent = "페이지 영역 고르기";
  const help = document.createElement("p");
  help.textContent = "같은 상품의 뒷면 성분표로 새 검사를 시작합니다.";
  const picker = document.createElement("input");
  picker.type = "file"; picker.accept = (capabilities.seller_image_mime || ["image/jpeg", "image/png"]).join(",");
  picker.hidden = true;
  const active = () => run === ctx && ctx.terminal && server() === ctx.server;
  const inspect = async getImage => {
    if (!active() || entryBusy) return;
    entryBusy = true; upload.disabled = region.disabled = true; syncEntryControls();
    try {
      await activeTab(ctx);
      const image = await getImage();
      if (image && active()) await startCase({ context: ctx, image });
    } catch (error) { if (active()) help.textContent = errorText(error); }
    finally { entryBusy = false; upload.disabled = region.disabled = false; syncEntryControls(); }
  };
  upload.onclick = () => { if (active() && !entryBusy) picker.click(); };
  picker.onchange = () => {
    const file = picker.files[0]; picker.value = "";
    if (!file) return;
    if (!picker.accept.split(",").includes(file.type) || file.size > (capabilities.seller_image_max_bytes || 10485760)) {
      help.textContent = "PNG 또는 JPEG 사진 · 최대 10MB"; return;
    }
    void inspect(() => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("사진을 열지 못했습니다."));
      reader.readAsDataURL(file);
    }));
  };
  region.onclick = () => void inspect(async () => (await selectRegion(ctx))?.image);
  container.replaceChildren(note, help, upload, region, picker);
}
