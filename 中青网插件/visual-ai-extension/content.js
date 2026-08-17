console.log('[青年心盾] content.js 已加载 v13（风险卡研判中占位）');
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
    openFloatingAssistant(message.payload?.source, {fresh: message.payload?.fresh});
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
  if(message?.type === 'RISK_REVIEW_STREAM'){
    onStreamEvent(message.payload);
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

// ── SPA 页面跳转检测（抖音/微博等 pushState 路由）──────────────
(function(){
  let _lastUrl = location.href;

  function _onUrlChange(){
    const cur = location.href;
    if(cur === _lastUrl) return;
    _lastUrl = cur;
    if(!reviewRoot) return;          // 面板未打开，不触发
    // 稍等页面渲染，再重新分析
    setTimeout(() => openFloatingAssistant(readPageContext()), 800);
  }

  // 拦截 pushState / replaceState
  const _push = history.pushState.bind(history);
  const _replace = history.replaceState.bind(history);
  history.pushState = function(...a){ _push(...a); _onUrlChange(); };
  history.replaceState = function(...a){ _replace(...a); _onUrlChange(); };

  // 浏览器前进/后退
  window.addEventListener('popstate', _onUrlChange);

  // 兜底：轮询（某些框架既不用 pushState 也不触发 popstate）
  setInterval(_onUrlChange, 2000);
})();

// ── 页面上下文提取：按平台分发到专用提取器，其余走通用兜底 ──
function readPageContext(){
  const host = location.hostname || '';
  try{
    if(/(^|\.)douyin\.com$/i.test(host)){
      const ctx = extractDouyin();
      if(ctx) return ctx;               // 提取失败返回 null，自动回退通用
    }
  }catch(_err){ /* 专用提取异常，静默回退通用提取 */ }
  return extractGeneric();
}

// 通用提取（静态文章页够用）：正文优先，meta description 仅作兜底。
// 真实网页(含测试页)的 <meta description> 常是 SEO/审核视角摘要，不是煽动正文本身，
// 优先送它会让六维判分严重偏低，故这里以 <article>/正文段落为主锚点。
function extractGeneric(){
  const title = document.title || '';
  const desc = document.querySelector('meta[name="description"]')?.content || '';
  const h1 = Array.from(document.querySelectorAll('h1')).map(el => el.textContent.trim()).filter(Boolean).slice(0,2).join('；');
  // 有 <article> 时取其 innerText（避免与内部 <p> 重复拼接）；否则退回段落/正文容器
  const articleEl = document.querySelector('article');
  const article = (articleEl
    ? articleEl.innerText
    : Array.from(document.querySelectorAll('p,.content,.text')).map(el => el.textContent.trim()).filter(Boolean).join(' ')
  ).replace(/\s+/g, ' ').trim().slice(0, 2000);
  const selected = String(window.getSelection?.() || '').trim();
  const body = document.body?.innerText?.replace(/\s+/g, ' ').trim().slice(0, 2000) || '';
  // 正文（h1+正文）优先；用户选中 > 正文 > meta 描述 > 整页文本兜底
  const mainText = [h1, article].filter(Boolean).join('。 ').trim();
  return {
    url: location.href,
    title,
    summary: selected || mainText || desc || body,
  };
}

// 抖音专用提取：以 data-e2e 属性为主锚点（class 名哈希会变，data-e2e 是其自测钩子，较稳），通用选择器兜底。
// 深度：正文 + 作者 + 互动数（赞/评/转）+ 热门评论。任一环节失败均不抛错。
function extractDouyin(){
  const text = el => (el?.textContent || '').replace(/\s+/g, ' ').trim();
  // detail-video-info 容器把「展开/正文/#标签/互动数/举报/发布时间」拼在一起，清掉正文以外的噪声
  const cleanDesc = raw => (raw || '')
    .replace(/^(展开|收起)\s*/, '')
    .split('举报')[0]
    .replace(/发布时间[:：].*$/, '')
    .replace(/\d{6,}\s*$/, '')      // 尾部拼接的赞/藏/转数字串
    .trim();
  // user-info 容器是「昵称+粉丝X+获赞Y+关注」，取昵称
  const cleanAuthor = raw => (raw || '').split(/粉丝|获赞|关注/)[0].trim();

  // 正文：当前抖音详情页用 detail-video-info（老版 video-desc 已废弃，保留兜底）
  const desc = cleanDesc(text(document.querySelector('[data-e2e="detail-video-info"]')))
    || text(pickVisible(document.querySelectorAll('[data-e2e="video-desc"], [data-e2e="feed-video-desc"]')))
    || document.querySelector('meta[name="description"]')?.content
    || (document.title || '').replace(/[-_|].*$/, '').trim();

  const author = cleanAuthor(text(document.querySelector('[data-e2e="user-info"]')))
    || text(pickVisible(document.querySelectorAll(
      '[data-e2e="video-author-nickname"], [data-e2e="feed-video-nickname"], [data-e2e="user-name"]'
    )));

  // 互动：当前详情页用 video-player-digg/collect/share（老版 *-count 保留兜底）
  const like = parseCount(text(document.querySelector('[data-e2e="video-player-digg"]')))
    || parseCount(text(pickVisible(document.querySelectorAll('[data-e2e="video-like-count"], [data-e2e="like-count"]'))));
  const collect = parseCount(text(document.querySelector('[data-e2e="video-player-collect"]')));
  const share = parseCount(text(document.querySelector('[data-e2e="video-player-share"]')))
    || parseCount(text(pickVisible(document.querySelectorAll('[data-e2e="video-share-count"], [data-e2e="share-count"]'))));
  const comment = parseCount(text(pickVisible(document.querySelectorAll('[data-e2e="video-comment-count"], [data-e2e="comment-count"]'))));

  // 热门评论：取评论列表前若干条正文（剔除用户名/时间等界面噪声，尽量取评论内容节点）
  const commentItems = Array.from(document.querySelectorAll('[data-e2e="comment-list"] [data-e2e="comment-item"]'))
    .length
    ? Array.from(document.querySelectorAll('[data-e2e="comment-list"] [data-e2e="comment-item"]'))
    : Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
  const comments = commentItems.slice(0, 5).map(item => {
    // 优先取评论内容节点，取不到则退回整条去掉用户名前缀
    const content = text(item.querySelector('[data-e2e="comment-item-content"], .comment-content, p, span:not([data-e2e])'))
      || text(item);
    return content;
  }).filter(c => c && c.length > 1).slice(0, 5);

  // 正文都没抓到，判定为提取失败，交回通用兜底
  if(!desc && !author && !comments.length) return null;

  const summary = [
    desc,
    comments.length ? `｜热评：${comments.slice(0,3).join(' / ')}` : ''
  ].filter(Boolean).join(' ').slice(0, 420);

  return {
    url: location.href,
    title: desc || document.title || '抖音视频',
    summary,
    platform: '抖音',
    author,
    interaction: { like, comment, share, collect, total: like + comment + share + collect },
    comments,
  };
}

// 从一组同类节点里挑“当前正在看”的那个：优先取几何中心最接近视口中心的可见节点，兜底取第一个。
function pickVisible(nodes){
  const list = Array.from(nodes || []).filter(Boolean);
  if(!list.length) return null;
  if(list.length === 1) return list[0];
  const vh = window.innerHeight || 0;
  const centerY = vh / 2;
  let best = list[0];
  let bestDist = Infinity;
  for(const el of list){
    const r = el.getBoundingClientRect?.();
    if(!r || r.width === 0 || r.height === 0) continue;   // 不可见/未渲染跳过
    if(r.bottom < 0 || r.top > vh) continue;               // 完全在视口外跳过
    const dist = Math.abs((r.top + r.bottom) / 2 - centerY);
    if(dist < bestDist){ bestDist = dist; best = el; }
  }
  return best;
}

// 抖音互动数解析："1.2万"→12000、"3.4w"/"10w+"→…、"1225"→1225；解析不出返回 0
function parseCount(raw){
  const s = String(raw || '').trim();
  if(!s) return 0;
  const m = /([\d.]+)\s*(万|w|亿)?/i.exec(s);
  if(!m) return 0;
  const num = parseFloat(m[1]);
  if(!isFinite(num)) return 0;
  const unit = (m[2] || '').toLowerCase();
  if(unit === '万' || unit === 'w') return Math.round(num * 10000);
  if(unit === '亿') return Math.round(num * 1e8);
  return Math.round(num);
}

// 抖音等 SPA 首屏内容异步渲染，提取可能早于正文出现（固定悬浮球时页面一加载就分析，
// 常抓到导航噪声）。分析前轮询到内容就绪再返回，最多等 ~6 秒后兜底返回当前结果。
async function readPageContextReady(){
  const onDouyin = /(^|\.)douyin\.com$/i.test(location.hostname || '');
  for(let i = 0; i < 12; i++){
    const ctx = readPageContext();
    const ready = onDouyin
      ? (ctx.platform === '抖音' && (ctx.summary || '').length >= 8)  // 抖音专用提取成功
      : (ctx.summary || '').length >= 20;                             // 普通页抓到实质正文
    if(ready) return ctx;
    await new Promise(r => setTimeout(r, 500));
  }
  return readPageContext();
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
            <button class="rr-btn" data-action="refresh" title="重新分析" aria-label="重新分析">↻</button>
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
  reviewRoot.querySelector('[data-action="refresh"]').addEventListener('click', () => {
    // 重新分析：清空旧数据 + 绕缓存重算，重走步骤条与卡片淡入动画
    openFloatingAssistant(reviewSource, {fresh: true});
  });
  reviewRoot.querySelector('[data-action="collapse"]').addEventListener('click', () => {
    // 收起：彻底收回到悬浮球（走统一的关闭动画并隐藏面板），而不是缩到半屏卡住
    if(reviewRoot.classList.contains('rr-open')) togglePanel();
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

  // 事件委托：处理案例展开/tab切换（避免 inline onclick 在隔离沙箱失效）
  const scroll = reviewRoot.querySelector('[data-role="body"]');
  scroll.addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if(!btn) return;
    const action = btn.dataset.action;
    const key    = btn.dataset.key;
    if(action === 'toggle-case' && key) rrToggleCase(key);
    if(action === 'switch-age'  && key) rrSwitchAge(btn, key, btn.dataset.age);
    if(action === 'switch-guide-age') rrSwitchGuideAge(btn, btn.dataset.age);
    if(action === 'export-report') rrExportReport(btn);
    if(action === 'open-positive' && key) rrOpenPositiveDetail(key);
    if(action === 'back-to-result') rrBackToResult();
    if(action === 'close-alert') btn.closest('.rr-alert')?.remove();
    if(action === 'jump-case' && key){
      const caseEl = reviewRoot?.querySelector(`#rr-case-${key}`);
      if(caseEl){
        caseEl.scrollIntoView({behavior:'smooth', block:'center'});
        caseEl.style.transition = 'box-shadow .2s';
        caseEl.style.boxShadow = '0 0 0 2px var(--rr-primary), 0 4px 16px rgba(0,0,0,.1)';
        setTimeout(() => { caseEl.style.boxShadow = ''; }, 1200);
      }
    }
    if(action === 'jump-major'){
      const majorEl = reviewRoot?.querySelector('.rr-major-alert');
      if(majorEl){
        majorEl.scrollIntoView({behavior:'smooth', block:'center'});
        const old = majorEl.style.boxShadow;
        majorEl.style.transition = 'box-shadow .2s';
        majorEl.style.boxShadow = '0 0 0 2px #c0392b, 0 6px 20px rgba(192,57,43,.32)';
        setTimeout(() => { majorEl.style.boxShadow = old || ''; }, 1300);
      }
    }
  });
}

async function restorePosition(){
  const saved = await chrome.storage.local.get(['riskReviewFabPos']);
  const pos = saved.riskReviewFabPos || {};
  const rawX = pos.x !== undefined ? Number(pos.x) : window.innerWidth - 50;
  const x = clamp(rawX, -50, window.innerWidth - 50);
  const y = clamp(Number(pos.y || window.innerHeight - 230), 8, window.innerHeight - 100);
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
  const x = clamp(event.clientX - dragState.offsetX, 8, window.innerWidth - 100);
  const y = clamp(event.clientY - dragState.offsetY, 8, window.innerHeight - 100);
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
  const fabCenterX = rect.left + 50;
  let targetX = null;
  if(fabCenterX < SNAP_ZONE) targetX = -50;
  else if(fabCenterX > window.innerWidth - SNAP_ZONE) targetX = window.innerWidth - 50;

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
  reviewRoot.classList.toggle('rr-fab-left', (x + 50) < window.innerWidth / 2);
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

// 首次出现后自动把面板弹开：等悬浮球出场动画（scale 0→1，~380ms）落定再弹，
// 读起来是“球冒出来→自动展开”。减弱动效时立即展开。
function autoRevealPanel(){
  if(!reviewRoot) return;
  const reveal = () => {
    if(reviewRoot && !reviewRoot.classList.contains('rr-open')) togglePanel();
  };
  if(prefersReducedMotion()) reveal();
  else setTimeout(reveal, 420);
}

// 检测态：分析进行中给根节点挂 rr-scanning，驱动悬浮球脉冲环 + 面板扫描光。
function rrSetScanning(on){
  if(!reviewRoot) return;
  reviewRoot.classList.toggle('rr-scanning', !!on);
}

// ── 分析中步骤条（伪进度）：结果返回前依次点亮，六维那步最慢、停在那里转 ──
let _rrStepTimer = null;
const RR_STEPS = ['读取页面内容', '匹配风险标签', '六维研判', '检索关联案例'];

function startAnalyzingSteps(){
  stopAnalyzingSteps();
  const items = RR_STEPS.map((s, i) =>
    `<li class="rr-step${i === 0 ? ' rr-step-active' : ''}"><span class="rr-step-dot"></span><span class="rr-step-label">${s}</span></li>`
  ).join('');
  setBody(`<div class="rr-analyzing"><div class="rr-analyzing-title">正在分析当前页面…</div><ul class="rr-steps">${items}</ul></div>`);
  let cur = 0;
  const HOLD = 2; // 停在“六维研判”——它确实是最慢的一步，等结果替换
  _rrStepTimer = setInterval(() => {
    const list = reviewRoot?.querySelectorAll('.rr-step');
    if(!list || !list.length){ stopAnalyzingSteps(); return; }
    if(cur < HOLD){
      list[cur].classList.remove('rr-step-active');
      list[cur].classList.add('rr-step-done');
      cur++;
      list[cur].classList.add('rr-step-active');
    }
  }, 1300);
}

function stopAnalyzingSteps(){
  if(_rrStepTimer){ clearInterval(_rrStepTimer); _rrStepTimer = null; }
}

// 扩展上下文是否仍有效（重新加载扩展后，旧页面的内容脚本会失效）
function rrExtAlive(){
  try { return !!(chrome && chrome.runtime && chrome.runtime.id); }
  catch(_){ return false; }
}
function rrIsStaleErr(err){
  const m = String((err && err.message) || err || '');
  return !rrExtAlive() || m.includes('context invalidated') || m.includes('Extension context');
}
function rrShowStale(){
  rrSetScanning(false);
  try { setBody('<div class="rr-error rr-stale">插件刚更新或被重新加载，本页面还在用旧版本。<br>请按 <b>F5 刷新本页面</b> 后再分析。</div>'); }
  catch(_){}
}

async function openFloatingAssistant(source, opts){
  opts = opts || {};
  // 首次出现（本次刚创建悬浮球）：稍后自动把面板弹开，让检测状态直接可见，
  // 不必等用户点悬浮球。SPA 换页/重新分析时不强开，尊重用户已收起的状态。
  const firstShow = !reviewRoot;
  await ensureRoot();
  // 扩展被重新加载后，旧标签页里的内容脚本会失去连接（Extension context invalidated）——
  // 此时任何 chrome.* 调用都会抛错，直接提示刷新，避免弹出原始英文报错。
  if(!rrExtAlive()){ rrShowStale(); return; }
  // 每次分析先清空上一轮结果与预警态，避免残留旧数据（点扩展图标时 opts.fresh 还会绕开后端缓存）
  reviewResult = null;
  const fab0 = reviewRoot.querySelector('.rr-fab');
  if(fab0) fab0.classList.remove('rr-fab-warn');
  rrResetFabLevel();
  const panel0 = reviewRoot.querySelector('.rr-panel');
  if(panel0) panel0.classList.remove('rr-panel-warn-l2', 'rr-panel-warn-l3', 'rr-panel-warn-l4');
  setHeaderUrl((source && source.url) || location.href);
  setBody('<div class="rr-loading">正在读取页面内容…</div>');
  // 首次出现即弹开面板并进入检测态：读取正文可能耗时数秒，让完整形态与扫描动效
  // 从一开始就可见，而不是先干等一个悬浮球。
  rrSetScanning(true);
  if(firstShow) autoRevealPanel();
  // 内容脚本是唯一能读 DOM 的地方：等正文就绪后实时提取，并覆盖背景页传来的瘦 source
  //（action 点击只有 tab.url/title、summary 为空）。
  const ctx = await readPageContextReady();
  reviewSource = { ...(source || {}), ...ctx };
  console.log('[心盾调试] 提取source=', JSON.stringify({
    title: reviewSource.title, summaryLen: (reviewSource.summary||'').length,
    summaryHead: (reviewSource.summary||'').slice(0,40),
    author: reviewSource.author, platform: reviewSource.platform,
    comments: (reviewSource.comments||[]).length
  }));
  setHeaderUrl(reviewSource.url || location.href);
  // 流式分析：搭骨架（含内容预览 + 步骤条），后续由 RISK_REVIEW_STREAM 事件增量填充；
  // 后端不支持/出错时 onStreamEvent 会回退到一次性分析。
  startStreamAnalysis();
  try{
    await chrome.runtime.sendMessage({
      type:'RISK_REVIEW_ANALYZE_STREAM',
      payload:{ endpoint:'http://localhost:8000/analyze/stream', age_group:'13-15', source:reviewSource }
    });
  }catch(err){
    console.log('[心盾调试] 流式启动异常=', String(err?.message || err));
    _rrStream = null;
    if(rrIsStaleErr(err)){ rrShowStale(); return; }
    fallbackOneShot();
  }
}

// ══════════════════════════════════════════════
//  流式渲染：边收后端事件边把各区块填进面板
// ══════════════════════════════════════════════
let _rrStream = null;   // {d:累积结果, sixInit:bool}

const RR_SHIFT_REASON = {
  'multi_category_stack': '命中 ≥2 类虚无话术叠加',
  'title_only_with_authoritative_body': '仅标题命中、正文史料规范',
};

function rrRiskCardHTML(d, pending){
  if(pending){
    // 六维研判完成前的占位：不显示会变的初判分，避免误导
    return `<section class="rr-card rr-risk-card rr-risk-pending">
      <div class="rr-risk-top">
        <div class="rr-risk-label-wrap">
          <span class="rr-risk-icon">⏳</span>
          <span class="rr-risk-word rr-risk-pending-word">六维研判中…</span>
        </div>
        <span class="rr-risk-score rr-risk-pending-score">···</span>
      </div>
      <div class="rr-bar"><div class="rr-gradient"></div>
        <div class="rr-dot rr-dot-pending" style="left:6%"></div></div>
      <div class="rr-labels"><span>正常</span><span>关注</span><span>误导</span><span>高危</span><span>重大</span></div>
    </section>`;
  }
  rrApplyMajorRiskDisplay(d);
  const level = normalizeLevel(d.risk_level || d.risk_code || 'L0');
  const lc = level.toLowerCase();
  const shift = Number(d.score_shift || 0);
  const base1 = d.base_score != null ? Number(d.base_score) : Number(d.composite_score || 0);
  const rawSum = base1 + shift;
  const scoreNum = d.risk_percent != null
    ? (Math.max(0, Math.min(100, Number(d.risk_percent))) / 20).toFixed(1)
    : Math.max(0, Math.min(5, rawSum)).toFixed(1);
  const capNote = rawSum > 5 ? '（升档封顶 5）' : (rawSum < 0 ? '（降档封底 0）' : '');
  const escs = (d.structured_result && d.structured_result.nihilism_detail
    && d.structured_result.nihilism_detail.escalations) || [];
  const shiftReason = escs.map(e => RR_SHIFT_REASON[e && e.id]).filter(Boolean).join('；');
  const breakdown = shift !== 0
    ? `<div class="rr-risk-breakdown">六维 ${base1.toFixed(1)} ${shift > 0 ? '＋ 升' : '－ 降'}${Math.abs(shift)}档 ${Math.abs(shift).toFixed(1)} ＝ ${scoreNum}${capNote}`
      + (shiftReason ? `<div class="rr-risk-why">${shift > 0 ? '升档' : '降档'}原因：${escapeHtml(shiftReason)}</div>` : '')
      + `</div>`
    : '';
  return `<section class="rr-card rr-risk-card rr-risk-${lc}">
      <div class="rr-risk-top">
        <div class="rr-risk-label-wrap">
          <span class="rr-risk-icon">${levelIcon(level)}</span>
          <span class="rr-risk-word" style="color:${levelColor(level)}">${escapeHtml(d.risk_label || levelLabel(level))}</span>
        </div>
        <span class="rr-risk-score" style="color:${levelColor(level)}">${scoreNum}<span class="rr-risk-of"> / 5</span></span>
      </div>
      ${breakdown}
      <div class="rr-bar"><div class="rr-gradient"></div>
        <div class="rr-dot" style="left:6%;border-color:${levelColor(level)}" data-target-left="${levelPos(level)}"></div></div>
      <div class="rr-labels"><span>正常</span><span>关注</span><span>误导</span><span>高危</span><span>重大</span></div>
    </section>`;
}

function rrDomainTagsHTML(d){
  const dt = d.domain_tags || [];
  if(!dt.length) return '';
  return `<div class="rr-section">潜在意识形态风险类型</div>
    <div class="rr-domain-tags">${dt.map(x =>
      `<div class="rr-dtag"><span class="rr-dtag-name">${escapeHtml(x.domain_name || '')}</span>`
      + ((x.keywords && x.keywords.length) ? `<span class="rr-dtag-kw">${x.keywords.slice(0, 6).map(k => escapeHtml(k)).join(' · ')}</span>` : '')
      + `</div>`).join('')}</div>`;
}

function rrTagsCardHTML(d){
  const tags = (d.matched_tags || []).slice(0,5);
  const level = normalizeLevel(d.risk_level || d.risk_code || 'L0');
  const narrative = d?.narrative?.summary || d.risk_explanation || buildNarrative(d, levelLabel(level));
  const domainHtml = rrDomainTagsHTML(d);
  return `<section class="rr-card">
      ${domainHtml}
      <div class="rr-section"${domainHtml ? ' style="margin-top:14px"' : ''}>识别标签</div>
      <div class="rr-tags">${tags.length
        ? tags.map(t => `<span class="rr-tag">${escapeHtml(t.keyword || t.l3_name || t.l2_name || '风险标签')}</span>`).join('')
          + (tags[0]?.domain ? `<span class="rr-tag domain">${escapeHtml(tags[0].domain.replace('风险',''))}</span>` : '')
        : '<span class="rr-tag rr-tag-empty">未命中明确标签</span>'}</div>
      <div class="rr-section" style="margin-top:14px">叙事分析</div>
      <div class="rr-narrative">${escapeHtml(narrative)}</div>
    </section>`;
}

// 重大风险规则统一移到顶部提示卡；左栏不再重复展示。
function rrMajorAlertHTML(sr){
  return '';
}

// 关联案例（弱化）：风险案例在上、相关案例在下，整体作为次要信息置于左栏底部
function rrCasesHTML(d){
  const positiveCases = (d.positive_cases || []).slice(0,4);
  const cases = (d.related_cases || d.cases || []).slice(0,4);
  const LV = {L4:4,L3:3,L2:2,L1:1,L0:0};
  const alertCase = cases.find(c => (LV[normalizeLevel(c.risk_level || 'L0')] || 0) >= 3);
  return `<div class="rr-related-block">
    <div class="rr-related-title">关联案例参考</div>
    <div class="rr-cases-head"><span class="rr-section" style="margin:0">风险案例</span>
      ${alertCase ? `<span class="rr-cases-count">1</span>` : ''}</div>
    <div id="rr-cases">${alertCase ? renderCase(alertCase)
      : '<section class="rr-case"><div class="rr-case-name">暂无预警风险案例</div><div class="rr-case-text">当前内容未命中需要预警的风险案例。</div></section>'}</div>
    ${positiveCases.length ? `
    <div class="rr-positive-head"><span class="rr-section" style="margin:0">相关案例</span>
      <span class="rr-positive-count">${positiveCases.length}</span></div>
    <div id="rr-positive-cases">${positiveCases.map(rrPositiveClickable).join('')}</div>` : ''}
  </div>
  ${rrGuidanceHTML(d)}`;
}

function rrCaseDisposals(c){
  if(!c) return {};
  return {
    '6-12岁':  c.disposal_6_12  || c['disposal_6_12']  || '',
    '13-15岁': c.disposal_13_15 || c['disposal_13_15'] || '',
    '16-18岁': c.disposal_16_18 || c['disposal_16_18'] || '',
  };
}

function rrPickGuidanceCase(d){
  const cases = (d.related_cases || d.cases || []).slice(0,4);
  const LV = {L4:4,L3:3,L2:2,L1:1,L0:0};
  const sorted = cases.slice().sort((a, b) => (LV[normalizeLevel(b.risk_level || 'L0')] || 0) - (LV[normalizeLevel(a.risk_level || 'L0')] || 0));
  return sorted.find(c => Object.values(rrCaseDisposals(c)).some(Boolean)) || null;
}

// 主研判区·青少年分级处置卡：按当前风险分析结果，AI 分龄生成处置策略 + 引导话术
let _rrGuideState = null;

function rrGuidanceHTML(d){
  const sr = (d && d.structured_result) || {};
  const label = d.risk_label || sr.risk_level || '';
  const types = (sr.risk_types || []).filter(Boolean);
  const domains = (sr.risk_domains || []).filter(Boolean);
  const points = ((sr.six_dim_detail || {}).evidence || []).map(e => e && e.note).filter(Boolean);
  _rrGuideState = { label, types, domains, points, generated: {}, curAge: '6-12', loaded: false };
  const AGES = [['6-12', '6-12岁'], ['13-15', '13-15岁'], ['16-18', '16-18岁']];
  const tabsHtml = AGES.map(([k, lbl], i) =>
    `<button class="rr-cd-tab${i === 0 ? ' active' : ''}" data-action="switch-guide-age" data-age="${k}">${lbl}</button>`
  ).join('');
  return `<section class="rr-guide-card">
    <div class="rr-guide-head">
      <div>
        <div class="rr-guide-title">青少年分级处置</div>
        <div class="rr-guide-meta">按当前内容风险分析，由 AI 分龄生成处置策略与引导话术</div>
      </div>
    </div>
    <div class="rr-cd-tabs">${tabsHtml}</div>
    <div class="rr-guide-block">
      <div class="rr-guide-block-lbl">处置策略</div>
      <div class="rr-guide-block-val" id="rr-guide-disposal">AI 生成中…</div>
    </div>
    <div class="rr-guide-block">
      <div class="rr-guide-block-lbl">引导话术</div>
      <div class="rr-guide-block-val" id="rr-guide-script">AI 生成中…</div>
    </div>
  </section>`;
}

// 刷新处置策略/引导话术两块为当前年龄段
function rrRefreshGuideBlocks(){
  if(!_rrGuideState) return;
  const g = _rrGuideState.generated[_rrGuideState.curAge];
  const done = _rrGuideState.done && _rrGuideState.done.has(_rrGuideState.curAge);
  const disEl = reviewRoot?.querySelector('#rr-guide-disposal');
  const scEl  = reviewRoot?.querySelector('#rr-guide-script');
  if(disEl) disEl.textContent = (g && g.strategy) || (done ? '本次未生成处置策略。' : 'AI 生成中…');
  if(scEl)  scEl.textContent  = (g && g.script)   || (done ? '本次未生成引导话术。' : 'AI 生成中…');
}

// 年龄段 tab 切换（处置策略 + 引导话术同步切换）
function rrSwitchGuideAge(btn, age){
  if(!_rrGuideState || !age) return;
  _rrGuideState.curAge = age;
  (btn.parentElement?.querySelectorAll('.rr-cd-tab') || []).forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  rrRefreshGuideBlocks();
  // 若该年龄段还没生成、也不在生成中，即时按需拉一次
  const inflight = _rrGuideState.inflight && _rrGuideState.inflight.has(age);
  if(_rrGuideState.done && !_rrGuideState.done.has(age) && !inflight) _rrFetchGuidanceAges([age]);
}

// 拉取指定年龄段的处置策略+引导话术，合并进状态并刷新
async function _rrFetchGuidanceAges(ages){
  if(!_rrGuideState || !ages || !ages.length) return;
  ages.forEach(a => (_rrGuideState.inflight ||= new Set()).add(a));
  try{
    const res = await fetch('http://localhost:8000/guidance/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        risk_label: _rrGuideState.label, risk_types: _rrGuideState.types,
        domains: _rrGuideState.domains, key_points: _rrGuideState.points, ages,
      }),
      signal: AbortSignal.timeout(50000),
    });
    if(res.ok) Object.assign(_rrGuideState.generated, (await res.json()).guidance || {});
  }catch(e){ /* 生成失败静默，走空态 */ }
  (_rrGuideState.done ||= new Set()); ages.forEach(a => _rrGuideState.done.add(a));
  rrRefreshGuideBlocks();
}

