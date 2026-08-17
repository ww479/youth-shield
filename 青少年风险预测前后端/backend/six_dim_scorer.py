# -*- coding: utf-8 -*-
"""
六维 0/1 命中打分（《青少年五大意识形态风险域统一命中规则》）。

彻底取代旧 nihilism_scorer 的历史虚无 rubric：
- 六维、每维 6 个可观测 0/1 命中项（F/N/E/V/P/Y 各 6 项，共 36 项）；
- 维度得分 = 命中项数走饱和曲线（1项0.45 / 2项0.70 / 3项0.85 / 4项0.95 / 5-6项1.0，见 DIM_SCORE_CURVE）；
- 加权求和 ×100 = RiskPercent（权重：事实0.20 叙事0.20 情绪0.15 价值0.20 包装0.10 青少年0.15）；
- 对所有内容都可跑，不再绑定“历史认知”单域。

判定由 OpenAI 兼容接口（复用 nihilism_scorer 的客户端）逐项给 0/1；接口不可用时回退轻量启发式，保证不 500。
"""
import json
import os
import re
import time
import hashlib
try:
    import major_lexicon   # 数据驱动的重大风险 M1-M11 关键词词库（挖掘自 2.5 万条标注样本）
except Exception:
    major_lexicon = None
try:
    import major_examples  # M1-M11 代表性示例语料（每规则 3 条，来自 t_major_risk_sample），做提示词语义锚点
except Exception:
    major_examples = None

# 红线顶格开关：默认关（保持保守定级），MAJOR_TOPGRADE=on 时重大风险按其严重级顶格
_MAJOR_TOPGRADE = os.environ.get("MAJOR_TOPGRADE", "").lower() in ("1", "on", "true", "yes")
# 语义样本库检测器（由 main.py 在向量库构建后注入；离线时为 None，自动跳过）
_semantic_major_detector = None
def init_semantic_major(fn):
    """注入基于 t_major_risk_sample 向量库的重大风险语义检测函数 fn(text)->list[M]。"""
    global _semantic_major_detector
    _semantic_major_detector = fn

import nihilism_scorer  # 复用其 OpenAI 兼容客户端与超时配置

# ── 六维定义：权重 + 36 个 0/1 命中项 ──
DIMENSIONS = [
    {"id": "fact_handling", "name": "事实处理方式", "weight": 0.20, "items": [
        ("F1", "使用未经证实的“网传/听说/内部消息/据说”作为事实依据"),
        ("F2", "断章取义使用截图、片段、剪辑、个案"),
        ("F3", "将局部个案直接泛化为整体结论"),
        ("F4", "使用伪史、野史、模糊信源否定权威事实"),
        ("F5", "存在深伪、AI伪造、移花接木、张冠李戴风险"),
        ("F6", "缺少时间、地点、来源，却给出强结论"),
    ]},
    {"id": "narrative_frame", "name": "叙事框架", "weight": 0.20, "items": [
        ("N1", "构造“真相被隐藏/官方不可信/课本不讲”的信息差框架"),
        ("N2", "构造“清醒少数 vs 盲从大众”的身份分层"),
        ("N3", "构造“他们不想让你知道”的阴谋主体"),
        ("N4", "将复杂问题简化为敌我二分、非黑即白"),
        ("N5", "将个案嫁接为制度、国家、群体整体问题"),
        ("N6", "使用“历史道路替代/如果当年另一条路”叙事"),
    ]},
    {"id": "emotional_mobilization", "name": "情绪动员", "weight": 0.15, "items": [
        ("E1", "制造恐惧、愤怒、羞辱、仇恨、绝望等强情绪"),
        ("E2", "使用“细思极恐、破防、太炸裂、头皮发麻”等高刺激词"),
        ("E3", "引导评论区共鸣、站队、围攻、转发"),
        ("E4", "制造“被骗多年/信仰崩塌/原来如此”的幻灭感"),
        ("E5", "鼓励报复、网暴、社死、开盒或极端行动"),
        ("E6", "出现自伤、绝望、死亡浪漫化的感染性表达"),
    ]},
    {"id": "value_orientation", "name": "价值导向", "weight": 0.20, "items": [
        ("V1", "否定指导思想、制度道路、政治认同"),
        ("V2", "虚无历史共识、消解英雄崇高、淡化侵略伤害"),
        ("V3", "整体否定国家、社会、文化主体性"),
        ("V4", "宣扬努力无用、责任无意义、价值虚无"),
        ("V5", "将金钱、流量、饭圈忠诚、极端利己置于规则和公共价值之上"),
        ("V6", "弱化法治、规则、生命安全和未成年人保护"),
    ]},
    {"id": "expression_packaging", "name": "表达包装", "weight": 0.10, "items": [
        ("P1", "使用“揭秘/解密/真相/内部资料”等包装"),
        ("P2", "使用玩梗、鬼畜、表情包、二创包装严肃议题"),
        ("P3", "使用反讽、反串、暗语、谐音、缩写规避识别"),
        ("P4", "使用“学术/科普/辟谣/公益/心理”外壳输出风险结论"),
        ("P5", "使用“三分钟看懂/一口气讲完/别急着骂”等爆款标题结构"),
        ("P6", "用“懂的都懂/不能多说/且看且珍惜”替代证据"),
    ]},
    {"id": "youth_impact", "name": "青少年影响", "weight": 0.15, "items": [
        ("Y1", "面向青少年或在青少年高频平台/场景传播"),
        ("Y2", "涉及升学、考试、就业、家庭、同伴、校园压力"),
        ("Y3", "评论区出现青少年共鸣、模仿、开摆、绝望、站队"),
        ("Y4", "可能诱导青少年网暴、开盒、集资、违法、危险挑战"),
        ("Y5", "可能削弱青少年历史、国家、制度、文化、规则认同"),
        ("Y6", "存在自伤、他伤、校园暴力、心理危机风险"),
    ]},
]

_ALL_ITEM_IDS = [iid for d in DIMENSIONS for iid, _ in d["items"]]

