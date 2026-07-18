# 历史虚无主义六维判分引擎 —— 工程落地方案（详细设计）

> 状态：设计文档，尚未实现。配套的规则物料见同目录 `scoring_rubric.yaml` /
> `hit_lexicon.yaml` / `README.md`。本文档回答"具体怎么写代码接进
> `backend/main.py`"，粒度到函数、数据结构、Prompt 原文、接线点行号。

## 0. 目标与范围

**目标**：让 `/analyze` 返回的六维分值（`fact_handling` 等）从"用单一
`total_risk_score` 启发式拆出来的假数据"变成"真正按 `scoring_rubric.yaml`
六维标准由大模型打分、再套 `hit_lexicon.yaml` 命中语料 + 叠加规则算出来
的结果"。

**范围边界（重要）**：`scoring_rubric.yaml`/`hit_lexicon.yaml` 目前只覆盖
**历史认知风险 → 历史虚无主义**这一个 L1/L2 组合。制度认同风险、心理韧性
风险等其余 4 个 L1 域目前没有对应的六维规则文档，本方案**不会、也不能**
覆盖它们——新引擎只在命中"历史虚无主义"相关内容时接管六维打分，其余
域继续走现有的启发式 `_dimension_score()`，行为不变。这是一个可以按域
逐步铺开的架构，不是推倒重来。

## 1. 现状回顾（详见 README.md，此处只列本方案要修改/依赖的点）

| 现状代码 | 位置 | 本方案的关系 |
|---|---|---|
| `_KEYWORDS` + `load_keywords()` | main.py:73-111 | 参考其"启动期加载进内存"模式，新增同构的 `load_hit_lexicon()`/`load_scoring_rubric()` |
| `match_keywords()` | main.py:361-368 | 新增平行函数 `match_hit_lexicon()`，不改动原函数 |
| `rag_cosine_search()` / `rag_search()` | main.py:370-509 | 语义兜底沿用，新增专供命中语料库的向量集合 |
| `NARRATIVE_RULES` | main.py:343-359 | 不改，六维引擎与叙事模式徽章是两套独立展示逻辑 |
| `_dimension_score()` / `_build_structured_result()` | main.py:583-692 | **核心接线点**，新增分支：命中历史虚无主义时用真实六维分值覆盖启发式结果 |
| `_ai_call_allowed/_ai_record_success/_ai_record_failure` | main.py:717-738 | 复用同一套熔断器，新的 LLM 调用和 `adapt_scripts_sync` 共享失败计数 |
| `analyze_from_corpus()` / `analyze_realtime()` | main.py:1307-1601 | **核心接线点**，在算出 `primary_domain`/`matched_tags` 之后插入新引擎调用 |
| `/ai_analyze` 端点 | main.py:2722-2787 | 不复用（它是给"未收录内容"做简单单标签判定的独立轻量接口，服务于 dashboard 前端而非插件），但其 JSON 解析写法（`re.search(r'\{.*\}', text, re.DOTALL)`）直接复用 |
| `_result_cache` (TTL 30min) | main.py:81-87 | 新引擎的六维结果作为 `result` 字典的一部分自然被缓存，不需要单独加缓存层 |

## 2. 总体架构

新增一个独立模块文件 **`backend/nihilism_scorer.py`**（不再继续往 3665
行的 `main.py` 里堆代码），对外只暴露一个函数：

```python
def run_nihilism_pipeline(text: str, *, title: str = "", platform: str = "",
                           age_group: str = "") -> dict | None:
    """
    历史虚无主义六维判分主入口。
    命中则返回：
        {
            "dimensions": {"fact_handling": 3, "narrative_frame": 4, ...},  # 0-5 整数 ×6
            "total_score": 21,          # 0-30，已套用叠加规则后的最终值
            "risk_code": "L3",          # 沿用 main.py 的 L0-L4 编码
            "risk_label": "高风险",
            "action": "限制推荐流量，要求删除贬损、阴谋类表述。",
            "matched_subtypes": ["1.6", "3.1"],   # 命中的命中规则小类 id
            "matched_categories": [1, 3],          # 命中的大类 id（供"多类叠加"规则判断用）
            "escalations_applied": ["closed_loop_rhetoric", "multi_category_stack"],
            "score_source": "llm",      # llm | llm_fallback_heuristic，供埋点/灰度对比用
        }
    未命中任何历史虚无主义关键词/语料时返回 None，调用方原样走现有逻辑。
    """
```

