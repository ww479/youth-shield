# -*- coding: utf-8 -*-
"""
历史虚无主义六维判分引擎。

对外只暴露 run_nihilism_pipeline()，main.py 只需要在算出 primary_domain
之后调用它，不需要了解内部三段式实现。设计方案见
rules/historical_nihilism/engineering_implementation_plan.md。

用法（main.py 接线）：
    import nihilism_scorer
    nihilism_scorer.load_rules()                     # 启动时调一次
    result = nihilism_scorer.run_nihilism_pipeline(   # 命中"历史认知风险"时调
        text, title=title, platform=platform, age_group=age_group,
        ai_client=ai_client,
        ai_call_allowed=_ai_call_allowed,
        ai_record_success=_ai_record_success,
        ai_record_failure=_ai_record_failure,
    )
"""
import json
import os
import re
import concurrent.futures

os.environ.setdefault("HF_HUB_OFFLINE", "1")   # 走本地缓存，避免联网拉 embedding 模型

import yaml

_RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "rules", "historical_nihilism")

DIMENSION_ORDER = [
    "fact_handling", "narrative_frame", "emotional_mobilization",
    "value_orientation", "expression_packaging", "youth_impact",
]

# 命中"历史虚无主义"高频活跃渠道，对应 escalation_rules 里的 youth_channel_boost
YOUTH_CHANNELS = {"抖音", "快手", "B站", "小红书"}

# 灰度开关：设为 False 时 run_nihilism_pipeline 直接跳过大模型调用，
# 走纯启发式兜底（见 _heuristic_dimensions），用于快速回滚。
ENABLE_NIHILISM_LLM_SCORING = os.environ.get("ENABLE_NIHILISM_LLM_SCORING", "true") == "true"

# 六维模型调用单次超时（秒）。外部模型中转偶发卡死（曾观测到单次 69s 无响应），
# 无超时会让整个 /analyze 永久挂起；超时后自动退回下一级兜底（Anthropic → 规则估算）。
# 健康时模型 2-6s 返回，12s 足够；调大可用环境变量 NIHILISM_LLM_TIMEOUT 覆盖。
NIHILISM_LLM_TIMEOUT = float(os.environ.get("NIHILISM_LLM_TIMEOUT", "12"))

# 升降档（档位移动）总开关：命中≥2大类升一档 / 仅标题命中降一档。
# 暂时关闭——只按六维总分定级、显示分不再出现"±升降档"公式。恢复：置 True 或环境变量 =true。
ENABLE_LEVEL_SHIFT = os.environ.get("ENABLE_NIHILISM_LEVEL_SHIFT", "false") == "true"

# ── OpenAI 兼容接口配置（当前 Anthropic 中转不可用，六维判分优先走这里）──
# 密钥默认从 ~/.codex/auth.json 的 OPENAI_API_KEY 读取（不写死在代码里、不进日志）。
_OPENAI_BASE_URL = os.environ.get("NIHILISM_OPENAI_BASE_URL", "")   # 配自己的 OpenAI 兼容中转地址
_OPENAI_MODEL = os.environ.get("NIHILISM_OPENAI_MODEL", "gpt-5.5")
_OPENAI_KEY_FILE = os.environ.get("NIHILISM_OPENAI_KEY_FILE", os.path.expanduser("~/.codex/auth.json"))
_openai_client = None
_openai_ready = None   # None=未初始化 / True=可用 / False=不可用（缓存，避免每次重试）

_RUBRIC: dict = {}
_LEXICON: dict = {}
_PHRASE_INDEX: list = []
_SUBTYPE_INDEX: dict = {}
_RISK_LEVELS: list = []   # _RUBRIC["risk_levels"]，按 L0..L4 顺序

# ── 语义匹配（命中短语向量库）配置 ──
# Chroma 用 cosine 距离（=1-余弦相似度）；距离 ≤ 阈值视为语义命中。
# 阈值越小越严（越接近才算命中）。默认 0.5，可用环境变量调，最终由215条评测标定。
SEMANTIC_DISTANCE_THRESHOLD = float(os.environ.get("NIHILISM_SEMANTIC_DIST", "0.5"))
SEMANTIC_MODEL = os.environ.get("NIHILISM_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")

# ── 分句判定配置 ──
# 逐句判定时的并发路数（每个命中句一次大模型调用）与单页最多判定的命中句数（防长文失控）。
SENTENCE_CONCURRENCY = int(os.environ.get("NIHILISM_SENT_CONCURRENCY", "6"))
MAX_JUDGE_SENTENCES = int(os.environ.get("NIHILISM_MAX_SENTENCES", "25"))
_embed_model = None
_chroma_client = None
_hit_collection = None


