let reviewRoot = null;
let reviewResult = null;
let reviewSource = null;
let dragState = null;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if(message?.type === 'RISK_REVIEW_PING'){
    sendResponse({ok:true});
    return true;
  }
  if(message?.type === 'RISK_REVIEW_GET_CONTEXT'){
    sendResponse(readPageContext());
    return true;
  }
  if(message?.type === 'RISK_REVIEW_PIN_OPEN'){
    openFloatingAssistant(message.payload?.source);
    sendResponse({ok:true});
    return true;
  }
  if(message?.type === 'RISK_REVIEW_PIN_CLOSE'){
    closeAssistant();
    sendResponse({ok:true});
    return true;
  }
  if(message?.type === 'RISK_REVIEW_RENDER'){
    renderReviewPanel(message.payload.result, message.payload.source);
    sendResponse({ok:true});
    return true;
  }
  return false;
});

chrome.storage.local.get(['riskReviewPinned'], saved => {
  if(saved.riskReviewPinned){
    openFloatingAssistant();
  }
});

function readPageContext(){
  const title = document.title || '';
  const desc = document.querySelector('meta[name="description"]')?.content || '';
  const h1 = Array.from(document.querySelectorAll('h1')).map(el => el.textContent.trim()).filter(Boolean).slice(0,2).join('；');
  const article = Array.from(document.querySelectorAll('article,p,.content,.text')).map(el => el.textContent.trim()).filter(Boolean).join(' ').slice(0,420);
  const selected = String(window.getSelection?.() || '').trim();
  const body = document.body?.innerText?.replace(/\s+/g, ' ').trim().slice(0, 420) || '';
  return {
    url: location.href,
    title,
    summary: selected || desc || h1 || article || body,
  };
}

async function ensureRoot(){
  if(reviewRoot) return;
  reviewRoot = document.createElement('div');
  reviewRoot.id = 'risk-review-root';
  reviewRoot.innerHTML = `
    <div class="rr-fab-wrap" data-role="fab-wrap">
      <button class="rr-fab" data-action="open" title="打开分析" aria-label="打开青年心盾分析面板"></button>
      <button class="rr-close-mini" data-action="close-mini" title="关闭" aria-label="关闭青年心盾">×</button>
      <aside class="rr-panel" role="complementary" aria-label="青年心盾内容分析">
        <header class="rr-head" data-role="panel-head">
          <div>
            <div class="rr-title">青年心盾 · 内容分析</div>
            <div class="rr-url" data-role="url"></div>
          </div>
          <div class="rr-actions">
            <button class="rr-btn" data-action="collapse" title="收起" aria-label="收起面板">-</button>
            <button class="rr-btn" data-action="close" title="关闭" aria-label="关闭面板">x</button>
          </div>
        </header>
        <main class="rr-scroll" data-role="body">
          <div class="rr-loading">正在分析当前页面...</div>
        </main>
      </aside>
    </div>
  `;
  document.documentElement.appendChild(reviewRoot);
  await restorePosition();
  if(!prefersReducedMotion()) anime({ targets: reviewRoot.querySelector('.rr-fab'), scale: [0, 1], duration: 380, easing: 'easeOutBack' });
  const fab = reviewRoot.querySelector('[data-action="open"]');
  const closeMini = reviewRoot.querySelector('[data-action="close-mini"]');

  fab.addEventListener('pointerdown', startFabDrag);
  reviewRoot.querySelector('[data-action="collapse"]').addEventListener('click', () => {
    reviewRoot.classList.remove('rr-open');
  });
  reviewRoot.querySelector('[data-action="close"]').addEventListener('click', async () => {
    await chrome.storage.local.set({riskReviewPinned:false});
    closeAssistant();
  });
  closeMini.addEventListener('click', async event => {
    event.preventDefault();
    event.stopPropagation();
    await chrome.storage.local.set({riskReviewPinned:false});
    closeAssistant();
  });
  closeMini.addEventListener('pointerdown', event => {
    event.preventDefault();
    event.stopPropagation();
  });
}

