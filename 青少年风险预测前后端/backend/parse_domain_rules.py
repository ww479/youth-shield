# -*- coding: utf-8 -*-
"""从《青少年五大意识形态风险域统一命中规则》docx 解析五大域的关键词簇 + 句式模板。
解析结果既可打印核对，也供 seed_domain_lexicon.py 灌库。"""
import os
import re

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

DOCX_PATH = os.environ.get(
    "UNIFIED_RULES_DOCX",
    r"C:\Users\29488\Desktop\项目\中青网项目\材料\命中规则\青少年五大意识形态风险域统一命中规则.docx",
)

DOMAIN_MAP = {
    "历史认知风险域": ("A", "historical_cognition", "历史认知"),
    "制度认同风险域": ("B", "institutional_identity", "制度认同"),
    "心理韧性风险域": ("C", "psychological_resilience", "心理韧性"),
    "网络素养风险域": ("D", "network_literacy", "网络素养"),
    "认知闭合风险域": ("E", "cognitive_closure", "认知闭合"),
}
_SPLIT = re.compile(r"[,，、;；\s]+")


def parse():
    doc = docx.Document(DOCX_PATH)
    domains = {}          # code -> {"id","name","clusters":{cluster:[kw]}, "templates":[(code,pattern,action)]}
    cur_domain = None     # code
    cur_sub = None        # 'kw' / 'tpl'
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            t = Paragraph(child, doc).text.strip()
            if not t:
                continue
            # 离开"五大风险域命中规则"章节后停止域采集，避免误收六维/等级表
            if ("六维" in t and "打分" in t) or t.startswith("统一判定流程"):
                cur_domain = None
                cur_sub = None
                continue
            for cn, (code, did, name) in DOMAIN_MAP.items():
                if t.startswith(cn):
                    cur_domain = code
                    domains.setdefault(code, {"id": did, "name": name, "code": code,
                                              "clusters": {}, "templates": []})
                    cur_sub = None
            if re.match(r"^[A-E]1\b", t) or "关键词簇" in t:
                cur_sub = "kw"
            elif re.match(r"^[A-E]2\b", t) or "核心句式模板" in t:
                cur_sub = "tpl"
            elif re.match(r"^[A-E]3\b", t) or "高危组合" in t:
                cur_sub = None
        elif child.tag.endswith("}tbl"):
            if not cur_domain or cur_sub is None:
                continue
            tbl = Table(child, doc)
            rows = [[c.text.strip() for c in r.cells] for r in tbl.rows]
            if not rows:
                continue
            body = rows[1:] if rows and ("词簇" in rows[0][0] or "编号" in rows[0][0]) else rows
            d = domains[cur_domain]
            if cur_sub == "kw":
                for r in body:
                    if len(r) >= 2 and r[0] and r[1]:
                        kws = [w for w in _SPLIT.split(r[1]) if w]
                        if kws:
                            d["clusters"][r[0]] = kws
                cur_sub = None
            elif cur_sub == "tpl":
                for r in body:
                    if len(r) >= 2 and r[0]:
                        code = r[0]
                        pattern = r[1] if len(r) > 1 else ""
                        action = r[2] if len(r) > 2 else ""
                        if code:
                            d["templates"].append((code, pattern, action))
                cur_sub = None
    return domains


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ds = parse()
    for code in ["A", "B", "C", "D", "E"]:
        d = ds.get(code)
        if not d:
            print(f"[{code}] 缺失"); continue
        nkw = sum(len(v) for v in d["clusters"].values())
        print(f"[{code}] {d['name']} | 词簇 {len(d['clusters'])} 组 / 关键词 {nkw} 个 | 句式模板 {len(d['templates'])} 条")
        for cl, kws in list(d["clusters"].items())[:2]:
            print(f"    词簇「{cl}」: {kws[:6]}{'…' if len(kws) > 6 else ''}")
        for code2, pat, act in d["templates"][:2]:
            print(f"    模板 {code2}: {pat[:40]} → {act}")