// 注入 DOM 后触发：先只生成「当前年龄段」（秒出），其余两段后台补生成（懒加载提速）
async function rrInitGuidance(){
  if(!_rrGuideState) return;
  _rrGuideState.done = new Set();
  const cur = _rrGuideState.curAge || '6-12';
  const rest = ['6-12','13-15','16-18'].filter(a => a !== cur);
  await _rrFetchGuidanceAges([cur]);   // 当前年龄段优先，尽快显示
  _rrFetchGuidanceAges(rest);          // 其余后台补，不阻塞
}

// 相关案例（正向）：可点击卡片，点击后在插件内跳转到详情页（不开新标签）
const _rrPositiveMap = new Map();
let _rrDetailReturnHTML = null;
let _rrDetailReturnScroll = 0;

function rrPositiveClickable(p){
  const key = p.case_id || ('P' + _rrPositiveMap.size);
  _rrPositiveMap.set(key, p);
  return `<section class="rr-positive-case rr-positive-clickable" data-action="open-positive" data-key="${escapeHtml(key)}">
      <div class="rr-positive-top">
        <span class="rr-positive-badge">${escapeHtml(p.source || '中青网')}</span>
        <span class="rr-positive-id">${escapeHtml(p.case_id || '')}</span>
      </div>
      <div class="rr-positive-name">${escapeHtml(p.case_name || '正向案例')}</div>
      <div class="rr-positive-reason">${escapeHtml(p.relation_reason || '')}</div>
      <span class="rr-positive-link">查看详情 →</span>
    </section>`;
}