`main.py` 里只需要 `from nihilism_scorer import run_nihilism_pipeline`，
在两个分析入口各加几行调用代码（见第 5 节），不侵入模块内部实现，方便
以后其他域（如果之后也整理出六维规则文档）复制这个模块模式再加一个
`xxx_scorer.py`，而不是继续在 `main.py` 里堆条件分支。

内部分三个阶段，与既定技术路线（关键词规则预筛 + RAG + 大模型判别 + 规则
后处理）一一对应：

```
run_nihilism_pipeline(text, title, platform, age_group)
  ├─ 1. prefilter_hit_lexicon(text, title)       # 关键词/语义预筛
  │     → 命中语料库 → 命中小类列表 + 大类列表 + 命中位置(标题/正文)
  │     → 完全未命中 → 直接返回 None，不调用大模型（省成本）
  ├─ 2. score_dimensions_with_llm(text, hit_subtypes)  # 大模型六维判分
  │     → 调 Claude，返回 {dimension_id: 0-5} ×6
  │     → 失败/熔断 → 降级为启发式（不是空手返回，见 4.4）
  └─ 3. apply_escalations(dimensions, hit_categories, platform, age_group, title_only)
        → 按 scoring_rubric.yaml 的 escalation_rules 调整分值/总分/等级
        → 映射到 risk_levels，返回最终结构
```

## 3. 数据加载层（模块级单例，启动时加载一次）

在 `nihilism_scorer.py` 顶部：

```python
import os, yaml

_RULES_DIR = os.path.join(os.path.dirname(__file__), "rules", "historical_nihilism")

_RUBRIC: dict = {}          # scoring_rubric.yaml 原样加载
_LEXICON: dict = {}         # hit_lexicon.yaml 原样加载
_PHRASE_INDEX: list = []    # 铺平后的 [{phrase, subtype_id, category_id, tier}]
_SUBTYPE_INDEX: dict = {}   # subtype_id -> 该小类的完整字典（含 judged_examples）

def load_rules():
    global _RUBRIC, _LEXICON, _PHRASE_INDEX, _SUBTYPE_INDEX
    with open(os.path.join(_RULES_DIR, "scoring_rubric.yaml"), encoding="utf-8") as f:
        _RUBRIC = yaml.safe_load(f)
    with open(os.path.join(_RULES_DIR, "hit_lexicon.yaml"), encoding="utf-8") as f:
        _LEXICON = yaml.safe_load(f)

    _PHRASE_INDEX = []
    _SUBTYPE_INDEX = {}
    for cat in _LEXICON["categories"]:
        for sub in cat["subtypes"]:
            _SUBTYPE_INDEX[sub["id"]] = {**sub, "category_id": cat["id"], "category_name": cat["name"]}
            for tier, phrases in sub["hit_phrases"].items():
                for phrase in phrases:
                    if len(phrase) >= 4:   # 太短的短语误伤率高，按项目已有 jieba 分词习惯设最短长度
                        _PHRASE_INDEX.append({
                            "phrase": phrase, "subtype_id": sub["id"],
                            "category_id": cat["id"], "tier": tier,
                        })
    print(f"✓ 历史虚无命中规则加载：{len(_SUBTYPE_INDEX)} 小类，{len(_PHRASE_INDEX)} 条命中短语")
```

**接线点**：`main.py` 的 `@app.on_event("startup")`（main.py:233-240）里，
在 `load_keywords()` 之后加一行 `nihilism_scorer.load_rules()`。

**为什么不建 DB 表**：`hit_lexicon.yaml`/`scoring_rubric.yaml` 是治理规则
文档的直接映射，更新节奏由"命中规则文档"驱动而不是运营人员在后台
增删——所以维护主体应该是文件+`build_hit_lexicon.py`重新生成，而不是
数据库里的一张可增删表。如果未来要给运营开一个"手动调整命中短语"的
后台页面，再迁移成 `t_hit_lexicon` 表也不迟，且迁移路径很直接
（`_PHRASE_INDEX` 这层结构和一张表几乎一一对应）。

## 4. 阶段一：关键词/语义预筛

