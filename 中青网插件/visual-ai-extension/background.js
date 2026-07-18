chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if(message?.type === 'RISK_REVIEW_EXPORT'){
    handleExport(message.payload)
      .then(sendResponse)
      .catch(err => sendResponse({ok:false, error:String(err?.message || err)}));
    return true;
  }
  if(message?.type === 'RISK_REVIEW_ANALYZE_STREAM'){
    handleAnalyzeStream(message.payload, _sender?.tab?.id);
    sendResponse({ok:true});   // 立即应答；分析事件通过 tabs.sendMessage 逐条推回
    return true;
  }
  if(message?.type !== 'RISK_REVIEW_ANALYZE') return false;
  handleAnalyze(message.payload)
    .then(sendResponse)
    .catch(err => sendResponse({ok:false, error:String(err?.message || err)}));
  return true;
});

// 由插件提取的 source 拼出发给后端的 page_text（作者+正文/热评）。
function buildPageText(source){
  source = source || {};
  const parts = [];
  if(source.author) parts.push(`作者：${source.author}`);
  if(source.summary) parts.push(source.summary);
  else if(source.title) parts.push(source.title);
  return parts.join('。 ').slice(0, 4000);
}

// 流式分析：读 /analyze/stream 的 NDJSON，逐条事件用 tabs.sendMessage 推回内容脚本。
async function handleAnalyzeStream(payload, tabId){
  const source = payload?.source || {};
  const endpoint = payload?.endpoint || 'http://localhost:8000/analyze/stream';
  const push = ev => { if(tabId != null) chrome.tabs.sendMessage(tabId, {type:'RISK_REVIEW_STREAM', payload:ev}).catch(() => {}); };
  if(!/^https?:\/\//i.test(source.url || '')){ push({event:'error', error:'仅支持 http(s) 页面'}); return; }
  try{
    const res = await fetch(endpoint, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ url: source.url, age_group: payload?.age_group || '13-15',
        no_cache: true, page_text: buildPageText(source) })
    });
    if(!res.ok || !res.body){ push({event:'error', error:'HTTP ' + res.status}); return; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    const flushLines = () => {
      let idx;
      while((idx = buf.indexOf('\n')) >= 0){
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if(line){ try{ push(JSON.parse(line)); }catch(_e){} }
      }
    };
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream:true});
      flushLines();
    }
    buf += decoder.decode();
    flushLines();
    const rest = buf.trim();
    if(rest){ try{ push(JSON.parse(rest)); }catch(_e){} }
  }catch(err){
    push({event:'error', error:String(err?.message || err)});
  }
}

async function handleExport(payload){
  const url = payload?.url || '';
  const endpoint = payload?.endpoint || 'http://localhost:8000/report/nihilism.docx';
  if(!/^https?:\/\//i.test(url)) throw new Error('仅支持对 http(s) 页面导出报告');
  const res = await fetch(endpoint, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    // 带上与 analyze 相同的 page_text，命中同一条缓存、拿到同样的六维结果
    body:JSON.stringify({url, age_group: payload?.age_group || '13-15', page_text: buildPageText(payload?.source)})
  });
  if(!res.ok) throw new Error(await res.text());
  const buf = await res.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = '';
  const CHUNK = 0x8000;
  for(let i = 0; i < bytes.length; i += CHUNK){
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  const b64 = btoa(binary);
  const cd = res.headers.get('Content-Disposition') || '';
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  const filename = m ? decodeURIComponent(m[1]) : '历史虚无评估报告.docx';
  return {ok:true, b64, filename};
}

chrome.action.onClicked.addListener(async tab => {
  if(!tab?.id) return;
  await chrome.storage.local.set({riskReviewPinned:true});
  await ensureContentScript(tab.id);
  await chrome.tabs.sendMessage(tab.id, {
    type:'RISK_REVIEW_PIN_OPEN',
    payload:{
      fresh:true,   // 点扩展图标：清空旧数据 + 绕开后端缓存重新分析
      source:{
        url: tab.url || '',
        title: tab.title || '',
        summary: '',
      }
    }
  });
});

async function ensureContentScript(tabId){
  try{
    await chrome.tabs.sendMessage(tabId, {type:'RISK_REVIEW_PING'});
  }catch(_err){
    await chrome.scripting.executeScript({target:{tabId}, files:['content.js']});
    await chrome.scripting.insertCSS({target:{tabId}, files:['content.css']});
  }
}

async function handleAnalyze(payload){
  if(payload.useDemoOnly){
    return {ok:true, fallback:true, result:buildFallbackResult(payload)};
  }

  const source = payload.source || {};
  const endpoint = payload.endpoint || 'http://localhost:8000/analyze';
  if(!/^https?:\/\//i.test(source.url || '')){
    return {ok:true, fallback:true, result:buildFallbackResult(payload)};
  }

  try{
    // 后端 AnalyzeRequest 用 page_text 接收插件直接提取的页面文本，有值即跳过服务端爬虫
    //（抖音等 SPA 页后端按 URL 爬不到，必须靠插件传）。其余字段后端未读取也不报错。
    const pageText = buildPageText(source);

    const res = await fetch(endpoint, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        url: source.url,
        age_group: payload.age_group || '13-15',
        no_cache: !!payload.no_cache,
        page_text: pageText,
        platform: source.platform || '',
        author: source.author || '',
        interaction: source.interaction || null,
        hot_comments: source.comments || [],
      })
    });
    if(!res.ok) throw new Error(await res.text());
    const result = await res.json();
    return {ok:true, fallback:false, result};
  }catch(_err){
    return {ok:true, fallback:true, result:buildFallbackResult(payload)};
  }
}

