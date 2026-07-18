# -*- coding: utf-8 -*-
"""
将《历史虚无命中规则-*.docx》解析为 hit_lexicon.yaml。

用法：
    python build_hit_lexicon.py

重跑说明：
    命中规则文档更新（文件名日期变化）后，直接重跑本脚本即可重新生成
    hit_lexicon.yaml，不需要人工重新转录。脚本只做纯文本结构切分，
    不依赖任何网络/数据库，可反复执行。

解析假设（均已对照原文档人工核对过，如文档格式变化需相应调整）：
    1. docx 是 zip 包，word/document.xml 里每个 <w:p> 是一个段落；
       用 </w:p> -> 换行、</w:tc> -> 制表符、</w:tr> -> 换行 的方式
       转成纯文本后，原文档里的每一句话基本对应一行。
    2. 每个小节标题（如 "1.1 课本未讲型"）在文中作为独立一行出现，
       用于切分小节文本块 —— 大类（7 个）和小类（43 个）列表已在
       CATEGORIES 中人工核对写死，不做正则猜测，避免大类标题里
       全角/半角标点不一致（比如 "4．" 用全角句号、直引号）导致误判。
    3. 每个小节内部固定包含 "一、命中规则"或"一、命中语料" /
       "二、归类逻辑" / "三、分风险判定" 三个子标题，顺序固定。
    4. 命中语料按"（中度样本）/（高样本）/（极高样本）"分组，
       组内每行可能包含多条以"；"分隔的短语。
    5. "三、分风险判定"下有 5 个风险等级判例，每个判例以
       "低风险/轻度风险/中度风险/高风险/极高风险"（可带数字前缀，如
       "1. 低风险"）独占一行开头。
"""
import glob
import html
import os
import re
import sys
import zipfile

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "hit_lexicon.yaml")

# 源文档所在目录：backend/rules/historical_nihilism -> 项目 -> 中青网项目/材料/命中规则
SOURCE_DIR = os.path.join(SCRIPT_DIR, "..", "..", "..", "..", "材料", "命中规则")
SOURCE_GLOB = os.path.join(SOURCE_DIR, "历史虚无命中规则-*.docx")

# ── 大类 / 小类清单（人工对照原文档核对写死，共 7 大类 43 小类）──
CATEGORIES = [
    (1, "揭秘/真相型", "制造神典型话语", [
        "1.1 课本未讲型", "1.2 删除封存型", "1.3 官方隐瞒型", "1.4 真相反转型",
        "1.5 少数清醒型", "1.6 删帖控评型", "1.7 内部资料型", "1.8 版本漏洞型",
    ]),
    (2, "祛魅/还原型", "消解英雄与崇高", [
        "2.1 摘滤镜型", "2.2 动机贬低型", "2.3 私德放大型", "2.4 普通人偷换型",
        "2.5 后人编造型", "2.6 感动操控型", "2.7 影像滤镜型", "2.8 被迫选择型",
        "2.9 成王败寇型", "2.10 细节翻盘型", "2.11 爆款塌房型", "2.12 横向类比降格型",
    ]),
    (3, "阴谋论/因果倒置型", "一切都是设计好的", [
        "3.1 一切早有剧本型", "3.2 幕后黑手隐形操控型", "3.3 谁获利谁主使型",
        "3.4 利益链条推导型", "3.5 棋盘棋子局论型", "3.6 巧合刻意化型",
        "3.7 表面 / 深层二元拆解型", "3.8 利益消解理想型",
    ]),
    (4, "碎片否定整体型", "用细节推翻结论", [
        "4.1 史料矛盾推翻定论型", "4.2 单一事件扩大化型", "4.3 局部缺陷泛化型",
    ]),
    (5, "反权威/反主流人设型", "把否定当个性", [
        "5.1 盲从韭菜贬低型", "5.2 独立思考绑架型", "5.3 青年觉醒煽动型",
        "5.4 禁区敢说勇者人设型",
    ]),
    (6, "娱乐化/宫斗化型", "把历史做成八卦", [
        "6.1 历史吃瓜梗型", "6.2 宫斗权力斗争简化型", "6.3 CP / 狗血剧情类比型",
        "6.4 饭圈塌房类比型", "6.5 爽文叙事解构历史型",
    ]),
    (7, "中外对比贬损型", "借历史否定现实", [
        "7.1 单一短板对比型", "7.2 假设历史路线型", "7.3 全盘制度贬损型",
    ]),
]

