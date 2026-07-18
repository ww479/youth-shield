# -*- coding: utf-8 -*-
"""
用 hit_lexicon.yaml 自带的判例做离线评测：
把每条 example_sentence 喂给 run_nihilism_pipeline，比较预测的 risk_code
和文档标注的 risk_level 是否落在同一档，输出混淆矩阵和总体准确率。

用法：
    cd backend
    python -m rules.historical_nihilism.eval_scorer

六维判分默认走 OpenAI 兼容接口（gpt-5.5），单次约数秒，故用线程池并发跑，
并每完成若干条打印一次进度（带 flush，方便后台 tail 观察）。
"""
import concurrent.futures
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import nihilism_scorer as ns

LEVEL_TO_CODE = {
    "低风险": "L0", "轻度风险": "L1", "中度风险": "L2",
    "高风险": "L3", "极高风险": "L4",
}

CONCURRENCY = int(os.environ.get("EVAL_CONCURRENCY", "6"))


def run_eval():
    ns.load_rules()
    ns.init_semantic()   # 自建 embedder + 向量库，启用语义匹配（与线上一致）

    cases = []
    for cat in ns._LEXICON["categories"]:
        for sub in cat["subtypes"]:
            for ex in sub["judged_examples"]:
                if ex["example_sentence"]:
                    cases.append((LEVEL_TO_CODE[ex["risk_level"]], ex["example_sentence"]))

    total = len(cases)
    print(f"共 {total} 条判例，并发 {CONCURRENCY} 路，开始评测…", flush=True)

    done = {"n": 0}
    lock = threading.Lock()

    def eval_one(item):
        expected, sentence = item
        result = ns.run_nihilism_pipeline(sentence)   # 无 ai_client → 走 OpenAI 优先
        actual = result["risk_code"] if result else "L0"
        source = result["score_source"] if result else "none"
        with lock:
            done["n"] += 1
            if done["n"] % 20 == 0 or done["n"] == total:
                print(f"  进度 {done['n']}/{total}", flush=True)
        return expected, actual, source

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for r in pool.map(eval_one, cases):
            results.append(r)

    correct = under_judged = llm_used = llm_fallback = 0
    confusion = {}
    for expected, actual, source in results:
        correct += (actual == expected)
        if source == "llm":
            llm_used += 1
        elif source == "llm_fallback_heuristic":
            llm_fallback += 1
        if int(actual[1]) < int(expected[1]):
            under_judged += 1
        confusion[(expected, actual)] = confusion.get((expected, actual), 0) + 1

    print()
    print(f"总样本数: {total}")
    print(f"打分来源: 真实AI={llm_used}, 降级兜底={llm_fallback}")
    print(f"总体准确率: {correct}/{total} = {correct/total:.1%}")
    print(f"往轻了判的数量: {under_judged}/{total} = {under_judged/total:.1%}（需重点关注）")
    print("混淆矩阵（期望等级 -> 实际等级: 条数）：")
    for (exp, act), cnt in sorted(confusion.items()):
        marker = "match" if exp == act else "MISS"
        print(f"  [{marker}] 期望{exp} 实际{act}: {cnt} 条")


if __name__ == "__main__":
    run_eval()
