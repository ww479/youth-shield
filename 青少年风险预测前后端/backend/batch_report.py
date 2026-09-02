#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量视频风险研判 → 离线 HTML 报告包。

用法：
    # 先跑 3 个试样看形式
    python batch_report.py --limit 3
    # 全量
    python batch_report.py
    # 换目录 / 换输出位置
    python batch_report.py --src ~/Desktop/视频 --out ~/Desktop/研判报告

产物（可直接压缩发送，全程离线、无外部依赖）：
    <out>/index.html              总览：风险分布 + 按等级排序的视频清单
    <out>/reports/NN_xxx.html     每个视频一页独立报告
    <out>/data/summary.json       结构化结果
    <out>/data/summary.csv        Excel 可直接打开的汇总表
    <out>/frames/                 关键风险帧截图
    <out>/.state.json             断点续跑记录（已完成的跳过）

设计要点：
  · 断点续跑：每个视频完成即写入 .state.json，中断后重跑自动跳过
  · 失败不中断：单个视频出错记录在案，继续跑下一个
  · 不复制视频原件（30 个共 1GB），改为抽关键风险帧截图
  · 转写与研判复用线上同一套后端逻辑（直接 import，不走 HTTP）
"""
import argparse
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import transcribe_api as T          # 转写 + 逐句红线 + 同音纠正 + 幻觉过滤
import main as M                    # 六维研判 + 汇总 + 标签叙事 + 话术

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".ts", ".m4v"}
LV_NAME = {1: "低风险", 2: "轻度风险", 3: "中度风险", 4: "高风险", 5: "极高风险"}
LV_COLOR = {1: "#2e9e5b", 2: "#e0b400", 3: "#e67e22", 4: "#d64541", 5: "#8e44ad"}

# 文章模式：素材是文字稿，没有真实时间轴，位置一律用「第 N 句」。
# 由 render_report / render_index 的调用方通过 set_article_mode() 打开。
ARTICLE_MODE = False
_SENT_NO = {}


def set_article_mode(on: bool = True):
    global ARTICLE_MODE
    ARTICLE_MODE = bool(on)


def pos_label(sec) -> str:
    """位置标签：视频给 mm:ss，文章给「第 N 句」。"""
    if not ARTICLE_MODE:
        return fmt_time(sec)
    try:
        v = round(float(sec), 2)
    except Exception:
        v = 0.0
    n = _SENT_NO.get(v)
    if n is None and _SENT_NO:          # 证据锚点可能落在句中，取最近的前一句
        ks = [k for k in _SENT_NO if k <= v]
        n = _SENT_NO[max(ks)] if ks else 1
    return f"第 {n or 1} 句"


# ────────────────────────── 转写 ──────────────────────────

def transcribe(path: str, dur: float) -> list:
    """跑 whisper，返回 [{start,end,text,major,major_names,...}]。"""
    wav = os.path.join("/tmp", f"_batch_{os.getpid()}.wav")
    subprocess.run([T.FFMPEG_BIN, "-y", "-v", "error", "-i", path,
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
                   check=True, timeout=1800)
    try:
        prompt = T.build_prompt("")
        # run_whisper 内含幻觉重试：提示词本身会在部分素材上诱发幻觉
        # （整段复读"字幕志愿者XX"或只吐一个"Zither Harp"），此时自动去掉提示词重跑
        out = T.run_whisper(wav, prompt)

        rows = []
        asm = T.SentenceAssembler(lambda **e: rows.append(e), duration=dur, prompt=prompt)
        for ln in out.splitlines():
            m = T._SEG_RE.match(ln.strip())
            if m:
                g = m.groups()
                asm.feed(T._hms_to_sec(*g[0:4]), T._hms_to_sec(*g[4:8]), g[8] or "")
        asm.close()
        return [r for r in rows if r.get("type") == "segment"]
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def grab_frame(video: str, sec: float, dst: str) -> bool:
    """抽某一秒的画面做证据截图（报告里不放视频原件，用截图替代）。"""
    try:
        subprocess.run([T.FFMPEG_BIN, "-y", "-v", "error", "-ss", str(max(0, sec)),
                        "-i", video, "-frames:v", "1", "-vf", "scale=480:-2", dst],
                       check=True, timeout=60)
        return os.path.exists(dst)
    except Exception:
        return False


# ────────────────────────── HTML 渲染 ──────────────────────────

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def fmt_time(sec) -> str:
    try:
        sec = int(float(sec))
    except Exception:
        sec = 0
    return f"{sec // 60:02d}:{sec % 60:02d}"


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#eef2ff;color:#0f172a;
     line-height:1.7;padding:28px 20px 60px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:24px;letter-spacing:-.4px;margin-bottom:6px}
h2{font-size:17px;margin:26px 0 12px;padding-bottom:7px;border-bottom:2px solid #dbe3f5}
.sub{font-size:13px;color:#64748b;margin-bottom:20px}
.card{background:#fff;border:1px solid rgba(99,120,210,.16);border-radius:14px;
      padding:18px 20px;margin-bottom:14px;box-shadow:0 2px 10px rgba(15,23,42,.05)}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;
      border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(15,23,42,.05)}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eef1f7}
th{background:#f6f8fd;font-size:12px;color:#475569;font-weight:700}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbff}
.lv{display:inline-block;padding:3px 11px;border-radius:44px;color:#fff;
    font-size:12px;font-weight:700;white-space:nowrap}
.score{font-size:30px;font-weight:800;letter-spacing:-1px}
.pill{display:inline-block;font-size:11px;padding:2px 9px;border-radius:44px;margin:0 4px 4px 0;
      background:rgba(61,111,212,.08);color:#3d6fd4;border:1px solid rgba(61,111,212,.16)}
.pill.red{background:rgba(214,69,65,.1);color:#b91c1c;border-color:rgba(214,69,65,.28);font-weight:700}
.pill.dom{background:rgba(124,58,237,.08);color:#6d28d9;border-color:rgba(124,58,237,.2)}
.bar{height:22px;border-radius:6px;background:#e9edf7;position:relative;overflow:hidden;margin:8px 0 4px}
.bar i{position:absolute;top:0;bottom:0;display:block}
.ticks{display:flex;justify-content:space-between;font-size:10px;color:#94a3b8}
.seg{display:flex;gap:12px;padding:8px 6px;border-bottom:1px solid #f1f4fa;font-size:13.5px}
.seg:last-child{border-bottom:none}
.seg .t{color:#3d6fd4;font-weight:700;font-size:11px;min-width:46px;padding-top:3px;
        font-variant-numeric:tabular-nums}
.seg.hot{background:rgba(214,69,65,.05);border-radius:7px}
.tag{font-size:10px;font-weight:700;padding:1px 7px;border-radius:44px;margin-left:7px;
     background:rgba(214,69,65,.12);color:#b91c1c;white-space:nowrap}
.rl{background:rgba(214,69,65,.05);border:1px solid rgba(214,69,65,.22);
    border-radius:11px;padding:13px 15px;margin-bottom:10px}
.rl h4{font-size:13.5px;color:#7f1d1d;margin-bottom:8px}
.rl .q{font-size:13px;color:#334155;padding:5px 0 5px 10px;border-left:2px solid rgba(214,69,65,.3);
       margin-top:6px}
.dim{margin:9px 0}
.dim .top{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px}
.dim .track{height:6px;border-radius:99px;background:rgba(99,120,210,.12);overflow:hidden}
.dim .fill{height:100%;border-radius:99px}
.guide{background:rgba(16,185,129,.05);border:1px solid rgba(16,185,129,.22);
       border-radius:11px;padding:13px 15px;margin-bottom:10px}
.guide .age{font-size:12px;font-weight:700;color:#047857;margin-bottom:7px}
.guide .k{font-size:11px;color:#64748b;margin-top:8px}
.frame{max-width:100%;border-radius:9px;border:1px solid #dbe3f5;margin-top:8px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.kv{display:flex;gap:26px;flex-wrap:wrap;font-size:13px;color:#475569}
.kv b{color:#0f172a;font-size:15px}
a{color:#3d6fd4;text-decoration:none}
a:hover{text-decoration:underline}
.foot{margin-top:34px;font-size:11.5px;color:#94a3b8;line-height:1.9;
      border-top:1px solid #dbe3f5;padding-top:14px}
/* ── 六维判据 + 命中证据 ── */
.dimcard{background:#fff;border:1px solid rgba(99,120,210,.16);border-radius:13px;
         padding:14px 17px;margin-bottom:11px;box-shadow:0 2px 10px rgba(15,23,42,.05)}
.dimtop{display:flex;justify-content:space-between;align-items:baseline;
        padding-bottom:9px;margin-bottom:10px;border-bottom:1px solid #eef1f7}
.dimname{font-size:15px;font-weight:800;letter-spacing:-.2px}
.dimcnt{font-size:13px;font-weight:700}
.dimw{font-size:11px;color:#94a3b8;font-weight:400}
.item{margin:11px 0 13px}
.item:last-child{margin-bottom:2px}
.ih{font-size:13.5px;font-weight:700;color:#1e293b;padding-left:9px;
    border-left:3px solid #3d6fd4;margin-bottom:6px}
.q{font-size:13px;color:#334155;background:#f8faff;border-radius:8px;
   padding:7px 11px;margin:5px 0 0 12px}
.qt{display:inline-block;font-size:11px;font-weight:700;color:#3d6fd4;
    margin-right:7px;font-variant-numeric:tabular-nums}
.why{display:block;font-size:11.5px;color:#64748b;margin-top:4px}
.q.nomatch{color:#94a3b8;background:#f8fafc;font-size:12px}
.misshint{font-size:12px;color:#94a3b8;padding:9px 4px}
/* 折叠全文 */
.fold summary{cursor:pointer;font-size:13.5px;font-weight:700;color:#3d6fd4;
              padding:11px 16px;background:#fff;border-radius:11px;
              border:1px solid rgba(61,111,212,.2);list-style:none}
.fold summary::-webkit-details-marker{display:none}
.fold summary::before{content:"▸ ";font-size:12px}
.fold[open] summary::before{content:"▾ "}
.fold summary:hover{background:#f6f9ff}
@media print{body{background:#fff;padding:0}.card,table{box-shadow:none}
             .fold summary{display:none}.fold>div{display:block!important}}
"""


