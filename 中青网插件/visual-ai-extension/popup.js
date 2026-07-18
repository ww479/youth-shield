const els = {
  pageUrl: document.getElementById('pageUrl'),
  statusBadge: document.getElementById('statusBadge'),
  pinBtn: document.getElementById('pinBtn'),
  skeleton: document.getElementById('skeleton'),
  result: document.getElementById('result'),
  error: document.getElementById('error'),
  errorText: document.getElementById('errorText'),
  retryBtn: document.getElementById('retryBtn'),
  platform: document.getElementById('platform'),
  sourceLink: document.getElementById('sourceLink'),
  contentTitle: document.getElementById('contentTitle'),
  contentSummary: document.getElementById('contentSummary'),
  author: document.getElementById('author'),
  interaction: document.getElementById('interaction'),
  cardSub: document.getElementById('cardSub'),
  tags: document.getElementById('tags'),
  riskWord: document.getElementById('riskWord'),
  riskDot: document.getElementById('riskDot'),
  narrative: document.getElementById('narrative'),
  domain: document.getElementById('domain'),
  dimensionCard: document.getElementById('dimensionCard'),
  dimensions: document.getElementById('dimensions'),
  scoreSourceNote: document.getElementById('scoreSourceNote'),
  dimSummary: document.getElementById('dimSummary'),
  hitCard: document.getElementById('hitCard'),
  hitSubtypes: document.getElementById('hitSubtypes'),
  hitReasonBlock: document.getElementById('hitReasonBlock'),
  hitReason: document.getElementById('hitReason'),
  hitEvidBlock: document.getElementById('hitEvidBlock'),
  hitEvidence: document.getElementById('hitEvidence'),
  hitPhrases: document.getElementById('hitPhrases'),
  hitEscBlock: document.getElementById('hitEscBlock'),
  hitEscalations: document.getElementById('hitEscalations'),
  cases: document.getElementById('cases'),
  positiveHead: document.getElementById('positiveHead'),
  positiveCount: document.getElementById('positiveCount'),
  positiveCases: document.getElementById('positiveCases'),
  alertBanner: document.getElementById('alertBanner'),
  abTitle:     document.getElementById('abTitle'),
  abDetail:    document.getElementById('abDetail'),
  abClose:     document.getElementById('abClose'),
};

const state = {
  tabId: null,
  source: {url:'', title:'', summary:''},
};

init();

async function init(){
  els.retryBtn.addEventListener('click', () => analyze());
  els.pinBtn.addEventListener('click', () => togglePinnedSidebar());
  await loadCurrentPage();
  await refreshPinState();
  analyze();
}

async function loadCurrentPage(){
  const [tab] = await chrome.tabs.query({active:true, currentWindow:true});
  state.tabId = tab?.id;
  state.source = {
    url: tab?.url || '',
    title: tab?.title || '',
    summary: '',
  };
  const context = await getPageContext(state.tabId);
  if(context){
    state.source = {
      url: context.url || state.source.url,
      title: context.title || state.source.title,
      summary: context.summary || '',
    };
  }
  els.pageUrl.textContent = state.source.url || '未读取到当前页面 URL';
}

async function getPageContext(tabId){
  if(!tabId) return null;
  try{
    return await chrome.tabs.sendMessage(tabId, {type:'RISK_REVIEW_GET_CONTEXT'});
  }catch(_err){
    return null;
  }
}

async function analyze(){
  showLoading();
  try{
    const res = await chrome.runtime.sendMessage({
      type:'RISK_REVIEW_ANALYZE',
      payload:{
        endpoint:'http://localhost:8000/analyze',
        useDemoOnly:false,
        age_group:'13-15',
        source:state.source,
      }
    });
    if(!res?.ok) throw new Error(res?.error || '分析失败');
    render(res.result, res.fallback);
  }catch(err){
    showError(formatError(err));
  }
}

async function refreshPinState(){
  const saved = await chrome.storage.local.get(['riskReviewPinned']);
  els.pinBtn.classList.toggle('active', Boolean(saved.riskReviewPinned));
  els.pinBtn.textContent = saved.riskReviewPinned ? '已固定' : '固定悬浮球';
}

async function togglePinnedSidebar(){
  const saved = await chrome.storage.local.get(['riskReviewPinned']);
  const next = !saved.riskReviewPinned;
  if(next){
    await chrome.storage.local.set({
      riskReviewPinned: true,
      riskReviewFabPos: {x: Math.max(8, screen.availWidth - 150), y: Math.max(8, screen.availHeight - 300)}
    });
  }else{
    await chrome.storage.local.set({riskReviewPinned: false});
  }
  await refreshPinState();
  if(!state.tabId) return;
  try{
    await ensureContentScript(state.tabId);
    await chrome.tabs.sendMessage(state.tabId, {
      type: next ? 'RISK_REVIEW_PIN_OPEN' : 'RISK_REVIEW_PIN_CLOSE',
      payload:{source: state.source}
    });
  }catch(_err){
    // 当前页面不允许注入时，仅保存开关状态。
  }
}