function buildFallbackResult(payload){
  const source = payload.source || {};
  const summary = source.summary || '当前页面内容已作为分析对象，插件将展示内容预览、识别标签、风险评级、叙事分析和关联案例。';
  const title = source.title || shortUrl(source.url) || '当前页面内容';
  const isFile = String(source.url || '').startsWith('file:');
  const isLocalDemo = /青年心盾|价值观审查|frontend\/index\.html/i.test(`${title} ${source.url}`);

  // 走兜底时也尽量用页面真实提取值（作者/互动），仅在缺失时回退到演示占位
  const hasRealInteraction = source.interaction && (source.interaction.total || source.interaction.like || source.interaction.comment || source.interaction.share);
  const author = source.author || (isFile ? '本地页面' : '当前账号');
  const interaction = hasRealInteraction ? source.interaction : {like: 12000, comment: 1840, share: 620};

  return {
    source: 'extension-demo',
    url: source.url || '',
    platform: source.platform || detectPlatform(source.url, title),
    title: isLocalDemo ? '内容风险分析演示页' : title,
    author,
    content_summary: isLocalDemo
      ? '页面用于展示内容风险识别、标签匹配、风险评级、叙事分析与关联案例能力。'
      : summary.slice(0, 220),
    matched_tags: isLocalDemo ? [
      {keyword:'庐山会议', domain:'历史认知风险', l2_name:'近现代史虚无化'},
      {keyword:'七千人大会', domain:'历史认知风险', l2_name:'历史事件再叙事'},
      {keyword:'历史认知', domain:'历史认知风险', l2_name:'叙事框架偏移'}
    ] : [
      {keyword:'当前页面', domain:'内容风险研判', l2_name:'页面上下文识别'},
      {keyword:'传播影响', domain:'内容风险研判', l2_name:'扩散风险评估'},
      {keyword:'评论语境', domain:'网络素养风险', l2_name:'情绪与立场识别'}
    ],
    risk_level: isLocalDemo ? 'L4' : 'L3',
    risk_label: isLocalDemo ? '极高风险' : '高风险',
    composite_score: isLocalDemo ? 3.8 : 3.1,
    primary_domain: isLocalDemo ? '历史认知风险' : '内容风险研判',
    interaction,
    narrative: {
      summary: isLocalDemo
        ? '该内容围绕历史事件与人物评价展开，容易通过片段化表述引导受众形成单一化历史理解，需要结合史实语境、资料来源和表达意图进行复核。'
        : '该页面已被插件作为当前分析对象，建议结合内容来源、标题语义、评论氛围和传播数据判断潜在风险。当前结果为插件演示版，后续可接入真实数据库和模型分析结果。'
    },
    related_cases: buildDemoCases(isLocalDemo),
    positive_cases: buildPositiveCases(isLocalDemo),
  };
}