def load_rules():
    """启动期调用一次，把 scoring_rubric.yaml / hit_lexicon.yaml 读进内存。"""
    global _RUBRIC, _LEXICON, _PHRASE_INDEX, _SUBTYPE_INDEX, _RISK_LEVELS

    with open(os.path.join(_RULES_DIR, "scoring_rubric.yaml"), encoding="utf-8") as f:
        _RUBRIC = yaml.safe_load(f)
    with open(os.path.join(_RULES_DIR, "hit_lexicon.yaml"), encoding="utf-8") as f:
        _LEXICON = yaml.safe_load(f)

    _RISK_LEVELS = _RUBRIC["risk_levels"]

    _PHRASE_INDEX = []
    _SUBTYPE_INDEX = {}
    for cat in _LEXICON["categories"]:
        for sub in cat["subtypes"]:
            _SUBTYPE_INDEX[sub["id"]] = {
                **sub, "category_id": cat["id"], "category_name": cat["name"],
            }
            for tier, phrases in sub["hit_phrases"].items():
                for phrase in phrases:
                    if len(phrase) >= 4:
                        _PHRASE_INDEX.append({
                            "phrase": phrase, "subtype_id": sub["id"],
                            "category_id": cat["id"], "tier": tier,
                        })
    print(f"✓ 历史虚无命中规则加载：{len(_SUBTYPE_INDEX)} 小类，{len(_PHRASE_INDEX)} 条命中短语")


def init_semantic(embed_model=None, chroma_client=None):
    """构建命中短语的语义向量库。启用后 prefilter 在精确匹配之外叠加语义检索。

    embed_model / chroma_client 传入则复用（main.py 已加载的那套，省内存）；
    不传则本模块自建（供 eval 等独立进程用）。构建失败自动回退纯精确匹配。
    load_rules() 必须先调过。
    """
    global _embed_model, _chroma_client, _hit_collection
    if not _PHRASE_INDEX:
        print("[nihilism_scorer] init_semantic 前需先 load_rules()，跳过")
        return
    try:
        if embed_model is not None and chroma_client is not None:
            _embed_model, _chroma_client = embed_model, chroma_client
        else:
            from sentence_transformers import SentenceTransformer
            import chromadb
            _embed_model = SentenceTransformer(SEMANTIC_MODEL)
            _chroma_client = chromadb.Client()
        phrases = [e["phrase"] for e in _PHRASE_INDEX]
        embs = _embed_model.encode(phrases, batch_size=64, show_progress_bar=False).tolist()
        _hit_collection = _chroma_client.get_or_create_collection(
            "nihilism_hit_phrases", metadata={"hnsw:space": "cosine"})
        _hit_collection.upsert(
            ids=[f"hp_{i}" for i in range(len(_PHRASE_INDEX))],
            embeddings=embs,
            documents=phrases,
            metadatas=[{"idx": i} for i in range(len(_PHRASE_INDEX))],
        )
        print(f"✓ 历史虚无命中短语语义库构建：{len(_PHRASE_INDEX)} 条，距离阈值 {SEMANTIC_DISTANCE_THRESHOLD}")
    except Exception as e:
        _hit_collection = None
        print(f"[nihilism_scorer] 语义库构建失败，回退纯精确匹配: {e}")


# ══════════════════════════════════════════════
#  阶段一：关键词预筛
# ══════════════════════════════════════════════