def render_report(item: dict, out_dir: str) -> str:
    if ARTICLE_MODE:                       # 供 pos_label() 把秒数换算为句号
        _SENT_NO.clear()
        for k, x in enumerate(item.get("sentences") or [], 1):
            _SENT_NO[round(float(x.get("start") or 0), 2)] = k
    """单个视频报告。聚焦两件事：① 解析出的原文　② 六维结果及其命中证据。"""
    s = item["summary"]
    sd = s.get("six_dim_detail") or {}
    lv = s.get("risk_level_num", 1)
    color = LV_COLOR.get(lv, "#2e9e5b")
    dur = item["duration"]
    hits = sum(d["hit_count"] for d in (sd.get("dimensions") or []))
    _ar = (sd.get("veto_rules") or []) + (sd.get("major_rules") or [])
    _rc = any(r.get("recheck") for r in _ar)
    nred = sum(1 for r in _ar if not _rc or r.get("recheck") == "已核实")
    hot_sents = [x for x in item["sentences"] if x.get("major")]

    # ── 证据按判据编号归拢：同一判据可能有多条原句支撑 ──
    ev_by_item = {}
    for e in (sd.get("evidence") or []):
        for d in (e.get("item_descs") or []):
            ev_by_item.setdefault(d["id"], []).append(e)

    # ── 六维：每个命中项列出它的原句证据（"为什么认为命中"）──
    dim_blocks = ""
    for d in (sd.get("dimensions") or []):
        rows = ""
        for it in (d.get("items") or []):
            if not it.get("hit"):
                continue
            evs = ev_by_item.get(it["id"]) or []
            quotes = "".join(
                f'<div class="q">'
                f'<span class="qt">{pos_label(e["at"]) if e.get("at") is not None else "—"}</span>'
                f'“{esc(e.get("span"))}”'
                + (f'<span class="why">{esc(e.get("note"))}</span>' if e.get("note") else "")
                + "</div>"
                for e in evs[:3]
            ) or '<div class="q nomatch">该判据由全段语境综合判定，无单句直接对应</div>'
            rows += (f'<div class="item"><div class="ih">{esc(it["desc"])}</div>{quotes}</div>')
        if not rows:
            continue
        dim_blocks += (
            f'<div class="dimcard"><div class="dimtop">'
            f'<span class="dimname">{esc(d["name"])}</span>'
            f'<span class="dimcnt" style="color:{color}">命中 {d["hit_count"]}/{d["total"]}'
            f'<span class="dimw"> · 权重 {int(d["weight"]*100)}%</span></span></div>'
            f'{rows}</div>'
        )
    if not dim_blocks:
        dim_blocks = '<div class="card" style="color:#64748b">未命中六维风险判据。</div>'

    # 未命中的维度概览（让"没命中什么"也一目了然）
    miss = " · ".join(f'{d["name"]} 0/6' for d in (sd.get("dimensions") or [])
                      if not d["hit_count"])
    if miss:
        dim_blocks += f'<div class="misshint">未涉及：{esc(miss)}</div>'

    # ── 风险红线：经模型复核后分组呈现 ──
    #   已核实 = 关键词命中且模型确认为作者本人主张，参与定级
    #   待复核 = 模型无法确认，或无对应语句，展示但不参与定级
    #   判不成立的（多为转述、引用、批驳他人观点）不再展示
    def _trig_html(r, show_verdict=False):
        out = []
        for t in (r.get("triggers") or [])[:4]:
            if show_verdict and t.get("recheck") == "不成立":
                continue
            vd = ""
            if show_verdict and t.get("recheck_reason"):
                vd = f'<span class="why">{esc(t["recheck_reason"])}</span>'
            out.append(f'<div class="q"><span class="qt">'
                       f'{pos_label(t["at"]) if t.get("at") is not None else "—"}</span>'
                       f'“{esc(t.get("span"))}”{vd}</div>')
        return "".join(out) or '<div class="q nomatch">由全段语境综合判定</div>'

    all_rules = (sd.get("veto_rules") or []) + (sd.get("major_rules") or [])
    rechecked = any(r.get("recheck") for r in all_rules)
    ok_rules = [r for r in all_rules
                if not rechecked or r.get("recheck") == "已核实"]
    pend_rules = [r for r in all_rules if rechecked and r.get("recheck") == "待复核"]
    drop_n = sum(1 for r in all_rules if rechecked and r.get("recheck") == "复核不成立")

    reds = ""
    for r in ok_rules:
        reds += (f'<div class="rl"><h4>{esc(r.get("name") or "")}'
                 f'{"：" if r.get("name") else ""}{esc(r.get("desc"))}</h4>'
                 f'{_trig_html(r, rechecked)}</div>')
    if not reds:
        reds = '<div class="card" style="color:#64748b">未发现经核实的风险红线。</div>'
    if pend_rules:
        reds += ('<div class="card" style="border-color:rgba(224,180,0,.4)">'
                 '<div style="font-size:13px;font-weight:700;color:#92650a;margin-bottom:8px">'
                 f'待复核 {len(pend_rules)} 项（模型未能确认，不计入等级判定）</div>' +
                 "".join(f'<div style="font-size:12.5px;color:#475569;padding:3px 0">'
                         f'· {esc(r.get("name") or r.get("id"))}'
                         f'{"：" + esc(r.get("recheck_note")) if r.get("recheck_note") else ""}</div>'
                         for r in pend_rules) + '</div>')
    if drop_n:
        reds += (f'<div class="misshint">另有 {drop_n} 项关键词命中经复核不成立'
                 f'（多为转述、引用或批驳他人观点），未计入。</div>')
    # 已核实为 0 但有待复核或被复核掉的条目时，该节仍需展示（说明复核发生过）
    _show_red = bool(ok_rules or pend_rules or drop_n)

    # ── 原文：风险句在前置摘要里，全文可折叠 ──
    hot_list = "".join(
        f'<div class="seg hot"><span class="t">{pos_label(x["start"])}</span>'
        f'<span>{esc(x["text"])}'
        f'<span class="tag">{esc("、".join(x.get("major_names") or []))}</span></span></div>'
        for x in hot_sents
    ) or '<div class="card" style="color:#64748b">无涉及重大风险的语句。</div>'

    full = "".join(
        f'<div class="seg{" hot" if x.get("major") else ""}">'
        f'<span class="t">{pos_label(x["start"])}</span><span>{esc(x["text"])}</span></div>'
        for x in item["sentences"]
    )

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>{esc(item['name'])} · 风险研判报告</title><style>{CSS}</style></head><body><div class="wrap">
<div class="sub"><a href="../index.html">← 返回总览</a></div>
<h1>{esc(item['name'])}</h1>
<div class="sub">{(str(item.get("char_count", 0)) + " 字") if ARTICLE_MODE else "时长 " + fmt_time(dur)}　·　{item['segment_count']} 句　·　分析时间 {esc(item['analyzed_at'])}</div>