async function restorePosition(){
  const saved = await chrome.storage.local.get(['riskReviewFabPos']);
  const pos = saved.riskReviewFabPos || {};
  const rawX = pos.x !== undefined ? Number(pos.x) : window.innerWidth - 45;
  const x = clamp(rawX, -45, window.innerWidth - 45);
  const y = clamp(Number(pos.y || window.innerHeight - 230), 8, window.innerHeight - 82);
  setFabPosition(x, y);
  updateFabSide(x);
}

function setFabPosition(x, y){
  if(!reviewRoot) return;
  reviewRoot.style.setProperty('--rr-x', `${x}px`);
  reviewRoot.style.setProperty('--rr-y', `${y}px`);
}

function setPanelOffset(y){
  if(!reviewRoot) return;
  reviewRoot.style.setProperty('--rr-panel-offset-y', `${y}px`);
}

function startFabDrag(event){
  event.preventDefault();
  event.stopPropagation();
  const wrap = reviewRoot.querySelector('[data-role="fab-wrap"]');
  const rect = wrap.getBoundingClientRect();
  dragState = {
    pointerId:event.pointerId,
    handle:event.currentTarget,
    startX:event.clientX,
    startY:event.clientY,
    offsetX:event.clientX - rect.left,
    offsetY:event.clientY - rect.top,
    moved:false,
  };
  dragState.handle.setPointerCapture(event.pointerId);
  dragState.handle.addEventListener('pointermove', moveFabDrag);
  dragState.handle.addEventListener('pointerup', endFabDrag, {once:true});
  dragState.handle.addEventListener('pointercancel', cancelFabDrag, {once:true});
}

function moveFabDrag(event){
  if(!dragState || event.pointerId !== dragState.pointerId) return;
  const dx = Math.abs(event.clientX - dragState.startX);
  const dy = Math.abs(event.clientY - dragState.startY);
  if(dx + dy > 4) dragState.moved = true;
  const x = clamp(event.clientX - dragState.offsetX, 8, window.innerWidth - 82);
  const y = clamp(event.clientY - dragState.offsetY, 8, window.innerHeight - 82);
  setFabPosition(x, y);
  if(reviewRoot.classList.contains('rr-open')) updatePanelOffset();
}

async function endFabDrag(event){
  if(!dragState || event.pointerId !== dragState.pointerId) return;
  const wrap = reviewRoot.querySelector('[data-role="fab-wrap"]');
  const wasClick = !dragState.moved;
  cleanupDragListeners(event.pointerId);
  const rect = wrap.getBoundingClientRect();
  updatePanelOffset();
  if(wasClick){
    togglePanel();
    setTimeout(() => { dragState = null; }, 0);
    return;
  }
  const SNAP_ZONE = 80;
  const fabCenterX = rect.left + 45;
  let targetX = null;
  if(fabCenterX < SNAP_ZONE) targetX = -45;
  else if(fabCenterX > window.innerWidth - SNAP_ZONE) targetX = window.innerWidth - 45;

  if(targetX !== null){
    // 吸边前先收起面板
    if(reviewRoot.classList.contains('rr-open')){
      const panel = reviewRoot.querySelector('.rr-panel');
      reviewRoot.classList.remove('rr-open');
      if(panel){ panel.style.opacity = '0'; panel.style.transform = ''; }
    }
    if(prefersReducedMotion()){
      wrap.style.transform = '';
      setFabPosition(targetX, rect.top);
      updateFabSide(targetX);
      updatePanelOffset();
      chrome.storage.local.set({riskReviewFabPos:{x:targetX, y:rect.top}});
    } else {
      anime({
        targets: wrap,
        translateX: targetX - rect.left,
        duration: 320,
        easing: 'easeOutExpo',
        complete: async () => {
          wrap.style.transform = '';
          setFabPosition(targetX, rect.top);
          updateFabSide(targetX);
          updatePanelOffset();
          await chrome.storage.local.set({riskReviewFabPos:{x:targetX, y:rect.top}});
        }
      });
    }
  } else {
    setFabPosition(rect.left, rect.top);
    updateFabSide(rect.left);
    await chrome.storage.local.set({riskReviewFabPos:{x:rect.left, y:rect.top}});
  }
  setTimeout(() => { dragState = null; }, 0);
}

