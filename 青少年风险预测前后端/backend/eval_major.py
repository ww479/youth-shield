# -*- coding: utf-8 -*-
"""重大意识形态风险 M1–M11 检测评测（可复现，回归基线）。

评两个量，都用现成表，默认只跑免费的关键词评测：
  1) 召回（正样本）：t_major_risk_sample 每条单标签 gold=M_k，
     pred=set(major_lexicon.detect_major(text))，逐规则/总体召回。
  2) 误报（负样本）：t_corpus 低风险语料抽样，测 detect_major 误命中任意 M 的比例。

可选 --llm：每规则分层抽 N 条走生产同源的 gpt-5.5 判定（six_dim_scorer._score_hits_llm），
给出「词库 vs 大模型」召回对比。会调用海外接口、走代理、有耗时/开销，默认不跑。

用法：
  ../.venv/bin/python eval_major.py                 # 只跑关键词评测（默认）
  ../.venv/bin/python eval_major.py --neutral 3000  # 负样本抽样量
  ../.venv/bin/python eval_major.py --llm --llm-per-rule 10
"""
import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import mysql.connector
from major_lexicon import detect_major, MAJOR_RULE_NAMES

RULES = [f"M{i}" for i in range(1, 12)]


def _conn():
    db = dict(host=os.environ.get("DB_HOST", "127.0.0.1"),
              port=int(os.environ.get("DB_PORT", "3306")),
              user=os.environ.get("DB_USER", "root"),
              password=os.environ.get("DB_PASSWORD", ""),
              database=os.environ.get("DB_NAME", "youth_ideology"),
              charset="utf8mb4", use_pure=True)
    return mysql.connector.connect(**db)


def _pct(a, b):
    return f"{(100.0 * a / b):.1f}%" if b else "—"


def eval_recall_keyword(cur):
    """词库 detect_major 在全量 t_major_risk_sample 上的逐规则召回。"""
    cur.execute("SELECT rule_code, sample_text FROM t_major_risk_sample")
    rows = cur.fetchall()
    total = {m: 0 for m in RULES}
    hit = {m: 0 for m in RULES}     # gold 命中（gold ∈ pred）
    any_hit = 0                     # 任意 M 命中（不管对不对）
    for r in rows:
        gold = r["rule_code"]
        if gold not in total:
            continue
        pred = set(detect_major(r["sample_text"] or ""))
        total[gold] += 1
        if pred:
            any_hit += 1
        if gold in pred:
            hit[gold] += 1
    n = sum(total.values())
    print(f"\n== 召回（关键词词库 · 全量 {n} 条正样本）==")
    print("| 规则 | 名称 | 样本 | 召回(gold∈pred) |")
    print("|---|---|---|---|")
    for m in RULES:
        print(f"| {m} | {MAJOR_RULE_NAMES.get(m,'')[:16]} | {total[m]} | {_pct(hit[m], total[m])} |")
    print(f"| **总体** |  | **{n}** | **{_pct(sum(hit.values()), n)}** |")
    print(f"任意 M 命中率（含跨规则命中）：{_pct(any_hit, n)}")
    return {"n": n, "recall": sum(hit.values()) / n if n else 0}


def eval_fp_keyword(cur, neutral_n):
    """词库在 t_corpus 低风险语料上的误命中率（精度代理·中性误报）。"""
    cur.execute(
        "SELECT COALESCE(NULLIF(full_text,''), content_summary, title) t "
        "FROM t_corpus WHERE risk_level=%s "
        "ORDER BY corpus_id LIMIT %s", ("低风险", neutral_n))
    rows = cur.fetchall()
    n = 0
    fp = 0
    for r in rows:
        txt = (r["t"] or "").strip()
        if not txt:
            continue
        n += 1
        if detect_major(txt):
            fp += 1
    print(f"\n== 误报（关键词词库 · 低风险语料 {n} 条抽样）==")
    print(f"误命中任意 M 的比例（越低越好）：{_pct(fp, n)}  （{fp}/{n}）")
    return {"n": n, "fp_rate": fp / n if n else 0}


def eval_llm(cur, per_rule):
    """每规则分层抽样，走生产同源的 gpt-5.5 判定，对比词库 vs 大模型召回。"""
    import six_dim_scorer
    print(f"\n== LLM 对比（gpt-5.5 · 每规则抽 {per_rule} 条）==")
    print("| 规则 | 抽样 | 词库召回 | 大模型召回 |")
    print("|---|---|---|---|")
    kw_hit = kw_n = llm_hit = llm_n = 0
    for m in RULES:
        cur.execute(
            "SELECT sample_text FROM t_major_risk_sample WHERE rule_code=%s "
            "ORDER BY id LIMIT %s", (m, per_rule))
        texts = [r["sample_text"] for r in cur.fetchall() if r["sample_text"]]
        k = l = 0
        for txt in texts:
            if m in set(detect_major(txt)):
                k += 1
            res = six_dim_scorer._score_hits_llm(txt)
            if res is not None and m in set(res.get("major") or []):
                l += 1
        kw_hit += k; kw_n += len(texts); llm_hit += l; llm_n += len(texts)
        print(f"| {m} | {len(texts)} | {_pct(k, len(texts))} | {_pct(l, len(texts))} |")
        sys.stdout.flush()
    print(f"| **总体** | **{kw_n}** | **{_pct(kw_hit, kw_n)}** | **{_pct(llm_hit, llm_n)}** |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--neutral", type=int, default=3000, help="中性负样本抽样量（低风险语料）")
    ap.add_argument("--llm", action="store_true", help="额外跑 gpt-5.5 对比（走代理、有开销，默认不跑）")
    ap.add_argument("--llm-per-rule", type=int, default=10, help="LLM 模式每规则抽样条数")
    args = ap.parse_args()

    cn = _conn()
    cur = cn.cursor(dictionary=True)
    try:
        eval_recall_keyword(cur)
        eval_fp_keyword(cur, args.neutral)
        if args.llm:
            eval_llm(cur, args.llm_per_rule)
        else:
            print("\n(未加 --llm，跳过大模型对比。加 --llm 可跑 gpt-5.5 留出集对比。)")
    finally:
        cur.close()
        cn.close()


if __name__ == "__main__":
    main()