<div class="card">
  <div style="display:flex;align-items:center;gap:24px;flex-wrap:wrap">
    <div class="lv" style="background:{color};font-size:15px;padding:6px 16px">
      {esc(s.get('risk_level'))}</div>
    <div class="kv">
      <div>风险特征 <b>{hits}</b> 项</div>
      <div>触及红线 <b>{nred}</b> 项</div>
      <div>风险语句 <b>{len(hot_sents)}</b> 句</div>
    </div>
  </div>
  {f'<div style="margin-top:12px;color:#334155;font-size:13.5px">{esc(sd.get("summary"))}</div>' if sd.get('summary') else ''}
  <div class="bar" style="margin-top:14px">{"".join(
      f'<i style="left:{(b["start"]/dur*100):.2f}%;width:{max(0.6,(b.get("end",b["start"])-b["start"])/dur*100):.2f}%;'
      f'background:{LV_COLOR.get(b["level"],"#2e9e5b")};opacity:{0.3 if b["level"]<=1 else 1}"></i>'
      for b in item["sentence_bands"] if dur > 0)}</div>
  <div class="ticks">{"".join(
      f'<span>{("第 " + str(max(1,int(item["segment_count"]*f))) + " 句") if ARTICLE_MODE else fmt_time(dur*f)}</span>'
      for f in (0,.25,.5,.75,1))}</div>