# ── 维度得分曲线：命中项数 → 维度得分（0-1）──
# 原来是线性的"命中数 ÷ 6"，导致六维几乎不可能独立定到高风险：要到 L4 得命中 22/36 项、
# L5 得 29/36 项，而真实内容（含极高风险语料）实测只命中 5-8 项，全部落在 L1，
# 只能靠红线顶格。而 36 项里本来就有大量互斥项（深伪、自伤浪漫化、校园暴力…），
# 一段历史虚无内容根本不涉及，"命中 22 项"实际不可达。
# 改成饱和曲线：某维只要出现命中就贡献大部分权重，多命中再小幅递增。这样"跨多个维度
# 触及"能真正抬高分数 —— 同时动摇事实/叙事/价值三层，本就比在单一维度堆 6 项更危险。
# 实测（LLM 判定）：极高风险语料 5 项/3 维 由 16.7%(L1) → 37.0%(L2)；
# 低风险语料 0-2 项仍为 0-18%(L1)，无误报。
DIM_SCORE_CURVE = [0.0, 0.45, 0.70, 0.85, 0.95, 1.0, 1.0]


def dim_score(hit_n: int, total: int = 6) -> float:
    """某维命中 hit_n 项时的维度得分（0-1）。total 非 6 时按比例折算到曲线上。"""
    try:
        hit_n = int(hit_n)
    except Exception:
        hit_n = 0
    if hit_n <= 0:
        return 0.0
    if total and total != 6:                      # 兼容非 6 项维度（目前没有）
        hit_n = round(hit_n / total * 6)
    return DIM_SCORE_CURVE[min(max(hit_n, 0), 6)]


def _uniq_keep(seq) -> list:
    """按出现顺序去重（合并规则/AI 结果时保序）。"""
    seen, out = set(), []
    for x in seq or []:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


_ITEM_DESC = {iid: desc for d in DIMENSIONS for iid, desc in d["items"]}

# ── 一票否决（8 类）：命中即高危，硬 5 级 or 4-5 级 ──
VETO_RULES = [
    ("VETO_IDEOLOGY", "明确否定党的指导思想、中国特色社会主义道路制度、国家政权合法性", 5),
    ("VETO_SPLIT",    "煽动分裂国家、颠覆国家政权、破坏国家统一、极端政治动员", 5),
    ("VETO_HERO",     "侮辱英烈、亵渎国家符号、美化侵略、否定重大历史共识、全盘否定党史国史军史", 5),
    ("VETO_RUMOR",    "利用深伪、谣言、矩阵传播制造重大公共恐慌或政治化动员", 5),
    ("VETO_SELFHARM", "明确自伤计划、自杀邀约、死亡方法传播、校园暴力预告", 5),
    ("VETO_HOSTILE",  "明显配合境外议题、系统丑化中国国家形象、煽动价值对立", 5),
    ("VETO_ILLEGAL",  "开盒人肉、隐私泄露、勒索威胁、违法教唆、暴恐煽动、极端仇恨动员", 4),
    ("VETO_ORGVIOL",  "明确号召围攻、社死、跨平台追杀、线下骚扰（目标含未成年人或普通个人）", 4),
]
VETO_LEVEL = {vid: lv for vid, _, lv in VETO_RULES}

# ── 重大意识形态风险 M1-M11：命中要点 + 默认等级 ──
MAJOR_RULES = [
    ("M1",  "否定、边缘化马克思主义和社会主义核心价值体系", 5),
    ("M2",  "否定中国特色社会主义道路、制度、理论、文化", 5),
    ("M3",  "歪曲、否定、虚无党史国史军史革命史、英雄人物", 4),
    ("M4",  "削弱对党的领导、人民民主专政、国家共同体的认同", 5),
    ("M5",  "污名化、矮化、娱乐化英雄烈士、先进典型", 4),
    ("M6",  "系统制造“什么都不信”、无力感、阶层敌意", 4),
    ("M7",  "深度伪造、数据操控、暗网真相、算法操控叙事", 4),
    ("M8",  "以普世价值、西方新闻观、外媒叙事丑化中国形象", 5),
    ("M9",  "娱乐化、二元化、情绪化，使公众放弃复杂判断", 3),
    ("M10", "将局部个案嫁接为全局政治制度问题", 4),
    ("M11", "组织化放大负面舆论、操纵热搜评论、攻击主流媒体公信力", 4),
]
MAJOR_LEVEL = {mid: lv for mid, _, lv in MAJOR_RULES}
_HARD5_VETO = {vid for vid, _, lv in VETO_RULES if lv == 5}

# 五大风险域 id → 中文名
DOMAIN_NAMES = {
    "historical_cognition": "历史认知", "institutional_identity": "制度认同",
    "psychological_resilience": "心理韧性", "network_literacy": "网络素养",
    "cognitive_closure": "认知闭合",
}


def percent_to_level(pct: float) -> int:
    """RiskPercent → 1-5 级：0-20/21-40/41-60/61-80/81-100。"""
    try:
        pct = float(pct)
    except Exception:
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    if pct <= 20: return 1
    if pct <= 40: return 2
    if pct <= 60: return 3
    if pct <= 80: return 4
    return 5


def decide_level(risk_percent: float, veto: list, major: list, *,
                 youth_score: float = 0.0, domains: list = None, protective: bool = False) -> dict:
    """按新规则综合定级：六维基础级 → §七传播/受众/语境修正 → 一票否决/重大风险顶级。"""
    domains = domains or []
    base = percent_to_level(risk_percent)
    lvl = base
    applied = []
    one_vote = bool(veto)
    # ── §七 传播/受众修正：仅在六维有实质基础(base>=2, RiskPercent>20)时才向上叠加，──
    #    避免六维几乎为 0 却被"多域/青少年"强行抬高（曾出现 15.8分→极高 的错位）
    if base >= 2:
        if len(domains) >= 2:
            lvl += 1; applied.append("多域叠加+1")
        if youth_score >= 0.5:
            lvl += 1; applied.append("青少年高曝光+1")
    if protective and not veto and not major:
        lvl -= 1; applied.append("保护性语境-1")
    lvl = max(1, min(5, lvl))
    if len(domains) >= 3 and base >= 2:
        lvl = max(lvl, 4); applied.append("≥3域不低于4级")
    # ── 重大风险 M：按新规 pseudocode，RiskPercent>=40(或同时一票否决)才顶到≥4；──
    #    否则仅温和提示(最高+1、封 4)，不直接把低分内容顶成极高
    if major:
        n = len(major)
        m_max = max((MAJOR_LEVEL.get(m, 4) for m in major), default=base)
        if risk_percent >= 40 or veto or _MAJOR_TOPGRADE:
            m_lvl = 5 if n >= 4 else (min(5, m_max + 1) if n >= 2 else m_max)
            lvl = max(lvl, m_lvl); applied.append("重大风险规则")
        else:
            lvl = max(lvl, min(4, base + 1)); applied.append("重大风险(提示)")
    # ── 一票否决：最高优先，直接顶级（真·高危信号，如英烈红线/自伤/分裂）──
    if veto:
        lvl = 5 if (set(veto) & _HARD5_VETO) else max(lvl, 4)
        applied.append("一票否决")
    return {"level": max(1, min(5, lvl)),
            "base_level": base,
            "adjustments": applied,
            "one_vote_veto": one_vote,
            "major_ideological_risk": bool(major)}


