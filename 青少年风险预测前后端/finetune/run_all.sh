#!/usr/bin/env bash
# 150 条数据 · Qwen3-8B-4bit · MLX LoRA 全流程
# 用法: ./run_all.sh [步骤]   步骤: prep|base|train|eval|compare|all
set -euo pipefail
cd "$(dirname "$0")"
PY=./.venv/bin/python
export HF_HUB_OFFLINE=1          # 模型已下载，训练时不再联网

step="${1:-all}"

if [[ "$step" == "prep" || "$step" == "all" ]]; then
  echo "▶ [1/5] 数据清洗 + 切分"
  $PY prep_data.py
fi

if [[ "$step" == "base" || "$step" == "all" ]]; then
  echo "▶ [2/5] 微调前基线（Qwen3-8B-4bit zero-shot）"
  $PY eval_model.py --mode base --tags-hint
fi

if [[ "$step" == "train" || "$step" == "all" ]]; then
  echo "▶ [3/5] LoRA 训练"
  $PY -m mlx_lm lora --config lora_config.yaml 2>&1 | tee train.log
fi

if [[ "$step" == "eval" || "$step" == "all" ]]; then
  echo "▶ [4/5] 微调后评测"
  $PY eval_model.py --mode lora --adapter ./adapters --tags-hint
fi

if [[ "$step" == "compare" || "$step" == "all" ]]; then
  echo "▶ [5/5] 对比表"
  $PY - <<'EOF'
import json, pathlib
D = pathlib.Path("data")
rows = []
for m, label in [("ollama","qwen2.5:7b zero-shot"),("base","Qwen3-8B zero-shot"),("lora","Qwen3-8B + LoRA(150条)")]:
    f = D/f"eval_{m}.json"
    if f.exists():
        rows.append((label, json.loads(f.read_text(encoding="utf-8"))))
if not rows:
    print("还没有评测结果"); raise SystemExit
keys = [("json_ok","JSON合法"),("domain_acc","一级域"),("tag_acc","二级标签"),
        ("level_acc","等级精确"),("level_1off","等级±1档"),("evidence_ok","证据可溯源")]
w = max(len(l) for l,_ in rows) + 2
print(f"\n{'模型':<{w}}" + "".join(f"{n:>12}" for _,n in keys) + f"{'中位延迟':>10}")
print("-" * (w + 12*len(keys) + 10))
for label, d in rows:
    line = f"{label:<{w}}" + "".join(f"{d[k]:>11.0%}" for k,_ in keys)
    lm = d.get("latency_median")
    print(line + (f"{lm:>9.1f}s" if lm else f"{'-':>10}"))
print(f"\n样本数 n={rows[0][1]['n']}（test 集，与训练集近重复隔离）")
EOF
fi
