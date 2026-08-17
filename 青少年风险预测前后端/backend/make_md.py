#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从批量研判结果生成 MD 汇总文档（可导入飞书）。

每个视频三部分：
  一、视频解析原文
  二、六维命中情况（含 JSON 块，附 msg 字段）
  三、命中证据（区分「关键词命中」与「模型判断」）

用法：
    python make_md.py                       # 默认读 ~/Desktop/视频风险研判报告/.state.json
    python make_md.py --out 汇总.md
"""
import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import six_dim_scorer as SD

LV_NAME = {1: "低风险", 2: "轻度风险", 3: "中度风险", 4: "高风险", 5: "极高风险"}
# 编号 → 所属维度中文名（供 JSON 与表格标注）
ITEM_DIM = {iid: d["name"] for d in SD.DIMENSIONS for iid, _ in d["items"]}


def fmt_time(sec) -> float:
    try:
        return round(float(sec), 2)
    except Exception:
        return 0.0


def hms(sec) -> str:
    try:
        s = int(float(sec))
    except Exception:
        s = 0
    return f"{s//60:02d}:{s%60:02d}"


def attribute(iid: str, sentences: list, evidence: list) -> dict:
    """判断某个命中项的归因方式。

    优先关键词：命中了哪个词、在哪一句（可复现、可核对）。
    没有关键词命中的，说明是大模型的语义判断，此时用模型给出的证据原句。
    """
    words = SD._ITEM_KW.get(iid) or []
    for s in sentences:
        txt = s.get("text") or ""
        for w in words:
            if w and w in txt:
                return {"source": "keyword", "keyword": w,
                        "at": fmt_time(s.get("start")), "span": txt}
    # 模型判断：取该判据对应的模型证据
    for e in evidence or []:
        ids = [d["id"] for d in (e.get("item_descs") or [])] or (e.get("items") or [])
        if iid in ids:
            return {"source": "model", "keyword": None,
                    "at": fmt_time(e.get("at")) if e.get("at") is not None else None,
                    "span": e.get("span") or "", "note": e.get("note") or ""}
    return {"source": "model", "keyword": None, "at": None, "span": "", "note": ""}


def build_one(idx: int, it: dict) -> str:
    s = it["summary"]
    sd = s.get("six_dim_detail") or {}
    dims = sd.get("dimensions") or []
    ev = sd.get("evidence") or []
    sents = it["sentences"]
    lv = s.get("risk_level_num", 1)

    out = [f"\n\n---\n\n# {idx}. {it['name']}\n",
           f"- 时长：{hms(it['duration'])}（{it['duration']:.0f} 秒）　句数：{it['segment_count']}",
           f"- **风险等级：{s.get('risk_level')}（{lv} 级）　风险分：{s.get('risk_percent')}/100**",
           f"- 分析时间：{it.get('analyzed_at','')}"]
    if sd.get("summary"):
        out.append(f"- 结论摘要：{sd['summary']}")

    # ── 一、原文 ──
    out.append(f"\n## 一、视频解析原文\n")
    out.append("| 时间 | 原文 |")
    out.append("| --- | --- |")
    for x in sents:
        mark = "　🔺" if x.get("major") else ""
        txt = (x.get("text") or "").replace("|", "｜")
        out.append(f"| {hms(x['start'])} | {txt}{mark} |")
    out.append("\n> 🔺 标记为涉及重大风险的语句。")

    # ── 二、六维命中情况 ──
    out.append(f"\n## 二、六维命中情况（36 项）\n")
    total_hit = sum(d["hit_count"] for d in dims)
    out.append(f"共命中 **{total_hit}/36** 项。\n")
    out.append("| 维度 | 权重 | 命中 | 命中编号 |")
    out.append("| --- | --- | --- | --- |")
    for d in dims:
        ids = [i["id"] for i in (d.get("items") or []) if i.get("hit")]
        out.append(f"| {d['name']} | {int(d['weight']*100)}% | {d['hit_count']}/{d['total']} "
                   f"| {'、'.join(ids) if ids else '—'} |")

    out.append("\n**逐项明细**（✅=命中，空=未命中）\n")
    out.append("| 编号 | 维度 | 判据 | 命中 |")
    out.append("| --- | --- | --- | --- |")
    for d in dims:
        for i in (d.get("items") or []):
            out.append(f"| {i['id']} | {d['name']} | {i['desc']} | {'✅' if i.get('hit') else ''} |")

    # ── JSON 块 ──
    hit_items = []
    for d in dims:
        for i in (d.get("items") or []):
            if i.get("hit"):
                a = attribute(i["id"], sents, ev)
                hit_items.append({
                    "id": i["id"], "dimension": d["name"], "criterion": i["desc"],
                    "source": a["source"],
                    "keyword": a.get("keyword"),
                    "at": a.get("at"),
                    "span": a.get("span"),
                    "msg": (f"关键词「{a['keyword']}」命中于 {hms(a['at'])} 的语句：{a['span']}"
                            if a["source"] == "keyword" else
                            (f"模型判断：{a.get('note') or '基于全段语境'}"
                             + (f"（{hms(a['at'])}　{a['span']}）" if a.get("at") is not None
                                else (f"（{a['span']}）" if a.get("span") else "")))),
                })

    payload = {
        "video": it["name"],
        "duration_sec": round(it["duration"], 1),
        "risk_level": s.get("risk_level"),
        "risk_level_num": lv,
        "risk_percent": s.get("risk_percent"),
        "hit_count": total_hit,
        "dimensions": [{
            "name": d["name"], "weight": d["weight"],
            "hit_count": d["hit_count"], "total": d["total"],
            "score": d["score"],
            "hit_ids": [i["id"] for i in (d.get("items") or []) if i.get("hit")],
        } for d in dims],
        "hit_items": hit_items,
        "redlines": [{
            "id": r.get("id"), "name": r.get("name"), "desc": r.get("desc"),
            "level": r.get("level"),
            "triggers": [{"at": fmt_time(t["at"]) if t.get("at") is not None else None,
                          "span": t.get("span"),
                          "confirmed": bool(t.get("confirmed"))} for t in (r.get("triggers") or [])],
            "msg": (f"触及{'严重违规' if str(r.get('id','')).startswith('VETO') else '重大风险'}："
                    f"{r.get('name') or ''}{r.get('desc') or ''}"),
        } for r in (sd.get("veto_rules") or []) + (sd.get("major_rules") or [])],
        "msg": (f"《{it['name']}》时长 {hms(it['duration'])}，判定为{s.get('risk_level')}"
                f"（{s.get('risk_percent')}/100）；命中六维判据 {total_hit}/36 项，"
                f"触及红线 {len((sd.get('veto_rules') or []) + (sd.get('major_rules') or []))} 项；"
                f"其中关键词命中 {sum(1 for h in hit_items if h['source']=='keyword')} 项、"
                f"模型语义判断 {sum(1 for h in hit_items if h['source']=='model')} 项。"),
    }
    out.append("\n**结构化结果（JSON）**\n")
    out.append("```json")
    out.append(json.dumps(payload, ensure_ascii=False, indent=2))
    out.append("```")

    # ── 三、命中证据 ──
    out.append(f"\n## 三、命中证据\n")
    kw = [h for h in hit_items if h["source"] == "keyword"]
    md = [h for h in hit_items if h["source"] == "model"]
    out.append(f"关键词命中 **{len(kw)}** 项，模型语义判断 **{len(md)}** 项。\n")

    if kw:
        out.append("### 关键词命中（可直接核对原句）\n")
        out.append("| 编号 | 判据 | 命中关键词 | 时间 | 原句 |")
        out.append("| --- | --- | --- | --- | --- |")
        for h in kw:
            out.append(f"| {h['id']} | {h['criterion']} | **{h['keyword']}** "
                       f"| {hms(h['at'])} | {(h['span'] or '').replace('|','｜')} |")
    if md:
        out.append("\n### 模型语义判断（无关键词命中，由大模型依据语境判定）\n")
        out.append("| 编号 | 判据 | 时间 | 模型引用原文 | 判定理由 |")
        out.append("| --- | --- | --- | --- | --- |")
        for h in md:
            t = hms(h["at"]) if h["at"] is not None else "—"
            span = (h.get("span") or "—").replace("|", "｜")
            note = ""
            for e in ev:
                ids = [d["id"] for d in (e.get("item_descs") or [])] or (e.get("items") or [])
                if h["id"] in ids and e.get("note"):
                    note = e["note"]; break
            out.append(f"| {h['id']} | {h['criterion']} | {t} | {span} "
                       f"| {note.replace('|','｜') or '基于全段语境综合判定'} |")

    reds = (sd.get("veto_rules") or []) + (sd.get("major_rules") or [])
    if reds:
        out.append("\n### 触及的风险红线\n")
        for r in reds:
            kind = "严重违规" if str(r.get("id", "")).startswith("VETO") else "重大风险"
            out.append(f"**{kind}｜{r.get('name') or ''}"
                       f"{'：' if r.get('name') else ''}{r.get('desc')}**（{r.get('level')} 级）\n")
            for t in (r.get("triggers") or [])[:5]:
                tag = "已核" if t.get("confirmed") else "待复核"
                at = hms(t["at"]) if t.get("at") is not None else "—"
                out.append(f"- [{at}]（{tag}）{(t.get('span') or '').replace('|','｜')}")
            if not (r.get("triggers") or []):
                out.append("- 由全段语境综合判定，无单句直接对应")
            out.append("")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(pathlib.Path.home() /
                    "Desktop/视频风险研判报告/.state.json"))
    ap.add_argument("--out", default=str(pathlib.Path.home() /
                    "Desktop/视频风险研判报告/视频研判汇总.md"))
    args = ap.parse_args()

    st = json.load(open(args.state, encoding="utf-8"))
    items = sorted(st.values(), key=lambda x: -x["summary"].get("risk_level_num", 1))

    cnt = {}
    for it in items:
        k = it["summary"].get("risk_level")
        cnt[k] = cnt.get(k, 0) + 1

    head = [
        "# 视频意识形态风险研判 · 汇总",
        "",
        f"- 视频总数：**{len(items)}**　总时长：{hms(sum(i['duration'] for i in items))}",
        "- 风险分布：" + "　".join(f"{k} {v} 个" for k, v in
                              sorted(cnt.items(), key=lambda x: -x[1])),
        "",
        "每个视频包含三部分：**一、视频解析原文**　**二、六维命中情况（含结构化 JSON）**"
        "　**三、命中证据**（区分关键词命中与模型判断）。",
        "",
        "## 视频索引",
        "",
        "| # | 视频 | 时长 | 风险等级 | 风险分 | 命中 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, it in enumerate(items, 1):
        s = it["summary"]; sd = s.get("six_dim_detail") or {}
        n = sum(d["hit_count"] for d in (sd.get("dimensions") or []))
        head.append(f"| {i} | {it['name'].replace('|','｜')} | {hms(it['duration'])} "
                    f"| {s.get('risk_level')} | {s.get('risk_percent')} | {n}/36 |")

    body = "".join(build_one(i, it) for i, it in enumerate(items, 1))
    doc = "\n".join(head) + body + (
        "\n\n---\n\n> 本文档由智能研判系统自动生成，结果供人工复核参考，不构成最终结论。\n"
        "> 风险等级：低（0–20）· 轻度（21–40）· 中度（41–60）· 高（61–80）· 极高（81–100）。\n"
        "> 引用内容均来自公开传播的网络视频，涉及人物系其公开言论，仅用于风险研判示例。\n")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成：{args.out}")
    print(f"  视频 {len(items)} 个，文档 {len(doc)} 字符，{doc.count(chr(10))+1} 行")


if __name__ == "__main__":
    main()