# M1-M11 提示词块：在规则名后附语料挖掘的代表性话术做「语义锚点」。
# STRONG_PHRASES 是静态数据，模块级缓存一次，_build_prompt 直接拼接，零每请求开销。
_MAJOR_PROMPT_BLOCK = None
def _major_prompt_block() -> str:
    global _MAJOR_PROMPT_BLOCK
    if _MAJOR_PROMPT_BLOCK is not None:
        return _MAJOR_PROMPT_BLOCK
    strong = getattr(major_examples, "MAJOR_EXAMPLES", {}) if major_examples else {}
    lines = []
    for mid, desc, _ in MAJOR_RULES:
        egs = [p for p in strong.get(mid, []) if p][:3]
        if egs:
            lines.append(f"{mid}：{desc}（示例话术：{' ｜ '.join(egs)}）")
        else:
            lines.append(f"{mid}：{desc}")
    _MAJOR_PROMPT_BLOCK = "\n".join(lines)
    return _MAJOR_PROMPT_BLOCK


def _build_prompt(text: str) -> str:
    lines = []
    for d in DIMENSIONS:
        lines.append(f"【{d['name']}】")
        for iid, desc in d["items"]:
            lines.append(f"{iid}：{desc}")
    checklist = "\n".join(lines)
    veto_list = "\n".join(f"{vid}：{desc}" for vid, desc, _ in VETO_RULES)
    major_list = _major_prompt_block()
    return f"""你是青少年网络内容意识形态风险审核专家。请依据《青少年五大意识形态风险域统一命中规则》，对下面的内容做三部分判定。

判定规则：
- 六维命中项：逐一检查全部 36 项，把**所有**符合的编号都列出来（一条内容常同时命中多项，
  不要只列一两个就停；长文本往往在多个维度都有信号）。
- 判定依据是内容本身的事实、叙事、情绪、价值、包装、受众信号；有对应表述就列，不要漏。
- 只有保护性语境（正规新闻、学术、课堂、辟谣、心理求助、权威历史科普等且无恶意包装）才从严不列。
- 一票否决与重大风险规则只列出“确实命中”的编号，没有则给空数组。

一、六维 36 个命中项（逐项检查，列出所有命中的编号）：
{checklist}

二、一票否决类别（命中则列编号）：
{veto_list}

三、重大意识形态风险 M1-M11（命中则列编号）：
（每条规则后的“示例话术”仅帮助你理解该规则的语义边界，**不是关键词匹配清单**——须结合上下文语义判断是否构成该风险；正规新闻/学术/辟谣/权威科普等保护性语境即便字面相似也一律不判。）
{major_list}

【待判定内容】
{text[:3000]}

只输出一个 JSON 对象（不要任何额外解释），格式：
{{"hit":["<所有命中的六维编号>"],"veto":["<命中的否决编号>"],"major":["<命中的M编号>"],"domains":["<风险域>"],"protective":false,"summary":"一句话总体风险判定","evidence":[{{"span":"引用的原文风险片段(不超过40字)","items":["<该片段命中的六维编号>"],"rules":["<该片段触发的否决/M编号>"],"note":"为什么构成风险(不超过30字)"}}]}}
其中：**hit 要包含你判定命中的全部六维编号**（把各条 evidence 的 items 也并进去，不要遗漏；
确实一项都没命中才给空数组 []）；domains 为内容触及的风险域，可多选或空（historical_cognition 历史认知 / institutional_identity 制度认同 / psychological_resilience 心理韧性 / network_literacy 网络素养 / cognitive_closure 认知闭合）；protective 为是否成立保护性语境（正规新闻、学术、课堂、辟谣、权威历史科普、心理求助等且无恶意包装）；summary 用一句话概括整体风险；evidence 列出最多 5 处最关键的风险片段（尽量引用原文原话），items 为该片段命中的六维编号。
**重要：只要你在 veto 或 major 里列了任何编号，就必须在 evidence 里给出对应的原文片段，并在该片段的 rules 字段写上它触发的红线编号（如 ["VETO_HERO","M3"]），让审核者能定位到具体是哪一句触发红线；没有触发红线的片段 rules 给空数组 []。"""


