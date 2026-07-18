# -*- coding: utf-8 -*-
"""
六维 0/1 命中打分（《青少年五大意识形态风险域统一命中规则》）。

彻底取代旧 nihilism_scorer 的历史虚无 rubric：
- 六维、每维 6 个可观测 0/1 命中项（F/N/E/V/P/Y 各 6 项，共 36 项）；
- 维度得分 = 命中数 / 6（平均，保证公平）；
- 加权求和 ×100 = RiskPercent（权重：事实0.20 叙事0.20 情绪0.15 价值0.20 包装0.10 青少年0.15）；
- 对所有内容都可跑，不再绑定“历史认知”单域。

判定由 OpenAI 兼容接口（复用 nihilism_scorer 的客户端）逐项给 0/1；接口不可用时回退轻量启发式，保证不 500。
"""
import json
import re
import time

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
        if risk_percent >= 40 or veto:
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


def _build_prompt(text: str) -> str:
    lines = []
    for d in DIMENSIONS:
        lines.append(f"【{d['name']}】")
        for iid, desc in d["items"]:
            lines.append(f"{iid}：{desc}")
    checklist = "\n".join(lines)
    veto_list = "\n".join(f"{vid}：{desc}" for vid, desc, _ in VETO_RULES)
    major_list = "\n".join(f"{mid}：{desc}" for mid, desc, _ in MAJOR_RULES)
    return f"""你是青少年网络内容意识形态风险审核专家。请依据《青少年五大意识形态风险域统一命中规则》，对下面的内容做三部分判定。

判定规则：
- 六维命中项每项只回答 0 或 1：命中=1，未命中=0。
- 仅依据内容本身的事实、叙事、情绪、价值、包装、受众信号判定，宁缺毋滥，没有明确证据一律判 0。
- 保护性语境（正规新闻、学术、课堂、辟谣、心理求助、权威历史科普等且无恶意包装）应从严判 0。
- 一票否决与重大风险规则只列出“确实命中”的编号，没有则给空数组。

一、六维 36 个命中项（0/1）：
{checklist}

二、一票否决类别（命中则列编号）：
{veto_list}

三、重大意识形态风险 M1-M11（命中则列编号）：
{major_list}

【待判定内容】
{text[:3000]}

只输出一个 JSON 对象（不要任何额外解释），格式：
{{"hits":{{"F1":0,"F2":1, ... ,"Y6":0}},"veto":["VETO_HERO"],"major":["M3","M5"],"domains":["historical_cognition"],"protective":false,"summary":"一句话总体风险判定","evidence":[{{"span":"引用的原文风险片段(不超过40字)","items":["V2","N1"],"note":"为什么构成风险(不超过30字)"}}]}}
其中：domains 为内容触及的风险域，可多选或空（historical_cognition 历史认知 / institutional_identity 制度认同 / psychological_resilience 心理韧性 / network_literacy 网络素养 / cognitive_closure 认知闭合）；protective 为是否成立保护性语境（正规新闻、学术、课堂、辟谣、权威历史科普、心理求助等且无恶意包装）；summary 用一句话概括整体风险；evidence 列出最多 5 处最关键的风险片段（尽量引用原文原话），items 为该片段命中的编号，没有明显风险片段则给空数组。"""


def _parse_judgment(raw: str) -> dict:
    """从模型输出里抽取 JSON，返回 {hits:{item:0/1}, veto:[...], major:[...]}；失败返回 None。"""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except Exception:
        return None
    raw_hits = data.get("hits") if isinstance(data.get("hits"), dict) else data
    hits = {}
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
        evidence.append({"span": span[:80], "items": items,
                         "note": str(e.get("note") or "").strip()[:60]})
    return {"hits": hits, "veto": veto, "major": major,
            "domains": domains, "protective": protective,
            "summary": summary, "evidence": evidence}


# ── LLM 熔断：代理故障时避免每次请求都长时间重试干等 ──
_LLM_FAIL_STREAK = 0
_LLM_COOLDOWN_UNTIL = 0.0
_LLM_COOLDOWN_SECS = 90


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
    for attempt in range(1):   # 代理不稳时单次即可，失败快速退到规则，避免长时间干等
        try:
            r = client.with_options(
                timeout=nihilism_scorer.NIHILISM_LLM_TIMEOUT, max_retries=0
            ).responses.create(
                model=nihilism_scorer._OPENAI_MODEL,
                input=[{"role": "user", "content": prompt}],
                reasoning={"effort": "low"},
            )
            parsed = _parse_judgment((r.output_text or "").strip())
            if parsed is not None:
                _LLM_FAIL_STREAK = 0
                return parsed
            last_err = "解析为空"
        except Exception as e:
            last_err = e
    _LLM_FAIL_STREAK += 1
    if _LLM_FAIL_STREAK >= 2:
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
    # 逐句证据：命中关键词的句子（最多 5 句）
    evidence = []
    for s in _split_sents(t):
        matched_items = [iid for iid, words in _ITEM_KW.items() if words and _has(s, words)]
        if matched_items:
            evidence.append({"span": s[:80], "items": matched_items[:4],
                             "note": "命中风险关键词"})
        if len(evidence) >= 5:
            break
    summary = ""   # 规则模式不塞判定句，前端按 source 显示"规则估算"说明
    return {"hits": hits, "veto": veto, "major": major, "domains": domains,
            "protective": False, "summary": summary, "evidence": evidence}



_SCORE_CACHE = {}
_SCORE_CACHE_MAX = 256


def score(text: str, *, use_llm: bool = True, domain_hits: list = None) -> dict:
    """对内容做六维 0/1 打分 + 一票否决 + M 规则 + §七修正 综合定级。LLM 结果按文本缓存。
    LLM 不可用时用规则版兜底（含 domain_hits 五域命中，尽量接近新规则）。"""
    key = hash((text or "").strip())
    if use_llm and key in _SCORE_CACHE:
        return _SCORE_CACHE[key]

    judgment = None
    source = "llm"
    if use_llm:
        judgment = _score_hits_llm(text)
    if judgment is None:
        judgment = _rule_based_judgment(text, domain_hits)
        source = "rule"
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
        dim_score = round(hit_n / len(d["items"]), 4)   # 命中数 / 6
        dim_scores[d["id"]] = dim_score
        weighted += dim_score * d["weight"]
        dims.append({
            "id": d["id"], "name": d["name"], "weight": d["weight"],
            "score": dim_score, "hit_count": hit_n, "total": len(d["items"]),
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
    if source == "llm":
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
        ev.append({"span": sents[i][:80], "items": items, "note": note,
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
        ds = round(hit_n / len(d["items"]), 4)
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