def prefilter_hit_lexicon(text: str, title: str = "") -> dict:
    """命中短语预筛：精确子串匹配 + 语义检索（并集）。未命中返回 None。

    - 精确匹配：短语作为子串出现在"标题+正文"里，零误报。
    - 语义匹配：把文本按句切分，每句在命中短语向量库里检索，
      距离 ≤ SEMANTIC_DISTANCE_THRESHOLD 视为命中（同义改写也能召回）。
      语义库未构建（init_semantic 未调/失败）时自动退化为纯精确匹配。
    """
    full_text = f"{title} {text}"
    hits = []
    seen = set()   # (phrase, subtype_id) 去重，精确优先

    # 1) 精确子串匹配
    for entry in _PHRASE_INDEX:
        phrase = entry["phrase"]
        if phrase in full_text:
            key = (phrase, entry["subtype_id"])
            if key not in seen:
                seen.add(key)
                hits.append({**entry, "in_title": bool(title) and phrase in title,
                             "match": "exact"})

    # 2) 语义检索（按句）
    if _hit_collection is not None and _embed_model is not None:
        sents = [s.strip() for s in _SENT_SPLIT_RE.split(full_text) if len(s.strip()) >= 4][:50]
        if sents:
            try:
                embs = _embed_model.encode(sents, show_progress_bar=False).tolist()
                res = _hit_collection.query(
                    query_embeddings=embs,
                    n_results=min(3, len(_PHRASE_INDEX)),
                    include=["metadatas", "distances"],
                )
                for si in range(len(sents)):
                    metas = res["metadatas"][si]
                    dists = res["distances"][si]
                    for m, d in zip(metas, dists):
                        if d > SEMANTIC_DISTANCE_THRESHOLD:
                            continue
                        entry = _PHRASE_INDEX[m["idx"]]
                        key = (entry["phrase"], entry["subtype_id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append({**entry, "in_title": False,
                                     "match": "semantic", "distance": round(d, 3)})
            except Exception as e:
                print(f"[nihilism_scorer] 语义检索失败，仅用精确命中: {e}")

    if not hits:
        return None

    subtype_ids = {h["subtype_id"] for h in hits}
    category_ids = {h["category_id"] for h in hits}
    title_only = bool(title) and all(h["in_title"] for h in hits)
    return {
        "hits": hits,
        "subtype_ids": subtype_ids,
        "category_ids": category_ids,
        "title_only": title_only,
    }


# ══════════════════════════════════════════════
#  阶段二：大模型六维判分
# ══════════════════════════════════════════════

def _build_prompt(text: str, hit_subtypes: set) -> str:
    dim_lines = []
    for dim in _RUBRIC["dimensions"]:
        bands = "；".join(f"{k}={v}" for k, v in dim["bands"].items())
        dim_lines.append(f"- {dim['id']}（{dim['name_cn']}）：{dim['definition']}。分档：{bands}")
    dims_text = "\n".join(dim_lines)

    # 规则初筛命中的候选话术类型 —— 大模型只能在这些候选里复核确认，不能新增
    cand_lines, fewshot = [], []
    for sid in sorted(hit_subtypes):
        sub = _SUBTYPE_INDEX.get(sid)
        if not sub:
            continue
        logic = (sub.get("classification_logic") or "").split("\n")[0][:80]
        cand_lines.append(f"- {sid} {sub['name']}：{logic}")
        if len(fewshot) < 2:
            mid_example = next(
                (e for e in sub["judged_examples"] if e["risk_level"] == "中度风险"), None
            )
            if mid_example:
                fewshot.append(
                    f"【{sub['name']}】判例：{mid_example['reasoning_text'][:200]}"
                )
    cand_text = "\n".join(cand_lines) or "（无明确候选，请据正文判断）"
    fewshot_block = (
        "参考同类判例，帮助你校准打分尺度：\n\n" + "\n\n".join(fewshot) + "\n\n"
    ) if fewshot else ""

    return f"""你是历史虚无主义内容风险评估专家。规则初筛已在下列候选话术类型上命中，请你复核：

{cand_text}

请完成四件事：
1. 六维打分：每维取值 0-5 的整数，分值越高风险越强。
{dims_text}
2. 类型复核：从上面候选类型里，只保留正文确实成立的类型 id（可多选）；候选都不成立就返回空数组。禁止编造候选之外的 id。
3. 证据短语：从正文中摘抄 1-4 条最能支撑判断的原话（保持原文，不要改写）。
4. 判定理由：一句话说明依据（40 字以内）。

{fewshot_block}待评估内容：
{text[:1500]}

只输出 JSON 对象，不要 markdown 代码块、不要任何解释文字：
{{"scores":{{"fact_handling":0,"narrative_frame":0,"emotional_mobilization":0,"value_orientation":0,"expression_packaging":0,"youth_impact":0}},"confirmed_types":[],"evidence":[],"reason":""}}"""