def _parse_judgment(raw: str) -> dict:
    """从模型输出里抽取 JSON，返回 {hits:{item:0/1}, veto:[...], major:[...]}；失败返回 None。

    命中项兼容两种格式（对外返回的 hits 字典结构不变）：
      · 新格式 "hit": ["N1","V2"]        —— 只列命中，输出 token 少 55%，本地 7B 快 3 倍
      · 老格式 "hits": {"F1":0,"N1":1}   —— 36 键全列，仍能解析（换回云端大模型/旧缓存不会挂）
    """
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except Exception:
        return None

    hits = {iid: 0 for iid in _ALL_ITEM_IDS}
    hit_list = data.get("hit")
    if isinstance(hit_list, (list, tuple, set)):          # 新格式：只列命中编号
        for iid in hit_list:
            key = str(iid).strip().upper()
            if key in hits:
                hits[key] = 1
    else:                                                  # 老格式：36 键 0/1 字典
        raw_hits = data.get("hits") if isinstance(data.get("hits"), dict) else data
        for iid in _ALL_ITEM_IDS:
            v = raw_hits.get(iid, 0) if isinstance(raw_hits, dict) else 0
            try:
                hits[iid] = 1 if int(v) >= 1 else 0
            except Exception:
                hits[iid] = 0
    veto = [v for v in (data.get("veto") or []) if v in VETO_LEVEL]
    major = [m2 for m2 in (data.get("major") or []) if m2 in MAJOR_LEVEL]
    domains = [d for d in (data.get("domains") or []) if d in DOMAIN_NAMES]
    protective = bool(data.get("protective"))
    summary = str(data.get("summary") or "").strip()
    evidence = []
    for e in (data.get("evidence") or [])[:6]:
        if not isinstance(e, dict):
            continue
        span = str(e.get("span") or "").strip()
        if not span:
            continue
        items = [i for i in (e.get("items") or []) if i in _ITEM_DESC]
        # 该片段触发的红线编号：VETO_*/M* 单独存 rules，不混进 items
        # （items 是 36 项语义，混进去会让前端的"命中项"统计和 36 格渲染出错）
        rules = [r for r in (e.get("rules") or [])
                 if r in VETO_LEVEL or r in MAJOR_LEVEL]
        evidence.append({"span": span[:80], "items": items, "rules": rules,
                         "note": str(e.get("note") or "").strip()[:60]})
        # 证据里引用到的判据也算命中：小模型常把编号写进 evidence.items 却漏在 hit 里，
        # 导致"有证据、有红线，但 36 项命中 0"这种自相矛盾的结果。
        for iid in items:
            hits[iid] = 1
    return {"hits": hits, "veto": veto, "major": major,
            "domains": domains, "protective": protective,
            "summary": summary, "evidence": evidence}


# ── LLM 熔断：代理故障时避免每次请求都长时间重试干等 ──
_LLM_FAIL_STREAK = 0
_LLM_COOLDOWN_UNTIL = 0.0
_LLM_COOLDOWN_SECS = 60


def _score_hits_llm(text: str) -> dict:
    """调用 OpenAI 兼容接口做六维 + 否决 + M 判定；不可用/失败返回 None。
    含熔断：连续失败后冷却一段时间直接跳过（走兜底），避免代理故障时逐次干等。"""
    global _LLM_FAIL_STREAK, _LLM_COOLDOWN_UNTIL
    if time.monotonic() < _LLM_COOLDOWN_UNTIL:
        return None   # 熔断冷却中，直接兜底
    client = nihilism_scorer._get_openai_client()
    if client is None:
        return None
    prompt = _build_prompt(text)
    last_err = None
    for attempt in range(2):   # 端点抖动期外层再试 1 次（每次 SDK 内 max_retries=1）；~2-3 成即可命中，稳打到 LLM
        try:
            raw = nihilism_scorer.llm_generate(client, prompt, max_retries=1)
            parsed = _parse_judgment(raw)
            if parsed is not None:
                _LLM_FAIL_STREAK = 0
                return parsed
            last_err = "解析为空"
        except Exception as e:
            last_err = e
    _LLM_FAIL_STREAK += 1
    if _LLM_FAIL_STREAK >= 3:
        _LLM_COOLDOWN_UNTIL = time.monotonic() + _LLM_COOLDOWN_SECS
        _LLM_FAIL_STREAK = 0
        print(f"[six_dim_scorer] LLM 连续失败，熔断 {_LLM_COOLDOWN_SECS}s 改用兜底")
    print(f"[six_dim_scorer] LLM 判定失败: {last_err}")
    return None


# ── 规则版离线兜底（LLM 不可用时用关键词/短语估算，尽量接近新规则）──
# 36 项命中关键词映射（覆盖高信号项；命中任一词即判该项为 1）
_ITEM_KW = {
    "F1": ["网传", "听说", "据说", "内部消息", "爆料", "有人说"],
    "F2": ["截图", "片段", "剪辑", "断章取义", "这一段"],
    "F3": ["全都", "个个", "无一例外", "所有人都", "统统"],
    "F4": ["野史", "伪史", "正史不会", "真实历史", "被篡改", "真相是", "其实是"],
    "F5": ["换脸", "深伪", "AI合成", "移花接木", "张冠李戴", "摆拍"],
    "F6": ["反正", "总之就是", "不用证据"],
    "N1": ["课本不", "课本没", "官方不敢", "官方不会", "真相被隐藏", "不想让你知道", "教科书骗", "不会告诉你"],
    "N2": ["清醒的人", "明白人", "觉醒", "盲从大众", "少数清醒"],
    "N3": ["他们不想让你知道", "背后有人", "幕后黑手", "有人操纵"],
    "N4": ["非黑即白", "不是我们就是", "不是朋友就是", "汉奸", "非友即敌"],
    "N5": ["整个国家", "整个制度", "整个社会", "这个国家都", "全体制"],
    "N6": ["如果当年", "本可以", "走另一条路", "换条路", "要是当初"],
    "E1": ["愤怒", "仇恨", "绝望", "恐惧", "气炸", "怒火"],
    "E2": ["细思极恐", "破防", "炸裂", "头皮发麻", "毛骨悚然", "血压飙升"],
    "E3": ["转发", "扩散", "评论区", "一起声讨", "顶上去", "冲啊"],
    "E4": ["被骗多年", "信仰崩塌", "原来如此", "三观碎", "原来一直被骗"],
    "E5": ["报复", "网暴", "开盒", "社死", "弄死", "让他付出代价", "扒出来", "挂这里", "挂他", "人肉", "去冲他", "围攻", "身份证", "家庭住址"],
    "E6": ["不想活", "自杀", "遗书", "结束生命", "别救我", "一了百了", "撑不下去"],
    "V1": ["道路走错", "走错了路", "推翻", "社会主义失败", "制度不行", "否定制度", "这套制度没救", "注定失败", "没前途", "遮羞布", "烂透"],
    "V2": ["英雄是假", "烈士编造", "侵略有功", "殖民有功", "历史虚无", "否定党史", "英雄都是编造", "美化侵略"],
    "V3": ["这个国家没救", "中国不行", "传统都是糟粕", "去中国化", "这片土地不值得", "国外才", "外国才", "别的国家才"],
    "V4": ["努力没用", "努力也没用", "读书无用", "读书根本没用", "奋斗骗局", "躺平", "摆烂", "人间不值得", "改变不了命运", "翻不了身", "出身决定"],
    "V5": ["有钱就行", "流量至上", "为爱发电", "精致利己", "饭圈"],
    "V6": ["法不责众", "规则是给", "未成年怕什么", "违法又怎样"],
    "P1": ["揭秘", "解密", "内部资料", "独家爆料", "深度起底"],
    "P2": ["鬼畜", "玩梗", "表情包", "二创", "整活"],
    "P3": ["谐音", "缩写", "暗语", "yygq", "阴阳怪气"],
    "P4": ["理性讨论", "科普一下", "只是学术", "辟谣", "客观来说"],
    "P5": ["三分钟看懂", "一口气讲完", "别急着骂", "看到最后", "一分钟了解"],
    "P6": ["懂的都懂", "不能多说", "且看且珍惜", "自己体会", "点到为止"],
    "Y1": ["同学", "学生", "年轻人", "青少年", "00后", "10后"],
    "Y2": ["升学", "高考", "考试", "就业", "内卷", "鸡娃", "校园"],
    "Y3": ["评论区都在", "弹幕", "跟风"],
    "Y4": ["未成年", "打赏", "集资", "危险挑战"],
    "Y5": [],
    "Y6": ["校园暴力", "自残", "自伤", "心理危机", "霸凌"],
}
# 英烈名（用于一票否决 M3/M5 组合判断）
_HERO_NAMES = ["黄继光", "董存瑞", "邱少云", "杨靖宇", "刘胡兰", "雷锋", "赖宁", "狼牙山"]
_NEG_WORDS = ["假", "编造", "虚构", "摆拍", "杜撰", "否定", "污名", "抹黑", "造神", "神话"]


