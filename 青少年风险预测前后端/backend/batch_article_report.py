#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量文章风险研判 → 离线 HTML 报告包。

与 batch_report.py（视频版）的唯一差别：素材是 .docx 文章，没有音频转写环节，
改为「读 docx → 按标点切句 → 用字符位置当伪时间轴」，之后的六维研判、
红线归因、标签叙事、话术生成、报告渲染全部复用同一套逻辑。

用法：
    python batch_article_report.py --limit 3          # 先跑 3 篇看形式
    python batch_article_report.py                    # 全量
    python batch_article_report.py --src <目录> --out <输出目录>

产物：
    <out>/index.html              总览
    <out>/reports/NN_xxx.html     每篇一页
    <out>/data/summary.{json,csv} 结构化结果
    <out>/.state.json             断点续跑
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import batch_report as B            # 复用报告渲染、CSV 导出
import main as M                    # 六维研判 + 汇总 + 标签叙事 + 话术
import major_lexicon as ML          # 逐句红线

SENT_END = "。！？!?…；;"
MIN_SENT = 6                        # 短于此长度的碎片并入上一句
CHARS_PER_SEC = 5.0                 # 伪时间轴：按每秒 5 字折算，仅用于定位与排序


def read_docx(path: str) -> str:
    """取 docx 全部正文（含表格），拼成一段文本。"""
    import docx
    d = docx.Document(path)
    parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            for c in row.cells:
                s = c.text.strip()
                if s:
                    parts.append(s)
    return "\n".join(parts)


def split_sentences(text: str) -> list:
    """按句末标点切句，保留标点；过短碎片并入上一句。

    输出结构与转写 segments 一致：{start, end, text, major}
    start/end 是按字数折算的伪秒数 —— 报告里用于定位与排序，不代表真实时间。
    """
    raw, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            s = buf.strip()
            if s:
                raw.append(s)
            buf = ""
    if buf.strip():
        raw.append(buf.strip())

    merged = []
    for s in raw:
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue
        if merged and len(s) < MIN_SENT:
            merged[-1] += s
        else:
            merged.append(s)

    segs, pos = [], 0
    for s in merged:
        start = pos / CHARS_PER_SEC
        pos += len(s)
        segs.append({
            "start": round(start, 2),
            "end": round(pos / CHARS_PER_SEC, 2),
            "text": s,
            "major": [],
        })
    return segs


def mark_redlines(segs: list) -> None:
    """逐句跑重大风险词库，就地写入 major 字段（与转写阶段同一逻辑）。"""
    for s in segs:
        try:
            hits = ML.detect_major(s["text"]) or []
        except Exception:
            hits = []
        ids = []
        for h in hits:
            hid = h.get("id") if isinstance(h, dict) else h
            if hid and hid not in ids:
                ids.append(hid)
        s["major"] = ids


def main():
    default_src = ("/Users/wangshuai/Library/Containers/com.tencent.xinWeChat/Data/"
                   "Documents/xwechat_files/wxid_1vn16raw7d8822_4053/msg/file/"
                   "2026-08/意识形态文章/意识形态文章")
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=default_src)
    ap.add_argument("--out", default=os.path.expanduser("~/Desktop/文章风险研判报告"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--window-sec", type=float, default=150.0)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    src, out = args.src, args.out
    for d in ("reports", "data"):
        os.makedirs(os.path.join(out, d), exist_ok=True)

    state_f = os.path.join(out, ".state.json")
    state = {}
    if os.path.exists(state_f) and not args.fresh:
        try:
            state = json.load(open(state_f, encoding="utf-8"))
        except Exception:
            state = {}

    files = sorted(f for f in os.listdir(src)
                   if f.lower().endswith(".docx") and not f.startswith("~$"))
    if args.limit:
        files = files[:args.limit]
    print(f"素材目录：{src}\n待处理：{len(files)} 篇（已完成 {len(state)} 篇会跳过）\n")

    items, failed = [], []
    t_all = time.time()
    for idx, fn in enumerate(files, 1):
        path = os.path.join(src, fn)
        stem = os.path.splitext(fn)[0]
        if fn in state and not args.fresh:
            items.append(state[fn])
            print(f"[{idx}/{len(files)}] 跳过（已完成）{stem[:34]}")
            continue

        print(f"[{idx}/{len(files)}] {stem[:44]}")
        t0 = time.time()
        try:
            text = read_docx(path)
            if len(text) < 50:
                raise RuntimeError(f"正文过短（{len(text)} 字），可能不是文章")

            segs = split_sentences(text)
            if not segs:
                raise RuntimeError("未切出有效句子")
            mark_redlines(segs)
            n_major = sum(1 for s in segs if s["major"])
            print(f"      解析 {len(text)} 字 / {len(segs)} 句"
                  f"（红线句 {n_major}）({time.time()-t0:.0f}s)")

            t1 = time.time()
            res = M.analyze_transcript_sync(
                [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs],
                "13-15", args.window_sec, not args.no_llm, True, stem)
            res["sentences"] = segs
            print(f"      研判 {res['summary']['risk_level']} "
                  f"{res['summary']['risk_percent']}/100 ({time.time()-t1:.0f}s)")

            item = {
                "name": stem, "file": fn,
                "duration": segs[-1]["end"],          # 伪时长，供渲染排序
                "char_count": len(text),
                "segment_count": len(segs),
                "report": f"{idx:02d}_{re.sub(r'[^\w一-鿿]+', '_', stem)[:40]}.html",
                "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "summary": res["summary"], "sentences": segs,
                "sentence_bands": res["sentence_bands"],
                "labels": res.get("labels") or {},
                "guidance": res.get("guidance") or {},
                "frames": [],                          # 文章无截图
            }
            with open(os.path.join(out, "reports", item["report"]),
                      "w", encoding="utf-8") as f:
                f.write(B.render_report(item, out))
            items.append(item)
            state[fn] = item
            with open(state_f, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception as e:
            print(f"      失败：{e}")
            failed.append({"name": stem, "file": fn, "error": str(e)})

    if items:
        with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
            f.write(B.render_index(items, failed, out, src))
        with open(os.path.join(out, "data", "summary.json"), "w", encoding="utf-8") as f:
            json.dump({"items": items, "failed": failed}, f, ensure_ascii=False, indent=2)

    print(f"\n完成 {len(items)} 篇，失败 {len(failed)} 篇，"
          f"总耗时 {(time.time()-t_all)/60:.1f} 分钟")
    print(f"报告目录：{out}")


if __name__ == "__main__":
    main()
