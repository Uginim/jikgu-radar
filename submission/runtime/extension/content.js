// 콘텐츠 스크립트: 열린 상품 페이지에서 에이전트의 '손과 눈'.
// - extract: 접힌 성분표를 펼치고, 제품명·브랜드·성분표 텍스트를 읽고, 성분표(또는 라벨 이미지) 위치로 스크롤
// - select_region: 사용자가 드래그로 성분표 영역을 고르게 한다(좌표를 돌려주면 패널이 캡처를 잘라 쓴다)
// - highlight / warn_cart / clear: 판정 결과를 페이지 위에 표시
// 성분 근거는 현재 상품의 명시된 절·표·사진 묶음으로 한정한다.
(() => {
  if (window.__nxLoaded) return;
  window.__nxLoaded = true;

  const FACTS = /^(?:supplement facts|nutrition(?:al)? (?:facts|information)|성분표(?:\s*\(영문\))?|영양\s*(?:성분(?:표)?|정보))\s*:?$/i;
  const DECLARATION = /^(other ingredients?|main ingredients?|ingredients?|기타\s*(?:성분|원료)|주(?:요)?\s*성분|원재료(?:명)?|성분|재료)(?:\s*[:：]\s*(.*)|\s*)$/i;
  const DOSAGE = /\d[\d,.]*\s*(?:mg|mcg|[μµ]g|g|iu|㎎|㎍)\b/i;
  const EXPAND_RE = /^(read more|show more|see more|view more|more details|show all|expand|더\s?보기|펼치기)$/i;
  const TAB_RE = /^(?:supplement facts|nutrition facts|(?:other )?ingredients?|ingredient info(?:rmation)?|성분표|성분(?: 정보)?|재료|원재료)$/i;
  const CART_RE = /(add to cart|add to basket|buy now|장바구니|바로\s?구매)/i;

  const visible = (el) => !!(el && el.offsetParent !== null && el.getClientRects().length);
  const text = (el) => (el?.innerText || el?.textContent || "").trim();
  let labelTarget = null, selectedRegion = null, area = null, areaTag = null, resultBadge = null;
  let annotationFrame = null, capturePreparing = false;
  let questionTargets = null, questionFocus = null, questionLimits = null;
  function questionBounds() {
    const rects = (questionTargets || []).filter(el => el.isConnected).map(el => el.getBoundingClientRect());
    if (!rects.length) return null;
    const left = Math.min(...rects.map(r => r.left)), top = Math.min(...rects.map(r => r.top));
    const right = Math.max(...rects.map(r => r.right)), bottom = Math.max(...rects.map(r => r.bottom));
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  }
  function questionViewport() {
    const r = questionBounds();
    if (!r) return null;
    const left = Math.max(0, r.left), top = Math.max(0, r.top);
    const right = Math.min(innerWidth, r.right), bottom = Math.min(innerHeight, r.bottom);
    const w = Math.max(0, Math.min(right - left, questionLimits?.width || Infinity));
    const h = Math.max(0, Math.min(bottom - top, questionLimits?.height || Infinity));
    const focus = questionFocus?.getBoundingClientRect();
    const x = focus && questionLimits ? Math.max(left, Math.min(right - w, focus.left - 8)) : left;
    const y = focus && questionLimits ? Math.max(top, Math.min(bottom - h, focus.top - (h - focus.height) / 2)) : top;
    return { x, y, w, h, dpr: devicePixelRatio, scrollX, scrollY,
      clipped: x > r.left || y > r.top || x + w < r.right || y + h < r.bottom };
  }
  const areaObserver = new ResizeObserver(scheduleArea);
  function positionArea() {
    annotationFrame = null;
    if (capturePreparing) { if (area) area.hidden = true; return; }
    if (!area) return;
    const roi = questionLimits ? questionViewport() : null;
    const r = roi ? { left: roi.x, top: roi.y, width: roi.w, height: roi.h }
      : questionBounds() || (labelTarget?.isConnected ? labelTarget.getBoundingClientRect() : selectedRegion
        ? { left: selectedRegion.x - scrollX, top: selectedRegion.y - scrollY, width: selectedRegion.w, height: selectedRegion.h } : null);
    if (!r || !r.width || !r.height) { area.hidden = true; return; }
    area.hidden = false;
    Object.assign(area.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
    if (areaTag) Object.assign(areaTag.style, { left: `${Math.max(0, 8 - r.left)}px`, top: r.top < 28 ? "4px" : "-28px" });
  }
  function scheduleArea() {
    if (area && annotationFrame === null) annotationFrame = requestAnimationFrame(positionArea);
  }
  window.addEventListener("scroll", scheduleArea, { passive: true, capture: true });
  window.addEventListener("resize", () => { selectedRegion = null; scheduleArea(); }, { passive: true });
  function clearArea() {
    areaObserver.disconnect();
    questionTargets = null; questionFocus = null; questionLimits = null;
    area?.remove(); area = null; areaTag = null; labelTarget = null; selectedRegion = null;
  }
  function showArea(label) {
    area?.remove();
    area = Object.assign(document.createElement("div"), { className: "nx-label-outline" });
    area.dataset.nxUi = "";
    areaTag = Object.assign(document.createElement("span"), { className: "nx-label-tag", textContent: label });
    area.append(areaTag); document.body.append(area); positionArea();
    if (labelTarget) areaObserver.observe(labelTarget);
  }
  async function revealQuestion(term, maxWidth, maxHeight) {
    if (!labelTarget?.isConnected || labelTarget.tagName === "IMG") return { shown: false };
    const needle = key(term);
    const rows = [...labelTarget.querySelectorAll("tr,[role=row],p,li")];
    if (labelTarget.matches("tr,[role=row],p,li")) rows.unshift(labelTarget);
    const row = needle ? rows.filter(el => visible(el) && key(text(el)).includes(needle))
      .sort((a, b) => text(a).length - text(b).length)[0] : labelTarget;
    if (!row) return { shown: false };
    clearMarks();
    questionLimits = Number.isFinite(maxWidth) && maxWidth > 0 && Number.isFinite(maxHeight) && maxHeight > 0
      ? { width: Math.min(448, maxWidth), height: Math.min(448, maxHeight) } : null;
    questionFocus = [...row.querySelectorAll("td,th,span")].filter(el => visible(el) && needle && key(text(el)).includes(needle))
      .sort((a, b) => text(a).length - text(b).length)[0] || row;
    questionTargets = [row.previousElementSibling, row, row.nextElementSibling]
      .filter(el => el && visible(el) && (el === labelTarget || labelTarget.contains(el)));
    row.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    areaObserver.disconnect();
    showArea(questionLimits ? "스스로 확인할 질문 행 주변" : needle ? "질문에 해당하는 행 · 이웃 행" : "질문의 성분표 원문");
    questionTargets.forEach(el => areaObserver.observe(el));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return { shown: true, rect: questionViewport(), row: text(row) };
  }
  function showResult(msg) {
    resultBadge?.remove();
    const labels = { blocked: "등록 보류 권고", not_found: "일치 항목 없음", review: "직접 확인 필요" };
    resultBadge = Object.assign(document.createElement("div"), { className: "nx-result-badge", textContent:
      `${labels[msg.verdict] || labels.review} · ${msg.complete ? "제공 범위 검토 완료" : "전체 검토 미완료"}` });
    resultBadge.dataset.nxUi = ""; resultBadge.dataset.result = msg.verdict;
    resultBadge.title = "표시 성분만 확인 · 통관 보증 아님";
    document.body.append(resultBadge);
    if (msg.seller_image) clearArea();
    return { shown: true };
  }

  const tidy = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const key = (s) => tidy(s).normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  const tags = (el) => `${el.id} ${el.getAttribute("class") || ""} ${el.getAttribute("aria-label") || ""}`;
  const unrelated = /recommend|sponsor|related[-_ ]?products?|frequently[-_ ]?bought|also[-_ ]?(?:viewed|bought)|product[-_ ]?(?:card|tile)|comparison|customers?[-_ ]?reviews?|추천|함께\s*본|연관\s*상품/i;
  function outside(el) {
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      if (p.matches("aside,nav,footer,[role=navigation],[role=dialog]") || unrelated.test(tags(p))) return true;
      if (p.matches("section,article,[role=region]") && unrelated.test(text(p.querySelector(":scope > :is(h2,h3)")))) return true;
      if (p.matches("li,article,[role=listitem],div") && p.parentElement?.childElementCount > 1) {
        const imageLink = (card) => card.querySelector(":scope > a[href]:has(img)");
        const link = imageLink(p);
        if (link && !/\.(?:jpe?g|png|webp|avif)$/i.test(link.pathname) && link.pathname !== location.pathname &&
            [...p.parentElement.children].some(s => s !== p && imageLink(s)?.pathname && imageLink(s).pathname !== link.pathname)) return true;
      }
    }
    return false;
  }
  const pageUrl = (s) => { try { return new URL(s, document.baseURI); } catch (_) { return null; } };
  const idMatch = (a, b) => !!(key(a) && key(b) && (key(a).includes(key(b)) || key(b).includes(key(a))));

  function identity() {
    const products = [];
    const collect = (node) => {
      if (Array.isArray(node)) return node.forEach(collect);
      if (!node || typeof node !== "object") return;
      if ([node["@type"]].flat().includes("Product") && typeof node.name === "string") products.push(node);
      if (node["@graph"]) collect(node["@graph"]);
      if (node.mainEntity) collect(node.mainEntity);
    };
    document.querySelectorAll('script[type="application/ld+json"]').forEach((el) => {
      try { collect(JSON.parse(el.textContent)); } catch (_) { /* invalid metadata */ }
    });
    const url = location.href.split("#")[0];
    const og = tidy(document.querySelector('meta[property="og:title"]')?.content);
    const doc = tidy(document.title);
    const matchesUrl = (p) => p.url && pageUrl(p.url)?.href.split("#")[0] === url;
    const validTitle = (s) => s.length > 2 && !/keyboard shortcut|skip to (?:main|content)|키보드|단축키|접근성|제품 요약[:은]|add(?:ed)? to cart|장바구니에 추가/i.test(s);
    const headings = [...document.querySelectorAll('h1,[role=heading][aria-level="1"],[itemprop=name]')].filter(el =>
      !outside(el) && !el.closest("a[href]") && validTitle(tidy(text(el))));
    const score = (el) => {
      const s = tidy(text(el));
      return 8 * Number(idMatch(doc, s)) + 8 * Number(idMatch(og, s)) +
        12 * Number(products.some(p => idMatch(p.name, s) && (!p.url || matchesUrl(p)))) +
        2 * Number(visible(el)) + Math.min(2, parseFloat(getComputedStyle(el).fontSize) / 30);
    };
    const heading = headings.map(el => ({el, weight:score(el)})).sort((a,b) => b.weight-a.weight)[0]?.el;
    const h = tidy(text(heading));
    const compatible = products.filter(p => (!p.url || matchesUrl(p)) && (matchesUrl(p) || idMatch(h, p.name) || idMatch(og || doc, p.name)));
    const product = compatible.length === 1 ? compatible[0] : null;
    const site = tidy(document.querySelector('meta[property="og:site_name"]')?.content);
    const metaTitle = (og || doc).split(/\s+[|–]\s+/)[0].trim();
    const fuller = h && key(metaTitle).endsWith(key(h)) && metaTitle.length < h.length + 40 && !/[:：]/.test(metaTitle);
    const title = tidy(product?.name) || (fuller ? metaTitle : h) || (validTitle(metaTitle) && metaTitle !== site ? metaTitle : "");
    const brand = tidy(typeof product?.brand === "string" ? product.brand : product?.brand?.name);
    const barcode = product?.gtin14 || product?.gtin13 || product?.gtin12 || product?.gtin8 || product?.gtin || "";
    return {heading, product, data:{url, title:title.slice(0,300), brand:brand.slice(0,120), brand_source:brand ? "jsonld:Product/brand" : null,
      barcode:String(barcode), barcode_source:barcode ? "jsonld:Product/gtin" : null}};
  }
  const readProduct = () => identity().data;

  function expandCollapsed() {
    const clicked = [];
    for (const d of document.querySelectorAll("details:not([open])")) {
      const s = text(d.querySelector(":scope > summary"));
      if (!outside(d) && TAB_RE.test(s)) { d.open = true; clicked.push(s); }
    }
    for (const el of document.querySelectorAll('button,[role=button],[role=tab],a[aria-controls],a[aria-expanded]')) {
      if (el.closest("form") || el.matches(':disabled,[aria-selected=true],[aria-expanded=true]') || !visible(el) || outside(el)) continue;
      const href = el.getAttribute("href");
      if (href && !href.startsWith("#")) continue;
      const s = tidy(text(el)), target = document.getElementById(el.getAttribute("aria-controls"));
      if (TAB_RE.test(s) || (EXPAND_RE.test(s) && target && /ingredient|facts|성분|원재료/i.test(tags(target)))) {
        el.click(); clicked.push(s);
      }
    }
    return clicked;
  }

  const servingLine = /^(?:serving size|servings per container|1회\s*제공량)\b/i;
  const tableRows = t => [...t.rows].filter(r => r.closest("table") === t)
    .map(r => [...r.cells].map(c => tidy(text(c))).join(" ").trim()).filter(Boolean);
  // Cross a responsive copy only if ordered nutrient names, amounts and DV values agree.
  const rowSignature = rows => rows.join(" ").toLowerCase().replace(/\b(?:description|amount per serving|daily value)\b|[%\s]/g, "");
  function precedingFacts(t, rows) {
    const before = [];
    for (let block = t, depth = 0; block && depth < 3; block = block.parentElement, depth++) {
      let prev = block.previousElementSibling;
      while (prev && !tidy(text(prev))) prev = prev.previousElementSibling;
      if (!prev) continue;
      if (prev.matches("table") && rowSignature(tableRows(prev)) === rowSignature(rows)) prev = prev.previousElementSibling;
      for (; prev && before.length < 6; prev = prev.previousElementSibling) {
        const parts = prev.matches("table") ? [tidy(text(prev.caption)), ...tableRows(prev)].filter(Boolean) : [tidy(text(prev))];
        if (!parts.length || !parts.every(s => FACTS.test(s) || servingLine.test(s))) break;
        before.unshift(...parts);
      }
      break;
    }
    const caption = tidy(text(t.caption));
    if (FACTS.test(caption) && !before.includes(caption)) before.push(caption);
    return before;
  }

  // A dosage in an advertisement is not evidence. Require an ingredient declaration
  // or a facts table with serving/DV columns, then copy only those rows and siblings.
  function labelSections() {
    const tables = [];
    for (const t of document.querySelectorAll("table")) {
      if (outside(t)) continue;
      const rows = tableRows(t);
      const header = rows.slice(0,5).join(" ");
      if (!rows.some(r => DOSAGE.test(r)) || !/supplement facts|nutrition facts|serving size|amount per serving|% ?(?:daily value|dv)|영양\s*성분|1회\s*제공량/i.test(header)) continue;
      if (t.querySelector("table,a[href] img")) continue;
      const doses = rows.filter(r => DOSAGE.test(r));
      const namedDoses = doses.filter(r => !/^amount per serving\b/i.test(r)).length;
      if (!rows.some(x => FACTS.test(x))) rows.unshift(...precedingFacts(t, rows));
      tables.push({el:t, rows, score:doses.length + Number(visible(t)), namedDoses});
    }
    // Non-table labels often use a heading followed by locally bounded rows.
    // Never widen to a whole product container to manufacture a missing table.
    for (const h of document.querySelectorAll("h2,h3,h4,h5,legend,strong,[role=heading]")) {
      if (!FACTS.test(tidy(text(h))) || h.closest("table,button") || outside(h)) continue;
      const rows = [tidy(text(h))];
      for (let p = h.nextElementSibling; p && rows.length < 160; p = p.nextElementSibling) {
        if (p.matches("h1,h2,h3,h4,h5,h6,button,section,article") || outside(p)) break;
        if (p.matches("table") || p.querySelector("table,button,a[href] img")) break;
        const s = text(p);
        if (!s || s.length > 3000 || DECLARATION.test(tidy(s)) || /^(directions|warnings?|suggested use)\b/i.test(s)) break;
        rows.push(...s.split(/\n/).map(tidy).filter(Boolean));
      }
      const doses = rows.filter(r => DOSAGE.test(r)).length;
      if (doses) tables.push({el:h, rows, score:doses});
    }
    tables.sort((a,b) => b.score - a.score || (b.namedDoses || 0) - (a.namedDoses || 0));
    const table = tables[0], lines = table ? [...table.rows] : [];
    let target = table?.el;
    const declarations = new Set();
    for (const el of document.querySelectorAll("h2,h3,h4,h5,h6,dt,th,strong,b,p,legend,[role=heading]")) {
      if (el.childElementCount > 4) continue;
      const m = DECLARATION.exec(tidy(text(el)));
      if (!m || outside(el) || el.closest("button,nav,summary,[role=tab]")) continue;
      let s = m[2], sibling;
      if (!s) {
        // A heading can be wrapped by an otherwise empty paragraph.
        let h = el;
        for (let n = 0; n < 3 && h && !h.nextElementSibling && tidy(text(h.parentElement)) === tidy(text(el)); n++) h = h.parentElement;
        sibling = h?.nextElementSibling;
        if (!sibling || sibling.matches("h1,h2,h3,h4,h5,h6,button,table") || sibling.querySelector("h1,h2,h3,h4,h5,table,button") || outside(sibling)) continue;
        // Preserve <br> even in stored collapsed sections without layout.
        const copy = sibling.cloneNode(true);
        copy.querySelectorAll("br").forEach(b => b.replaceWith("\n"));
        copy.querySelectorAll("p,li,div").forEach(p => p.append("\n"));
        s = copy.textContent.trim();
      }
      if (!s || s.length > 7000 || /mass balance|may be mixed during processing/i.test(s)) continue;
      const nested = s.trim().split("\n")[0];
      const section = DECLARATION.test(nested) ? s.trim() : `${m[1]}: ${s}`.trim(), k = key(section);
      if (declarations.has(k)) continue;
      declarations.add(k); lines.push(section);
      if (!target) target = sibling || el;
    }
    return {lines, target, hasFactsHeading:!!table?.rows.some(row => FACTS.test(row))};
  }

  // Link a high-resolution URL to its *actual gallery thumbnail*, not merely
  // to another image in page scripts. Never execute gallery configuration.
  function galleryVariants() {
    const map = new Map();
    for (const script of document.scripts) {
      const raw = script.textContent;
      if (!raw || !/"thumb(?:nail)?"\s*:/i.test(raw)) continue;
      for (const m of raw.matchAll(/['"](\[\s*\{[^'\n]{1,200000}?\}\s*\])['"]/g)) {
        let items; try { items = JSON.parse(m[1]); } catch (_) { continue; }
        for (const item of items) {
          const thumb = pageUrl(item?.thumb || item?.thumbnail || ""), high = item?.hiRes || item?.large;
          if (!thumb || typeof high !== "string" || pageUrl(high)?.protocol !== "https:") continue;
          const name = thumb.pathname.split("/").pop();
          // Saved pages rewrite thumbnail hosts. Use a basename only if unambiguous.
          map.set(name, map.has(name) && map.get(name) !== high ? null : high);
        }
      }
    }
    return map;
  }
  function gallery(identity) {
    const images = [], byUrl = new Set(), groups = new Map(), title = identity.data.title;
    const add = (src, el) => {
      const u = src && pageUrl(src);
      if (!u || !["http:","https:"].includes(u.protocol) || byUrl.has(u.href) || !/\.(?:jpe?g|png|webp|avif)(?:$|[/?])/i.test(u.pathname) && !el) return;
      byUrl.add(u.href); images.push({url:u.href, el});
    };
    for (const im of document.images) {
      if (outside(im) || /\bbanner\b|quality.promise|video thumbnail|play-icon-overlay/i.test(im.src + " " + im.alt)) continue;
      const link = im.closest("a[href]"), dest = link && pageUrl(link.getAttribute("href"));
      if (dest && dest.href.split("#")[0] !== location.href.split("#")[0] && !/\.(?:jpe?g|png|webp|avif)$/i.test(dest.pathname)) continue;
      let g = im.parentElement;
      while (g && g !== document.body) {
        if (/gallery|thumbnail|alt[-_]?images|product[-_]?images?|image[-_]?block|media[-_]?viewer/i.test(tags(g)) && g.querySelectorAll("img").length >= 2) break;
        g = g.parentElement;
      }
      if (!g || g === document.body || g.querySelectorAll("a[href] img").length > 15) continue;
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(im);
    }
    const grade = ([g, imgs]) => {
      const explicit = /product|gallery|alt[-_]?images/i.test(tags(g)), anchor = imgs.some(im => im.alt.length > 7 && idMatch(title, im.alt));
      const selected = g.querySelector('[aria-selected=true],[aria-current=true],[class*="selected"],[class*="active"]');
      const multiple = new Set(imgs.map(im => im.currentSrc || im.src)).size >= 2;
      return (explicit || anchor || selected) ? 16*Number(multiple)+8*Number(anchor)+4*Number(!!selected)+2*Number(explicit)+Number(!!g.closest("main,[role=main]")) : 0;
    };
    const best = [...groups].map(group => ({group, weight:grade(group)})).sort((a,b) => b.weight-a.weight)[0];
    if (best?.weight) {
      const variants = galleryVariants();
      for (const im of best.group[1]) {
        let large;
        try { large = JSON.parse(im.dataset.lgimg || "{}").url; } catch (_) { /* not JSON */ }
        const thumb = pageUrl(im.currentSrc || im.src)?.pathname.split("/").pop();
        add(variants.get(thumb) || im.dataset.zoomImage || im.dataset.largeImg || im.dataset.oldHires || large ||
          im.closest('a[href]')?.getAttribute('href')?.match(/^[^#]+\.(?:jpg|png|webp)(?:\?.*)?$/i)?.[0] || im.currentSrc || im.src, im);
      }
    }
    if (!images.length && identity.product?.image) {
      for (const value of [identity.product.image].flat()) add(typeof value === "string" ? value : value?.url || value?.contentUrl, null);
    }
    return images.slice(0,8);
  }

  async function imageCandidate({url}) {
    // Anonymous image load only. The API receives bounded pixels; it must not fetch
    // arbitrary seller-controlled URLs. Canvas security failures become abstentions.
    const im = new Image(); im.crossOrigin = "anonymous"; im.referrerPolicy = "no-referrer";
    const ok = await new Promise(resolve => {
      const done = good => { clearTimeout(timer); im.onload = im.onerror = null; resolve(good); };
      const timer = setTimeout(() => { done(false); im.removeAttribute("src"); }, 5000);
      im.onload = () => done(true); im.onerror = () => done(false); im.src = url;
    });
    if (ok && Math.max(im.naturalWidth, im.naturalHeight) >= 200 && im.naturalWidth * im.naturalHeight <= 40000000) {
      try {
        const scale = Math.min(1,1600/Math.max(im.naturalWidth, im.naturalHeight)), canvas = document.createElement("canvas");
        canvas.width = Math.max(1,Math.round(im.naturalWidth*scale)); canvas.height = Math.max(1,Math.round(im.naturalHeight*scale));
        const ctx = canvas.getContext("2d"); ctx.fillStyle = "white"; ctx.fillRect(0,0,canvas.width,canvas.height);
        ctx.drawImage(im,0,0,canvas.width,canvas.height); const data = canvas.toDataURL("image/jpeg",.9);
        if (data.length <= 2000000) return {url, data};
      } catch (_) { /* CORS / decoding failure: never substitute an unrelated image */ }
    }
    return {url};
  }
  async function extract(generic = true) {
    const access = pageAccess();
    if (access.blocked) return access; // The same URL can switch to a challenge between preflight and collection.
    clearMarks(); clearArea(); resultBadge?.remove(); resultBadge = null;
    const clicked = expandCollapsed();
    if (clicked.length) await new Promise(r => setTimeout(r,600));
    const id = identity(), label = labelSections();
    // An unheaded dosage grid can be a retailer transcription rather than the
    // package's facts panel. Keep it, but seek an independent gallery label.
    const candidates = generic && !label.hasFactsHeading ? gallery(id) : [];
    if (!generic && !label.hasFactsHeading) { label.lines = []; label.target = null; }
    const images = await Promise.all(candidates.map(imageCandidate));
    if (clicked.length || candidates.length) {
      const current = pageAccess();
      if (current.blocked) return current;
    }
    const pageText = label.lines.join("\n"), html = document.createElement("div");
    for (const line of label.lines) for (const segment of line.split(/\n/)) {
      const p = document.createElement("p"); p.textContent = segment; html.append(p);
    }
    labelTarget = label.target || null; // A gallery candidate is not yet an identified label.
    if (labelTarget && visible(labelTarget)) {
      labelTarget.scrollIntoView({ block: "center", behavior: "smooth" });
      showArea("확인할 성분표");
    }
    return {...id.data, page_text:pageText, ingredient_html:html.innerHTML, images, expanded:clicked,
      found:label.lines.length ? "text" : images.some(im => im.data) ? "image" : null};
  }

  function selectRegion() {
    return new Promise((resolve) => {
      const ov = Object.assign(document.createElement("div"), { className: "nx-select-overlay" });
      const box = Object.assign(document.createElement("div"), { className: "nx-select-box" });
      const hint = Object.assign(document.createElement("div"), { className: "nx-select-hint",
        textContent: "성분표 부분을 드래그해서 선택해 주세요 (Esc 취소)" });
      document.body.append(ov, hint);
      let sx, sy;
      const done = (rect) => {
        ov.remove(); box.remove(); hint.remove(); document.removeEventListener("keydown", esc);
        if (rect) { clearArea(); selectedRegion = { x: rect.x + scrollX, y: rect.y + scrollY, w: rect.w, h: rect.h }; }
        resolve(rect);
      };
      const esc = (e) => { if (e.key === "Escape") done(null); };
      document.addEventListener("keydown", esc);
      ov.addEventListener("mousedown", (e) => { sx = e.clientX; sy = e.clientY; document.body.append(box); });
      ov.addEventListener("mousemove", (e) => {
        if (sx == null) return;
        Object.assign(box.style, { left: Math.min(sx, e.clientX) + "px", top: Math.min(sy, e.clientY) + "px",
          width: Math.abs(e.clientX - sx) + "px", height: Math.abs(e.clientY - sy) + "px" });
      });
      ov.addEventListener("mouseup", (e) => {
        const r = { x: Math.min(sx, e.clientX), y: Math.min(sy, e.clientY), w: Math.abs(e.clientX - sx), h: Math.abs(e.clientY - sy) };
        // 오버레이를 지운 다음 프레임에서 캡처해야 파란 상자가 찍히지 않는다
        done(r.w > 10 && r.h > 10 ? { ...r, dpr: window.devicePixelRatio } : null);
      });
    });
  }

  function clearMarks() {
    document.querySelectorAll("mark.nx-blocked, mark.nx-review").forEach((m) => m.replaceWith(document.createTextNode(m.textContent)));
    document.querySelectorAll(".nx-cart-warning").forEach((w) => w.remove());
  }

  function highlight(terms) {
    clearMarks();
    if (!labelTarget || labelTarget.tagName === "IMG") return { marked: 0, reason: "글자 위치 미확인" };
    let first = null;
    for (const t of terms) {
      const needle = String(t.text || "").toLowerCase();
      if (!needle) continue;
      const walker = document.createTreeWalker(labelTarget, NodeFilter.SHOW_TEXT);
      const hits = [];
      while (walker.nextNode()) {
        const n = walker.currentNode;
        if (!visible(n.parentElement) || n.parentElement.closest("script,style,mark,[data-nx-ui]")) continue;
        if (n.nodeValue.toLowerCase().includes(needle)) hits.push(n);
      }
      for (const n of hits) {
        const i = n.nodeValue.toLowerCase().indexOf(needle);
        const mid = n.splitText(i); mid.splitText(needle.length);
        const m = Object.assign(document.createElement("mark"), { className: t.level === "blocked" ? "nx-blocked" : "nx-review",
          title: t.level === "blocked" ? `식약처 해외직구 차단 성분: ${t.entry || ""}` : "확인 필요" });
        m.dataset.tip = t.level === "blocked"
          ? `${t.text} → ${String(t.entry || "목록명 확인 필요").split(/[（(]/)[0].trim()}`
          : "직접 확인 필요";
        mid.replaceWith(m); m.append(mid);
        first = first || m;
      }
    }
    if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
    scheduleArea();
    return { marked: document.querySelectorAll("mark.nx-blocked, mark.nx-review").length };
  }

  function warnCart(message) {
    const btns = [...document.querySelectorAll("#add-to-cart-button, #buy-now-button, button, input[type=submit], a")]
      .filter((b) => visible(b) && CART_RE.test(text(b) || b.value || b.getAttribute("aria-label") || ""));
    btns.slice(0, 3).forEach((b) => {
      const w = Object.assign(document.createElement("div"), { className: "nx-cart-warning", textContent: "⚠ " + message });
      b.insertAdjacentElement("afterend", w);
    });
    return { warned: Math.min(btns.length, 3) };
  }

  // (P1) 검색 결과 페이지에서 상품 링크 모으기: 아이허브 상품 주소는 /pr/ 를 포함한다
  function listProducts(n) {
    const seen = new Set(), out = [];
    for (const a of document.querySelectorAll('a[href*="/pr/"]')) {
      const href = a.href.split("?")[0].split("#")[0];
      const title = (a.getAttribute("title") || a.getAttribute("aria-label") || text(a)).trim();
      if (seen.has(href) || title.length < 8) continue;
      seen.add(href);
      const img = a.querySelector("img") || a.closest("div")?.querySelector("img");
      out.push({ url: href, title: title.slice(0, 200), image: img?.currentSrc || img?.src || null });
      if (out.length >= (n || 5)) break;
    }
    return out;
  }

  function batchProductUrl(value) {
    const u = pageUrl(value);
    if (!u || !["http:", "https:"].includes(u.protocol) || u.username || u.password) return null;
    const id = u.pathname.match(/\/pr\/[^?#]+\/(\d+)\/?$/i)?.[1]
      || u.pathname.match(/\/(?:dp|gp\/product)\/([a-z0-9]{10})(?:\/|$)/i)?.[1];
    if (!id) return null;
    u.hash = "";
    return { url: u.href, key: `${u.origin}/${id.toUpperCase()}` };
  }
  const gateNotice = {
    security: "보안 확인 화면입니다. 확인을 마친 뒤 다시 검사",
    login: "로그인이 필요한 화면입니다. 로그인한 뒤 다시 검사",
    empty: "페이지 본문이 비어 있습니다. 상품이 표시된 뒤 다시 검사",
    not_product: "상품 상세 페이지가 아닙니다. 상품을 연 뒤 다시 검사",
  };
  const stopPage = (kind) => ({ blocked: true, kind, reason: gateNotice[kind], url: location.href });
  function batchAccess() {
    const status = performance.getEntriesByType("navigation")[0]?.responseStatus;
    const title = tidy(document.title), heading = tidy(document.querySelector("h1")?.innerText);
    const securityHeading = /^(?:just a moment|잠시만 기다리(?:십시오|세요)|access denied|robot check|security check|checking your browser|verify (?:that )?you are human|보안 확인(?: 수행 중)?)(?:[.!…\s]|$)/i;
    const wallElement = document.querySelector("#challenge-stage,#challenge-running,#challenge-form,#cf-error-details,form[action*='validateCaptcha']");
    const challengeScript = document.querySelector("script[src*='/cdn-cgi/challenge-platform/']");
    if (securityHeading.test(title) || securityHeading.test(heading) || visible(wallElement) ||
        (challengeScript && /보안 확인|verify (?:that )?you are human|security verification/i.test((document.body?.innerText || "").slice(0, 1000))))
      return stopPage("security");
    const loginHeading = /^(?:sign[ -]?in|log[ -]?in|로그인|계정에 로그인)(?:\s|$|[|:·–-])/i;
    const loginPath = /\/(?:login|signin|sign-in|log-in)(?:\/|$)/i.test(location.pathname);
    const password = [...document.querySelectorAll('input[type="password"]')].find(visible);
    if (status === 401 || loginPath || (password && (loginHeading.test(heading) || loginHeading.test(title) ||
        password.closest('[role="dialog"][aria-modal="true"],dialog[open]')))) return stopPage("login");
    if (status === 403 || status === 429) return stopPage("security");
    if (status === 404 || status === 410) return { blocked: false, read_error: `상품 페이지 없음 (${status})`, retryable: false };
    if (status >= 500) return { blocked: false, read_error: `상품 페이지 응답 실패 (${status})`, retryable: true };
    return { blocked: false, url: location.href };
  }

  // Batch search accepts listings. Single scans require evidence of one current product, not a title or URL alone.
  function pageAccess() {
    const access = batchAccess();
    if (access.blocked) return access;
    if (access.read_error) return stopPage(access.retryable ? "empty" : "not_product");
    const body = (document.body?.innerText || "").trim();
    if (!body) return stopPage("empty");
    const id = identity();
    if (id.product) return access;
    // Match a retailer's actual submitted item identifier to a complete URL path segment.
    // This also covers unavailable or image-only products which no longer expose a buy button.
    const pathIds = location.pathname.toLowerCase().split("/");
    const currentSku = [...document.querySelectorAll('input[name]')].some(el =>
      /^(?:sku|asin|gtin(?:8|12|13|14)?|product[_-]?id|item[_-]?id)$/i.test(el.name) &&
      /^[a-z0-9_-]{4,64}$/i.test(el.value) && pathIds.includes(el.value.toLowerCase()) && !outside(el));
    if (currentSku && id.data.title && body.length >= 24) return access;
    const heading = id.heading;
    if (!heading || !visible(heading)) return stopPage(body.length < 24 ? "empty" : "not_product");
    const ogType = document.querySelector('meta[property="og:type"]')?.content || "";
    if (/^(?:og:)?product(?:[.:]|$)/i.test(ogType) || heading.closest('[itemtype$="/Product"]')) return access;
    const hasOffer = [...document.querySelectorAll('button,input[type=submit],input[type=button],[role=button]')].some(el =>
      visible(el) && CART_RE.test(el.getAttribute("aria-label") || el.value || text(el)) && !outside(el));
    if (hasOffer) return access;
    const productDetails = document.querySelector('meta[property^="product:price"],meta[itemprop=sku],meta[itemprop=gtin13]');
    if (productDetails || [...document.querySelectorAll('[itemprop=price],[itemprop=sku]')].some(el => visible(el) && !outside(el))) return access;
    // Some manufacturers publish label information without an offer.
    const primaryImage = [...document.images].some(im => im.alt.length > 4 && idMatch(id.data.title, im.alt) && !outside(im));
    if (primaryImage && /(?:supplement facts|nutrition facts|other ingredients\s*:|성분표|원재료명)/i.test(body)) return access;
    return stopPage("not_product");
  }
  function batchLinks() {
    const access = batchAccess();
    if (access.blocked || access.read_error) return access;
    const found = [], seen = new Set();
    for (const a of document.querySelectorAll("a[href]")) {
      if (!visible(a) || a.closest('nav,header,footer,[role=navigation],[data-ad-id],[data-component-type="s-sponsored-result"],.AdHolder')) continue;
      const card = a.closest('[data-component-type="s-search-result"],article,[itemtype$="/Product"],[class*="product-card"]');
      if (card && [...card.querySelectorAll('.puis-sponsored-label-text,[aria-label="Sponsored"],.s-sponsored-label-info-icon')].some(visible)) continue;
      let parent = a, sponsored = false;
      for (let depth = 0; parent && depth < 4; depth++, parent = parent.parentElement) {
        if (/sponsor|advertis|광고/i.test(tags(parent))) { sponsored = true; break; }
      }
      if (sponsored) continue;
      const product = batchProductUrl(a.href);
      if (!product || new URL(product.url).origin !== location.origin || seen.has(product.key)) continue;
      const title = tidy(a.getAttribute("title") || text(a.querySelector("h2,h3")) || text(card?.querySelector("h2,h3")) || a.querySelector("img")?.alt || text(a));
      if (!title || /^(?:add to cart|buy now|장바구니|구매)$/i.test(title)) continue;
      seen.add(product.key); found.push({ url: product.url, title: title.slice(0,300) });
      if (found.length === 5) break;
    }
    return { blocked: false, url: location.href, candidates: found };
  }
  function batchSearch(query) {
    const access = batchAccess();
    if (access.blocked || access.read_error) return access;
    for (const form of document.forms) {
      if ((form.getAttribute("method") || "get").toLowerCase() !== "get") continue;
      const input = [...form.querySelectorAll('input[type=search],input[name="field-keywords"],input[name=k],input[name=q],input[name=kw]')]
        .find(el => visible(el) && !el.disabled && !["hidden","number","password"].includes(el.type));
      const action = pageUrl(form.getAttribute("action") || location.href);
      if (!input?.name || !action || action.origin !== location.origin || !/\/search(?:\/|$)|^\/s(?:\/|$)/i.test(action.pathname)) continue;
      for (const [name, value] of new FormData(form)) {
        if (typeof value === "string") action.searchParams.set(name, value);
      }
      action.searchParams.set(input.name, String(query)); action.hash = "";
      return { blocked: false, url: action.href };
    }
    return { blocked: false, error: "이 페이지에서 같은 사이트의 읽기 전용 검색 폼을 찾지 못했습니다. 목록을 직접 열어 읽거나 상품 주소를 넣으세요." };
  }

  chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    const run = {
      list_products: () => listProducts(msg.n),
      batch_access: () => batchAccess(),
      page_access: () => pageAccess(),
      batch_links: () => batchLinks(),
      batch_search: () => batchSearch(msg.query),
      context: () => readProduct(),
      select_label_asset: () => {
        const asset = pageUrl(msg.asset);
        if (!asset || !["http:","https:"].includes(asset.protocol)) return { shown: false };
        asset.search = ""; asset.hash = "";
        const chosen = gallery(identity()).find(candidate => {
          const url = pageUrl(candidate.url); if (!url) return false;
          url.search = ""; url.hash = ""; return url.href === asset.href;
        });
        if (!chosen?.el?.isConnected) return { shown: false };
        clearMarks(); labelTarget = chosen.el; selectedRegion = null;
        showArea("선택한 성분표 사진 · 글자 위치미확인");
        return { shown: true };
      },
      reveal_label: () => {
        if (!labelTarget?.isConnected) return { shown: false };
        labelTarget.scrollIntoView({ block: "center", behavior: "smooth" });
        return { shown: true };
      },
      reveal_question: () => revealQuestion(msg.term, msg.max_width, msg.max_height),
      question_rect: () => questionViewport(),
      inspection_result: () => showResult(msg),
      capture_prepare: () => {
        capturePreparing = true;
        if (area) area.hidden = true;
        if (resultBadge) resultBadge.hidden = true;
        return { prepared: true };
      },
      capture_finish: () => {
        capturePreparing = false;
        if (resultBadge) resultBadge.hidden = false;
        if (msg.captured && (labelTarget || selectedRegion)) {
          showArea(questionTargets ? "질문에 해당하는 행 · 이웃 행" : "캡처한 성분표 · 글자 위치 미확인"); area.classList.add("nx-captured");
        } else if (area) area.hidden = false;
        return { shown: !!area };
      },
      extract: () => extract(msg.site_generic !== false),
      select_region: () => selectRegion(),
      highlight: () => highlight(msg.terms || []),
      warn_cart: () => warnCart(msg.message),
      clear: () => { clearMarks(); clearArea(); resultBadge?.remove(); resultBadge = null; },
    }[msg.type];
    if (!run) return;
    Promise.resolve(run()).then((r) => reply({ ok: true, result: r })).catch((e) => reply({ ok: false, error: String(e) }));
    return true;
  });
})();