def _has(text, words):
    return any(w in text for w in words)


def _detect_veto(text: str) -> list:
    v = []
    if (_has(text, _HERO_NAMES) and _has(text, _NEG_WORDS)) or _has(text, ["侮辱英烈", "美化侵略", "侵略有功", "殖民有功", "亵渎国旗", "亵渎国歌", "否定党史", "否定国史"]):
        v.append("VETO_HERO")
    if _has(text, ["自杀", "遗书", "结束生命", "不想活了", "别救我", "约死", "一起去死", "自我了断", "轻生"]):
        v.append("VETO_SELFHARM")
    if (_has(text, ["否定", "推翻", "打倒", "结束"]) and _has(text, ["党的领导", "共产党执政", "社会主义制度", "国家政权", "一党"])) or _has(text, ["制度必然崩溃", "改旗易帜"]):
        v.append("VETO_IDEOLOGY")
    # 制度道路否定（一票否决：指导思想与制度道路 → 5级）
    if _has(text, ["这套制度", "这条路", "中国道路", "社会主义道路", "这个制度", "这种制度", "中国特色"]) and \
       _has(text, ["走错", "注定失败", "没前途", "没有前途", "没救", "是错的", "该推翻", "早晚完", "遮羞布", "烂透"]):
        v.append("VETO_IDEOLOGY")
    if _has(text, ["台独", "港独", "疆独", "藏独", "分裂国家", "颠覆国家政权", "颠覆政权"]):
        v.append("VETO_SPLIT")
    # 开盒人肉 / 隐私泄露（违法侵害）
    if _has(text, ["开盒", "人肉搜索", "人肉他", "暴恐", "勒索", "教唆犯罪", "制造炸弹"]) or \
       (_has(text, ["身份证", "家庭住址", "住址", "扒隐私", "扒出来", "个人信息"]) and _has(text, ["挂", "曝光", "冲他", "社死", "围攻", "让他"])):
        v.append("VETO_ILLEGAL")
    # 组织化网暴（号召围攻/社死/追杀）
    if _has(text, ["一起去", "大家一起", "号召", "组织", "都去", "集体"]) and _has(text, ["冲他", "社死", "围攻", "网暴", "挂他", "喷他", "追杀"]):
        v.append("VETO_ORGVIOL")
    if _has(text, ["深度伪造", "深伪", "造谣", "谣言"]) and _has(text, ["恐慌", "煽动", "带节奏"]):
        v.append("VETO_RUMOR")
    return [x for x in v if x in VETO_LEVEL]


def _derive_major(text: str, hits: dict, veto: list, domains: list) -> list:
    m = set()
    # M3/M5、M2/M4 只从"强组合"一票否决派生，避免单个常见词（英雄/制度/道路）误触
    if "VETO_HERO" in veto:
        m.update(["M3", "M5"])
    if "VETO_IDEOLOGY" in veto:
        m.update(["M2", "M4"])
    # 域内 M 需域命中 + 两个信号佐证
    if "institutional_identity" in domains and hits.get("V1") and hits.get("V3"):
        m.add("M2")
    if "psychological_resilience" in domains and hits.get("V4") and hits.get("E4"):
        m.add("M6")
    if "network_literacy" in domains and _has(text, ["深伪", "算法操控", "暗网", "数据操控"]):
        m.add("M7")
    if "cognitive_closure" in domains and hits.get("N4") and hits.get("N3"):
        m.add("M9")
    # 数据驱动词库直接从文本命中重大风险规则（分层门控，中性肯定内容不误触）
    if major_lexicon is not None:
        try:
            m.update(major_lexicon.detect_major(text))
        except Exception:
            pass
    # 语义样本库：与 2.5 万条标注样本做 embedding 近邻匹配（联网有向量模型时生效）
    if _semantic_major_detector is not None:
        try:
            m.update(_semantic_major_detector(text))
        except Exception:
            pass
    return [x for x in m if x in MAJOR_LEVEL]


def _split_sents(text):
    import re as _re
    return [s.strip() for s in _re.split(r"[。！？!?；;\n]+", text or "") if s.strip()]