function cancelFabDrag(event){
  cleanupDragListeners(event.pointerId);
  dragState = null;
}

function cleanupDragListeners(pointerId){
  const handle = dragState?.handle;
  if(!handle) return;
  try{
    if(handle.hasPointerCapture?.(pointerId)) handle.releasePointerCapture(pointerId);
  }catch(_err){}
  handle.removeEventListener('pointermove', moveFabDrag);
}

function togglePanel(){
  updatePanelOffset();
  const panel = reviewRoot.querySelector('.rr-panel');
  const isOpen = reviewRoot.classList.contains('rr-open');
  if(prefersReducedMotion()){
    reviewRoot.classList.toggle('rr-open');
    panel.style.opacity = isOpen ? '0' : '1';
    return;
  }
  if(!isOpen){
    reviewRoot.classList.add('rr-open');
    anime({ targets: panel, opacity: [0, 1], scale: [0.88, 1], duration: 220, easing: 'easeOutExpo' });
  } else {
    anime({ targets: panel, opacity: [1, 0], scale: [1, 0.88], duration: 180, easing: 'easeInQuad',
      complete: () => reviewRoot.classList.remove('rr-open') });
  }
}

function updatePanelOffset(){
  const wrap = reviewRoot.querySelector('[data-role="fab-wrap"]');
  const rect = wrap.getBoundingClientRect();
  const panelH = Math.min(420, window.innerHeight - 32);
  const preferred = -Math.min(170, Math.max(0, panelH - 92));
  const minOffset = 12 - rect.top;
  const maxOffset = window.innerHeight - 12 - rect.top - panelH;
  const offsetY = clamp(preferred, Math.min(minOffset, maxOffset), Math.max(minOffset, maxOffset));
  setPanelOffset(offsetY);
}

function updateFabSide(x){
  if(!reviewRoot) return;
  reviewRoot.classList.toggle('rr-fab-left', (x + 45) < window.innerWidth / 2);
}