TIER_SUFFIX_MARKERS = {
    "（中度样本）": "中度",
    "（高样本）": "高",
    "（极高样本）": "极高",
}

# 从 3.2 起，命中语料改用"中度样本：短语"这种前缀+同行内容的格式，
# 而不是"...（中度样本）"作为独占一行的组标题，两种格式在文档中并存。
TIER_PREFIX_MARKERS = {
    "中度样本：": "中度",
    "高样本：": "高",
    "极高样本：": "极高",
}

RISK_MARKER_RE = re.compile(
    r"^\d*[\.、]?\s*(低风险|轻度风险|中度风险|高风险|极高风险)\s*$"
)
EXAMPLE_LINE_RE = re.compile(r"^(?:包含)?例句[:：]\s*(.+)$")


def resolve_source_path() -> str:
    matches = sorted(glob.glob(SOURCE_GLOB))
    if not matches:
        raise FileNotFoundError(f"未找到命中规则文档，检查路径：{SOURCE_GLOB}")
    # 文件名以日期结尾，取字典序最大（即日期最新）的一份
    return matches[-1]


def extract_plain_text(docx_path: str) -> str:
    with zipfile.ZipFile(docx_path) as zf:
        xml_bytes = zf.read("word/document.xml")
    content = xml_bytes.decode("utf-8")
    content = re.sub(r"<w:tab/>", "\t", content)
    content = re.sub(r"<w:br/>", "\n", content)
    content = re.sub(r"</w:p>", "\n", content)
    content = re.sub(r"</w:tc>", "\t", content)
    content = re.sub(r"</w:tr>", "\n", content)
    text = re.sub(r"<[^>]+>", "", content)
    text = html.unescape(text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def split_into_blocks(full_text: str, headings: list) -> dict:
    """按小节标题切分全文，返回 {heading: block_text}。"""
    positions = []
    cursor = 0
    for heading in headings:
        needle = f"\n{heading}\n"
        idx = full_text.find(needle, cursor)
        if idx == -1:
            # 兜底：不要求前导换行（标题可能出现在文本开头）
            idx = full_text.find(heading, cursor)
            if idx == -1:
                raise ValueError(f"未在文档中找到小节标题：{heading!r}")
        else:
            idx += 1  # 跳过前导换行，指向标题本身
        positions.append(idx)
        cursor = idx + len(heading)

    blocks = {}
    for i, heading in enumerate(headings):
        start = positions[i]
        end = positions[i + 1] if i + 1 < len(positions) else len(full_text)
        blocks[heading] = full_text[start:end]
    return blocks


def parse_hit_phrases(lines: list) -> dict:
    tiers = {"中度": [], "高": [], "极高": []}
    current_tier = None

    def add_phrases(tier, text):
        for frag in text.split("；"):
            frag = frag.strip().rstrip("。").strip()
            if frag:
                tiers[tier].append(frag)

    for line in lines:
        line = line.strip()
        if not line:
            continue

        prefix_hit = None
        for label, tier in TIER_PREFIX_MARKERS.items():
            if line.startswith(label):
                prefix_hit = (tier, line[len(label):])
                break
        if prefix_hit:
            current_tier, remainder = prefix_hit
            add_phrases(current_tier, remainder)
            continue

        suffix_tier = None
        for suffix, tier in TIER_SUFFIX_MARKERS.items():
            if line.endswith(suffix):
                suffix_tier = tier
                break
        if suffix_tier:
            current_tier = suffix_tier
            continue

        if current_tier is None:
            continue  # 标题行之类的杂散文本，忽略
        add_phrases(current_tier, line)
    return tiers


def parse_judged_examples(lines: list) -> list:
    marker_positions = []
    for i, raw in enumerate(lines):
        m = RISK_MARKER_RE.match(raw.strip())
        if m:
            marker_positions.append((i, m.group(1)))

    examples = []
    for idx, (line_no, risk_level) in enumerate(marker_positions):
        start = line_no + 1
        end = marker_positions[idx + 1][0] if idx + 1 < len(marker_positions) else len(lines)
        block_lines = [l.strip() for l in lines[start:end] if l.strip()]
        example_sentence = ""
        for bl in block_lines:
            em = EXAMPLE_LINE_RE.match(bl)
            if em:
                example_sentence = em.group(1)
                break
        examples.append({
            "risk_level": risk_level,
            "example_sentence": example_sentence,
            "reasoning_text": "\n".join(block_lines),
        })
    return examples


def parse_subtype_block(subtype_id: str, subtype_name: str, block_text: str) -> dict:
    lines = block_text.split("\n")
    # lines[0] 是小节标题本身

    idx_yi = idx_er = idx_san = None
    for i, l in enumerate(lines):
        stripped = l.strip()
        if idx_yi is None and stripped in ("一、命中规则", "一、命中语料"):
            idx_yi = i
        elif idx_yi is not None and idx_er is None and stripped == "二、归类逻辑":
            idx_er = i
        elif idx_er is not None and idx_san is None and stripped == "三、分风险判定":
            idx_san = i
            break

    if idx_yi is None or idx_er is None or idx_san is None:
        raise ValueError(f"小节 {subtype_id} {subtype_name} 缺少一/二/三子标题，需人工核对")

    hit_phrase_lines = lines[idx_yi + 1: idx_er]
    classification_lines = [l.strip() for l in lines[idx_er + 1: idx_san] if l.strip()]
    judged_lines = lines[idx_san + 1:]

    return {
        "id": subtype_id,
        "name": subtype_name,
        "classification_logic": "\n".join(classification_lines),
        "hit_phrases": parse_hit_phrases(hit_phrase_lines),
        "judged_examples": parse_judged_examples(judged_lines),
    }


def build_lexicon() -> dict:
    source_path = resolve_source_path()
    full_text = extract_plain_text(source_path)

    all_headings = []
    heading_to_name = {}
    for _cat_id, _cat_name, _cat_subtitle, subtype_headings in CATEGORIES:
        for heading in subtype_headings:
            all_headings.append(heading)
            sid, sname = heading.split(" ", 1)
            heading_to_name[heading] = (sid, sname)

    blocks = split_into_blocks(full_text, all_headings)

    categories_out = []
    for cat_id, cat_name, cat_subtitle, subtype_headings in CATEGORIES:
        subtypes_out = []
        for heading in subtype_headings:
            sid, sname = heading_to_name[heading]
            subtypes_out.append(parse_subtype_block(sid, sname, blocks[heading]))
        categories_out.append({
            "id": cat_id,
            "name": cat_name,
            "subtitle": cat_subtitle,
            "subtypes": subtypes_out,
        })

    return {
        "source_doc": os.path.basename(source_path),
        "categories": categories_out,
    }


def validate(lexicon: dict) -> list:
    problems = []
    total_subtypes = 0
    for cat in lexicon["categories"]:
        for sub in cat["subtypes"]:
            total_subtypes += 1
            sid = sub["id"]
            for tier in ("中度", "高", "极高"):
                if not sub["hit_phrases"].get(tier):
                    problems.append(f"{sid} {sub['name']}：缺少「{tier}」命中语料")
            if len(sub["judged_examples"]) != 5:
                problems.append(
                    f"{sid} {sub['name']}：判例条数为 {len(sub['judged_examples'])}，应为 5"
                )
            if not sub["classification_logic"]:
                problems.append(f"{sid} {sub['name']}：缺少归类逻辑文本")
    return problems, total_subtypes


def main():
    lexicon = build_lexicon()
    problems, total_subtypes = validate(lexicon)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(lexicon, f, allow_unicode=True, sort_keys=False, width=100)

    print(f"✓ 已生成 {OUTPUT_PATH}，共 {total_subtypes} 个小类")
    if problems:
        print(f"⚠ 校验发现 {len(problems)} 个问题，需人工核对：")
        for p in problems:
            print(f"  - {p}")
    else:
        print(f"✓ 校验通过：{total_subtypes}/{total_subtypes} 个小类均含 3 类命中语料 + 5 条风险判例")


if __name__ == "__main__":
    main()