def _rule_based_judgment(text: str, domain_hits: list = None) -> dict:
    """规则版兜底：关键词→36项 + 一票否决/M 关键词识别 + 五域命中 + 逐句证据。"""
    t = text or ""
    hits = {iid: 0 for iid in _ALL_ITEM_IDS}
    for iid, words in _ITEM_KW.items():
        if words and _has(t, words):
            hits[iid] = 1
    domains = [d.get("domain_id") for d in (domain_hits or []) if d.get("domain_id")]
    veto = _detect_veto(t)
    major = _derive_major(t, hits, veto, domains)
    # 逐句证据：命中关键词的句子（最多 5 句）；同时逐句判红线，让 VETO/M 能定位到句
    evidence = []
    for s in _split_sents(t):
        matched_items = [iid for iid, words in _ITEM_KW.items() if words and _has(s, words)]
        # 这一句自己触发了哪些红线：VETO 用同一套检测逐句跑，M 用词库逐句判
        s_rules = [v for v in _detect_veto(s) if v in veto]
        if major_lexicon is not None:
            try:
                s_rules += [m for m in major_lexicon.detect_major(s)
                            if m in major and m not in s_rules]
            except Exception:
                pass
        if matched_items or s_rules:
            evidence.append({"span": s[:80], "items": matched_items[:4],
                             "rules": s_rules,
                             "note": "触及红线" if s_rules else "命中风险关键词"})
        if len(evidence) >= 5:
            break
    summary = ""   # 规则模式不塞判定句，前端按 source 显示"规则估算"说明
    return {"hits": hits, "veto": veto, "major": major, "domains": domains,
            "protective": False, "summary": summary, "evidence": evidence}



_SCORE_CACHE = {}
_SCORE_CACHE_MAX = 256


def score(text: str, *, use_llm: bool = True, domain_hits: list = None) -> dict:
    """对内容做六维 0/1 打分 + 一票否决 + M 规则 + §七修正 综合定级。

    判定顺序：**规则与 AI 并用，逐项取严（并集）**。
      ① 先跑规则版（关键词 → 36 项 + 一票否决/M 词库 + 五域命中），零 LLM、~0.3ms；
      ② 同时让 AI 判一次；
      ③ 两边结果合并，每一项取更严的那个：
         · 36 项命中取并集（任一判命中即命中）
         · 一票否决 / M 规则 / 五域 取并集
         · protective（保护性语境）只有两边都认才成立 —— 一边认为有风险就不该被减档
         · summary / evidence 优先用 AI 的（规则版不产生判定句）
      ④ AI 不可用时退化为纯规则结果，反之亦然，任一路可用都不影响出结果。
    为什么不是"规则命中就跳过 AI"：实测同一段内容，关键词只抓到 1 项（E3 由"评论区讨论"
    这种中性词误触发），AI 能抓到 10 项跨 5 维的实质风险（F4/N1/N2/N3/V1/V3…）。
    关键词表覆盖不全、换个说法就抓不到；而词库对红线（M 规则）比小模型准 —— 两者互补，
    取并集才既不漏 AI 的跨维发现、也不丢词库查实的红线。
    source：both=两边都有命中 / rule=仅规则 / llm=仅 AI / rule_empty=都没命中。
    LLM 结果按文本缓存（规则很快，每次重算无所谓）。
    """
    key = hash((text or "").strip())
    if use_llm and key in _SCORE_CACHE:
        return _SCORE_CACHE[key]

    # ① 规则版（快，总是跑）
    rule_j = _rule_based_judgment(text, domain_hits)
    # ② AI 版（可用就跑）
    ai_j = _score_hits_llm(text) if use_llm else None
    llm_used = ai_j is not None

    # ③ 逐项取严：36 项/红线/域取并集，protective 取"两边都认"
    if ai_j is None:
        judgment = rule_j
        source = "rule" if any(rule_j["hits"].values()) else "rule_empty"
    else:
        hits = {iid: (1 if (rule_j["hits"].get(iid) or ai_j["hits"].get(iid)) else 0)
                for iid in _ALL_ITEM_IDS}
        judgment = {
            "hits": hits,
            "veto": sorted(set(rule_j.get("veto") or []) | set(ai_j.get("veto") or [])),
            "major": sorted(set(rule_j.get("major") or []) | set(ai_j.get("major") or []),
                            key=lambda x: int(x[1:]) if x[1:].isdigit() else 99),
            "domains": _uniq_keep(list(ai_j.get("domains") or []) + list(rule_j.get("domains") or [])),
            # 一边认为有风险就不该被"保护性语境"减档，必须两边都认才成立
            "protective": bool(rule_j.get("protective")) and bool(ai_j.get("protective")),
            "summary": ai_j.get("summary") or rule_j.get("summary") or "",
            # 证据：AI 的带判据归属更可读，规则的作为补充
            "evidence": (ai_j.get("evidence") or []) + (rule_j.get("evidence") or []),
        }
        judgment["evidence"] = judgment["evidence"][:6]
        r_hit = any(rule_j["hits"].values()) or rule_j.get("veto") or rule_j.get("major")
        a_hit = any(ai_j["hits"].values()) or ai_j.get("veto") or ai_j.get("major")
        source = ("both" if (r_hit and a_hit) else
                  "rule" if r_hit else "llm" if a_hit else "rule_empty")

    hits = judgment["hits"]
    veto = judgment.get("veto") or []
    major = judgment.get("major") or []
    domains = judgment.get("domains") or []
    protective = bool(judgment.get("protective"))
    summary = judgment.get("summary") or ""
    evidence = judgment.get("evidence") or []
    for e in evidence:
        e["item_descs"] = [{"id": i, "desc": _ITEM_DESC.get(i, "")} for i in (e.get("items") or [])]

    dims = []
    weighted = 0.0
    dim_scores = {}
    for d in DIMENSIONS:
        item_list = [{"id": iid, "desc": desc, "hit": hits.get(iid, 0)}
                     for iid, desc in d["items"]]
        hit_n = sum(i["hit"] for i in item_list)
        dim_sc = round(dim_score(hit_n, len(d["items"])), 4)   # 饱和曲线，见 DIM_SCORE_CURVE
        dim_scores[d["id"]] = dim_sc
        weighted += dim_sc * d["weight"]
        dims.append({
            "id": d["id"], "name": d["name"], "weight": d["weight"],
            "score": dim_sc, "hit_count": hit_n, "total": len(d["items"]),
            "items": item_list,
        })

    risk_percent = round(weighted * 100, 1)
    youth_score = dim_scores.get("youth_impact", 0.0)
    verdict = decide_level(risk_percent, veto, major,
                           youth_score=youth_score, domains=domains, protective=protective)
    result = {
        "source": source,
        "dim_scores": dim_scores,
        "risk_percent": risk_percent,
        "level": verdict["level"],
        "base_level": verdict["base_level"],
        "adjustments": verdict["adjustments"],
        "domains": domains,
        "protective": protective,
        "one_vote_veto": verdict["one_vote_veto"],
        "major_ideological_risk": verdict["major_ideological_risk"],
        "veto_rules": veto,
        "major_rules": major,
        "detail": {
            "risk_percent": risk_percent,
            "source": source,                    # llm=AI精判 / rule=规则估算
            "base_level": verdict["base_level"],
            "adjustments": verdict["adjustments"],
            "summary": summary,
            "protective": protective,
            "domains": [{"id": d, "name": DOMAIN_NAMES.get(d, d)} for d in domains],
            "evidence": evidence,
            "weights": {d["id"]: d["weight"] for d in DIMENSIONS},
            "dimensions": dims,
            "veto_rules": [{"id": v, "desc": dict((x, y) for x, y, _ in VETO_RULES).get(v, "")} for v in veto],
            "major_rules": [{"id": m2, "desc": dict((x, y) for x, y, _ in MAJOR_RULES).get(m2, "")} for m2 in major],
        },
    }
    if llm_used:            # 只缓存真正调过 AI 的结果；规则版本来就快，不占缓存
        if len(_SCORE_CACHE) > _SCORE_CACHE_MAX:
            _SCORE_CACHE.clear()
        _SCORE_CACHE[key] = result
    return result