def _parse_llm_judgment(raw: str, candidate_ids: set) -> dict:
    """解析大模型输出：六维分值（严格校验）+ 复核类型 / 证据短语 / 理由（尽力解析）。

    六维取值非法直接抛异常（触发降级兜底）；其余三项缺失或格式异常时按空处理，
    不影响主链路。confirmed_types 只保留落在 candidate_ids 里的 id（大模型不得新增）。
    """
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"未找到 JSON：{raw[:100]}")
    data = json.loads(m.group())

    scores_obj = data.get("scores") if isinstance(data.get("scores"), dict) else data
    dims = {}
    for dim in DIMENSION_ORDER:
        v = int(scores_obj.get(dim, -1))
        if not (0 <= v <= 5):
            raise ValueError(f"维度 {dim} 取值越界：{v}")
        dims[dim] = v

    confirmed = []
    for t in (data.get("confirmed_types") or []):
        t = str(t).strip()
        if t in candidate_ids and t not in confirmed:
            confirmed.append(t)

    evidence = []
    for e in (data.get("evidence") or []):
        e = str(e).strip()
        if e and e not in evidence:
            evidence.append(e[:120])
        if len(evidence) >= 4:
            break

    reason = str(data.get("reason") or "").strip()[:80]
    return {"dimensions": dims, "confirmed_types": confirmed,
            "evidence": evidence, "reason": reason}


def _get_openai_client():
    """惰性构建 OpenAI 兼容客户端，密钥从 ~/.codex/auth.json 读取。不可用返回 None。"""
    global _openai_client, _openai_ready
    if _openai_ready is not None:
        return _openai_client
    key = os.environ.get("NIHILISM_OPENAI_API_KEY")
    if not key:
        try:
            key = json.load(open(_OPENAI_KEY_FILE, encoding="utf-8")).get("OPENAI_API_KEY")
        except Exception:
            key = None
    if not key:
        _openai_ready = False
        return None
    try:
        import httpx
        from openai import OpenAI
        # trust_env=False：绕过 Windows 系统代理（注册表 WinINET 代理）。
        # 系统若配过已关闭的本地代理，httpx 默认会把请求塞去该端口 → ConnectionRefused，
        # 表现为 six_dim_scorer 的 "Connection error." 熔断兜底。
        _http_client = httpx.Client(trust_env=False, timeout=NIHILISM_LLM_TIMEOUT)
        _openai_client = OpenAI(base_url=(_OPENAI_BASE_URL or None), api_key=key, http_client=_http_client)
        _openai_ready = True
        print(f"✓ 历史虚无六维判分接入 OpenAI 兼容接口：{_OPENAI_MODEL} @ {_OPENAI_BASE_URL or 'openai官方'}")
    except Exception as e:
        print(f"[nihilism_scorer] OpenAI 客户端初始化失败: {e}")
        _openai_client, _openai_ready = None, False
    return _openai_client


def score_dimensions_with_openai(text: str, hit_subtypes: set) -> dict:
    """用 OpenAI 兼容接口（Responses API）做六维判分 + 类型复核 + 证据/理由；失败返回 None。"""
    client = _get_openai_client()
    if client is None:
        return None
    prompt = _build_prompt(text, hit_subtypes)
    try:
        # input 必须传消息列表（proxy 在并发下会拒绝纯字符串: "Input must be a list"）
        r = client.with_options(timeout=NIHILISM_LLM_TIMEOUT, max_retries=0).responses.create(
            model=_OPENAI_MODEL,
            input=[{"role": "user", "content": prompt}],
            reasoning={"effort": "low"},
        )
        return _parse_llm_judgment((r.output_text or "").strip(), hit_subtypes)
    except Exception as e:
        print(f"[nihilism_scorer] OpenAI 六维判分失败: {e}")
        return None


def score_dimensions_with_llm(text: str, hit_subtypes: set, *,
                               ai_client, ai_call_allowed,
                               ai_record_success, ai_record_failure) -> dict:
    """用 Anthropic 接口做六维判分 + 类型复核 + 证据/理由；失败/熔断返回 None。"""
    if not ai_call_allowed():
        return None
    prompt = _build_prompt(text, hit_subtypes)
    try:
        chunks = []
        with ai_client.with_options(timeout=NIHILISM_LLM_TIMEOUT, max_retries=0).messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,   # 六维分值 + 类型 + 证据短语 + 理由，比纯打分需要更多 token
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for t in stream.text_stream:
                chunks.append(t)
        result = _parse_llm_judgment("".join(chunks).strip(), hit_subtypes)
        ai_record_success()
        return result
    except Exception as e:
        ai_record_failure()
        print(f"[nihilism_scorer] Anthropic 六维判分失败: {e}")
        return None


