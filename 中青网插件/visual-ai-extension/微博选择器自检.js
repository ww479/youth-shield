// ═══════════════════════════════════════════════════════════
// 微博提取器自检 —— 在微博详情页按 F12 打开 Console，粘贴运行
// 用法：打开 https://weibo.com/7624881855/QlqWAxghL 后运行
// 输出：每条选择器是否命中 + 最终提取结果
// ═══════════════════════════════════════════════════════════
(() => {
  const text = el => (el?.textContent || '').replace(/\s+/g, ' ').trim();
  const probe = (label, sel) => {
    const n = document.querySelectorAll(sel).length;
    const first = text(document.querySelector(sel)).slice(0, 60);
    console.log(`${n ? '✅' : '❌'} [${label}] ${sel}\n     命中 ${n} 个${first ? ` | "${first}…"` : ''}`);
    return n;
  };

  console.log('%c━━━ 正文选择器 ━━━', 'font-weight:bold;color:#3d6fd4');
  probe('详情页正文', '[class*="detail_wbtext"]');
  probe('移动端正文', '.weibo-text');
  probe('Feed 正文', '[class*="Feed_body"] [class*="detail_text"]');
  probe('旧版 node-type', '[node-type="feed_list_content"]');
  probe('旧版 WB_text', '.WB_text');
  probe('wbpro 正文', '[class*="wbpro-feed-content"]');

  console.log('%c━━━ 作者选择器 ━━━', 'font-weight:bold;color:#3d6fd4');
  probe('详情页昵称', '[class*="head_name"], [class*="head-info_name"]');
  probe('移动端昵称', '.weibo-og .m-text-box h3');
  probe('usercard 链接', 'a[class*="ALink_default"][usercard]');

  console.log('%c━━━ 互动数选择器 ━━━', 'font-weight:bold;color:#3d6fd4');
  probe('工具栏', '[class*="toolbar_main"], [class*="toolbar_wrap"], .WB_feed_handle');
  const tb = document.querySelector('[class*="toolbar_main"], [class*="toolbar_wrap"], .WB_feed_handle');
  if (tb) {
    const nums = Array.from(tb.querySelectorAll('[class*="toolbar_num"], [class*="toolbar_count"], em, .line'))
      .map(el => text(el)).filter(Boolean);
    console.log('     工具栏内文本:', nums);
  }

  console.log('%c━━━ 评论选择器 ━━━', 'font-weight:bold;color:#3d6fd4');
  probe('评论正文', '[class*="commentList"] [class*="text"], [class*="comment_wrap"] [class*="text"], .comment-list .card9 .txt');

  console.log('%c━━━ 若上面正文全部 ❌，运行这段找真实类名 ━━━', 'font-weight:bold;color:#d4763d');
  // 兜底探测：找页面上最长的文本块，打印它的类名链
  let best = null, bestLen = 0;
  document.querySelectorAll('div,p,span,article').forEach(el => {
    if (el.children.length > 3) return;                 // 只看叶子附近的节点
    const t = (el.innerText || '').trim();
    if (t.length > bestLen && t.length < 3000) { bestLen = t.length; best = el; }
  });
  if (best) {
    console.log('最长文本块:', bestLen, '字');
    console.log('  className:', best.className);
    console.log('  tagName  :', best.tagName);
    console.log('  祖先类名链:', (() => {
      const chain = []; let p = best;
      for (let i = 0; i < 5 && p; i++) { chain.push(`${p.tagName}.${p.className || '(无)'}`); p = p.parentElement; }
      return chain.join('  ←  ');
    })());
    console.log('  文本预览 :', bestLen > 0 ? best.innerText.slice(0, 200) : '');
  }
})();