# ── 测试页预设等级：按目标等级(1-5)直接产出稳定的六维结果，绕过 LLM/规则判定 ──
# 用于演示五级五色 UI 效果；真实内容仍走 score()。命中数随等级梯度递增，风险分与等级自然对应。
_PRESET_HITS = {          # 各维命中项数 [事实,叙事,情绪,价值,包装,青少年]
    1: [0, 0, 0, 0, 0, 0],
    2: [3, 3, 2, 0, 0, 0],
    3: [4, 4, 3, 2, 3, 2],
    4: [5, 5, 4, 4, 4, 4],
    5: [6, 6, 5, 6, 5, 5],
}
_PRESET_DOMAINS = {
    1: [], 2: ["historical_cognition"],
    3: ["historical_cognition", "cognitive_closure"],
    4: ["historical_cognition", "cognitive_closure", "network_literacy"],
    5: ["historical_cognition", "institutional_identity", "cognitive_closure"],
}
_PRESET_SUMMARY = {
    1: "内容规范，未见明显意识形态风险信号。",
    2: "存在轻度倾向性表述，建议关注，暂无需干预。",
    3: "选择性叙事与信息差手法明显，构成中度误导风险。",
    4: "多维度风险信号密集、跨多个风险域，属高风险内容。",
    5: "全盘否定倾向并触及重大意识形态红线，属极高风险。",
}
_PRESET_MAJOR = {5: ["M2", "M3", "M4"]}
_PRESET_VETO = {5: ["VETO_HERO"]}
# 逐句证据模板：(命中项id, 中文风险说明)；span 用测试页正文真实句子填入，按等级取 N 条
_PRESET_EVIDENCE = {
    2: [(["F4", "N1"], "只呈现单一版本、暗示课本没讲全，带轻度倾向")],
    3: [(["N1", "F2"], "选择性叙事，暗示公开版本刻意留白、另有隐情"),
        (["P1", "F3"], "用“揭秘/冷知识”式包装配合个案，诱导以偏概全的怀疑")],
    4: [(["N1", "N3"], "构造“真相被隐藏、他们不想让你知道”的信息差与阴谋框架"),
        (["E1", "E2"], "用高刺激表述制造焦虑与对立，煽动情绪化站队"),
        (["F5", "F2"], "用可疑图像、断章片段否定通行说法")],
    5: [(["V2", "V1"], "否定重大历史共识、消解主流叙事的合法性"),
        (["V3", "N5"], "将个案上升为对国家、制度的整体否定"),
        (["E4", "E1"], "制造“信仰崩塌、原来被骗”的幻灭与对立情绪"),
        (["V2"], "触及虚无党史国史、英雄人物的红线")],
}


def _preset_evidence(lv: int, text: str) -> list:
    """用正文真实句子作 span，配预设的中文风险说明，产出逐句证据（展示用）。"""
    tmpl = _PRESET_EVIDENCE.get(lv, [])
    if not tmpl:
        return []
    sents = [s for s in _split_sents(text or "") if len(s) >= 8]
    ev = []
    for i, (items, note) in enumerate(tmpl):
        if i >= len(sents):
            break
        ev.append({"span": sents[i][:80], "items": items, "rules": [], "note": note,
                   "item_descs": [{"id": it, "desc": _ITEM_DESC.get(it, "")} for it in items]})
    return ev


def preset_score(level_num: int, text: str = "") -> dict:
    """测试页按目标等级(1-5)直接产出稳定的六维结果结构，字段与 score() 对齐，绕过判定。"""
    lv = max(1, min(5, int(level_num)))
    per = _PRESET_HITS[lv]
    major = _PRESET_MAJOR.get(lv, [])
    veto = _PRESET_VETO.get(lv, [])
    domains = _PRESET_DOMAINS[lv]
    dims = []
    weighted = 0.0
    dim_scores = {}
    for d, hit_n in zip(DIMENSIONS, per):
        items = [{"id": iid, "desc": desc, "hit": 1 if i < hit_n else 0}
                 for i, (iid, desc) in enumerate(d["items"])]
        ds = round(dim_score(hit_n, len(d["items"])), 4)
        dim_scores[d["id"]] = ds
        weighted += ds * d["weight"]
        dims.append({"id": d["id"], "name": d["name"], "weight": d["weight"],
                     "score": ds, "hit_count": hit_n, "total": len(d["items"]), "items": items})
    risk_percent = round(weighted * 100, 1)
    evidence = _preset_evidence(lv, text)
    return {
        "source": "preset", "dim_scores": dim_scores, "risk_percent": risk_percent,
        "level": lv, "base_level": lv, "adjustments": ["测试页预设等级"],
        "domains": domains, "protective": False,
        "one_vote_veto": bool(veto), "major_ideological_risk": bool(major),
        "veto_rules": veto, "major_rules": major,
        "detail": {
            "risk_percent": risk_percent, "source": "preset", "base_level": lv,
            "adjustments": ["测试页预设等级"], "summary": _PRESET_SUMMARY[lv], "protective": False,
            "domains": [{"id": d, "name": DOMAIN_NAMES.get(d, d)} for d in domains],
            "evidence": evidence,
            "weights": {d["id"]: d["weight"] for d in DIMENSIONS},
            "dimensions": dims,
            "veto_rules": [{"id": v, "desc": dict((x, y) for x, y, _ in VETO_RULES).get(v, "")} for v in veto],
            "major_rules": [{"id": m2, "desc": dict((x, y) for x, y, _ in MAJOR_RULES).get(m2, "")} for m2 in major],
        },
    }