```python
def prefilter_hit_lexicon(text: str, title: str = "") -> dict | None:
    """
    返回：
        {
            "hits": [{"phrase":..., "subtype_id":..., "category_id":..., "tier":..., "in_title": bool}],
            "subtype_ids": {...},      # 去重后的命中小类 id 集合
            "category_ids": {...},     # 去重后的命中大类 id 集合（供"多类叠加"规则用）
            "title_only": bool,        # 命中短语是否全部只出现在标题、正文完全没命中
        }
    未命中返回 None。
    """
    full_text = f"{title} {text}"
    hits = []
    for entry in _PHRASE_INDEX:
        phrase = entry["phrase"]
        if phrase in full_text:
            hits.append({**entry, "in_title": phrase in title})
    if not hits:
        return None   # 精确匹配失败时，是否要接 rag_cosine_search 语义兜底，见 4.1

    subtype_ids = {h["subtype_id"] for h in hits}
    category_ids = {h["category_id"] for h in hits}
    title_only = bool(hits) and all(h["in_title"] for h in hits)
    return {"hits": hits, "subtype_ids": subtype_ids,
            "category_ids": category_ids, "title_only": title_only}
```

### 4.1 语义兜底（精确匹配失败时）

现有 `rag_cosine_search()`（main.py:370-436）是对 `_KEYWORDS`（四库关键词表）
建的 Chroma collection，`hit_lexicon.yaml` 里的短语目前没有对应的向量库。
建议：

- **Phase 1（先落地精确匹配版）**：`_PHRASE_INDEX` 只做精确子串匹配，
  不接语义兜底。原因：命中语料库本身就是"预设话术模式"的穷举，覆盖面
  已经比关键词表窄，先用精确匹配验证整条链路（含大模型判分、规则后处理）
  能跑通、准确率如何，语义兜底作为 Phase 2 的独立任务，避免一次改动
  同时引入两个不确定因素（新判分逻辑 + 新向量检索）。
- **Phase 2（如果精确匹配命中率不够）**：仿照 `build_keyword_collection()`
  （main.py:199-218）新增一个 `_hit_phrase_collection`，把 `_PHRASE_INDEX`
  逐条 embed 存 Chroma，命中阈值参考现有 `CASE_SIM_THRESHOLD = 0.65`
  （main.py:858）。

### 4.2 标题/正文区分（为叠加规则 4 服务）

`escalation_rules` 里的 `title_only_with_authoritative_body` 规则要求
区分"标题命中但正文完整补充权威史料"。上面 `title_only` 字段只解决了
"命中短语是否只出现在标题"的一半问题——另一半"正文是否补充了权威史料
背景"无法用关键词判断，必须交给大模型在打分时一并判断（体现在
`fact_handling` 维度的分值里，如果正文确实引用权威史料，模型应该打
低分）。所以这条规则在代码里的判定条件是：
`title_only == True` **且** `dimensions["fact_handling"] <= 1`
（对照 `scoring_rubric.yaml` 里 fact_handling=0/1 的定义就是"引用权威
正史"），而不是单靠 `title_only` 就下调分值。第 6 节的规则引擎会体现
这个组合判断。

### 4.3 多大类叠加

`category_ids` 集合的大小就是"命中虚无大类数量"，`escalation_rules` 里
`multi_category_stack` 规则的触发条件是 `len(category_ids) >= 2`，直接
读 `prefilter_hit_lexicon()` 的返回值即可，不需要额外计算。

### 4.4 完全未命中时的行为

`run_nihilism_pipeline()` 在 `prefilter_hit_lexicon()` 返回 `None` 时
直接返回 `None`，调用方（`analyze_from_corpus`/`analyze_realtime`）按
现有逻辑继续跑，即完全不影响其余 4 个风险域、也不影响"历史认知风险"
域里不属于历史虚无主义套路的内容（那些内容目前也是启发式打分，暂不
处理，等对应命中规则文档整理出来再复用同一套模块模式）。

## 5. 阶段二：大模型六维判分

### 5.1 Prompt 设计

