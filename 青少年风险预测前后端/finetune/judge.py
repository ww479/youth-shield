#!/usr/bin/env python3
"""风险研判 CLI —— 交互或单次，看微调模型的实际输出。

用法:
  ./.venv/bin/python judge.py                      # 交互模式，逐段粘贴
  ./.venv/bin/python judge.py -t "要判的一段话"      # 单次
  ./.venv/bin/python judge.py -t "..." --raw        # 只看模型原始 JSON，不查表
  ./.venv/bin/python judge.py -t "..." --base       # 不加 adapter（基座对照）
  ./.venv/bin/python judge.py -t "..." -a ./adapters/0000200_adapters.safetensors
  echo "一段话" | ./.venv/bin/python judge.py       # 管道
"""
import argparse, json, re, sys, time
from pathlib import Path

D = Path(__file__).parent / "data"
MODEL = "mlx-community/Qwen3-8B-4bit"
SYS = "你是青少年意识形态风险研判专家。依据给定标签体系判定风险，只输出 JSON。"
LEVEL_DESC = {"V1": "低风险", "V2": "中风险", "V3": "高风险", "V4": "极高风险"}
LEVEL_COLOR = {"V1": "\033[32m", "V2": "\033[33m", "V3": "\033[38;5;208m", "V4": "\033[31m"}
R, B, DIM = "\033[0m", "\033[1m", "\033[2m"


def build_input(text, platform="未知", path="未知", with_hint=False):
    p = (f"【内容】{text}\n【平台】{platform}\n【传播路径】{path}\n\n"
         f"请判定：一级风险域、二级标签、风险等级(V1-V4)、风险描述、判定证据。")
    if with_hint:      # 基座对照才需要，微调模型不需要
        tags = sorted(json.loads((D / "sop_table.json").read_text(encoding="utf-8")))
        p += ("\n\n【可选二级标签】" + "、".join(tags) +
              "\n【可选一级风险域】历史认知风险、制度认同风险、认知闭合风险、心理韧性风险、网络素养与舆论风险"
              "\n输出格式: {\"evidence\":[...],\"domain\":\"...\",\"tags\":[\"...\"],"
              "\"level\":\"V1|V2|V3|V4\",\"risk_desc\":\"...\"}")
    return p


def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def render(res, src_text, sop, show_sop=True, elapsed=None):
    lv = res.get("level", "?")
    c = LEVEL_COLOR.get(lv, "")
    print(f"\n{B}{'─'*66}{R}")
    print(f"  {c}{B}【{lv} {LEVEL_DESC.get(lv,'?')}】{R}  "
          f"{res.get('domain','?')}  ›  {B}{'、'.join(res.get('tags') or ['?'])}{R}")
    print(f"{B}{'─'*66}{R}")

    ev = res.get("evidence") or []
    if ev:
        print(f"\n  {B}证据链{R}" + f"{DIM}（✓=原文可溯源）{R}")
        for e in ev:
            ok = isinstance(e, str) and e[:12] in src_text
            print(f"    {'✓' if ok else '✗'} {e}")

    if res.get("risk_desc"):
        print(f"\n  {B}风险描述{R}\n    {res['risk_desc']}")

    if show_sop:
        tag = (res.get("tags") or [None])[0]
        entry = sop.get(tag)
        if not entry:
            print(f"\n  {DIM}（SOP 查表未命中标签 {tag!r}）{R}")
        else:
            print(f"\n{DIM}  ── 以下由 SOP 查表填充，非模型生成 ──{R}")
            for key, label in [("disposal_platform", "平台处置"), ("guide_6_12", "6-12岁引导"),
                               ("guide_13_15", "13-15岁引导"), ("guide_16_18", "16-18岁引导"),
                               ("script", "引导话术"), ("review", "复盘结论")]:
                pool = entry.get(key) or []
                if pool:
                    txt = pool[0].replace("\n", "\n" + " " * 6)
                    more = f" {DIM}(+{len(pool)-1} 备选){R}" if len(pool) > 1 else ""
                    print(f"\n  {B}{label}{R}{more}\n      {txt}")
    if elapsed:
        print(f"\n{DIM}  {elapsed:.1f}s{R}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-t", "--text", help="要判定的内容；省略则进入交互/读 stdin")
    ap.add_argument("-p", "--platform", default="未知")
    ap.add_argument("--path", default="未知", help="传播路径")
    ap.add_argument("-a", "--adapter", default="./adapters", help="adapter 目录或 .safetensors")
    ap.add_argument("--base", action="store_true", help="不加 adapter，用基座对照")
    ap.add_argument("--raw", action="store_true", help="只打印模型原始 JSON")
    ap.add_argument("--max-tokens", type=int, default=360)
    args = ap.parse_args()

    sop = json.loads((D / "sop_table.json").read_text(encoding="utf-8"))
    from mlx_lm import load, generate
    kw = {} if args.base else {"adapter_path": args.adapter}
    tag = "基座 zero-shot" if args.base else f"LoRA {args.adapter}"
    print(f"{DIM}加载 {MODEL}  [{tag}] ...{R}", file=sys.stderr)
    model, tok = load(MODEL, **kw)

    def judge(text):
        prompt = build_input(text, args.platform, args.path, with_hint=args.base)
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}]
        chat = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                       tokenize=False, enable_thinking=False)
        t0 = time.time()
        raw = generate(model, tok, prompt=chat, max_tokens=args.max_tokens, verbose=False)
        el = time.time() - t0
        if args.raw:
            print(raw.strip()); print(f"{DIM}{el:.1f}s{R}"); return
        res = parse_json(raw)
        if not res:
            print(f"\n  ✗ JSON 解析失败，原始输出：\n{raw[:400]}\n"); return
        render(res, text, sop, show_sop=not args.raw, elapsed=el)

    if args.text:
        judge(args.text); return
    if not sys.stdin.isatty():
        t = sys.stdin.read().strip()
        if t:
            judge(t)
        return

    print(f"{DIM}交互模式：粘贴一段话回车判定，空行或 Ctrl-D 退出{R}")
    while True:
        try:
            t = input(f"\n{B}> {R}").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not t:
            break
        judge(t)


if __name__ == "__main__":
    main()