async function ensureContentScript(tabId){
  try{
    await chrome.tabs.sendMessage(tabId, {type:'RISK_REVIEW_PING'});
  }catch(_err){
    await chrome.scripting.executeScript({target:{tabId}, files:['content.js']});
    await chrome.scripting.insertCSS({target:{tabId}, files:['content.css']});
  }
}

function showLoading(){
  els.result.hidden = true;
  els.error.hidden = true;
  els.skeleton.hidden = false;
  setBadge('分析中', '');
  hideAlert();
}

function showError(text){
  els.skeleton.hidden = true;
  els.result.hidden = true;
  els.error.hidden = false;
  els.errorText.textContent = text;
  setBadge('失败', 'err');
}

function render(d, fallback){
  els.skeleton.hidden = true;
  els.result.hidden = false;
  els.error.hidden = true;
  setBadge(fallback ? '演示' : '完成', fallback ? 'warn' : 'ok');

  const url = d.url || state.source.url || '';
  const title = d.title || state.source.title || '当前页面内容';
  const summary = d.content_summary || d.summary || state.source.summary || '暂未读取到正文内容。';
  const platform = d.platform || detectPlatform(url);
  const level = normalizeLevel(d.risk_level || d.risk_code || 'L0');
  const levelText = d.risk_label || levelLabel(level);
  const tags = (d.matched_tags || []).slice(0,5);
  const narrative = d?.narrative?.summary || d.risk_explanation || buildNarrative(d, levelText);
  const cases = (d.related_cases || d.cases || []).slice(0,4);
  const LV_ORDER_P = {L4:4,L3:3,L2:2,L1:1,L0:0};
  const alertCase = cases.find(c => (LV_ORDER_P[normalizeLevel(c.risk_level || 'L0')] || 0) >= 3);

  els.platform.textContent = platform;
  els.sourceLink.href = /^https?:\/\//i.test(url) ? url : '#';
  els.sourceLink.style.visibility = /^https?:\/\//i.test(url) ? 'visible' : 'hidden';
  els.contentTitle.textContent = title;
  els.contentSummary.textContent = summary;
  els.author.textContent = `@${d.author || platform || '用户'}`;
  els.interaction.textContent = formatInteraction(d.interaction);
  els.cardSub.textContent = title.slice(0,30);
  els.riskWord.textContent = levelText;
  els.riskWord.style.color = levelColor(level);
  els.riskDot.style.borderColor = levelColor(level);
  els.narrative.textContent = narrative;
  els.domain.textContent = d.primary_domain || tags[0]?.domain || '';

  // 六维分析 + 命中详情（历史虚无主义命中时才有）
  renderNihilism(d.structured_result);

  // 相关案例（原"中青网正向案例"）：与风险案例完全分开，无风险配色，先于风险案例展示
  const positiveCases = (d.positive_cases || []).slice(0,4);
  if(els.positiveHead && els.positiveCases){
    els.positiveHead.hidden = !positiveCases.length;
    els.positiveCases.hidden = !positiveCases.length;
    if(positiveCases.length){
      els.positiveCount.textContent = positiveCases.length;
      els.positiveCases.innerHTML = positiveCases.map(renderPositiveCase).join('');
    } else {
      els.positiveCases.innerHTML = '';
    }
  }

  els.tags.innerHTML = tags.length ? tags.map(tag => `
    <span class="tag">${escapeHtml(tag.keyword || tag.l3_name || tag.l2_name || '风险标签')}</span>
  `).join('') + (tags[0]?.domain ? `<span class="tag domain">${escapeHtml(tags[0].domain.replace('风险',''))}</span>` : '') : '<span class="tag muted">未命中明确标签</span>';

  // 风险案例（原"关联案例"）：只展示触发预警的那一条
  hideAlert();
  els.cases.innerHTML = '';
  if(!alertCase){
    els.cases.innerHTML = '<div class="case"><div class="case-name">暂无预警风险案例</div><div class="case-snippet">当前内容未命中需要预警的风险案例。</div></div>';
  } else {
    const lv = normalizeLevel(alertCase.risk_level || 'L0');
    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <article class="case">
        <div class="case-band" style="background:${levelColor(lv)}"></div>
        <div class="case-top">
          <span class="lv" style="color:${levelColor(lv)};border-color:${levelColor(lv)}">${levelLabel(lv)}</span>
          ${alertCase.risk_domain ? `<span class="case-domain">${escapeHtml(alertCase.risk_domain)}</span>` : ''}
          <span class="case-id">${escapeHtml(alertCase.case_id || '')}</span>
        </div>
        <div class="case-name">${escapeHtml(alertCase.case_name || '风险案例')}</div>
        <div class="case-snippet">${escapeHtml((alertCase.representative_text || alertCase.spread_mechanism || alertCase.spread_path || '').slice(0,90) || '暂无摘要')}</div>
        <div class="case-foot">
          <span>${escapeHtml(alertCase.risk_type || '')}</span>
          ${alertCase.source_url ? `<a href="${escapeAttr(alertCase.source_url)}" target="_blank" rel="noopener">查看原文 ↗</a>` : ''}
        </div>
      </article>`;
    els.cases.appendChild(wrap.firstElementChild);
  }

  // 卡片依次淡入
  anime({
    targets: els.result.querySelectorAll('.preview, .analysis-card, .positive-head, .positive-cases, .cases-head, .cases'),
    opacity: [0, 1],
    translateY: [12, 0],
    delay: anime.stagger(70),
    duration: 260,
    easing: 'easeOutExpo',
  });

  // 标签错位弹入
  anime({
    targets: els.tags.querySelectorAll('.tag'),
    opacity: [0, 1],
    scale: [0.82, 1],
    delay: anime.stagger(55, {start: 180}),
    duration: 200,
    easing: 'easeOutBack',
  });

  // 风险点从左端滑到目标位置
  els.riskDot.style.left = '6%';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    els.riskDot.style.left = levelPos(level);
  }));
}

function setBadge(text, type){
  els.statusBadge.textContent = text;
  els.statusBadge.className = `badge ${type || ''}`.trim();
}

function formatError(err){
  if(state.source.url.startsWith('file:')){
    return '当前是本地 file 页面。如果无法读取页面内容，请在扩展详情里开启“允许访问文件网址”。';
  }
  return String(err?.message || err || '插件分析失败，请确认后端服务是否启动。');
}

function normalizeLevel(level){
  const raw = String(level ?? '').trim();
  if(!raw) return 'L0';
  if(/^L[0-4]$/i.test(raw)) return raw.toUpperCase();
  const zh = {'极高风险':'L4','高风险':'L3','中度风险':'L2','轻度风险':'L1','低风险':'L0'};
  if(zh[raw]) return zh[raw];
  const num = {'1':'L0','2':'L1','3':'L2','4':'L3','5':'L4'};
  if(num[raw]) return num[raw];
  const light = {green:'L0', yellow:'L1', orange:'L2', red:'L3', purple:'L4'};
  if(light[raw.toLowerCase()]) return light[raw.toLowerCase()];
  return raw;
}

function levelLabel(level){
  return ({L4:'极高风险',L3:'高风险',L2:'中度风险',L1:'轻度风险',L0:'低风险'})[level] || level || '低风险';
}

// 新版《五大风险域》五色灯：L0=1绿 L1=2黄 L2=3橙 L3=4红 L4=5紫
function levelColor(level){
  return ({L4:'#8e44ad',L3:'#d64541',L2:'#e67e22',L1:'#e0b400',L0:'#2e9e5b'})[level] || '#2e9e5b';
}

function levelPos(level){
  return ({L4:'90%',L3:'70%',L2:'50%',L1:'30%',L0:'10%'})[level] || '10%';
}

function renderNihilism(sr){
  const detail = sr && sr.nihilism_detail;
  if(!detail){
    els.dimensionCard.hidden = true;
    els.hitCard.hidden = true;
    return;
  }
  els.dimensionCard.hidden = false;
  els.hitCard.hidden = false;

  // 汇总行：总分/平均/等级，让"六维→风险等级"的对应关系一目了然
  const maxTotal = detail.max_total || 30;
  const avg = (detail.total_score / (detail.dimensions.length || 6)).toFixed(2);
  const lv = normalizeLevel(sr.risk_code || 'L0');
  els.dimSummary.innerHTML = `总分 <b>${detail.total_score}</b>/${maxTotal}　·　平均 <b>${avg}</b>/5　·　<span style="color:${levelColor(lv)}">${escapeHtml(sr.risk_level || levelLabel(lv))}</span>`;

  // 六维雷达图（整体轮廓用风险等级色）+ 维度明细列表
  els.dimensions.innerHTML =
    `<div class="radar-wrap">${radarSVG(detail.dimensions, levelColor(lv))}</div>` +
    (detail.dimensions || []).map(dim => {
      const color = dimColor(dim.score);
      return `<div class="dim-row">
        <span class="dim-dot" style="background:${color}"></span>
        <span class="dim-rowname">${escapeHtml(dim.name)}</span>
        <span class="dim-rowscore" style="color:${color}">${dim.score}/${dim.max || 5}</span>
        <div class="dim-rowdesc">${escapeHtml(dim.band_desc || dim.definition || '')}</div>
      </div>`;
    }).join('');

  // 打分来源提示（AI 不可用时诚实标注为规则估算）
  if(detail.score_source === 'llm_fallback_heuristic'){
    els.scoreSourceNote.hidden = false;
    els.scoreSourceNote.textContent = '⚠ AI 服务暂不可用，当前六维为规则估算值（非模型逐维判分），仅供参考。';
  }else{
    els.scoreSourceNote.hidden = true;
  }

  // 大模型判定理由
  if(detail.llm_reason){
    els.hitReasonBlock.hidden = false;
    els.hitReason.textContent = detail.llm_reason;
  }else{
    els.hitReasonBlock.hidden = true;
  }

  // 命中话术套路（带大模型复核结论）
  const subs = detail.matched_subtypes || [];
  els.hitSubtypes.innerHTML = subs.length
    ? subs.map(s => `<span class="hit-chip"><b>${escapeHtml(s.id)}</b> ${escapeHtml(s.name)}<span class="hit-chip-cat">${escapeHtml(s.category)}</span>${confirmBadge(s.confirmed)}</span>`).join('')
    : '<span class="hit-empty">无</span>';

  // 大模型抽取证据
  const evid = detail.llm_evidence || [];
  if(evid.length){
    els.hitEvidBlock.hidden = false;
    els.hitEvidence.innerHTML = evid.map(e => `<div class="evid-item">“${escapeHtml(e)}”</div>`).join('');
  }else{
    els.hitEvidBlock.hidden = true;
  }

  // 命中原文片段（带风险档位标签；语义命中标注"语义近似"）
  const phrases = detail.matched_phrases || [];
  els.hitPhrases.innerHTML = phrases.length
    ? phrases.map(p => `<div class="hit-phrase"><span class="tier ${tierCls(p.tier)}">${escapeHtml(p.tier)}</span><span class="hit-phrase-text">${escapeHtml(p.phrase)}</span>${p.match === 'semantic' ? '<span class="mtag">语义近似</span>' : ''}</div>`).join('')
    : '<span class="hit-empty">无</span>';

  // 叠加 / 降档规则
  const esc = detail.escalations || [];
  if(esc.length){
    els.hitEscBlock.hidden = false;
    els.hitEscalations.innerHTML = esc.map(e => `<div class="esc-item">• ${escapeHtml(e.desc)}</div>`).join('');
  }else{
    els.hitEscBlock.hidden = true;
  }
}

// 大模型对命中类型的复核结论徽标：true=确认 / false=疑似误命中 / null=未复核（AI 兜底）
function confirmBadge(confirmed){
  if(confirmed === true) return '<span class="confirm confirm-yes">✓ 已复核</span>';
  if(confirmed === false) return '<span class="confirm confirm-no">⚠ 疑似误命中</span>';
  return '<span class="confirm confirm-na">未复核</span>';
}

function dimColor(score){
  if(score >= 5) return '#c0392b';
  if(score >= 4) return '#e67e22';
  if(score >= 3) return '#d4ac0d';
  if(score >= 1) return '#27ae60';
  return '#95a5a6';
}

// 六维雷达图：单实体轮廓，整体用风险等级色。返回 SVG 字符串。
function radarSVG(dims, color){
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

function tierCls(tier){
  return ({'极高':'t4','高':'t3','中度':'t2'})[tier] || 't2';
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

function escapeAttr(value){
  return escapeHtml(value).replace(/`/g, '&#96;');
}

function renderPositiveCase(p){
  const url = p.source_url || '';
  const tag = url ? 'a' : 'article';
  const linkAttrs = url ? ` href="${escapeAttr(url)}" target="_blank" rel="noopener"` : '';
  return `
    <${tag} class="positive-case${url ? ' positive-case-link' : ''}"${linkAttrs}>
      <div class="positive-top">
        <span class="positive-badge">${escapeHtml(p.source || '中青网')}</span>
        <span class="positive-id">${escapeHtml(p.case_id || '')}</span>
      </div>
      <div class="positive-name">${escapeHtml(p.case_name || '正向案例')}</div>
      <div class="positive-reason">${escapeHtml(p.relation_reason || '')}</div>
      ${url ? `<span class="positive-link">查看原文 ↗</span>` : ''}
    </${tag}>`;
}

function hideAlert(){
  if(els.alertBanner) els.alertBanner.className = 'alert-banner';
}

if(els.abClose) els.abClose.addEventListener('click', hideAlert);