```python
DIMENSION_ORDER = ["fact_handling", "narrative_frame", "emotional_mobilization",
                    "value_orientation", "expression_packaging", "youth_impact"]

def _build_prompt(text: str, hit_subtypes: set) -> str:
    dim_lines = []
    for dim in _RUBRIC["dimensions"]:
        bands = "；".join(f"{k}={v}" for k, v in dim["bands"].items())
        dim_lines.append(f"- {dim['id']}（{dim['name_cn']}）：{dim['definition']}。分档：{bands}")
    dims_text = "\n".join(dim_lines)

    # few-shot：优先用命中小类自带的判例，没命中任何小类时不给 few-shot
    # （不给通用 few-shot，避免误导模型把普通历史内容也往虚无主义方向靠）
    fewshot_text = ""
    if hit_subtypes:
        # 每个命中小类只取 1 条判例（中度风险，代表"刚好开始出问题"的边界样本，
        # 比低风险判例更有区分度，比高/极高判例更不容易让模型"锚定"过高分）
        examples = []
        for sid in list(hit_subtypes)[:2]:   # 最多取 2 个小类，控制 prompt 长度
            sub = _SUBTYPE_INDEX.get(sid)
            if not sub:
                continue
            mid_example = next((e for e in sub["judged_examples"] if e["risk_level"] == "中度风险"), None)
            if mid_example:
                examples.append(
                    f"【{sub['name']}】归类逻辑：{sub['classification_logic']}\n"
                    f"判例：{mid_example['reasoning_text']}"
                )
        fewshot_text = "\n\n".join(examples)

    return f"""你是历史虚无主义内容风险评估专家。请严格按以下六个维度给内容打分，
每个维度取值 0-5 的整数，分值越高风险越强：

{dims_text}

{"参考同类命中规则的判例，帮助你校准打分尺度：\n\n" + fewshot_text if fewshot_text else ""}

待评估内容：
{text[:1500]}

只输出 JSON 对象，不要 markdown 代码块、不要任何解释文字：
{{"fact_handling":0,"narrative_frame":0,"emotional_mobilization":0,"value_orientation":0,"expression_packaging":0,"youth_impact":0}}"""
```

设计取舍说明：

- **few-shot 只取"中度风险"判例**：命中规则文档每个小类有 5 档判例
  （低/轻度/中度/高/极高），全塞进去 Prompt 太长（单个小类 5 档判例
  加起来将近 500-800 字）。选中度风险作为锚点，是因为它是"刚好从
  正常表述滑向虚无话术"的分界点，比低风险判例（信息量少）或极高风险
  判例（容易让模型对所有输入都倾向打高分，即"锚定效应"）更有校准价值。
  如果后续离线评测（见第 8 节）发现模型系统性打分偏低/偏高，可以调整
  这里的判例选取策略（比如按命中的 tier 选同档判例，而不是固定选
  "中度风险"）。
- **text 截断 1500 字**：对齐 `analyze_realtime()` 里 `text[:4000]` 的
  惯例但更短，因为六维判分不需要通篇原文，配合 `adapt_scripts_sync()`
  里 `case_ctx`/`base_content[:300]` 这种"够用就好"的截断风格。
- **最多 2 个 few-shot 小类**：预筛可能同时命中多个小类（比如"揭秘型"+
  "阴谋论型"），全部展开会让 Prompt 线性增长，2 个基本能覆盖组合命中
  的主要模式。

### 5.2 调用与解析

```python
import json, re
from anthropic import Anthropic

def score_dimensions_with_llm(text: str, hit_subtypes: set,
                               ai_client: Anthropic,
                               ai_call_allowed, ai_record_success, ai_record_failure) -> dict:
    """
    返回 {dimension_id: int} ×6。失败时返回 None（由调用方决定是否降级）。
    ai_call_allowed/ai_record_success/ai_record_failure 是 main.py 里现成的
    熔断器函数，直接传引用进来，不在本模块里重复造一套熔断状态。
    """
    if not ai_call_allowed():
        return None
    prompt = _build_prompt(text, hit_subtypes)
    try:
        chunks = []
        with ai_client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for t in stream.text_stream:
                chunks.append(t)
        raw = "".join(chunks).strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"未找到 JSON：{raw[:100]}")
        data = json.loads(m.group())
        result = {}
        for dim in DIMENSION_ORDER:
            v = int(data.get(dim, -1))
            if not (0 <= v <= 5):
                raise ValueError(f"维度 {dim} 取值越界：{v}")
            result[dim] = v
        ai_record_success()
        return result
    except Exception as e:
        ai_record_failure()
        print(f"[nihilism_scorer] 六维判分失败: {e}")
        return None
```