function prefersReducedMotion(){
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function closeAssistant(){
  if(reviewRoot){
    reviewRoot.remove();
    reviewRoot = null;
  }
}

async function openFloatingAssistant(source){
  await ensureRoot();
  reviewSource = source || readPageContext();
  setHeaderUrl(reviewSource.url || location.href);
  setBody('<div class="rr-loading">正在分析当前页面...</div>');
  try{
    const res = await chrome.runtime.sendMessage({
      type:'RISK_REVIEW_ANALYZE',
      payload:{
        endpoint:'http://localhost:8000/analyze',
        useDemoOnly:false,
        age_group:'13-15',
        source:reviewSource,
      }
    });
    if(!res?.ok) throw new Error(res?.error || '分析失败');
    renderReviewPanel(res.result, reviewSource);
  }catch(err){
    setBody(`<div class="rr-error">${escapeHtml(String(err?.message || err || '分析失败'))}</div>`);
  }
}

async function renderReviewPanel(result, source){
  reviewResult = result || {};
  reviewSource = source || readPageContext();
  await ensureRoot();
  setHeaderUrl(reviewResult.url || reviewSource.url || location.href);

  const d = reviewResult;
  const url = d.url || reviewSource.url || location.href;
  const title = d.title || reviewSource.title || '当前页面内容';
  const summary = d.content_summary || d.summary || reviewSource.summary || '暂未读取到正文内容。';
  const platform = d.platform || detectPlatform(url);
  const level = normalizeLevel(d.risk_level || d.risk_code || 'L0');
  const tags = (d.matched_tags || []).slice(0,5);
  const cases = (d.related_cases || d.cases || []).slice(0,4);
  const narrative = d?.narrative?.summary || d.risk_explanation || buildNarrative(d, levelLabel(level));

  setBody(`
    <section class="rr-card">
      <div class="rr-meta"><span>${escapeHtml(platform)}</span><span>${escapeHtml(formatInteraction(d.interaction))}</span></div>
      <div class="rr-name">${escapeHtml(title)}</div>
      <div class="rr-summary">${escapeHtml(summary)}</div>
    </section>

    <section class="rr-card">
      <div class="rr-section">识别标签</div>
      <div class="rr-tags">
        ${tags.length ? tags.map(tag => `<span class="rr-tag">${escapeHtml(tag.keyword || tag.l3_name || tag.l2_name || '风险标签')}</span>`).join('') : '<span class="rr-tag">未命中明确标签</span>'}
        ${tags[0]?.domain ? `<span class="rr-tag domain">${escapeHtml(tags[0].domain.replace('风险',''))}</span>` : ''}
      </div>

      <div class="rr-section">风险评级</div>
      <div class="rr-risk" style="color:${levelColor(level)}">${escapeHtml(d.risk_label || levelLabel(level))}</div>
      <div class="rr-bar">
        <div class="rr-gradient"></div>
        <div class="rr-dot" style="left:6%;border-color:${levelColor(level)}" data-target-left="${levelPos(level)}"></div>
      </div>
      <div class="rr-labels"><span>低风险</span><span>轻度</span><span>中度</span><span>高风险</span><span>极高</span></div>

      <div class="rr-section">叙事分析</div>
      <div class="rr-narrative">${escapeHtml(narrative)}</div>
    </section>

    <div class="rr-section">关联案例</div>
    <div id="rr-alert"></div>
    <div id="rr-cases">${cases.length ? '' : '<section class="rr-case"><div class="rr-case-name">暂无关联案例</div><div class="rr-case-text">当前结果未返回关联案例。</div></section>'}</div>
  `);

  const dot = reviewRoot?.querySelector('.rr-dot[data-target-left]');
  if(dot){
    const target = dot.dataset.targetLeft;
    anime({ targets: dot, left: [dot.style.left, target], duration: 600, easing: 'easeOutExpo', delay: 120 });
  }

  // 案例逐张渲染，扫到第一个 L2+ 时触发预警横幅
  if(cases.length){
    const casesEl  = reviewRoot?.querySelector('#rr-cases');
    const alertEl  = reviewRoot?.querySelector('#rr-alert');
    const LV_ORDER = {L4:4,L3:3,L2:2,L1:1,L0:0};
    let alertShown = false;
    cases.forEach((c, i) => {
      setTimeout(() => {
        if(!casesEl) return;
        const wrap = document.createElement('div');
        wrap.innerHTML = renderCase(c);
        casesEl.appendChild(wrap.firstElementChild);
        const lv = normalizeLevel(c.risk_level || 'L0');
        if(!alertShown && alertEl && (LV_ORDER[lv]||0) >= 2){
          alertShown = true;
          setTimeout(() => showRRAlert(alertEl, c, lv), 160);
        }
      }, i * 800);
    });
  }
}

function renderCase(c){
  const lv = normalizeLevel(c.risk_level || 'L0');
  const url = c.source_url || c.data_source_url || '';
  return `
    <section class="rr-case">
      <div class="rr-band" style="background:${levelColor(lv)}"></div>
      <div class="rr-case-top">
        <span class="rr-lv" style="color:${levelColor(lv)};border-color:${levelColor(lv)}">${levelLabel(lv)}</span>
        ${c.risk_domain ? `<span class="rr-domain">${escapeHtml(c.risk_domain)}</span>` : ''}
        <span class="rr-id">${escapeHtml(c.case_id || '')}</span>
      </div>
      <div class="rr-case-name">${escapeHtml(c.case_name || '关联案例')}</div>
      <div class="rr-case-text">${escapeHtml((c.representative_text || c.spread_mechanism || c.spread_path || '').slice(0,90) || '暂无摘要')}</div>
      ${url ? `<div class="rr-case-foot"><a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="rr-case-link">查看原文 ↗</a></div>` : ''}
    </section>
  `;
}

function setHeaderUrl(url){
  const el = reviewRoot?.querySelector('[data-role="url"]');
  if(el) el.textContent = url || '';
}

function setBody(html){
  const el = reviewRoot?.querySelector('[data-role="body"]');
  if(el) el.innerHTML = html;
}

function clamp(value, min, max){
  return Math.min(Math.max(value, min), max);
}

function normalizeLevel(level){
  const raw = String(level || '').trim();
  const map = {'极高风险':'L4','高风险':'L3','中度风险':'L2','轻度风险':'L1','低风险':'L0'};
  return map[raw] || raw || 'L0';
}

function levelLabel(level){
  return ({L4:'极高风险',L3:'高风险',L2:'中度风险',L1:'轻度风险',L0:'低风险'})[level] || level || '低风险';
}

function levelColor(level){
  return ({L4:'#c0392b',L3:'#e67e22',L2:'#d4ac0d',L1:'#27ae60',L0:'#95a5a6'})[level] || '#95a5a6';
}

function levelPos(level){
  return ({L4:'88%',L3:'66%',L2:'43%',L1:'20%',L0:'6%'})[level] || '6%';
}

function buildNarrative(d, levelText){
  const domain = d.primary_domain || d.matched_tags?.[0]?.domain || '当前内容';
  const tag = d.matched_tags?.[0]?.keyword || '相关表达';
  return `${domain}下命中“${tag}”等风险信号，综合判定为${levelText}。建议结合内容来源、表达语境、评论反馈和传播范围进行人工复核。`;
}

function formatInteraction(interaction = {}){
  const total = interaction.total || interaction.like || interaction.comment || interaction.share || 0;
  return total ? `互动 ${fmt(total)}` : '互动数据待补充';
}

function fmt(num){
  const n = Number(num || 0);
  if(n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return String(n || 0);
}

function detectPlatform(url){
  const text = String(url || '').toLowerCase();
  if(text.includes('douyin')) return '抖音';
  if(text.includes('weibo')) return '微博';
  if(text.includes('bilibili') || text.includes('b23.tv')) return 'B站';
  if(text.includes('xiaohongshu')) return '小红书';
  if(text.includes('file:')) return '本地页面';
  return '当前页面';
}

function escapeHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[ch]));
}

// ── 悬浮面板预警横幅 ──────────────────────────────────────
const _RR_LV_ACTION = {
  L4:'请立即人工复核，建议限制推荐',
  L3:'建议人工复核，关注传播范围',
  L2:'建议关注，注意后续变化',
};

function showRRAlert(el, c, lv){
  if(!el) return;
  const action = _RR_LV_ACTION[lv] || '建议关注';
  const detail = [c.case_name, c.risk_type].filter(Boolean).join(' · ');
  el.innerHTML = `
    <div class="rr-alert rr-alert-${lv.toLowerCase()}">
      <div class="rr-alert-stripe"></div>
      <div class="rr-alert-body">
        <div class="rr-alert-dot"></div>
        <div class="rr-alert-content">
          <div class="rr-alert-eye">案例预警 · Case Alert</div>
          <div class="rr-alert-title">关联到${levelLabel(lv)}案例 — ${escapeHtml(action)}</div>
          <div class="rr-alert-detail">${escapeHtml(detail)}</div>
        </div>
        <button class="rr-alert-close" onclick="this.closest('.rr-alert').remove()">✕</button>
      </div>
    </div>`;
}