</div>

<h2>一、六维研判结果与命中证据</h2>
<div class="sub" style="margin:-4px 0 12px">每条判据下方为支撑它的原文语句及判定理由</div>
{dim_blocks}

{f'<h2>二、风险红线</h2>{reds}' if _show_red else ''}

<h2>{'三' if _show_red else '二'}、涉及风险的语句</h2>
{hot_list}

<h2>{'四' if _show_red else '三'}、{'文章原文' if ARTICLE_MODE else '视频解析原文'}（共 {item['segment_count']} 句）</h2>
<details class="fold"><summary>展开查看全文</summary>
<div class="card" style="margin-top:10px">{full}</div></details>

<div class="foot">
本报告由智能研判系统自动生成，结果供人工复核参考，不构成最终结论。<br>
风险等级：低（0–20）· 轻度（21–40）· 中度（41–60）· 高（61–80）· 极高（81–100）。
</div></div></body></html>"""


def render_index(items: list, failed: list, out_dir: str, src: str) -> str:
    """总览页：风险分布 + 按等级排序清单。"""
    cnt = {i: 0 for i in range(1, 6)}
    for it in items:
        cnt[it["summary"].get("risk_level_num", 1)] += 1
    total = len(items) or 1

    dist = "".join(
        f'<div style="flex:1;text-align:center">'
        f'<div style="font-size:26px;font-weight:800;color:{LV_COLOR[i]}">{cnt[i]}</div>'
        f'<div style="font-size:11.5px;color:#64748b">{LV_NAME[i]}</div></div>'
        for i in range(1, 6)
    )
    stack = "".join(
        f'<i style="width:{cnt[i]/total*100:.2f}%;background:{LV_COLOR[i]};position:static;'
        f'display:inline-block;height:22px"></i>'
        for i in range(1, 6) if cnt[i]
    )

    rows = ""
    for it in sorted(items, key=lambda x: (-x["summary"].get("risk_level_num", 1),
                                          -sum(d["hit_count"] for d in
                                               ((x["summary"].get("six_dim_detail") or {}).get("dimensions") or [])))):
        s = it["summary"]; sd = s.get("six_dim_detail") or {}
        lv = s.get("risk_level_num", 1)
        _ar = (sd.get("veto_rules") or []) + (sd.get("major_rules") or [])
        _rc = any(r.get("recheck") for r in _ar)
        nred = sum(1 for r in _ar if not _rc or r.get("recheck") == "已核实")
        doms = "、".join(d.get("name", "") for d in (sd.get("domains") or [])) or "—"
        rows += (f'<tr><td><a href="reports/{esc(it["report"])}">{esc(it["name"])}</a></td>'
                 f'<td>{it["segment_count"] if ARTICLE_MODE else fmt_time(it["duration"])}</td>'
                 f'<td><span class="lv" style="background:{LV_COLOR[lv]}">{LV_NAME[lv]}</span></td>'
                 f'<td style="font-weight:700;color:{LV_COLOR[lv]}">'
                 f'{sum(x["hit_count"] for x in (sd.get("dimensions") or []))}/36</td>'
                 f'<td>{nred or "—"}</td>'
                 f'<td style="font-size:12px;color:#475569">{esc(doms)}</td></tr>')

    fail_html = ""
    if failed:
        fail_html = ('<h2>未能完成的文件</h2><div class="card">' +
                     "".join(f'<div style="font-size:13px;color:#b91c1c">{esc(f["name"])} — '
                             f'{esc(f["error"])}</div>' for f in failed) + "</div>")

    hi = cnt[4] + cnt[5]
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>{"文章" if ARTICLE_MODE else "视频"}风险研判报告 · 总览</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{"文章" if ARTICLE_MODE else "视频"}意识形态风险研判报告</h1>
<div class="sub">{f"共 {len(items)} 篇文章　·　总字数 {sum(i.get('char_count',0) for i in items):,}"
   if ARTICLE_MODE else f"共 {len(items)} 个视频　·　总时长 {fmt_time(sum(i['duration'] for i in items))}"}
　·　生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="card">
  <div style="display:flex;gap:10px;margin-bottom:14px">{dist}</div>
  <div class="bar" style="margin:0">{stack}</div>
  <div style="margin-top:12px;font-size:13.5px;color:#334155">
    其中 <b style="color:#d64541">{hi}</b> {"篇文章" if ARTICLE_MODE else "个视频"}达到高风险及以上，建议优先复核。
  </div>
</div>

<h2>{"文章" if ARTICLE_MODE else "视频"}清单（按风险等级排序）</h2>
<table><thead><tr><th>{"文章" if ARTICLE_MODE else "视频"}</th><th>{"句数" if ARTICLE_MODE else "时长"}</th><th>风险等级</th><th>六维命中</th>
<th>红线</th><th>涉及风险类型</th></tr></thead><tbody>{rows}</tbody></table>

{fail_html}

<div class="foot">
本报告由智能研判系统自动生成，结果供人工复核参考，不构成最终结论。<br>
风险等级：低（0–20）· 轻度（21–40）· 中度（41–60）· 高（61–80）· 极高（81–100）。<br>
素材目录：{esc(src)}
</div></div></body></html>"""