关键设计点：**六维分值做严格校验**（`0 <= v <= 5` 且六个 key 齐全），
校验失败当作调用失败处理，触发降级——这是因为六维分值会直接决定风险
等级、进而决定"暂缓拦截发布"这类处置动作，比 `adapt_scripts_sync()`
里"改写失败就跳过"的容错要求更高，宁可退回启发式也不能用一个格式错误
但被误解析出来的分值。

### 5.3 超时控制

参考 `/chat/stream` 里 `asyncio.wait_for(loop.run_in_executor(...), timeout=8)`
（main.py:2835-2837）的模式。`run_nihilism_pipeline` 是在
`analyze_realtime`/`analyze_from_corpus` 内部被同步调用的（这两个函数本身
已经通过 `loop.run_in_executor` 在线程池里跑，main.py:1629/1631），所以
`score_dimensions_with_llm` 内部不需要再包一层 `asyncio`，但需要给
`ai_client.messages.stream` 加超时保护——`anthropic` SDK 支持
`with_options(timeout=...)`，建议：

```python
with ai_client.messages.stream(..., ).with_options(timeout=6.0) as stream:
    ...
```

避免单次分析请求因为大模型响应慢而拖垮 `/analyze` 接口整体的响应时间
（插件侧是同步等待弹窗展示结果的，главный 体验诉求是"快"）。

### 5.4 降级策略

`run_nihilism_pipeline` 在 `score_dimensions_with_llm` 返回 `None` 时，
**不是直接放弃**，而是退回现有的 `_dimension_score()` 启发式（把
`prefilter_hit_lexicon` 命中的最高 tier 映射成一个近似分值），保证
"历史虚无主义命中了关键词，但这次大模型调用恰好失败/熔断"时，用户
依然能看到一个合理的风险等级，而不是该内容突然"无风险"。这与现有
`adapt_scripts_sync`/`ai_analyze` 的降级哲学（有兜底不空手）保持一致。
返回结果里 `score_source` 字段标记这次是 `"llm"` 还是
`"llm_fallback_heuristic"`，方便日后统计降级发生的频率，判断要不要
提高熔断阈值或优化超时时间。

## 6. 阶段三：规则后处理（叠加/降档）

```python
def apply_escalations(dimensions: dict, prefilter: dict,
                       platform: str, age_group: str) -> dict:
    """按 scoring_rubric.yaml 的 escalation_rules 顺序处理，返回：
        {"dimensions": {...}, "total_score": int, "applied": [rule_id, ...]}
    """
    dims = dict(dimensions)
    applied = []

    # 规则 1：closed_loop_rhetoric —— target=dimension，需要文本层面判断
    # "闭环话术"（越辟谣越真/删帖=心虚），这类表述本身就应该被预筛的
    # "1.6 删帖控评型"/"1.4 真相反转型" 等小类命中，所以触发条件简化为：
    # 命中小类里包含 1.6 或 1.3（官方隐瞒型，含类似闭环逻辑的判例）
    if {"1.6", "1.3"} & prefilter["subtype_ids"]:
        dims["fact_handling"] = min(5, dims["fact_handling"] + 2)
        applied.append("closed_loop_rhetoric")

    # 规则 2：youth_channel_boost —— target=dimension，纯上下文判断，不看文本
    YOUTH_CHANNELS = {"抖音", "快手", "B站", "小红书"}   # 短视频/青少年活跃平台
    if platform in YOUTH_CHANNELS or age_group in ("6-12", "13-15"):
        dims["youth_impact"] = min(5, dims["youth_impact"] + 2)
        applied.append("youth_channel_boost")

    total = sum(dims.values())

    # 规则 3：multi_category_stack —— target=total，看预筛命中的大类数
    level_shift = 0
    if len(prefilter["category_ids"]) >= 2:
        total = min(30, total + 2)
        level_shift += 1
        applied.append("multi_category_stack")

    # 规则 4：title_only_with_authoritative_body —— target=total，降档
    # 条件：命中短语只出现在标题 + fact_handling 维度本身不高（模型已经
    # 判断正文史料处理规范），见 4.2 的组合判断说明
    if prefilter["title_only"] and dims["fact_handling"] <= 1:
        total = max(0, total - 2)
        level_shift -= 1
        applied.append("title_only_with_authoritative_body")

    risk = _score_to_risk_level(total, level_shift)
    return {"dimensions": dims, "total_score": total, **risk, "applied": applied}


def _score_to_risk_level(total: int, level_shift: int) -> dict:
    levels = _RUBRIC["risk_levels"]  # 已按 L0..L4 顺序排列
    idx = next(i for i, lv in enumerate(levels) if lv["score_range"][0] <= total <= lv["score_range"][1])
    idx = max(0, min(len(levels) - 1, idx + level_shift))
    lv = levels[idx]
    return {"risk_code": lv["code"], "risk_label": lv["name_cn"], "action": lv["action"]}
```

