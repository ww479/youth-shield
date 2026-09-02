#!/usr/bin/env python3
"""标准案例 xlsx → 清洗 → ChatML 训练集 + SOP 查表 + 金标准。

只依赖标准库：直接解压 xlsx 读 XML，不需要 pandas/openpyxl。

产出（均在 finetune/data/）：
  train.jsonl / valid.jsonl / test.jsonl   MLX LoRA 用（{"messages":[...]}）
  gold.jsonl                               金标准评测集（含 needs_review 标记）
  sop_table.json                           二级标签 → 处置/话术/复盘 查表
  report.md                                清洗报告：改了什么、丢了什么
"""
import json, re, sys, zipfile, hashlib, random
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter, defaultdict

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XLSX = ("/Users/wangshuai/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
        "xwechat_files/wxid_1vn16raw7d8822_4053/msg/file/2026-08/案例整理-20260729(1).xlsx")
OUT = Path(__file__).parent / "data"
SEED = 20260820

# ── 归一化规则（来自实际数据盘点）──────────────────────────────
DOMAIN_FIX = {"网络素养风险": "网络素养与舆论风险"}   # 30 条 vs 4 条，标签表只有后者
PLATFORM_FIX = {
    "贴吧": "百度贴吧", "社交媒体": "社交平台", "社交媒体平台": "社交平台",
    "微博/社交平台": "微博", "b站": "B站", "短视频平台": "抖音",
}
DOMAINS = ["历史认知风险", "制度认同风险", "认知闭合风险", "心理韧性风险", "网络素养与舆论风险"]
LEVELS = ["V1", "V2", "V3", "V4"]
COLS = {  # 列号 → 语义名
    "A": "case_id", "B": "domain", "C": "tag", "D": "title", "E": "summary",
    "F": "date", "G": "platform", "H": "path", "I": "effect", "J": "interact",
    "K": "source", "L": "risk_desc", "M": "level", "N": "disposal_platform",
    "O": "guide_6_12", "P": "guide_13_15", "Q": "guide_16_18",
    "R": "script", "S": "review",
}


def read_sheet(zf, path, shared):
    rows = []
    for row in ET.fromstring(zf.read(path)).iter(NS + "row"):
        cells = {}
        for c in row.iter(NS + "c"):
            col = "".join(ch for ch in (c.get("r") or "") if ch.isalpha())
            v, is_ = c.find(NS + "v"), c.find(NS + "is")
            if c.get("t") == "s" and v is not None:
                val = shared[int(v.text)]
            elif is_ is not None:
                val = "".join(t.text or "" for t in is_.iter(NS + "t"))
            else:
                val = v.text if v is not None else ""
            cells[col] = (val or "").strip()
        rows.append(cells)
    return rows


def excel_serial_to_date(n: float) -> str:
    """Excel 1900 日期序列号 → YYYY-MM-DD（含 1900 闰年 bug 的经典 -2 偏移）。"""
    from datetime import date, timedelta
    return (date(1899, 12, 30) + timedelta(days=int(n))).isoformat()