function buildPositiveCases(isLocalDemo){
  if(isLocalDemo){
    return [
      {
        case_id:'P-101',
        case_name:'中青网《铭记历史 缅怀先烈》专题报道',
        source:'中青网',
        relation_reason:'同为历史认知类议题，提供权威史实梳理和主流历史叙事参考。',
        source_url:'https://www.youth.cn/'
      },
      {
        case_id:'P-102',
        case_name:'中青网青年说：英烈精神代代传',
        source:'中青网',
        relation_reason:'针对英烈类议题的正向引导案例，可用于对比参考。',
        source_url:'https://www.youth.cn/'
      }
    ];
  }
  return [
    {
      case_id:'P-201',
      case_name:'中青网理性发声引导青年网络文明',
      source:'中青网',
      relation_reason:'与当前页面同属网络素养议题，展示正向舆论引导的表达方式。',
      source_url:'https://www.youth.cn/'
    }
  ];
}

function buildDemoCases(isLocalDemo){
  if(isLocalDemo){
    return [
      {
        case_id:'L2-002',
        case_name:'邓某杰贬低中共抗战地位案',
        risk_domain:'历史认知风险',
        risk_type:'近现代史虚无化、旧时代美化和反向叙事',
        risk_level:'L3',
        representative_text:'网民围绕抗战史和历史人物评价进行片段化表达，弱化主流历史叙事和历史共识。',
        total_interaction:14200,
        source_url:''
      },
      {
        case_id:'L2-001',
        case_name:'吹捧蒋介石、捏造“共军只毙敌851人”',
        risk_domain:'历史认知风险',
        risk_type:'近现代史虚无化、旧时代美化和反向叙事',
        risk_level:'L4',
        representative_text:'网络舆论场出现“翻案史学”式表述，以片段数据否定整体历史贡献。',
        total_interaction:23600,
        source_url:''
      },
      {
        case_id:'L1-001',
        case_name:'罗昌平侮辱“冰雕连”英烈案',
        risk_domain:'历史认知风险',
        risk_type:'英烈亵渎 / 娱乐化',
        risk_level:'L4',
        representative_text:'使用社交账号发布不当内容，戏谑、侮辱英烈，引发公众强烈愤慨。',
        total_interaction:22000,
        source_url:''
      },
      {
        case_id:'L1-003',
        case_name:'网民侮辱陈尔晋、王曼霞烈士案',
        risk_domain:'历史认知风险',
        risk_type:'英烈亵渎 / 娱乐化',
        risk_level:'L3',
        representative_text:'在短视频平台发布不当言论，造成不良影响。',
        total_interaction:9800,
        source_url:''
      }
    ];
  }
  return [
    {
      case_id:'C1-001',
      case_name:'热点内容评论区情绪对立扩散',
      risk_domain:'网络素养风险',
      risk_type:'情绪感染与圈层煽动',
      risk_level:'L3',
      representative_text:'评论区围绕争议议题出现标签化表达和对立情绪，带动二次传播。',
      total_interaction:12800,
      source_url:''
    },
    {
      case_id:'C1-002',
      case_name:'短视频内容片段化误读传播',
      risk_domain:'内容风险研判',
      risk_type:'语境缺失与误读扩散',
      risk_level:'L2',
      representative_text:'短视频切片缺少上下文，容易造成片面理解和二次加工传播。',
      total_interaction:7600,
      source_url:''
    }
  ];
}

function detectPlatform(url, title){
  const text = `${url || ''} ${title || ''}`.toLowerCase();
  if(text.includes('douyin')) return '抖音';
  if(text.includes('weibo')) return '微博';
  if(text.includes('bilibili') || text.includes('b23.tv')) return 'B站';
  if(text.includes('xiaohongshu')) return '小红书';
  if(text.includes('zhihu')) return '知乎';
  if(text.includes('file:')) return '本地页面';
  return '当前页面';
}

function shortUrl(url){
  try{
    const parsed = new URL(url);
    if(parsed.protocol === 'file:') return decodeURIComponent(parsed.pathname.split('/').pop() || '本地页面');
    return parsed.hostname;
  }catch(_err){
    return url || '';
  }
}