**应用顺序**（严格对应 `scoring_rubric.yaml` 里的注释）：先处理两条
`target: dimension` 的规则并重新求和，再处理两条 `target: total` 的
规则；`level_shift` 是两条 total 规则各自 ±1 的叠加结果（理论上最多
同时 +1/-1，不会两条同时触发到 +2 的地步，因为触发条件互斥——多类叠加
要求命中≥2大类，标题降档要求 `fact_handling<=1`，实践中两者较少同时
成立，但代码仍按"分别计算、最后叠加 level_shift"处理，不假设互斥，
更稳健）。

## 7. 与 main.py 的接线点（逐处列出）

### 7.1 启动加载

main.py:233-240 `startup()` 函数里新增一行：

```python
@app.on_event("startup")
async def startup():
    _init_pool()
    load_keywords()
    load_scripts()
    build_keyword_collection()
    build_case_index()
    build_positive_case_index()
    nihilism_scorer.load_rules()   # 新增
```

### 7.2 实时分析路径 `analyze_realtime()`

main.py:1474-1601。在算出 `matched`/`primary`（约 1541 行 `primary =
max(domain_scores, key=domain_scores.get)`）之后插入：

```python
nihilism_result = None
if primary == "历史认知风险":
    nihilism_result = nihilism_scorer.run_nihilism_pipeline(
        text, title=title, platform=detect_platform(url), age_group=age_group,
    )
```

返回字典里新增一个键 `"_nihilism": nihilism_result`（下划线前缀，标记为
内部字段，不直接暴露给插件，`_build_structured_result()` 消费后即可）。

### 7.3 语料库命中路径 `analyze_from_corpus()`

main.py:1307-1468，同样在算出 `primary`（约 1322 行）之后插入相同调用，
`text` 用 `full_text`（约 1309 行已经拼好的 title+content_summary+full_text）。

### 7.4 `_build_structured_result()` 改造

main.py:597-692，这是最终决定六维数值的地方。核心改动在
`dimensions = {...}`（约 648-655 行）这段：

```python
nihilism = result.get("_nihilism")
if nihilism:
    dimensions = nihilism["dimensions"]
    # 命中历史虚无主义引擎时，风险等级/处置建议也优先用它的结论
    risk_code = nihilism["risk_code"]
    risk_level = nihilism["risk_label"]
    suggested_action = [nihilism["action"]]
    risk_score = round(nihilism["total_score"] / 6, 2)  # 折算回原有 0-5 量纲，兼容前端已有展示逻辑
else:
    # 现有启发式逻辑原样保留，服务于其余 4 个风险域
    dimensions = {
        "fact_handling": _dimension_score(content_score or risk_score),
        ...
    }
```

注意 `risk_score`/`review_priority` 等既有字段的量纲是 0-5（对齐
`t_corpus.total_risk_score`），而 `nihilism["total_score"]` 是 0-30，
折算成 `total_score / 6` 是为了不破坏前端/插件已经按 0-5 量纲写的展示
逻辑（比如高亮阈值 `s>=3.5`，main.py:518）。这是本方案里**唯一**需要
"兼容旧量纲"的地方，其余字段（`risk_code`/`risk_label`/`suggested_action`）
本来就是离散枚举值，不存在量纲问题。

### 7.5 `result` 字典不要把 `_nihilism` 原样透出给插件