def _build_rejudge_prompt(text: str, dim_ids: list) -> str:
    """零维保底专用 prompt：只针对初判为 0 的维度，让模型看完整正文重打分。"""
    dim_meta = {d["id"]: d for d in _RUBRIC["dimensions"]}
    lines = []
    for did in dim_ids:
        meta = dim_meta.get(did, {})
        bands = "；".join(f"{k}={v}" for k, v in meta.get("bands", {}).items())
        lines.append(f"- {did}（{meta.get('name_cn', did)}）：{meta.get('definition', '')}。分档：{bands}")
    dims_text = "\n".join(lines)
    keys = ",".join(f'"{d}":0' for d in dim_ids)
    return f"""你是历史虚无主义内容风险评估专家。下列维度在初次「逐句」判分中得分为 0，可能是分句时语境割裂被低估。请你结合【完整正文】重新为这些维度打分：

{dims_text}

评分要求：每维取 0-5 的整数，分值越高风险越强；请依据完整语境独立判断，不要沿用逐句结论。

完整正文：
{text[:1500]}

只输出 JSON 对象，不要 markdown、不要解释：
{{"scores":{{{keys}}}}}"""


def _parse_rejudge(raw: str, dim_ids: list) -> dict:
    """解析零维重判输出，只保留请求的维度且取值合法（0-5）。解析不出对应维度则略过。"""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"零维重判未找到 JSON：{raw[:100]}")
    data = json.loads(m.group())
    scores_obj = data.get("scores") if isinstance(data.get("scores"), dict) else data
    out = {}
    for did in dim_ids:
        try:
            v = int(scores_obj.get(did, -1))
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 5:
            out[did] = v
    return out


def rejudge_zero_dimensions(text: str, zero_dims: list, *, ai_client=None,
                             ai_call_allowed=None, ai_record_success=None,
                             ai_record_failure=None) -> dict:
    """对初判为 0 的维度，用大模型看完整正文重打分。

    判分顺序与主链路一致：OpenAI 兼容接口 → Anthropic。返回 {dim: 0-5}；
    模型不可用 / 调用失败返回 {}（由调用方按“强制最低 1 分”兜底）。
    """
    if not (ENABLE_NIHILISM_LLM_SCORING and zero_dims):
        return {}
    prompt = _build_rejudge_prompt(text, zero_dims)

    # 1) OpenAI 兼容接口优先
    client = _get_openai_client()
    if client is not None:
        try:
            r = client.with_options(timeout=NIHILISM_LLM_TIMEOUT, max_retries=0).responses.create(
                model=_OPENAI_MODEL,
                input=[{"role": "user", "content": prompt}],
                reasoning={"effort": "low"},
            )
            return _parse_rejudge((r.output_text or "").strip(), zero_dims)
        except Exception as e:
            print(f"[nihilism_scorer] OpenAI 零维重判失败: {e}")

    # 2) Anthropic 兜底
    if ai_client is not None and ai_call_allowed is not None and ai_call_allowed():
        try:
            chunks = []
            with ai_client.with_options(timeout=NIHILISM_LLM_TIMEOUT, max_retries=0).messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for t in stream.text_stream:
                    chunks.append(t)
            res = _parse_rejudge("".join(chunks).strip(), zero_dims)
            if ai_record_success:
                ai_record_success()
            return res
        except Exception as e:
            if ai_record_failure:
                ai_record_failure()
            print(f"[nihilism_scorer] Anthropic 零维重判失败: {e}")

    return {}


def _heuristic_judgment(prefilter: dict) -> dict:
    """LLM 不可用时的兜底：按命中的最高 tier 粗估六维分值，类型/证据退化为规则命中结果。"""
    TIER_SCORE = {"中度": 2, "高": 3, "极高": 4}
    top_tier = "中度"
    for h in prefilter["hits"]:
        if TIER_SCORE.get(h["tier"], 0) > TIER_SCORE.get(top_tier, 0):
            top_tier = h["tier"]
    base = TIER_SCORE[top_tier]
    dims = {dim: base for dim in DIMENSION_ORDER}

    evidence = []
    for h in prefilter["hits"]:
        if h["phrase"] not in evidence:
            evidence.append(h["phrase"])
        if len(evidence) >= 4:
            break

    return {
        "dimensions": dims,
        # 兜底时类型取规则全部命中，未经大模型复核（confirmed 在 detail 里会标为 None）
        "confirmed_types": sorted(prefilter["subtype_ids"]),
        "evidence": evidence,
        "reason": "AI 服务暂不可用，按规则命中的最高档估算，类型与证据未经大模型复核。",
    }