# ────────────────────────── 主流程 ──────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/Desktop/视频"))
    ap.add_argument("--out", default=os.path.expanduser("~/Desktop/视频风险研判报告"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个（试样用）")
    ap.add_argument("--window-sec", type=float, default=150.0)
    ap.add_argument("--no-llm", action="store_true", help="只用规则，不调大模型（快但不准）")
    ap.add_argument("--fresh", action="store_true", help="忽略断点记录，全部重跑")
    args = ap.parse_args()

    src, out = args.src, args.out
    for d in ("reports", "data", "frames"):
        os.makedirs(os.path.join(out, d), exist_ok=True)

    state_f = os.path.join(out, ".state.json")
    state = {}
    if os.path.exists(state_f) and not args.fresh:
        try:
            state = json.load(open(state_f, encoding="utf-8"))
        except Exception:
            state = {}

    files = sorted(f for f in os.listdir(src)
                   if os.path.splitext(f)[1].lower() in VIDEO_EXT)
    if args.limit:
        files = files[:args.limit]
    print(f"素材目录：{src}\n待处理：{len(files)} 个（已完成 {len(state)} 个会跳过）\n")

    items, failed = [], []
    t_all = time.time()
    for idx, fn in enumerate(files, 1):
        path = os.path.join(src, fn)
        stem = os.path.splitext(fn)[0]
        if fn in state and not args.fresh:
            items.append(state[fn]); print(f"[{idx}/{len(files)}] 跳过（已完成）{stem[:34]}")
            continue

        print(f"[{idx}/{len(files)}] {stem[:40]}")
        t0 = time.time()
        try:
            dur = T._probe_duration(path)
            if dur <= 0:
                raise RuntimeError("无法读取媒体信息")

            segs = transcribe(path, dur)
            print(f"      转写 {len(segs)} 句 ({time.time()-t0:.0f}s)")
            if not segs:
                raise RuntimeError("未识别到有效人声")

            t1 = time.time()
            res = M.analyze_transcript_sync(
                [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs],
                "13-15", args.window_sec, not args.no_llm, True, stem)
            # 逐句红线用转写阶段已算好的（含同音纠正）
            res["sentences"] = segs
            print(f"      研判 {res['summary']['risk_level']} "
                  f"{res['summary']['risk_percent']}/100 ({time.time()-t1:.0f}s)")

            # 抽关键风险帧（取风险最高的前 2 句）
            frames = []
            hot = sorted((x for x in segs if x.get("major")),
                         key=lambda x: -len(x.get("major") or []))[:2]
            for k, hsent in enumerate(hot):
                fname = f"{idx:02d}_{k}.jpg"
                if grab_frame(path, hsent["start"] + 0.5,
                              os.path.join(out, "frames", fname)):
                    frames.append({"file": fname, "at": hsent["start"],
                                   "text": hsent["text"][:40]})

            item = {
                "name": stem, "file": fn, "duration": dur,
                "segment_count": len(segs),
                "report": f"{idx:02d}_{re.sub(r'[^\\w一-鿿]+', '_', stem)[:40]}.html",
                "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "summary": res["summary"], "sentences": segs,
                "sentence_bands": res["sentence_bands"],
                "labels": res.get("labels") or {},
                "guidance": res.get("guidance") or {},
                "frames": frames,
            }
            with open(os.path.join(out, "reports", item["report"]), "w", encoding="utf-8") as f:
                f.write(render_report(item, out))
            items.append(item)
            state[fn] = item
            json.dump(state, open(state_f, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"      ✓ 报告已生成（累计 {time.time()-t_all:.0f}s）")
        except Exception as e:
            print(f"      ✗ 失败：{e}")
            failed.append({"name": stem, "error": str(e)[:160]})

    if not items:
        print("\n没有成功处理的视频，未生成报告。")
        return

    # 总览 + 数据文件
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(items, failed, out, src))

    slim = [{
        **({"文章": i["name"], "字数": i.get("char_count", 0),
            "句数": i["segment_count"]} if ARTICLE_MODE else
           {"视频": i["name"], "时长秒": round(i["duration"], 1)}),
        "风险等级": i["summary"].get("risk_level"),
        "定级依据": (i["summary"].get("six_dim_detail") or {}).get("level_source") or "",
        "六维命中": sum(d["hit_count"] for d in
                              ((i["summary"].get("six_dim_detail") or {}).get("dimensions") or [])),
        "红线数": sum(1 for r in (((i["summary"].get("six_dim_detail") or {}).get("veto_rules") or [])
                                 + ((i["summary"].get("six_dim_detail") or {}).get("major_rules") or []))
                     if r.get("recheck") in (None, "已核实")),
        "词库命中句数": sum(1 for x in i["sentences"] if x.get("major")),
        "涉及风险类型": "、".join(d.get("name", "") for d in
                          ((i["summary"].get("six_dim_detail") or {}).get("domains") or [])),
        "结论摘要": (i["summary"].get("six_dim_detail") or {}).get("summary") or "",
    } for i in sorted(items, key=lambda x: -x["summary"].get("risk_level_num", 1))]

    json.dump({"generated_at": datetime.now().isoformat(), "source": src,
               "total": len(items), "items": slim},
              open(os.path.join(out, "data", "summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    # utf-8-sig：Excel 打开中文 CSV 不乱码
    with open(os.path.join(out, "data", "summary.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(slim[0].keys()))
        w.writeheader(); w.writerows(slim)

    print(f"\n{'='*54}\n完成 {len(items)} 个" + (f"，失败 {len(failed)} 个" if failed else "")
          + f"，总耗时 {(time.time()-t_all)/60:.1f} 分钟")
    print(f"报告目录：{out}\n打开：{os.path.join(out,'index.html')}")


if __name__ == "__main__":
    main()