def norm_date(s: str) -> str | None:
    """四种混用格式统一：44475.4013 / 2016/9/20 / 2023/5 / 2025-5-24 → ISO。"""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):          # Excel 序列号
        try:
            return excel_serial_to_date(float(s))
        except Exception:
            return None
    s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    p = [x for x in s.split("-") if x]
    try:
        if len(p) >= 3:
            return f"{int(p[0]):04d}-{int(p[1]):02d}-{int(p[2]):02d}"
        if len(p) == 2:
            return f"{int(p[0]):04d}-{int(p[1]):02d}-01"   # 只到月，按月初
        if len(p) == 1 and len(p[0]) == 4:
            return f"{int(p[0]):04d}-01-01"
    except ValueError:
        return None
    return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if not Path(XLSX).exists():
        sys.exit(f"找不到 xlsx: {XLSX}")

    with zipfile.ZipFile(XLSX) as zf:
        shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
                  for si in ET.fromstring(zf.read("xl/sharedStrings.xml"))]
        raw_case = read_sheet(zf, "xl/worksheets/sheet3.xml", shared)   # 标准案例
        raw_tag = read_sheet(zf, "xl/worksheets/sheet2.xml", shared)    # 二级标签表

    # ── 二级标签表：19 个合法标签 + 关键词库 ──
    tag_meta = {}
    for r in raw_tag[1:]:
        name = r.get("B", "").strip()
        if not name or name.startswith(("1.", "2.", "3.", "4.", "二级")):
            continue
        tag_meta[name] = {
            "domain": DOMAIN_FIX.get(r.get("A", "").strip(), r.get("A", "").strip()),
            "keywords": [k.strip() for k in re.split(r"[、\t]", r.get("D", "")) if k.strip()],
            "disposal": r.get("G", "").strip(),
        }
    TAG_KEYWORDS.update({k: v["keywords"] for k, v in tag_meta.items()})

    # ── 案例行清洗 ──
    log = {"dropped": [], "fixed": Counter(), "id_remap": []}
    seen_id, recs = Counter(), []
    for r in raw_case[1:]:
        rec = {COLS[k]: r.get(k, "").strip() for k in COLS}
        if not rec["case_id"]:
            continue

        # 硬校验：缺摘要 / 标签不合法 / 等级不合法 → 丢弃
        if not rec["summary"]:
            log["dropped"].append((rec["case_id"], "缺内容摘要")); continue
        if rec["domain"] in DOMAIN_FIX:
            rec["domain"] = DOMAIN_FIX[rec["domain"]]
            log["fixed"]["风险域归一"] += 1
        if rec["tag"] not in tag_meta:
            log["dropped"].append((rec["case_id"], f"标签不在19项内: {rec['tag']}")); continue
        if rec["level"] not in LEVELS:
            log["dropped"].append((rec["case_id"], f"等级非法: {rec['level'] or '空'}")); continue

        # 平台归一
        if rec["platform"] in PLATFORM_FIX:
            rec["platform"] = PLATFORM_FIX[rec["platform"]]
            log["fixed"]["平台归一"] += 1

        # 日期归一
        d = norm_date(rec["date"])
        if d != rec["date"]:
            log["fixed"]["日期归一"] += 1
        rec["date"] = d or ""

        # 案例 ID 去重：重复的追加 -dup2/-dup3
        seen_id[rec["case_id"]] += 1
        if seen_id[rec["case_id"]] > 1:
            new = f"{rec['case_id']}-dup{seen_id[rec['case_id']]}"
            log["id_remap"].append((rec["case_id"], new))
            rec["case_id"] = new

        # 人名脱敏（第 11 节合规点）：训练侧不固化真实姓名
        rec["summary_anon"] = anonymize(rec["summary"])
        recs.append(rec)

    # ── 同标签内等级矛盾检测（12 个标签有此问题）──
    lvl_by_tag = defaultdict(Counter)
    for r in recs:
        lvl_by_tag[r["tag"]][r["level"]] += 1
    majority = {t: c.most_common(1)[0][0] for t, c in lvl_by_tag.items()}
    for r in recs:
        r["level_conflict"] = (len(lvl_by_tag[r["tag"]]) > 1 and r["level"] != majority[r["tag"]])

    # ── SOP 查表：二级标签 → 各文本字段的候选池 ──
    sop = {}
    for t in tag_meta:
        rows = [r for r in recs if r["tag"] == t]
        if not rows:
            continue
        sop[t] = {f: sorted({r[f] for r in rows if r[f]})
                  for f in ("disposal_platform", "guide_6_12", "guide_13_15",
                            "guide_16_18", "script", "review")}
    (OUT / "sop_table.json").write_text(
        json.dumps(sop, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 切分：近重复同侧 + 等级分层（150 条太少，不做时间切分）──
    def near_key(r):   # 用 risk_desc 归一后做近重复分组（同标签同描述必同侧）
        return hashlib.md5((r["tag"] + re.sub(r"\W", "", r["risk_desc"])[:60]).encode()).hexdigest()
    groups = defaultdict(list)
    for r in recs:
        groups[near_key(r)].append(r)
    gl = sorted(groups.values(), key=lambda g: g[0]["case_id"])
    random.Random(SEED).shuffle(gl)
    n = len(gl)
    n_tr, n_va = int(n * 0.8), int(n * 0.1)
    split = {"train": gl[:n_tr], "valid": gl[n_tr:n_tr + n_va], "test": gl[n_tr + n_va:]}

    for name, gs in split.items():
        rows = [r for g in gs for r in g]
        with (OUT / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"messages": build_chatml(r)}, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rows)} 条 / {len(gs)} 组")

    # ── 金标准（含全部 test + 矛盾标记）──
    with (OUT / "gold.jsonl").open("w", encoding="utf-8") as f:
        for r in [x for g in split["test"] for x in g]:
            f.write(json.dumps({
                "case_id": r["case_id"], "input": build_input(r),
                "y": {"domain": r["domain"], "tags": [r["tag"]], "level": r["level"]},
                "needs_review": r["level_conflict"],
            }, ensure_ascii=False) + "\n")

    # ── 报告 ──
    rep = ["# 清洗报告\n", f"- 原始行: {len(raw_case)-1}  合法: {len(recs)}  丢弃: {len(log['dropped'])}\n",
           f"- 合法二级标签: {len(tag_meta)}  有案例的: {len(sop)}\n\n## 修正\n"]
    rep += [f"- {k}: {v} 处\n" for k, v in log["fixed"].items()]
    rep.append(f"- 案例ID重复重编: {len(log['id_remap'])} 处\n\n## 丢弃明细\n")
    rep += [f"- {i}: {why}\n" for i, why in log["dropped"]]
    rep.append("\n## 同标签内等级矛盾（金标准里标 needs_review）\n")
    for t, c in lvl_by_tag.items():
        if len(c) > 1:
            rep.append(f"- {t}: {dict(c)} → 取多数 {majority[t]}\n")
    rep.append("\n## 零样本标签（两万条时必须定向补齐）\n")
    rep += [f"- {t}\n" for t in tag_meta if t not in sop]
    rep.append("\n## 输出多样性（生成式微调门槛 ≥0.30）\n")
    for f_ in ("risk_desc", "disposal_platform", "script", "review"):
        u = len({r[f_] for r in recs if r[f_]})
        rep.append(f"- {f_}: {u}/{len(recs)} = **{u/len(recs):.2f}**\n")
    (OUT / "report.md").write_text("".join(rep), encoding="utf-8")
    print(f"\n丢弃 {len(log['dropped'])} 条，ID重编 {len(log['id_remap'])} 处")
    print(f"报告: {OUT/'report.md'}")