# ══════════════════════════════════════════════
#  阶段三：规则后处理
# ══════════════════════════════════════════════

def apply_escalations(dimensions: dict, prefilter: dict,
                       platform: str = "", age_group: str = "") -> dict:
    dims = dict(dimensions)
    applied = []

    # 规则1：closed_loop_rhetoric —— 用命中的小类近似判断"闭环话术"
    if {"1.6", "1.3"} & prefilter["subtype_ids"]:
        dims["fact_handling"] = min(5, dims["fact_handling"] + 2)
        applied.append("closed_loop_rhetoric")

    # 规则2：youth_channel_boost —— 纯上下文判断
    if platform in YOUTH_CHANNELS or age_group in ("6-12", "13-15"):
        dims["youth_impact"] = min(5, dims["youth_impact"] + 2)
        applied.append("youth_channel_boost")

    # 总分严格等于六维之和 —— 保证"风险等级"与界面「六维分析」完全对应，
    # 不再在总分上单独 ±2（那样会导致 总分 ≠ 六维之和、两者脱节）。
    # 多类叠加 / 标题降档只保留"升/降一档"的语义（level_shift）。
    total = sum(dims.values())
    level_shift = 0

    # 升降档规则（暂时关闭；置 ENABLE_LEVEL_SHIFT=True 或环境变量恢复）：
    #   规则3 multi_category_stack —— 命中≥2大类，升一档
    #   规则4 title_only_with_authoritative_body —— 仅标题命中且正文史料规范，降一档
    if ENABLE_LEVEL_SHIFT:
        if len(prefilter["category_ids"]) >= 2:
            level_shift += 1
            applied.append("multi_category_stack")
        if prefilter["title_only"] and dims["fact_handling"] <= 1:
            level_shift -= 1
            applied.append("title_only_with_authoritative_body")

    risk = _score_to_risk_level(total, level_shift)
    return {"dimensions": dims, "total_score": total, "applied": applied,
            "level_shift": level_shift, **risk}


def _score_to_risk_level(total: int, level_shift: int = 0) -> dict:
    idx = next(
        i for i, lv in enumerate(_RISK_LEVELS)
        if lv["score_range"][0] <= total <= lv["score_range"][1]
    )
    idx = max(0, min(len(_RISK_LEVELS) - 1, idx + level_shift))
    lv = _RISK_LEVELS[idx]
    return {"risk_code": lv["code"], "risk_label": lv["name_cn"], "action": lv["action"]}


def _build_detail(prefilter: dict, dims: dict, total_score: int,
                   applied: list, score_source: str, judgment: dict = None) -> dict:
    """构建给前端展示用的可读详情：六维分档解释 + 命中的话术套路（含大模型复核结论）
    + 命中短语 + 大模型证据/理由 + 升降档规则。"""
    judgment = judgment or {}
    dim_meta = {d["id"]: d for d in _RUBRIC["dimensions"]}
    dimensions = []
    for did in DIMENSION_ORDER:
        meta = dim_meta.get(did, {})
        score = dims[did]
        bands = meta.get("bands", {})
        band_desc = bands.get(score) or bands.get(str(score)) or ""
        dimensions.append({
            "id": did,
            "name": meta.get("name_cn", did),
            "definition": meta.get("definition", ""),
            "score": score,
            "max": 5,
            "band_desc": band_desc,
        })

    esc_meta = {r["id"]: r for r in _RUBRIC.get("escalation_rules", [])}
    escalations = [
        {"id": eid, "desc": esc_meta.get(eid, {}).get("trigger_detail", eid)}
        for eid in applied
    ]

    # 只有真正走了大模型判分，confirmed 复核结论才有意义；兜底时置 None（前端显示"未复核"）
    llm_confirmed = score_source == "llm"
    confirmed_set = set(judgment.get("confirmed_types") or [])
    subtypes = []
    for sid in sorted(prefilter["subtype_ids"]):
        s = _SUBTYPE_INDEX.get(sid, {})
        subtypes.append({
            "id": sid, "name": s.get("name", ""), "category": s.get("category_name", ""),
            "confirmed": (sid in confirmed_set) if llm_confirmed else None,
        })

    seen, phrases = set(), []
    for h in prefilter["hits"]:
        key = (h["phrase"], h["subtype_id"])
        if key in seen:
            continue
        seen.add(key)
        phrases.append({"phrase": h["phrase"], "subtype": h["subtype_id"],
                        "tier": h["tier"], "match": h.get("match", "exact")})

    return {
        "dimensions": dimensions,
        "total_score": total_score,
        "max_total": 30,
        "matched_subtypes": subtypes,
        "matched_phrases": phrases,
        "llm_reason": judgment.get("reason", ""),
        "llm_evidence": judgment.get("evidence") or [],
        "escalations": escalations,
        "score_source": score_source,
    }