function rrPositiveDetailHTML(p){
  const kw = String(p.keywords || '').split(/[,，、;；]/).map(s => s.trim()).filter(Boolean);
  const meta = [p.domain, p.risk_type].filter(Boolean).join(' · ');
  return `
    <div class="rr-detail-head">
      <button class="rr-back-btn" data-action="back-to-result">← 返回</button>
      <span class="rr-detail-title">相关案例详情</span>
    </div>
    <section class="rr-card">
      <div class="rr-positive-top">
        <span class="rr-positive-badge">${escapeHtml(p.source || '中青网')}</span>
        <span class="rr-positive-id">${escapeHtml(p.case_id || '')}</span>
      </div>
      <div class="rr-name">${escapeHtml(p.case_name || '正向案例')}</div>
      ${meta ? `<div class="rr-detail-meta">${escapeHtml(meta)}</div>` : ''}
      ${kw.length ? `<div class="rr-cd-lbl" style="margin-top:12px">核心关键词</div>
        <div class="rr-kw-tags">${kw.map(k => `<span class="rr-kw-tag">${escapeHtml(k)}</span>`).join('')}</div>` : ''}
      <div class="rr-cd-lbl" style="margin-top:12px">案例内容</div>
      <div class="rr-narrative">${escapeHtml(p.representative_text || p.relation_reason || '暂无更多内容')}</div>
      ${p.source_url ? `<div class="rr-cd-lbl" style="margin-top:12px">原文链接</div>
        <div class="rr-detail-url">${escapeHtml(p.source_url)}</div>` : ''}
    </section>`;
}

function rrOpenPositiveDetail(key){
  const p = _rrPositiveMap.get(key);
  const body = reviewRoot?.querySelector('[data-role="body"]');
  if(!p || !body) return;
  _rrDetailReturnHTML = body.innerHTML;   // 记住当前分析视图，返回时还原
  _rrDetailReturnScroll = body.scrollTop;  // 记住滚动位置，返回时回到点击处
  body.innerHTML = `<div class="rr-detail-view">${rrPositiveDetailHTML(p)}</div>`;
  body.scrollTop = 0;
  if(!prefersReducedMotion()){
    anime({ targets: body.children, opacity: [0, 1], translateY: [10, 0], delay: anime.stagger(60), duration: 240, easing: 'easeOutExpo' });
  }
}

function rrBackToResult(){
  const body = reviewRoot?.querySelector('[data-role="body"]');
  if(body && _rrDetailReturnHTML != null){
    body.innerHTML = _rrDetailReturnHTML;
    _rrDetailReturnHTML = null;
    body.scrollTop = _rrDetailReturnScroll;   // 还原到点击"相关案例"时的位置
  }
}



function rrFadeIn(el){
  if(el && !prefersReducedMotion()){
    anime({ targets: el, opacity: [0, 1], translateY: [10, 0], duration: 260, easing: 'easeOutExpo' });
  }
  rrAutoScroll();   // 每有新内容出现就自动跟随往下滚
}