`/analyze` 接口目前把整个 `result` 字典原样返回（main.py:1635 `return
result`），如果 `_nihilism` 字段里包含 `matched_subtypes`/`escalations_applied`
这类内部调试信息，建议在返回前删掉或移到 `structured_result` 里一个
明确命名的 `debug` 子字段，避免插件端解析到未预期的字段结构。简单做法：
在 `_build_structured_result()` 最后 `return {...}` 之前 `result.pop("_nihilism",
None)`，需要调试信息时改用下面 7.6 的独立 debug 接口查。

### 7.6（可选）调试接口

开发/测试阶段建议加一个轻量接口，方便直接对着一段文本看六维判分结果，
不用绕经 `/analyze` 的 DB 查询/案例检索等无关逻辑：

```python
class NihilismDebugReq(BaseModel):
    text: str
    title: str = ""
    platform: str = ""
    age_group: str = ""

@app.post("/debug/nihilism_score")
async def debug_nihilism_score(req: NihilismDebugReq):
    result = nihilism_scorer.run_nihilism_pipeline(
        req.text, title=req.title, platform=req.platform, age_group=req.age_group,
    )
    return result or {"matched": False}
```

上线前可以去掉或加鉴权，仅供联调使用。

## 8. 验证方案

`hit_lexicon.yaml` 里 43 个小类各自的 5 条 `judged_examples` 天然是一份
**215 条带标注答案的评测集**（`example_sentence` + 文档给定的
`risk_level`），这是本方案相比"凭空写 Prompt 然后线上试错"的最大优势
——不需要额外标注数据就能离线验证新引擎的判分是否靠谱。

建议在 `backend/rules/historical_nihilism/` 下新增 `eval_scorer.py`：

```python
"""
用 hit_lexicon.yaml 自带的 215 条判例做离线评测：
把 example_sentence 喂给 run_nihilism_pipeline，
比较预测的 risk_code 和文档标注的 risk_level 是否落在同一档，
输出混淆矩阵和总体准确率。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import nihilism_scorer

LEVEL_TO_CODE = {"低风险": "L0", "轻度风险": "L1", "中度风险": "L2",
                 "高风险": "L3", "极高风险": "L4"}

def run_eval():
    nihilism_scorer.load_rules()
    total, correct = 0, 0
    confusion = {}
    for cat in nihilism_scorer._LEXICON["categories"]:
        for sub in cat["subtypes"]:
            for ex in sub["judged_examples"]:
                if not ex["example_sentence"]:
                    continue
                expected = LEVEL_TO_CODE[ex["risk_level"]]
                result = nihilism_scorer.run_nihilism_pipeline(ex["example_sentence"])
                actual = result["risk_code"] if result else "L0"
                total += 1
                correct += (actual == expected)
                confusion.setdefault((expected, actual), 0)
                confusion[(expected, actual)] += 1
    print(f"总体准确率: {correct}/{total} = {correct/total:.1%}")
    for (exp, act), cnt in sorted(confusion.items()):
        marker = "✓" if exp == act else "✗"
        print(f"  {marker} 期望{exp} 实际{act}: {cnt} 条")

if __name__ == "__main__":
    run_eval()
```

**验证节奏建议**：

1. 先跑一遍，如果准确率明显偏低（比如 < 60%），大概率是 Prompt 的六维
   分档描述不够清楚，或 few-shot 选取策略需要调整（见 5.1 的取舍说明），
   优先调 Prompt，不要急着调 `escalation_rules` 的阈值。
2. 特别关注"错判方向"：如果模型系统性地把中度/高风险判成低风险
   （宁可漏判也不误判），风险比反过来更大——这类误判会直接影响
   "暂缓拦截发布"这类处置动作是否被触发，建议对漏判方向的准确率单独
   拉出来看，而不是只看总体准确率。
3. 这份评测脚本也应该在以后每次改动 Prompt/`escalation_rules` 后重跑，
   防止无意中的回归。

## 9. 灰度与回滚

新增一个环境变量/配置开关（简单起见先用模块级常量，配合 main.py
现有"数据库连接失败就跳过"的朴素风格，不引入额外的配置中心）：

```python
# nihilism_scorer.py 顶部
ENABLE_NIHILISM_LLM_SCORING = os.environ.get("ENABLE_NIHILISM_LLM_SCORING", "true") == "true"
```