# ══════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════

def _judge_one(seg_text: str, seg_prefilter: dict, *, ai_client,
               ai_call_allowed, ai_record_success, ai_record_failure):
    """对单个文本片段做一次判分，返回 (judgment, score_source)。

    判分顺序与整段判定一致：OpenAI 兼容接口 → Anthropic → 规则启发式兜底。
    """
    cand_ids = seg_prefilter["subtype_ids"]
    judgment = None
    score_source = "llm_fallback_heuristic"
    if ENABLE_NIHILISM_LLM_SCORING:
        judgment = score_dimensions_with_openai(seg_text, cand_ids)
        if judgment is not None:
            score_source = "llm"
        elif ai_client is not None:
            judgment = score_dimensions_with_llm(
                seg_text, cand_ids,
                ai_client=ai_client, ai_call_allowed=ai_call_allowed,
                ai_record_success=ai_record_success, ai_record_failure=ai_record_failure,
            )
            if judgment is not None:
                score_source = "llm"
    if judgment is None:
        judgment = _heuristic_judgment(seg_prefilter)
        score_source = "llm_fallback_heuristic"
    return judgment, score_source


def _split_sentences(text: str, title: str = "") -> list:
    """把标题 + 正文切成句子片段（去重、去过短），标题作为首个片段参与判定。"""
    segs = []
    if title and title.strip():
        segs.append(title.strip())
    segs += [s.strip() for s in _SENT_SPLIT_RE.split(text) if len(s.strip()) >= 4]
    seen, out = set(), []
    for s in segs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _build_sentence_record(seg_text: str, seg_prefilter: dict,
                            judgment: dict, score_source: str) -> dict:
    """把单句判分结果整理成前端/报告用的逐句记录。"""
    dims = judgment["dimensions"]
    total = sum(dims.values())
    risk = _score_to_risk_level(total, 0)
    llm_confirmed = score_source == "llm"
    confirmed_set = set(judgment.get("confirmed_types") or [])
    subs = []
    for sid in sorted(seg_prefilter["subtype_ids"]):
        s = _SUBTYPE_INDEX.get(sid, {})
        subs.append({
            "id": sid, "name": s.get("name", ""), "category": s.get("category_name", ""),
            "confirmed": (sid in confirmed_set) if llm_confirmed else None,
        })
    return {
        "text": seg_text,
        "dimensions": {d: dims[d] for d in DIMENSION_ORDER},
        "total_score": total,
        "risk_code": risk["risk_code"],
        "risk_label": risk["risk_label"],
        "matched_subtypes": subs,
        "llm_reason": judgment.get("reason", ""),
        "llm_evidence": judgment.get("evidence") or [],
        "score_source": score_source,
    }


# ══════════════════════════════════════════════
#  主入口（逐句判定 + 整页聚合）
# ══════════════════════════════════════════════