// 流式进行时自动滚到底部跟随新内容；若用户已手动往上翻看上文（距底 >120px）则不打扰。
function rrAutoScroll(){
  if(!_rrStream) return;
  const body = reviewRoot?.querySelector('[data-role="body"]');
  if(!body) return;
  if(body.scrollHeight - body.scrollTop - body.clientHeight > 120) return;
  requestAnimationFrame(() => {
    body.scrollTo({ top: body.scrollHeight, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  });
}

function rrAnimateDot(){
  const dot = reviewRoot?.querySelector('#rr-sec-risk .rr-dot[data-target-left]');
  if(dot && !prefersReducedMotion()){
    anime({ targets: dot, left: [dot.style.left, dot.dataset.targetLeft], duration: 600, easing: 'easeOutExpo', delay: 120 });
  }
}

// 步骤状态：i=0读取 1匹配 2六维 3案例；state 'active'|'done'
function rrStepSet(i, state){
  const li = reviewRoot?.querySelectorAll('#rr-steps-wrap .rr-step')[i];
  if(!li) return;
  li.classList.remove('rr-step-active', 'rr-step-done');
  li.classList.add(state === 'done' ? 'rr-step-done' : 'rr-step-active');
}

function startStreamAnalysis(){
  rrSetScanning(true);   // 进入检测态：悬浮球脉冲 + 面板扫描光
  const stepsHtml = RR_STEPS.map((s, i) =>
    `<li class="rr-step"><span class="rr-step-dot"></span><span class="rr-step-label">${s}</span></li>`).join('');
  setBody(`
    <div id="rr-alert"></div>
    <div class="rr-cols">
      <div class="rr-col rr-col-a">
        <div class="rr-col-title">研判结论</div>
        <div id="rr-sec-preview"></div>
        <div id="rr-sec-risk"></div>
        <div id="rr-sec-major"></div>
        <div id="rr-sec-tags"></div>
        <div id="rr-sec-cases"></div>
      </div>
      <div class="rr-col rr-col-b">
        <div class="rr-col-title">六维分析</div>
        <div id="rr-steps-wrap"><ul class="rr-steps">${stepsHtml}</ul></div>
        <div id="rr-sec-six"></div>
      </div>
    </div>`);
  _rrStream = { d: Object.assign({}, reviewSource), sixInit: false };
  // 内容预览：本地 source 立即渲染
  const prev = reviewRoot.querySelector('#rr-sec-preview');
  if(prev){ prev.innerHTML = rrPreviewCardHTML(_rrStream.d, reviewSource); }
  // 六维区先显示"研判中"进行态，让分析过程可见（结果到达 sixdim 事件后替换）
  const six0 = reviewRoot.querySelector('#rr-sec-six');
  if(six0){ six0.innerHTML = rrSixPendingHTML(); }
  rrStepSet(0, 'done');   // 读取内容（source 已就绪）
  rrStepSet(1, 'active'); // 匹配标签中
}

// 六维"研判中"进行态：六个维度名逐一脉冲，营造分析过程感
function rrSixPendingHTML(){
  const names = ['事实处理方式', '叙事框架', '情绪动员', '价值导向', '表达包装', '青少年影响'];
  const rows = names.map((n, i) =>
    `<div class="rr-sixp-row" style="animation-delay:${(i * 0.18).toFixed(2)}s"><span class="rr-sixp-dot"></span>${n}</div>`).join('');
  return `<section class="rr-card rr-six-pending">
      <div class="rr-section">六维研判 · 进行中</div>
      <div class="rr-sixp-hint">正在按六维 0/1 命中项逐维研判…</div>
      <div class="rr-sixp-list">${rows}</div>
    </section>`;
}

function onStreamEvent(ev){
  if(!ev || !_rrStream) return;
  const d = _rrStream.d;
  if(ev.event === 'error'){
    // 流式失败 → 回退一次性分析
    _rrStream = null;
    fallbackOneShot();
    return;
  }
  if(ev.event === 'tags'){
    Object.assign(d, ev);
    reviewResult = d;
    const risk = reviewRoot.querySelector('#rr-sec-risk');
    const tagsEl = reviewRoot.querySelector('#rr-sec-tags');
    // 六维对所有内容都跑，出终判前风险卡一律占位"六维研判中"，避免先闪一个关键词初判分（如 1.9）
    const pending = true;
    if(risk){ risk.innerHTML = rrRiskCardHTML(d, pending); rrFadeIn(risk); }
    if(tagsEl){ tagsEl.innerHTML = rrTagsCardHTML(d); rrFadeIn(tagsEl); }
    rrStepSet(1, 'done'); rrStepSet(2, 'active');
    return;
  }
  if(ev.event === 'sentence'){
    const six = reviewRoot.querySelector('#rr-sec-six');
    if(six && !_rrStream.sixInit){
      six.innerHTML = `<section class="rr-card"><div class="rr-section">六维研判（逐句进行中…）</div><div id="rr-live-sents"></div></section>`;
      _rrStream.sixInit = true;
    }
    const list = reviewRoot.querySelector('#rr-live-sents');
    if(list){
      const s = ev.sentence || {};
      const lv = normalizeLevel(s.risk_code || 'L0');
      const row = document.createElement('div');
      row.className = 'rr-live-sent';
      row.innerHTML = `<span class="rr-sent-lv" style="background:${levelColor(lv)}">${escapeHtml(levelLabel(lv))}</span>`
        + `<span class="rr-live-sent-text">${escapeHtml((s.text || '').slice(0,42))}</span>`;
      list.appendChild(row);
      rrFadeIn(row);
    }
    return;
  }
  if(ev.event === 'sixdim'){
    if(ev.structured_result) d.structured_result = ev.structured_result;
    if(ev.risk_level) d.risk_level = ev.risk_level;
    if(ev.risk_label) d.risk_label = ev.risk_label;
    if(ev.composite_score != null) d.composite_score = ev.composite_score;
    if('base_score' in ev) d.base_score = ev.base_score;
    if('score_shift' in ev) d.score_shift = ev.score_shift;
    if(ev.risk_percent != null) d.risk_percent = ev.risk_percent;
    if(ev.warning_light) d.warning_light = ev.warning_light;
    if(ev.risk_level_num != null) d.risk_level_num = ev.risk_level_num;
    rrApplyMajorRiskDisplay(d);
    reviewResult = d;
    // 六维等级/升降档可能变，重渲染风险卡
    const risk = reviewRoot.querySelector('#rr-sec-risk');
    if(risk){ risk.innerHTML = rrRiskCardHTML(d); rrAnimateDot(); }
    // 重大风险规则已统一展示在顶部提示卡
    const major = reviewRoot.querySelector('#rr-sec-major');
    if(major){ major.innerHTML = rrMajorAlertHTML(d.structured_result); if(major.innerHTML) rrFadeIn(major); }
    rrSetFabLevel(d.risk_level);
    // 顶部区域：高风险以上显示预警；中低风险仅显示政策与讲话参考
    const ideoEl = reviewRoot.querySelector('#rr-alert');
    if(ideoEl){
      if(rrIsMajorRisk(d)){
        if(showIdeologyAlert(ideoEl, d)) _rrStream.alerted = true;
      }else if(rrIsHighRisk(d)){
        if(showHighRiskAlert(ideoEl, d)) _rrStream.alerted = true;
      }else{
        showPolicyReference(ideoEl, d);
      }
    }
    // 用最终聚合的六维块替换逐句 live 列表；无六维数据时给出明确空态，避免直接清成空白
    const six = reviewRoot.querySelector('#rr-sec-six');
    if(six){
      six.innerHTML = buildNihilismHtml(d.structured_result)
        || '<section class="rr-card rr-empty-col">本页内容未触发六维研判分析。</section>';
      rrFadeIn(six);
    }
    rrStepSet(2, 'done'); rrStepSet(3, 'active');
    return;
  }
  if(ev.event === 'cases'){
    if(ev.related_cases) d.related_cases = ev.related_cases;
    if(ev.positive_cases) d.positive_cases = ev.positive_cases;
    reviewResult = d;
    console.log('[心盾调试] cases事件 positive=', (ev.positive_cases||[]).length, ' related=', (ev.related_cases||[]).length);
    const casesEl = reviewRoot.querySelector('#rr-sec-cases');
    if(casesEl){
      try{ casesEl.innerHTML = rrCasesHTML(d); }
      catch(e){ console.log('[心盾调试] rrCasesHTML 出错=', String(e && e.message || e)); }
      rrFadeIn(casesEl);
      rrInitGuidance();
    }
    rrStepSet(3, 'done');
    return;
  }
  if(ev.event === 'done'){
    rrSetScanning(false);   // 检测完成，退出扫描态
    // 兜底：若全程未收到 sixdim 事件（后端未产出六维），六维区会卡在"研判中"，
    // 结束时据实收敛为空态提示，避免一直转圈。
    const six = reviewRoot.querySelector('#rr-sec-six');
    if(six && !(d.structured_result && (d.structured_result.six_dim_detail || d.structured_result.nihilism_detail))){
      six.innerHTML = '<section class="rr-card rr-empty-col">本页内容未触发六维研判分析。</section>';
    }
    const steps = reviewRoot.querySelector('#rr-steps-wrap');
    if(steps){
      if(prefersReducedMotion()) steps.remove();
      else anime({ targets: steps, opacity: [1, 0], height: 0, duration: 300, easing: 'easeInQuad', complete: () => steps.remove() });
    }
    _rrStream = null;
  }
}

// 流式失败/后端不支持时的一次性兜底
async function fallbackOneShot(){
  setBody('<div class="rr-loading">正在分析当前页面...</div>');
  try{
    const res = await chrome.runtime.sendMessage({
      type:'RISK_REVIEW_ANALYZE',
      payload:{ endpoint:'http://localhost:8000/analyze', useDemoOnly:false, no_cache:true, age_group:'13-15', source:reviewSource }
    });
    if(!res?.ok) throw new Error(res?.error || '分析失败');
    renderReviewPanel(res.result, reviewSource);
  }catch(err){
    rrSetScanning(false);
    if(rrIsStaleErr(err)){ rrShowStale(); return; }
    setBody(`<div class="rr-error">${escapeHtml(String(err?.message || err || '分析失败'))}</div>`);
  }
}

function rrPreviewCardHTML(d, source){
  const url = d.url || (source && source.url) || location.href;
  const title = d.title || (source && source.title) || '当前页面内容';
  const summary = d.content_summary || d.summary || (source && source.summary) || '正在读取正文…';
  const platform = d.platform || (source && source.platform) || detectPlatform(url);
  const interaction = formatInteraction(d.interaction || (source && source.interaction));
  return `<section class="rr-card rr-preview-card">
      <div class="rr-preview-top">
        <span class="rr-plat-badge">${escapeHtml(platform)}</span>
        ${interaction ? `<span class="rr-interact">${escapeHtml(interaction)}</span>` : ''}
      </div>
      <div class="rr-name">${escapeHtml(title)}</div>
      <div class="rr-summary">${escapeHtml(summary)}</div>
    </section>`;
}

async function renderReviewPanel(result, source){
  stopAnalyzingSteps();   // 结果到了，停掉分析中步骤条
  rrSetScanning(false);   // 退出检测态
  reviewResult = result || {};
  reviewSource = source || readPageContext();
  await ensureRoot();
  setHeaderUrl(reviewResult.url || reviewSource.url || location.href);

  const d = reviewResult;
  rrApplyMajorRiskDisplay(d);
  const url = d.url || reviewSource.url || location.href;
  const title = d.title || reviewSource.title || '当前页面内容';
  const summary = d.content_summary || d.summary || reviewSource.summary || '暂未读取到正文内容。';
  const platform = d.platform || detectPlatform(url);
  const level = normalizeLevel(d.risk_level || d.risk_code || 'L0');
  const tags = (d.matched_tags || []).slice(0,5);
  const cases = (d.related_cases || d.cases || []).slice(0,4);
  const positiveCases = (d.positive_cases || []).slice(0,4);
  const LV_ORDER = {L4:4,L3:3,L2:2,L1:1,L0:0};
  const alertCase = cases.find(c => (LV_ORDER[normalizeLevel(c.risk_level || 'L0')] || 0) >= 3);
  const narrative = d?.narrative?.summary || d.risk_explanation || buildNarrative(d, levelLabel(level));

  const lc = level.toLowerCase();
  // 显示分体现升降档：基础分(六维/6) + 升降档(1档=1.0分)，封顶5封底0。
  // 前端由 base+shift 重算，保证下方公式自洽；触发封顶/封底时追加说明，避免"加不齐"的误会。
  const shift = Number(d.score_shift || 0);
  const base1 = d.base_score != null ? Number(d.base_score) : Number(d.composite_score || 0);
  const rawSum = base1 + shift;
  const finalNum = Math.max(0, Math.min(5, rawSum));
  const scoreNum = d.risk_percent != null
    ? (Math.max(0, Math.min(100, Number(d.risk_percent))) / 20).toFixed(1)
    : finalNum.toFixed(1);
  const capNote = rawSum > 5 ? '（升档封顶 5）' : (rawSum < 0 ? '（降档封底 0）' : '');
  // 升降档原因：后端已把命中的升降档规则放在 nihilism_detail.escalations 里，取其中的档位规则解释
  const RR_SHIFT_REASON = {
    'multi_category_stack': '命中 ≥2 类虚无话术叠加',
    'title_only_with_authoritative_body': '仅标题命中、正文史料规范',
  };
  const rrEscs = (d.structured_result && d.structured_result.nihilism_detail
    && d.structured_result.nihilism_detail.escalations) || [];
  const shiftReason = rrEscs.map(e => RR_SHIFT_REASON[e && e.id]).filter(Boolean).join('；');
  const scoreBreakdown = shift !== 0
    ? `<div class="rr-risk-breakdown">六维 ${base1.toFixed(1)} ${shift > 0 ? '＋ 升' : '－ 降'}${Math.abs(shift)}档 ${Math.abs(shift).toFixed(1)} ＝ ${scoreNum}${capNote}`
      + (shiftReason ? `<div class="rr-risk-why">${shift > 0 ? '升档' : '降档'}原因：${escapeHtml(shiftReason)}</div>` : '')
      + `</div>`
    : '';
  const interaction = formatInteraction(d.interaction);

  setBody(`
    <div id="rr-alert"></div>

    <div class="rr-cols">
    <div class="rr-col rr-col-a">
    <div class="rr-col-title">研判结论</div>

    <!-- 内容预览卡 -->
    <section class="rr-card rr-preview-card">
      <div class="rr-preview-top">
        <span class="rr-plat-badge">${escapeHtml(platform)}</span>
        ${interaction ? `<span class="rr-interact">${escapeHtml(interaction)}</span>` : ''}
      </div>
      <div class="rr-name">${escapeHtml(title)}</div>
      <div class="rr-summary">${escapeHtml(summary)}</div>
    </section>

    <!-- 风险评级卡（独立、带色背景） -->
    <section class="rr-card rr-risk-card rr-risk-${lc}">
      <div class="rr-risk-top">
        <div class="rr-risk-label-wrap">
          <span class="rr-risk-icon">${levelIcon(level)}</span>
          <span class="rr-risk-word" style="color:${levelColor(level)}">${escapeHtml(d.risk_label || levelLabel(level))}</span>
        </div>
        <span class="rr-risk-score" style="color:${levelColor(level)}">${scoreNum}<span class="rr-risk-of"> / 5</span></span>
      </div>
      ${scoreBreakdown}
      <div class="rr-bar">
        <div class="rr-gradient"></div>
        <div class="rr-dot" style="left:6%;border-color:${levelColor(level)}" data-target-left="${levelPos(level)}"></div>
      </div>
      <div class="rr-labels"><span>正常</span><span>关注</span><span>误导</span><span>高危</span><span>重大</span></div>
    </section>

    <!-- 重大风险规则已统一展示在顶部提示卡 -->
    ${rrMajorAlertHTML(d.structured_result)}

    <!-- 识别标签 + 叙事分析卡 -->
    <section class="rr-card">
      ${rrDomainTagsHTML(d)}
      <div class="rr-section"${(d.domain_tags && d.domain_tags.length) ? ' style="margin-top:14px"' : ''}>识别标签</div>
      <div class="rr-tags">
        ${tags.length
          ? tags.map(tag => `<span class="rr-tag">${escapeHtml(tag.keyword || tag.l3_name || tag.l2_name || '风险标签')}</span>`).join('')
            + (tags[0]?.domain ? `<span class="rr-tag domain">${escapeHtml(tags[0].domain.replace('风险',''))}</span>` : '')
          : '<span class="rr-tag rr-tag-empty">未命中明确标签</span>'}
      </div>
      <div class="rr-section" style="margin-top:14px">叙事分析</div>
      <div class="rr-narrative">${escapeHtml(narrative)}</div>
    </section>

    <!-- 关联案例（弱化，置于左栏底部）：风险案例在上、相关案例在下 -->
    <div class="rr-related-block">
      <div class="rr-related-title">关联案例参考</div>
      <div class="rr-cases-head">
        <span class="rr-section" style="margin:0">风险案例</span>
        ${alertCase ? `<span class="rr-cases-count">1</span>` : ''}
      </div>
      <div id="rr-cases">${alertCase ? '' : '<section class="rr-case"><div class="rr-case-name">暂无预警风险案例</div><div class="rr-case-text">当前内容未命中需要预警的风险案例。</div></section>'}</div>

      ${positiveCases.length ? `
      <div class="rr-positive-head">
        <span class="rr-section" style="margin:0">相关案例</span>
        <span class="rr-positive-count">${positiveCases.length}</span>
      </div>
      <div id="rr-positive-cases">${positiveCases.map(renderPositiveCase).join('')}</div>
      ` : ''}
    </div>
    ${rrGuidanceHTML(d)}
    </div>

    <div class="rr-col rr-col-b">
    <div class="rr-col-title">六维分析</div>
    ${buildNihilismHtml(d.structured_result) || '<section class="rr-card rr-empty-col">本页内容未触发六维研判分析。</section>'}
    </div>
    </div>
  `);

  rrSetFabLevel(level);
  rrInitGuidance();

  const dot = reviewRoot?.querySelector('.rr-dot[data-target-left]');
  if(dot){
    const target = dot.dataset.targetLeft;
    anime({ targets: dot, left: [dot.style.left, target], duration: 600, easing: 'easeOutExpo', delay: 120 });
  }

  // 卡片逐条淡入（一条条出来，中间有间隔）——尊重 prefers-reduced-motion
  if(!prefersReducedMotion()){
    const blocks = reviewRoot?.querySelectorAll(
      '[data-role="body"] .rr-col > *:not(.rr-col-title)'
    );
    if(blocks && blocks.length){
      anime({ targets: blocks, opacity: [0, 1], translateY: [10, 0],
        delay: anime.stagger(90), duration: 260, easing: 'easeOutExpo' });
    }
  }

  // 顶部区域：真命中重大意识形态风险 → 重大风险提示；普通高风险 → 高风险预警；中低风险 → 政策参考
  const alertEl  = reviewRoot?.querySelector('#rr-alert');
  if(rrIsMajorRisk(d)) showIdeologyAlert(alertEl, d);
  else if(rrIsHighRisk(d)) showHighRiskAlert(alertEl, d);
  else showPolicyReference(alertEl, d);

  // 风险案例仍渲染到左栏关联案例区（那条触发案例）
  if(alertCase){
    const casesEl  = reviewRoot?.querySelector('#rr-cases');
    if(casesEl){
      const wrap = document.createElement('div');
      wrap.innerHTML = renderCase(alertCase);
      casesEl.appendChild(wrap.firstElementChild);
    }
  }
}

// 案例数据缓存（用于展开详情）
const _rrCaseMap = new Map();

// ── 六维雷达图（新模型：6 轴，值域 0-1，标签显示命中数 n/6）──
function rrRadar6(dims, color){
  const N = dims.length || 6, cx = 150, cy = 140, R = 76;
  const ang = i => (-90 + i * (360 / N)) * Math.PI / 180;
  const pt = (i, r) => [cx + r * Math.cos(ang(i)), cy + r * Math.sin(ang(i))];
  let grid = '';
  for(let g = 1; g <= 5; g++){
    const r = R * g / 5;
    grid += `<polygon points="${dims.map((_, i) => pt(i, r).map(v => v.toFixed(1)).join(',')).join(' ')}" fill="none" stroke="${g === 5 ? '#c2d0d8' : '#e6edf1'}" stroke-width="1"/>`;
  }
  let spokes = '', labels = '', dots = '';
  const dpoly = dims.map((d, i) => pt(i, R * Math.max(0.02, d.score || 0)).map(v => v.toFixed(1)).join(',')).join(' ');
  dims.forEach((d, i) => {
    const [ex, ey] = pt(i, R);
    spokes += `<line x1="${cx}" y1="${cy}" x2="${ex.toFixed(1)}" y2="${ey.toFixed(1)}" stroke="#e6edf1" stroke-width="1"/>`;
    const [lx, ly] = pt(i, R + 16);
    const c = Math.cos(ang(i)), s = Math.sin(ang(i));
    const anchor = Math.abs(c) < 0.3 ? 'middle' : (c > 0 ? 'start' : 'end');
    const dy = s < -0.3 ? -1 : (s > 0.3 ? 11 : 4);
    labels += `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" dy="${dy}" font-size="10.5" font-weight="700" fill="#48596a">${escapeHtml(d.name)}<tspan dx="3" font-size="10" font-weight="800" fill="${color}">${d.hit_count}/${d.total}</tspan></text>`;
    const [px, py] = pt(i, R * Math.max(0.02, d.score || 0));
    dots += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="2.6" fill="${color}"/>`;
  });
  return `<svg viewBox="0 0 300 286" width="100%" role="img" aria-label="六维雷达图">${grid}${spokes}<polygon points="${dpoly}" fill="${color}" fill-opacity="0.16" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>${dots}${labels}</svg>`;
}

// ── 六维分析（新规则）：六维研判(雷达+逐维) + 命中详情·逐句证据 ──
function buildSixDimHtml(sd, sr){
  if(!sd || !Array.isArray(sd.dimensions)) return '';
  const basePct = sd.risk_percent != null ? Number(sd.risk_percent) : 0;
  const major = !!(sr && sr.major_ideological_risk);
  const finalPctRaw = sr && sr.risk_percent != null ? Number(sr.risk_percent) : basePct;
  const finalPct = major ? Math.max(81, finalPctRaw) : finalPctRaw;
  const lvl = major ? 'L4' : normalizeLevel(sr && sr.risk_code);
  const color = levelColor(lvl);
  const finalNum = major ? 5 : levelNum(lvl);
  const dims = sd.dimensions;
  const score5 = Math.max(0, Math.min(100, finalPct)) / 20;
  const explainBits = [];
  if(Math.abs(finalPct - basePct) >= 0.1){
    explainBits.push(`六维基础加权 ${basePct.toFixed(1)}/100`);
  }
  if(sd.adjustments && sd.adjustments.length){
    explainBits.push(`定级修正：${sd.adjustments.map(a => escapeHtml(a)).join('、')}`);
  }
  const finalExplain = explainBits.length
    ? `<div class="rr-dim-sub">${explainBits.join('　·　')}</div>`
    : '';

  // 逐维：迷你条 + 命中项完整明细（命中项显示说明，未命中项紧凑编号）
  const dimRows = dims.map(d => {
    const pctW = Math.round((d.weight || 0) * 100);
    const barW = Math.round((d.score || 0) * 100);
    const items = d.items || [];
    const hitLines = items.filter(it => it.hit).map(it =>
      `<div class="rr-sd-hititem"><span class="rr-sd-hitid">${escapeHtml(it.id)}</span>`
      + `<span class="rr-sd-hittext">${escapeHtml(it.desc)}</span></div>`).join('');
    const missChips = items.filter(it => !it.hit).map(it =>
      `<span class="rr-sd-chip" title="${escapeHtml(it.id + '　' + it.desc)}">${escapeHtml(it.id)}</span>`).join('');
    return `<div class="rr-sd-dim">
        <div class="rr-sd-dim-head">
          <span class="rr-sd-dim-name">${escapeHtml(d.name)}</span>
          <span class="rr-sd-dim-score" style="color:${color}">${d.hit_count}/${d.total}<span class="rr-sd-w"> · 权重${pctW}%</span></span>
        </div>
        <div class="rr-sd-bar"><div class="rr-sd-bar-fill" style="width:${barW}%;background:${color}"></div></div>
        ${hitLines ? `<div class="rr-sd-hitlist">${hitLines}</div>` : '<div class="rr-sd-none">本维度无命中项</div>'}
        ${missChips ? `<div class="rr-sd-chips rr-sd-miss"><span class="rr-sd-miss-label">未命中</span>${missChips}</div>` : ''}
      </div>`;
  }).join('');

  // 命中详情 · 逐句证据
  const ev = (sd.evidence || []);
  const evHtml = ev.length ? ev.map(e => {
    const descs = (e.item_descs && e.item_descs.length) ? e.item_descs.map(x => x.desc).filter(Boolean) : [];
    const chips = descs.map(t => `<span class="rr-ev-tag">${escapeHtml(t)}</span>`).join('');
    return `<div class="rr-ev">
        <div class="rr-ev-span">“${escapeHtml(e.span)}”</div>
        <div class="rr-ev-meta">${chips}${e.note ? `<span class="rr-ev-note">${escapeHtml(e.note)}</span>` : ''}</div>
      </div>`;
  }).join('') : '<div class="rr-ev-empty">未提取到明显风险片段。</div>';

  return `
    <section class="rr-card">
      <div class="rr-section">六维风险研判</div>
      <div class="rr-dim-summary">综合风险 <b>${finalPct.toFixed(1)}</b>/100　·　<b>${score5.toFixed(1)}</b>/5　·　综合定级 <b style="color:${color}">${finalNum}级</b>${sd.summary ? `　·　${escapeHtml(sd.summary)}` : ''}${finalExplain}</div>
      ${(sd.domains && sd.domains.length) ? `<div class="rr-sd-domains">潜在意识形态风险类型：${sd.domains.map(x => `<span class="rr-sd-domain">${escapeHtml(x.name || x.id || x)}</span>`).join('')}</div>` : ''}
      <div class="rr-radar">${rrRadar6(dims, color)}</div>
      <div class="rr-sd-dims">${dimRows}</div>
    </section>
    <section class="rr-card">
      <div class="rr-section">命中详情 · 逐句证据</div>
      <div class="rr-ev-list">${evHtml}</div>
    </section>
    <button class="rr-export-btn" data-action="export-report">⬇ 导出评估报告（Word）</button>`;
}

// ── 六维分析 + 命中详情（新规则走 six_dim_detail；旧历史虚无结果走 nihilism_detail）──
function buildNihilismHtml(sr){
  if(sr && sr.six_dim_detail) return buildSixDimHtml(sr.six_dim_detail, sr);
  const detail = sr && sr.nihilism_detail;
  if(!detail) return '';

  const maxTotal = detail.max_total || 30;
  const avg = (detail.total_score / (detail.dimensions.length || 6)).toFixed(2);
  const lvCode = normalizeLevel((sr && sr.risk_code) || 'L0');
  const lvLabel = (sr && sr.risk_level) || levelLabel(lvCode);
  const summaryHtml = `<div class="rr-dim-summary">总分 <b>${detail.total_score}</b>/${maxTotal}　·　平均 <b>${avg}</b>/5　·　<span style="color:${levelColor(lvCode)}">${escapeHtml(lvLabel)}</span></div>`;

  // 雷达图（整体轮廓用风险等级色）+ 维度明细行
  const dimsHtml =
    `<div class="rr-radar-wrap">${rrRadarSVG(detail.dimensions, levelColor(lvCode))}</div>` +
    (detail.dimensions || []).map(dim => {
      const color = rrDimColor(dim.score);
      return `<div class="rr-dim-row">
        <span class="rr-dim-dot" style="background:${color}"></span>
        <span class="rr-dim-rowname">${escapeHtml(dim.name)}</span>
        <span class="rr-dim-rowscore" style="color:${color}">${dim.score}/${dim.max || 5}</span>
        <div class="rr-dim-rowdesc">${escapeHtml(dim.band_desc || dim.definition || '')}</div>
      </div>`;
    }).join('');

  const srcNote = detail.score_source === 'llm_fallback_heuristic'
    ? '<div class="rr-src-note">⚠ AI 服务暂不可用，当前六维为规则估算值（非模型逐维判分），仅供参考。</div>'
    : '';

  const subs = detail.matched_subtypes || [];
  const subsHtml = subs.length
    ? subs.map(s => `<span class="rr-hit-chip"><b>${escapeHtml(s.id)}</b> ${escapeHtml(s.name)}<span class="rr-hit-cat">${escapeHtml(s.category)}</span>${rrConfirmBadge(s.confirmed)}</span>`).join('')
    : '<span class="rr-hit-empty">无</span>';

  const reasonHtml = detail.llm_reason
    ? `<div class="rr-hit-block">
        <div class="rr-hit-label">大模型判定理由</div>
        <div class="rr-hit-reason">${escapeHtml(detail.llm_reason)}</div>
      </div>`
    : '';

  const evid = detail.llm_evidence || [];
  const evidHtml = evid.length
    ? `<div class="rr-hit-block">
        <div class="rr-hit-label">大模型抽取证据</div>
        <div class="rr-hit-evid">${evid.map(e => `<div class="rr-evid-item">“${escapeHtml(e)}”</div>`).join('')}</div>
      </div>`
    : '';

  const phrases = detail.matched_phrases || [];
  const phrasesHtml = phrases.length
    ? phrases.map(p => `<div class="rr-hit-phrase"><span class="rr-tier ${rrTierCls(p.tier)}">${escapeHtml(p.tier)}</span><span class="rr-hit-ptext">${escapeHtml(p.phrase)}</span>${p.match === 'semantic' ? '<span class="rr-mtag">语义近似</span>' : ''}</div>`).join('')
    : '<span class="rr-hit-empty">无</span>';

  const esc = detail.escalations || [];
  const escHtml = esc.length ? `
    <div class="rr-hit-block">
      <div class="rr-hit-label">叠加 / 降档规则</div>
      <div class="rr-hit-esc">${esc.map(e => `<div class="rr-esc-item">• ${escapeHtml(e.desc)}</div>`).join('')}</div>
    </div>` : '';

  // 逐句判定明细
  const sents = detail.sentences || [];
  const sentsHtml = sents.length
    ? sents.map(s => {
        const lv = normalizeLevel(s.risk_code || 'L0');
        const types = (s.matched_subtypes || [])
          .map(x => `<span class="rr-sent-type">${escapeHtml(x.id)} ${escapeHtml(x.name)}${rrConfirmBadge(x.confirmed)}</span>`)
          .join('');
        return `<div class="rr-sent">
          <div class="rr-sent-head">
            <span class="rr-sent-lv" style="background:${levelColor(lv)}">${escapeHtml(levelLabel(lv))}</span>
            <span class="rr-sent-score">${s.total_score}/30</span>
          </div>
          <div class="rr-sent-text">${escapeHtml(s.text)}</div>
          <div class="rr-sent-types">${types}</div>
          ${s.llm_reason ? `<div class="rr-sent-reason">${escapeHtml(s.llm_reason)}</div>` : ''}
        </div>`;
      }).join('')
    : '<span class="rr-hit-empty">无命中句</span>';
  const sentsCard = sents.length ? `
    <section class="rr-card">
      <div class="rr-section">逐句判定（命中 ${sents.length} 句，按风险排序）</div>
      <div class="rr-sents">${sentsHtml}</div>
    </section>` : '';

  return `
    <section class="rr-card">
      <div class="rr-section">六维分析</div>
      ${summaryHtml}
      <div class="rr-dims">${dimsHtml}</div>
      ${srcNote}
    </section>
    <section class="rr-card">
      <div class="rr-section">命中详情</div>
      ${reasonHtml}
      <div class="rr-hit-block">
        <div class="rr-hit-label">命中话术套路</div>
        <div class="rr-hit-chips">${subsHtml}</div>
      </div>
      ${evidHtml}
      <div class="rr-hit-block">
        <div class="rr-hit-label">命中原文片段（规则库）</div>
        <div class="rr-hit-phrases">${phrasesHtml}</div>
      </div>
      ${escHtml}
    </section>
    ${sentsCard}
    <button class="rr-export-btn" data-action="export-report">⬇ 导出评估报告（Word）</button>`;
}

// 大模型对命中类型的复核结论徽标：true=确认 / false=疑似误命中 / null=未复核（AI 兜底）
function rrConfirmBadge(confirmed){
  if(confirmed === true) return '<span class="rr-confirm rr-confirm-yes">✓ 已复核</span>';
  if(confirmed === false) return '<span class="rr-confirm rr-confirm-no">⚠ 疑似误命中</span>';
  return '<span class="rr-confirm rr-confirm-na">未复核</span>';
}

// 导出评估报告（Word）：经 background 拉取 docx，前端转 blob 触发下载
async function rrExportReport(btn){
  const url = (reviewResult && reviewResult.url) || (reviewSource && reviewSource.url) || location.href;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '正在生成报告…';
  try{
    const res = await chrome.runtime.sendMessage({
      type:'RISK_REVIEW_EXPORT',
      payload:{ endpoint:'http://localhost:8000/report/nihilism.docx', url, age_group:'13-15', source:reviewSource }
    });
    if(!res?.ok) throw new Error(res?.error || '生成失败');
    const bytes = Uint8Array.from(atob(res.b64), c => c.charCodeAt(0));
    const blob = new Blob([bytes], {type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
    const dlUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = dlUrl; a.download = res.filename || '历史虚无评估报告.docx';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(dlUrl), 4000);
    btn.textContent = '✓ 已下载';
    setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2000);
  }catch(err){
    btn.textContent = '导出失败，重试';
    btn.disabled = false;
    console.error('[青年心盾] 导出报告失败:', err);
  }
}

function rrDimColor(score){
  if(score >= 5) return '#c0392b';
  if(score >= 4) return '#e67e22';
  if(score >= 3) return '#d4ac0d';
  if(score >= 1) return '#27ae60';
  return '#95a5a6';
}

function rrRadarSVG(dims, color){
  const N = dims.length || 6, cx = 160, cy = 148, R = 82, MAX = 5;
  const ang = i => (-90 + i * (360 / N)) * Math.PI / 180;
  const pt = (i, r) => [cx + r * Math.cos(ang(i)), cy + r * Math.sin(ang(i))];
  let grid = '';
  for(let g = 1; g <= 5; g++){
    const r = R * g / 5;
    const poly = dims.map((_, i) => pt(i, r).map(v => v.toFixed(1)).join(',')).join(' ');
    grid += `<polygon points="${poly}" fill="none" stroke="${g === 5 ? '#c2d0d8' : '#e6edf1'}" stroke-width="1"/>`;
  }
  let spokes = '', labels = '', dots = '';
  const dpoly = dims.map((d, i) => pt(i, R * d.score / MAX).map(v => v.toFixed(1)).join(',')).join(' ');
  dims.forEach((d, i) => {
    const [ex, ey] = pt(i, R);
    spokes += `<line x1="${cx}" y1="${cy}" x2="${ex.toFixed(1)}" y2="${ey.toFixed(1)}" stroke="#e6edf1" stroke-width="1"/>`;
    const [lx, ly] = pt(i, R + 15);
    const c = Math.cos(ang(i)), s = Math.sin(ang(i));
    const anchor = Math.abs(c) < 0.3 ? 'middle' : (c > 0 ? 'start' : 'end');
    const dy = s < -0.3 ? -1 : (s > 0.3 ? 11 : 4);
    labels += `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" dy="${dy}" font-size="10.5" font-weight="700" fill="#48596a">${d.name}<tspan dx="3" font-size="10" font-weight="800" fill="${color}">${d.score}</tspan></text>`;
    const [px, py] = pt(i, R * d.score / MAX);
    dots += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="2.6" fill="${color}"/>`;
  });
  return `<svg viewBox="0 0 320 296" width="100%" role="img" aria-label="六维雷达图">${grid}${spokes}<polygon points="${dpoly}" fill="${color}" fill-opacity="0.16" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>${dots}${labels}</svg>`;
}

function rrTierCls(tier){
  return ({'极高':'t4','高':'t3','中度':'t2'})[tier] || 't2';
}

function renderCase(c){
  const lv = normalizeLevel(c.risk_level || 'L0');
  const url = c.source_url || c.data_source_url || '';
  const key = c.case_id || String(Math.random());
  _rrCaseMap.set(key, c);
  return `
    <section class="rr-case" id="rr-case-${escapeHtml(key)}">
      <div class="rr-band" style="background:${levelColor(lv)}"></div>
      <div class="rr-case-top">
        <span class="rr-lv" style="color:${levelColor(lv)};border-color:${levelColor(lv)}">${levelLabel(lv)}</span>
        ${c.risk_domain ? `<span class="rr-domain">${escapeHtml(c.risk_domain)}</span>` : ''}
        <span class="rr-id">${escapeHtml(c.case_id || '')}</span>
      </div>
      <div class="rr-case-name">${escapeHtml(c.case_name || '风险案例')}</div>
      <div class="rr-case-text">${escapeHtml((c.representative_text || c.spread_mechanism || c.spread_path || '').slice(0,90) || '暂无摘要')}</div>
      <div class="rr-case-foot">
        <button class="rr-case-btn" data-action="toggle-case" data-key="${escapeHtml(key)}">查看详情 ▾</button>
        ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="rr-case-link">原文 ↗</a>` : ''}
      </div>
      <div class="rr-case-detail" id="rr-detail-${escapeHtml(key)}"></div>
    </section>
  `;
}

function renderPositiveCase(p){
  const url = p.source_url || '';
  const tag = url ? 'a' : 'section';
  const linkAttrs = url ? ` href="${escapeHtml(url)}" target="_blank" rel="noopener"` : '';
  return `
    <${tag} class="rr-positive-case${url ? ' rr-positive-case-link' : ''}"${linkAttrs}>
      <div class="rr-positive-top">
        <span class="rr-positive-badge">${escapeHtml(p.source || '中青网')}</span>
        <span class="rr-positive-id">${escapeHtml(p.case_id || '')}</span>
      </div>
      <div class="rr-positive-name">${escapeHtml(p.case_name || '正向案例')}</div>
      <div class="rr-positive-reason">${escapeHtml(p.relation_reason || '')}</div>
      ${url ? `<span class="rr-positive-link">查看原文 ↗</span>` : ''}
    </${tag}>
  `;
}

function rrToggleCase(key){
  const detailEl = reviewRoot?.querySelector(`#rr-detail-${key}`);
  const btn = reviewRoot?.querySelector(`#rr-case-${key} .rr-case-btn`);
  if(!detailEl) return;

  if(detailEl.classList.contains('rr-cd-open')){
    detailEl.classList.remove('rr-cd-open');
    if(btn) btn.textContent = '查看详情 ▾';
    return;
  }

  const c = _rrCaseMap.get(key);
  if(!c) return;

  // 核心关键词
  const kwList = (c.core_keywords || '').split(/[,，、]/).map(k=>k.trim()).filter(Boolean);
  const kwHtml = kwList.length
    ? kwList.map(k=>`<span class="rr-kw-tag">${escapeHtml(k)}</span>`).join('')
    : '<span style="color:var(--rr-subtle);font-size:12px">暂无</span>';

  // 处置策略内容
  const dis = {
    '6-12岁':  c.disposal_6_12  || c['disposal_6_12']  || '',
    '13-15岁': c.disposal_13_15 || c['disposal_13_15'] || '',
    '16-18岁': c.disposal_16_18 || c['disposal_16_18'] || '',
  };
  const ages = Object.keys(dis);
  const tabsHtml = ages.map((a,i)=>
    `<button class="rr-cd-tab${i===0?' active':''}" data-action="switch-age" data-key="${escapeHtml(key)}" data-age="${escapeHtml(a)}">${a}</button>`
  ).join('');

  detailEl.innerHTML = `
    <div class="rr-cd-section">
      <div class="rr-cd-lbl">核心关键词</div>
      <div class="rr-kw-tags">${kwHtml}</div>
    </div>
    <div class="rr-cd-section">
      <div class="rr-cd-lbl">传播机制</div>
      <div class="rr-cd-spread">${escapeHtml(c.spread_mechanism || c.spread_path || '暂无')}</div>
    </div>
    <div class="rr-cd-section">
      <div class="rr-cd-lbl">代表文本</div>
      <div class="rr-cd-quote">${escapeHtml(c.representative_text || '暂无')}</div>
    </div>
    <div class="rr-cd-section">
      <div class="rr-cd-lbl">处置策略 &amp; 引导话术</div>
      <div class="rr-cd-tabs">${tabsHtml}</div>
      <div class="rr-cd-val" id="rr-dis-${escapeHtml(key)}">${escapeHtml(dis['6-12岁'] || '暂无')}</div>
    </div>
    ${c.review_conclusion ? `<div class="rr-cd-section">
      <div class="rr-cd-lbl">复盘结论</div>
      <div class="rr-cd-val">${escapeHtml(c.review_conclusion.slice(0,220))}</div>
    </div>` : ''}
  `;

  // 存储处置策略供 tab 切换用
  detailEl._dis = dis;
  detailEl.classList.add('rr-cd-open');
  if(btn) btn.textContent = '收起 ▴';

  // 异步加载话术（如果后端可用）
  if(c.case_id){
    rrLoadScripts(key, c.case_id, c.risk_domain || '');
  }
}

function rrSwitchAge(tabEl, key, age){
  const detailEl = reviewRoot?.querySelector(`#rr-detail-${key}`);
  if(!detailEl) return;
  detailEl.querySelectorAll('.rr-cd-tab').forEach(t=>t.classList.remove('active'));
  tabEl.classList.add('active');
  const disEl = detailEl.querySelector(`#rr-dis-${key}`);
  if(disEl) disEl.textContent = (detailEl._dis || {})[age] || '暂无';
}

async function rrLoadScripts(key, caseId, domain){
  try{
    const p = new URLSearchParams({case_id: caseId, domain});
    const res = await fetch(`http://localhost:8000/scripts?${p}`, {signal: AbortSignal.timeout(8000)});
    if(!res.ok) return;
    const data = await res.json();
    const scripts = data.scripts || {};
    const dis = {};
    ['6-12','13-15','16-18'].forEach(age=>{
      const items = scripts[age] || [];
      if(items[0]?.script_content) dis[age+'岁'] = items[0].script_content;
    });
    if(!Object.values(dis).some(Boolean)) return;
    const detailEl = reviewRoot?.querySelector(`#rr-detail-${key}`);
    if(!detailEl) return;
    // 用话术覆盖处置策略
    Object.assign(detailEl._dis, dis);
    // 刷新当前显示的 tab
    const activeTab = detailEl.querySelector('.rr-cd-tab.active');
    const curAge = activeTab?.textContent;
    const disEl = detailEl.querySelector(`#rr-dis-${key}`);
    if(disEl && curAge && dis[curAge]) disEl.textContent = dis[curAge];
  }catch(e){ /* 话术加载失败静默处理 */ }
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

// ── 统一等级模型 ──
// 内部沿用 L0–L4 五档桶，视觉映射为新版《五大风险域》1–5 级五色灯：
//   L0=1级🟢绿  L1=2级🟡黄  L2=3级🟠橙  L3=4级🔴红  L4=5级🟣紫
const RR_LV_NUM   = {L0:1, L1:2, L2:3, L3:4, L4:5};
const RR_LV_COLOR = {L0:'#2e9e5b', L1:'#e0b400', L2:'#e67e22', L3:'#d64541', L4:'#8e44ad'};
const RR_LV_ICON  = {L0:'🟢', L1:'🟡', L2:'🟠', L3:'🔴', L4:'🟣'};
const RR_LV_POS   = {L0:'10%', L1:'30%', L2:'50%', L3:'70%', L4:'90%'};
const RR_LV_LABEL = {L0:'低风险', L1:'轻度风险', L2:'中度风险', L3:'高风险', L4:'极高风险'};

function normalizeLevel(level){
  const raw = String(level ?? '').trim();
  if(!raw) return 'L0';
  // 旧版 L0–L4
  if(/^L[0-4]$/i.test(raw)) return raw.toUpperCase();
  // 中文风险文案
  const zh = {'极高风险':'L4','高风险':'L3','中度风险':'L2','轻度风险':'L1','低风险':'L0'};
  if(zh[raw]) return zh[raw];
  // 新版数字 1–5 级
  const num = {'1':'L0','2':'L1','3':'L2','4':'L3','5':'L4'};
  if(num[raw]) return num[raw];
  // 新版灯色 warning_light
  const light = {green:'L0', yellow:'L1', orange:'L2', red:'L3', purple:'L4'};
  if(light[raw.toLowerCase()]) return light[raw.toLowerCase()];
  return raw;
}

function levelLabel(level){ return RR_LV_LABEL[level] || level || '低风险'; }
function levelColor(level){ return RR_LV_COLOR[level] || RR_LV_COLOR.L0; }
function levelPos(level){ return RR_LV_POS[level] || '10%'; }
function levelNum(level){ return RR_LV_NUM[level] || 1; }
function levelIcon(level){ return RR_LV_ICON[level] || '🟢'; }

// 悬浮球按风险等级五色着色（L0绿 L1黄 L2橙 L3红 L4紫）；无结果时清空回默认青色
function rrSetFabLevel(level){
  const fab = reviewRoot && reviewRoot.querySelector('.rr-fab');
  if(!fab) return;
  fab.classList.remove('rr-fab-l0','rr-fab-l1','rr-fab-l2','rr-fab-l3','rr-fab-l4','rr-fab-warn');
  const L = normalizeLevel(level);
  if(/^L[0-4]$/.test(L)) fab.classList.add('rr-fab-' + L.toLowerCase());
}
function rrResetFabLevel(){
  const fab = reviewRoot && reviewRoot.querySelector('.rr-fab');
  if(fab) fab.classList.remove('rr-fab-l0','rr-fab-l1','rr-fab-l2','rr-fab-l3','rr-fab-l4','rr-fab-warn');
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
  return String(value ?? '')
    // 抹去命中规则内部编号（如 L2-32、（L1-13·L2-26）），不对用户暴露
    .replace(/[（(]\s*L\d{1,2}-\d{1,3}(?:\s*[·、,]\s*L\d{1,2}-\d{1,3})*\s*[）)]/g, '')
    .replace(/L\d{1,2}-\d{1,3}/g, '')
    .replace(/[&<>"']/g, ch => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[ch]));
}

// ── 悬浮面板预警横幅 ──────────────────────────────────────
const _RR_LV_ACTION = {
  L4:'请立即人工复核，建议限制推荐',
  L3:'建议人工复核，关注传播范围',
  L2:'建议关注，注意后续变化',
};

function rrIsHighRisk(d){
  const sr = (d && d.structured_result) || {};
  const lv = normalizeLevel((d && (d.risk_level || d.risk_code)) || sr.risk_code || 'L0');
  return ({L4:4,L3:3,L2:2,L1:1,L0:0}[lv] || 0) >= 3;
}

function rrApplyMajorRiskDisplay(d){
  if(!d || !d.structured_result || !d.structured_result.major_ideological_risk) return d;
  d.risk_level = 'L4';
  d.risk_code = 'L4';
  d.risk_label = '极高风险';
  d.warning_light = 'purple';
  d.risk_level_num = 5;
  const pct = Number(d.risk_percent || d.structured_result.risk_percent || 0);
  d.risk_percent = Math.max(81, isFinite(pct) ? pct : 0);
  d.composite_score = Math.max(4.05, Number(d.composite_score || d.structured_result.risk_score || 0));
  d.structured_result.risk_code = 'L4';
  d.structured_result.risk_level = '极高风险';
  d.structured_result.warning_light = 'purple';
  d.structured_result.risk_level_num = 5;
  d.structured_result.risk_percent = Math.max(81, Number(d.structured_result.risk_percent || 0));
  d.structured_result.risk_score = Math.max(4.05, Number(d.structured_result.risk_score || 0));
  return d;
}

function showPolicyReference(el, d){
  if(!el) return false;
  const sr = (d && d.structured_result) || {};
  const lv = normalizeLevel((d && (d.risk_level || d.risk_code)) || sr.risk_code || 'L0');
  const types = (sr.risk_types || []).slice(0, 3).filter(Boolean);
  const domains = (sr.risk_domains || []).slice(0, 3).filter(Boolean);
  const focus = types.length ? types.join('、') : (domains.length ? domains.join('、') : '未发现明确高风险指向');
  // 低/中风险：复用预警卡结构与配色（等级色 + 大标题），文案保持“研判参考、不触发预警”
  el.innerHTML = `
    <div class="rr-alert rr-alert-policy rr-alert-${lv.toLowerCase()}">
      <div class="rr-alert-stripe"></div>
      <div class="rr-alert-body">
        <div class="rr-alert-icon">${levelIcon(lv)}</div>
        <div class="rr-alert-dot"></div>
        <div class="rr-alert-content" style="flex:1;min-width:0">
          <div class="rr-alert-eye">${escapeHtml(levelLabel(lv))} · 研判参考</div>
          <div class="rr-alert-title">当前为${escapeHtml(levelLabel(lv))}，仅供研判参考</div>
          <div class="rr-alert-grid">
            <div class="rr-alert-section">
              <div class="rr-alert-k">参考关注</div>
              <div class="rr-alert-v">${escapeHtml(focus)}</div>
            </div>
            <div class="rr-alert-section">
              <div class="rr-alert-k">政策与导向参考</div>
              <div class="rr-alert-v">参照网络信息内容生态治理、未成年人网络保护等要求，关注是否诱导偏激认知、情绪对立或错误价值取向；结合青年成长成才与主流价值引导，优先采用事实澄清、正向案例和分龄引导。</div>
            </div>
          </div>
        </div>
      </div>
    </div>`;
  return true;
}

const RR_MAJOR_FALLBACK_DESC = {
  M1:'否定、边缘化马克思主义和社会主义核心价值体系',
  M2:'否定中国特色社会主义道路、制度、理论、文化',
  M3:'歪曲、否定、虚无党史国史军史革命史、英雄人物',
  M4:'削弱对党的领导、人民民主专政、国家共同体的认同',
  M5:'污名化、矮化、娱乐化英雄烈士、先进典型',
  M6:'系统制造“什么都不信”、无力感、阶层敌意',
  M7:'深度伪造、数据操控、暗网真相、算法操控叙事',
  M8:'以普世价值、西方新闻观、外媒叙事丑化中国形象',
  M9:'娱乐化、二元化、情绪化，使公众放弃复杂判断',
  M10:'将局部个案嫁接为全局政治制度问题',
  M11:'组织化放大负面舆论、操纵热搜评论、攻击主流媒体公信力',
};

function rrMajorRiskItems(d){
  const sr = (d && d.structured_result) || {};
  const sd = sr.six_dim_detail || {};
  const items = [];
  const seen = new Set();
  (sd.major_rules || []).forEach(m => {
    const id = m.id || '';
    if(id && seen.has(id)) return;
    if(id) seen.add(id);
    items.push({id, desc: m.desc || RR_MAJOR_FALLBACK_DESC[id] || ''});
  });
  (sr.major_risk_rules || []).forEach(id => {
    if(!id || seen.has(id)) return;
    seen.add(id);
    items.push({id, desc: RR_MAJOR_FALLBACK_DESC[id] || '命中重大意识形态风险类型'});
  });
  if(!items.length && sd.veto_rules && sd.veto_rules.length){
    sd.veto_rules.slice(0, 2).forEach(v => items.push({id: v.id || '重大风险', desc: v.desc || '触及重大意识形态风险红线'}));
  }
  if(!items.length){
    const t = ((sr.risk_types || [])[0] || '高风险意识形态内容');
    items.push({id: '重点关注', desc: t});
  }
  return items.slice(0, 4);
}

function rrMajorRiskCardRows(items){
  // 只展示命中内容的中文描述，不显示 M/VETO 等规则编号符号
  return items.map(item => `<div class="rr-major-type">
      <span class="rr-major-type-desc">${escapeHtml(item.desc || '需结合上下文进行人工复核')}</span>
    </div>`).join('');
}

// 重大意识形态风险提示：放在原顶部提示位置，不再使用案例关联提示。
function showIdeologyAlert(el, d){
  if(!el) return false;
  const sr = (d && d.structured_result) || {};
  const lv  = normalizeLevel(d.risk_level || d.risk_code || sr.risk_code || 'L4');
  if(!rrIsMajorRisk(d)) return false;   // 仅在真命中重大意识形态风险(M规则/红线)时显示
  const items = rrMajorRiskItems(d);
  const action = _RR_LV_ACTION[lv] || '建议人工复核';
  el.innerHTML = `
    <div class="rr-alert rr-alert-ideology rr-alert-${lv.toLowerCase()}">
      <div class="rr-alert-stripe"></div>
      <div class="rr-alert-body">
        <div class="rr-alert-icon">${levelIcon(lv)}</div>
        <div class="rr-alert-dot"></div>
        <div class="rr-alert-content" style="flex:1;min-width:0">
          <div class="rr-alert-eye">意识形态重大风险提示</div>
          <div class="rr-alert-title">命中重大意识形态风险类型 — ${escapeHtml(action)}</div>
          <div class="rr-alert-grid">
            <div class="rr-alert-section">
              <div class="rr-alert-k">重大风险类型</div>
              <div class="rr-major-types">${rrMajorRiskCardRows(items)}</div>
            </div>
            <div class="rr-alert-section">
              <div class="rr-alert-k">政策及行动指导文件参考</div>
              <div class="rr-alert-v">参考网络信息内容生态治理、未成年人网络保护、英雄烈士保护、爱国主义教育等相关要求，建议进入人工复核、证据留存、分龄引导与传播范围评估流程。</div>
            </div>
            <div class="rr-alert-section">
              <div class="rr-alert-k">总书记论述指示</div>
              <div class="rr-alert-v">坚持正确政治方向、舆论导向和价值取向，强化青少年理想信念教育、历史教育和网络文明引导，防止错误思潮和不良信息影响青少年成长。</div>
            </div>
          </div>
        </div>
        <button class="rr-alert-close" data-action="close-alert">✕</button>
      </div>
    </div>`;
  const alertDiv = el.querySelector('.rr-alert');
  if(alertDiv) alertDiv.scrollIntoView({behavior: 'smooth', block: 'start'});
  const panel = reviewRoot?.querySelector('.rr-panel');
  if(panel) panel.classList.add('rr-panel-warn-' + lv.toLowerCase());
  return true;
}

// 是否真命中重大意识形态风险（命中 M 规则/一票否决红线，后端 major_ideological_risk）。
// 注意与 rrIsHighRisk 区分：后者只看风险等级(L3+)，前者是内容定性，二者不等价。
function rrIsMajorRisk(d){
  return !!(d && d.structured_result && d.structured_result.major_ideological_risk);
}

// 提取六维实际命中项的中文描述（最具体的命中内容，不含 F/N/V 等编号）
function rrHitItemDescs(sr, limit){
  const sd = (sr && sr.six_dim_detail) || {};
  const out = [];
  (sd.dimensions || []).forEach(dim => {
    (dim.items || []).forEach(it => {
      if(it.hit && it.desc) out.push(it.desc);
    });
  });
  return out.slice(0, limit || 4);
}

// 普通高风险预警：命中高风险(L3红/L4紫)但未触及重大意识形态风险红线时显示，
// 不套“重大意识形态风险”定性，避免把普通高分内容误报为重大意识形态风险。
function showHighRiskAlert(el, d){
  if(!el) return false;
  if(!rrIsHighRisk(d)) return false;
  const sr = (d && d.structured_result) || {};
  const lv = normalizeLevel(d.risk_level || d.risk_code || sr.risk_code || 'L3');
  const action = _RR_LV_ACTION[lv] || '建议人工复核';
  const hitItems = rrHitItemDescs(sr, 4);
  const types = (sr.risk_types || []).slice(0, 3).filter(Boolean);
  const domains = (sr.risk_domains || []).slice(0, 3).filter(Boolean);
  const focusList = hitItems.length ? hitItems
    : (types.length ? types : (domains.length ? domains : ['综合六维研判命中高风险']));
  el.innerHTML = `
    <div class="rr-alert rr-alert-highrisk rr-alert-${lv.toLowerCase()}">
      <div class="rr-alert-stripe"></div>
      <div class="rr-alert-body">
        <div class="rr-alert-icon">${levelIcon(lv)}</div>
        <div class="rr-alert-dot"></div>
        <div class="rr-alert-content" style="flex:1;min-width:0">
          <div class="rr-alert-eye">${escapeHtml(levelLabel(lv))}预警</div>
          <div class="rr-alert-title">命中${escapeHtml(levelLabel(lv))}内容 — ${escapeHtml(action)}</div>
          <div class="rr-alert-grid">
            <div class="rr-alert-section">
              <div class="rr-alert-k">风险关注点</div>
              <div class="rr-major-types">${focusList.map(t => `<div class="rr-major-type"><span class="rr-major-type-desc">${escapeHtml(t)}</span></div>`).join('')}</div>
            </div>
            <div class="rr-alert-section">
              <div class="rr-alert-k">处置建议</div>
              <div class="rr-alert-v">建议进入人工复核，关注传播范围与受众，结合正向案例做分龄引导。</div>
            </div>
          </div>
        </div>
        <button class="rr-alert-close" data-action="close-alert">✕</button>
      </div>
    </div>`;
  const alertDiv = el.querySelector('.rr-alert');
  if(alertDiv) alertDiv.scrollIntoView({behavior: 'smooth', block: 'start'});
  const panel = reviewRoot?.querySelector('.rr-panel');
  if(panel) panel.classList.add('rr-panel-warn-' + lv.toLowerCase());
  return true;
}