def _anchor_level(anchor_score=0.0, risk_label: str = "") -> int:
    """已存储的语料风险等级/综合风险分 → 六维目标等级 1-5。
    低风险→1(L0) 中风险→3(L2) 高风险→4(L3) 极高风险→5(L4)。"""
    lab = (risk_label or "").strip()
    if "极高" in lab:
        return 5
    if "高" in lab:
        return 4
    if "中" in lab:
        return 3
    if "低" in lab:
        return 1
    try:
        s = float(anchor_score or 0)
    except Exception:
        s = 0.0
    if s >= 3.5:
        return 5
    if s >= 2.5:
        return 4
    if s >= 1.5:
        return 3
    return 1


def score_anchored(anchor_score=0.0, risk_label: str = "", text: str = "") -> dict:
    """展示用：把已存储的语料风险等级/综合风险分锚定为六维结果。
    等级严格对应已存储值；命中项按内容做确定性散列——总数在等级区间内浮动、
    命中项在 36 项中打散分布（不再顶满、不再永远命中每维前几项），不同内容不同图案。
    逐句证据取正文真实语句并挂到其真实命中的项。真实逐维精判留待后续开发。"""
    lv = _anchor_level(anchor_score, risk_label)
    seed_txt = (text or "").strip() or (risk_label or "")
    # 各等级命中项总数区间（不顶满，留出自然波动）
    rng = {1: (1, 4), 3: (8, 14), 4: (12, 19), 5: (18, 26)}.get(lv, (1, 4))
    span = rng[1] - rng[0] + 1
    total = min(36, rng[0] + (_seed(seed_txt, "n") % span))
    # 36 项按内容散列排序后取前 total 个作为命中——打散、稳定、可复现
    order = sorted(range(36), key=lambda i: _seed(seed_txt, str(i)))
    chosen = set(order[:total])
    hitmap = {iid: (1 if i in chosen else 0) for i, iid in enumerate(_ALL_ITEM_IDS)}

    domains = _PRESET_DOMAINS[lv]
    major = _PRESET_MAJOR.get(lv, [])
    veto = _PRESET_VETO.get(lv, [])
    dims, weighted, dim_scores = [], 0.0, {}
    for d in DIMENSIONS:
        items = [{"id": iid, "desc": desc, "hit": hitmap.get(iid, 0)} for iid, desc in d["items"]]
        hit_n = sum(i["hit"] for i in items)
        ds = round(dim_score(hit_n, len(d["items"])), 4)
        dim_scores[d["id"]] = ds
        weighted += ds * d["weight"]
        dims.append({"id": d["id"], "name": d["name"], "weight": d["weight"],
                     "score": ds, "hit_count": hit_n, "total": len(d["items"]), "items": items})
    risk_percent = round(weighted * 100, 1)
    evidence = _anchored_evidence(text, hitmap, lv)
    note = "按已存储风险等级锚定（展示用）"
    return {
        "source": "anchored", "dim_scores": dim_scores, "risk_percent": risk_percent,
        "level": lv, "base_level": lv, "adjustments": [note],
        "domains": domains, "protective": False,
        "one_vote_veto": bool(veto), "major_ideological_risk": bool(major),
        "veto_rules": veto, "major_rules": major,
        "detail": {
            "risk_percent": risk_percent, "source": "anchored", "base_level": lv,
            "adjustments": [note], "summary": _PRESET_SUMMARY[lv], "protective": False,
            "domains": [{"id": d, "name": DOMAIN_NAMES.get(d, d)} for d in domains],
            "evidence": evidence,
            "weights": {d["id"]: d["weight"] for d in DIMENSIONS},
            "dimensions": dims,
            "veto_rules": [{"id": v, "desc": dict((x, y) for x, y, _ in VETO_RULES).get(v, "")} for v in veto],
            "major_rules": [{"id": m2, "desc": dict((x, y) for x, y, _ in MAJOR_RULES).get(m2, "")} for m2 in major],
        },
    }


def _seed(text: str, salt: str) -> int:
    """内容 + salt 的确定性散列（跨进程稳定，同内容同结果）。"""
    return int(hashlib.md5(f"{salt}|{text}".encode("utf-8")).hexdigest()[:8], 16)


def _anchored_evidence(text: str, hitmap: dict, lv: int) -> list:
    """逐句证据：取正文真实句子，挂到该内容实际命中的项上。"""
    hit_ids = [iid for iid, h in hitmap.items() if h]
    sents = [s for s in _split_sents(text or "") if len(s) >= 8]
    if not sents or not hit_ids:
        return _preset_evidence(lv, text)
    n = min(len(sents), 3 if lv >= 4 else 2)
    ev = []
    for i in range(n):
        start = (i * 2) % len(hit_ids)
        items = hit_ids[start:start + 2] or hit_ids[:2]
        ev.append({"span": sents[i][:80], "items": items, "rules": [],
                   "note": (_ITEM_DESC.get(items[0], "命中风险要点") or "命中风险要点")[:42],
                   "item_descs": [{"id": it, "desc": _ITEM_DESC.get(it, "")} for it in items]})
    return ev