def run_nihilism_pipeline_stream(text: str, *, title: str = "", platform: str = "",
                                  age_group: str = "", ai_client=None,
                                  ai_call_allowed=None, ai_record_success=None,
                                  ai_record_failure=None):
    """逐句流式版：每判完一句 yield {"type":"sentence","sentence":rec}，
    末尾 yield {"type":"result","result":整页聚合}。整页未命中则不产出任何事件。

    - 每个命中句单独跑「六维 + 类型复核 + 证据 + 理由」，用 as_completed 判完一句冒一句。
    - 整页六维取各命中句逐维最大值（最严值）+ 零维保底，升降档按全页命中并集判定。
    - run_nihilism_pipeline 为其薄封装（drain 取末态），非流式路径行为不变。
    """
    if not _PHRASE_INDEX:
        return  # load_rules() 还没跑过，静默跳过
    whole = prefilter_hit_lexicon(text, title=title)
    if whole is None:
        return  # 整页都没命中，历史虚无引擎不介入

    ai_kw = dict(ai_client=ai_client, ai_call_allowed=ai_call_allowed,
                 ai_record_success=ai_record_success, ai_record_failure=ai_record_failure)

    # 分句 + 每句预筛，保留命中句（并发判分前先筛，省调用）
    hit_segs = []
    for seg in _split_sentences(text, title):
        pf = prefilter_hit_lexicon(seg)
        if pf is not None:
            hit_segs.append((seg, pf))
        if len(hit_segs) >= MAX_JUDGE_SENTENCES:
            print(f"[nihilism_scorer] 命中句超过 {MAX_JUDGE_SENTENCES} 句，超出部分本轮不逐句判定")
            break
    # 没有单句命中（可能是跨句命中）→ 退回整段作为单一片段判定，保证不漏
    if not hit_segs:
        hit_segs = [(text[:1500], whole)]

    sentences = []
    if len(hit_segs) == 1:
        seg, pf = hit_segs[0]
        rec = _build_sentence_record(seg, pf, *_judge_one(seg, pf, **ai_kw))
        sentences.append(rec)
        yield {"type": "sentence", "sentence": rec}
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=SENTENCE_CONCURRENCY) as pool:
            futs = {pool.submit(_judge_one, seg, pf, **ai_kw): (seg, pf) for seg, pf in hit_segs}
            for fut in concurrent.futures.as_completed(futs):
                seg, pf = futs[fut]
                rec = _build_sentence_record(seg, pf, *fut.result())
                sentences.append(rec)
                yield {"type": "sentence", "sentence": rec}   # 判完一句冒一句

    # 按风险从高到低排序，最严句排前面
    sentences.sort(key=lambda s: s["total_score"], reverse=True)

    # ── 整页聚合 ──
    agg_dims = {d: max((s["dimensions"][d] for s in sentences), default=0)
                for d in DIMENSION_ORDER}

    # ── 零维保底：某维在所有命中句里都是 0 → 用大模型看完整正文重判，仍为 0/不可用则强制最低 1 分。
    zero_dims = [d for d in DIMENSION_ORDER if agg_dims[d] == 0]
    if zero_dims:
        rj = rejudge_zero_dimensions(text, zero_dims, **ai_kw)
        for d in zero_dims:
            agg_dims[d] = max(1, rj.get(d, 0))

    union_subtypes, union_cats, confirmed_types = set(), set(), set()
    any_llm = False
    for s in sentences:
        for sub in s["matched_subtypes"]:
            union_subtypes.add(sub["id"])
            union_cats.add(sub["id"].split(".")[0])
            if sub["confirmed"]:
                confirmed_types.add(sub["id"])
        if s["score_source"] == "llm":
            any_llm = True

    overall_source = "llm" if any_llm else "llm_fallback_heuristic"
    agg_prefilter = {
        "hits": whole["hits"],
        "subtype_ids": union_subtypes or whole["subtype_ids"],
        "category_ids": union_cats or whole["category_ids"],
        "title_only": whole["title_only"],
    }
    top = sentences[0]  # 最严句，用它的理由/证据作为整页代表
    agg_judgment = {
        "confirmed_types": sorted(confirmed_types),
        "reason": top["llm_reason"],
        "evidence": top["llm_evidence"],
    }

    result = apply_escalations(agg_dims, agg_prefilter, platform=platform, age_group=age_group)
    result["detail"] = _build_detail(
        agg_prefilter, result["dimensions"], result["total_score"],
        result["applied"], overall_source, agg_judgment,
    )
    result["detail"]["sentences"] = sentences   # 逐句明细
    result["detail"]["rejudged_dims"] = zero_dims   # 触发零维保底重判的维度（可能为空）
    result["matched_subtypes"] = sorted(agg_prefilter["subtype_ids"])
    result["matched_categories"] = sorted(agg_prefilter["category_ids"])
    result["confirmed_subtypes"] = sorted(confirmed_types)
    result["escalations_applied"] = result.pop("applied")
    result["score_source"] = overall_source
    yield {"type": "result", "result": result}


def run_nihilism_pipeline(text: str, *, title: str = "", platform: str = "",
                           age_group: str = "", ai_client=None,
                           ai_call_allowed=None, ai_record_success=None,
                           ai_record_failure=None) -> dict:
    """非流式薄封装：drain 逐句流式生成器，返回末尾整页聚合结果；整页未命中返回 None。"""
    final = None
    for ev in run_nihilism_pipeline_stream(
        text, title=title, platform=platform, age_group=age_group,
        ai_client=ai_client, ai_call_allowed=ai_call_allowed,
        ai_record_success=ai_record_success, ai_record_failure=ai_record_failure,
    ):
        if ev.get("type") == "result":
            final = ev["result"]
    return final