NAME_RE = re.compile(r"(罗昌平|孙杰|陈尔晋|王曼霞|邱少云|黄继光)")
_ANON = {}
TAG_KEYWORDS: dict[str, list[str]] = {}   # 由 main() 从二级标签表填充，供 pick_evidence 用
def anonymize(s: str) -> str:
    """真实姓名 → 代号。英烈姓名保留（判断依赖它们），仅脱敏施害者。"""
    keep = {"邱少云", "黄继光", "陈尔晋", "王曼霞"}
    def rep(m):
        n = m.group(1)
        if n in keep:
            return n
        _ANON.setdefault(n, f"某网民{chr(65+len(_ANON))}")
        return _ANON[n]
    return NAME_RE.sub(rep, s)


SYS = "你是青少年意识形态风险研判专家。依据给定标签体系判定风险，只输出 JSON。"

def build_input(r):
    return (f"【内容】{r['summary_anon']}\n"
            f"【平台】{r['platform'] or '未知'}\n"
            f"【传播路径】{r['path'] or '未知'}\n\n"
            f"请判定：一级风险域、二级标签、风险等级(V1-V4)、风险描述、判定证据。")

def build_chatml(r):
    # 字段顺序 = evidence → domain → tags → level → risk_desc（先抄证据再判断，抗幻觉）
    out = {
        "evidence": pick_evidence(r),
        "domain": r["domain"],
        "tags": [r["tag"]],
        "level": r["level"],
        "risk_desc": r["risk_desc"],
    }
    return [
        {"role": "system", "content": SYS},
        {"role": "user", "content": build_input(r)},
        {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
    ]

def pick_evidence(r):
    """从摘要里抽标签关键词命中的原文片段作为 evidence（保证是原文子串）。"""
    txt = r["summary_anon"]
    hits = []
    for kw in TAG_KEYWORDS.get(r["tag"], []):
        if kw and len(kw) >= 2 and kw in txt and kw not in hits:
            hits.append(kw)
        if len(hits) >= 3:
            break
    if not hits:   # 无关键词命中：退回摘要首句
        hits = [re.split(r"[。；\n]", txt)[0][:40]]
    return hits


if __name__ == "__main__":
    main()