`run_nihilism_pipeline` 开头检查这个开关，为 `False` 时直接跳过大模型
调用、走纯启发式路径（等同于第 5.4 节的降级分支），这样如果上线后发现
新引擎判分不稳定，运维可以直接改环境变量重启服务回滚，不需要改代码
重新发布。

## 10. 性能与成本

- 每次命中历史虚无主义关键词才会触发一次大模型调用（`max_tokens=200`，
  Prompt 含六维定义+最多 2 条判例，预估输入 ~800-1200 tokens），对照
  `_ai_analyze`/`adapt_scripts_sync` 现有的调用规模量级相当，不需要
  额外扩容。
- `_result_cache` 已按 URL 缓存整份 `/analyze` 结果 30 分钟
  （main.py:1620-1624），六维判分结果包含在内自动被缓存，重复访问同一
  URL 不会重复调用大模型。
- 未命中关键词的内容（大多数情况）直接在 `prefilter_hit_lexicon()` 就
  返回 `None`，完全不产生大模型调用，成本可控。

## 11. 分步实施清单

1. 新建 `backend/nihilism_scorer.py`，实现 `load_rules()` +
   `prefilter_hit_lexicon()`（第 3、4 节），先不接大模型，用固定假分值
   跑通数据加载和预筛逻辑。
2. 实现 `score_dimensions_with_llm()`（第 5 节），单独写一个临时脚本
   （不接 main.py）调几条 `hit_lexicon.yaml` 里的例句，人工看输出是否
   合理，再进入下一步。
3. 实现 `apply_escalations()` + `_score_to_risk_level()`（第 6 节），
   补 `eval_scorer.py`（第 8 节），跑 215 条判例评测，迭代 Prompt 直到
   准确率达到可接受水平。
4. 按 7.1-7.4 节接入 `main.py`：启动加载 → `analyze_realtime` →
   `analyze_from_corpus` → `_build_structured_result`。**建议每接一处
   跑一次 `/analyze` 手动验证**，不要 4 处一次性改完再测。
5. 加 7.6 节的调试接口，方便前端/插件联调时对着具体文本看判分依据。
6. 接入 9 节的灰度开关，先在测试环境用真实历史虚无类内容跑一轮，观察
   延迟和熔断触发频率，再上生产。
7. （可选，Phase 2）如果精确匹配命中率不够，按 4.1 节接语义兜底。

## 12. 风险与待确认问题

- **规则 1（closed_loop_rhetoric）的触发条件是简化过的**：原文档定义
  是"出现闭环话术"这一语义模式，不是某个具体小类 id，第 6 节里用
  `{"1.6","1.3"} & subtype_ids` 做近似，可能有漏判（比如闭环话术出现在
  其他小类的文本里）。如果第 8 节评测发现这条规则命中率明显偏低，需要
  重新设计触发判断，比如单独给"闭环话术"提炼一个独立的短语子集。
- **规则 2（youth_channel_boost）的 `platform`/`age_group` 从哪来**：
  `analyze_realtime()` 目前的 `platform` 是从 URL 域名猜的
  （`detect_platform()`），`age_group` 是插件请求里用户手动选的三档之一
  （main.py:245），两者都已经在现有调用链路里，接线时直接透传即可，
  不需要新增数据源。
- **规则 4（标题命中/正文补充史料）依赖模型对 `fact_handling` 的判断
  是否准确**：如果模型本身在标题党内容上判断不准，这条降档规则可能
  被滥用（比如模型把明显有问题的正文也判成 `fact_handling<=1`），需要
  在第 8 节评测里专门挑几条"标题党但正文站得住脚"和"标题党且正文也有
  问题"的对照样本检验。当前 `hit_lexicon.yaml` 的判例里没有专门针对
  这条规则的样本，可能需要后续让业务方补充几个例句。
- **其余 4 个风险域没有覆盖**：本方案上线后，插件对"历史认知风险"类
  内容的判分会变得更准，但"制度认同风险"等其余域依然是启发式假数据，
  用户体验上会出现"同一个插件，不同风险域的分析深度不一致"的观感，
  需要和王怡欢确认是否要在近期把其余域的命中规则文档也补齐，走同一套
  `xxx_scorer.py` 模式接入。
