#!/usr/bin/env python3
"""同一把尺子量三个对象：基座 zero-shot / 微调后 / 本地 qwen2.5:7b。

用法:
  python eval_model.py --mode base                      # 基座 zero-shot
  python eval_model.py --mode lora --adapter ./adapters # 微调后
  python eval_model.py --mode ollama --model qwen2.5:7b # 本地已有模型对照
"""
import argparse, json, re, time
from pathlib import Path
from collections import Counter

D = Path(__file__).parent / "data"
LEVELS = ["V1", "V2", "V3", "V4"]
LV = {v: i for i, v in enumerate(LEVELS)}
SYS = "你是青少年意识形态风险研判专家。依据给定标签体系判定风险，只输出 JSON。"


def parse_json(raw: str) -> dict | None:
    """容错解析：剥 markdown 围栏 / 取第一个平衡括号对。"""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def gen_mlx(model, tok, prompt, max_tokens=320):
    from mlx_lm import generate
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}]
    text = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                   tokenize=False, enable_thinking=False)
    return generate(model, tok, prompt=text, max_tokens=max_tokens, verbose=False)


def gen_ollama(model_name, prompt):
    import urllib.request
    body = json.dumps({
        "model": model_name, "stream": False, "options": {"temperature": 0.1},
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request("http://localhost:11434/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["message"]["content"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["base", "lora", "ollama"], required=True)
    ap.add_argument("--model", default="mlx-community/Qwen3-8B-4bit")
    ap.add_argument("--adapter", default="./adapters")
    ap.add_argument("--tags-hint", action="store_true",
                    help="prompt 里附 19 个标签清单（zero-shot 必须给，否则不可能猜中）")
    args = ap.parse_args()

    gold = [json.loads(l) for l in (D / "gold.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    tags = sorted(json.loads((D / "sop_table.json").read_text(encoding="utf-8")))
    hint = ("\n\n【可选二级标签】" + "、".join(tags) +
            "\n【可选一级风险域】历史认知风险、制度认同风险、认知闭合风险、心理韧性风险、网络素养与舆论风险"
            "\n输出格式: {\"evidence\":[...],\"domain\":\"...\",\"tags\":[\"...\"],\"level\":\"V1|V2|V3|V4\",\"risk_desc\":\"...\"}"
            ) if args.tags_hint else ""

    if args.mode in ("base", "lora"):
        from mlx_lm import load
        kw = {"adapter_path": args.adapter} if args.mode == "lora" else {}
        print(f"加载 {args.model} {kw or '(基座)'} ...")
        model, tok = load(args.model, **kw)
        call = lambda p: gen_mlx(model, tok, p)
    else:
        call = lambda p: gen_ollama(args.model, p)

    ok_json = dom_hit = tag_hit = lvl_hit = lvl_1off = ev_ok = 0
    lat, preds = [], []
    for i, g in enumerate(gold, 1):
        t0 = time.time()
        try:
            raw = call(g["input"] + hint)
        except Exception as e:
            print(f"[{i}] 调用失败 {type(e).__name__}: {e}"); preds.append(None); continue
        lat.append(time.time() - t0)
        p = parse_json(raw)
        preds.append(p)
        if not p:
            print(f"[{i}] JSON 解析失败: {raw[:80]!r}"); continue
        ok_json += 1
        y = g["y"]
        dom_hit += p.get("domain") == y["domain"]
        tag_hit += bool(set(p.get("tags") or []) & set(y["tags"]))
        pl = p.get("level")
        lvl_hit += pl == y["level"]
        if pl in LV:
            lvl_1off += abs(LV[pl] - LV[y["level"]]) <= 1
        ev = p.get("evidence") or []
        src = g["input"]
        ev_ok += bool(ev) and all(isinstance(e, str) and e[:12] in src for e in ev)
        mark = "✓" if pl == y["level"] else "✗"
        print(f"[{i}/{len(gold)}] {mark} 域{'✓' if p.get('domain')==y['domain'] else '✗'} "
              f"标签{'✓' if set(p.get('tags') or [])&set(y['tags']) else '✗'} "
              f"等级 {pl}/{y['level']}  {lat[-1]:.1f}s")

    n = len(gold)
    print(f"\n{'='*46}\n模式={args.mode} 样本={n}  (needs_review={sum(g['needs_review'] for g in gold)})")
    print(f"JSON 合法率     {ok_json}/{n} = {ok_json/n:.0%}")
    print(f"一级域准确率     {dom_hit}/{n} = {dom_hit/n:.0%}")
    print(f"二级标签命中率   {tag_hit}/{n} = {tag_hit/n:.0%}")
    print(f"等级精确准确率   {lvl_hit}/{n} = {lvl_hit/n:.0%}")
    print(f"等级±1档准确率   {lvl_1off}/{n} = {lvl_1off/n:.0%}")
    print(f"证据可溯源率     {ev_ok}/{n} = {ev_ok/n:.0%}")
    if lat:
        lat.sort()
        print(f"延迟 中位 {lat[len(lat)//2]:.1f}s  P95 {lat[int(len(lat)*0.95)-1]:.1f}s")

    out = D / f"eval_{args.mode}.json"
    out.write_text(json.dumps({
        "mode": args.mode, "n": n,
        "json_ok": ok_json / n, "domain_acc": dom_hit / n, "tag_acc": tag_hit / n,
        "level_acc": lvl_hit / n, "level_1off": lvl_1off / n, "evidence_ok": ev_ok / n,
        "latency_median": lat[len(lat)//2] if lat else None,
        "preds": preds,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    main()
