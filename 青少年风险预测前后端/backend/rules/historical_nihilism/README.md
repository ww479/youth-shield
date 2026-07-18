# 历史虚无主义命中规则 —— 配置说明

本目录是《历史虚无命中规则-20260708.docx》的结构化产物，是**规则物料**，
目前不含任何被 `backend/main.py` 实际调用的代码。这份说明写清楚：这批
文件是什么、以及未来接进 `main.py` 时该怎么接。

## 文件

- `scoring_rubric.yaml` —— 六维评分标准（`fact_handling`/`narrative_frame`/
  `emotional_mobilization`/`value_orientation`/`expression_packaging`/
  `youth_impact`，各 0-5 分）+ 五级风险总分区间（0-30 分，L0-L4）+ 4 条
  叠加/降档规则。手工整理，文档更新后需要人工核对再改。
- `hit_lexicon.yaml` —— 7 大类、43 小类的命中语料库（每类含中度/高/极高
  三档命中短语、归类逻辑、5 条风险等级判例；当前共 644 条命中短语，
  由 20260708 文档生成）。由 `build_hit_lexicon.py`
  从源 docx 自动解析生成，**不要手改这个文件**，改动应该改脚本或改源文档
  再重新生成。
- `build_hit_lexicon.py` —— 解析脚本，见文件内文档字符串。文档更新（比如
  改成 `历史虚无命中规则-20260901.docx`）后直接重跑 `python
  build_hit_lexicon.py` 即可，脚本会自动挑日期最新的一份 docx。

## 现状：main.py 里对应的缺口

`backend/main.py` 目前有三处和这套规则相关，但都还没真正实现六维判分：

1. `NARRATIVE_RULES`（约第 343 行）：只有 5 条通用叙事规则，覆盖 5 个
   L1 风险域各一条，和这里 43 个小类的命中语料体系无关。
2. `_dimension_score()` / `_build_structured_result()`（约第 583-692 行）：
   六维分值字段名虽然和 `scoring_rubric.yaml` 的 `dimensions[].id` 完全
   一致，但取值是把 `t_corpus` 里人工标注的单一 `total_risk_score`
   （0-5 分）用启发式规则拆出来的，**不是**按 `scoring_rubric.yaml` 里
   的六维标准分别打分。
3. 全文唯一调用大模型的地方是 `adapt_scripts_sync()` / `/scripts/adapt`
   （话术改写），风险判分完全没有调用大模型，靠数据库里预先标注好的
   `total_risk_score`/`risk_level`，或 `match_keywords()`/
   `rag_cosine_search()` 命中已有 `t_risk_label`/`t_corpus` 记录来决定。

也就是说，这套详细的六维规则和命中语料库目前在系统里是"设计蓝图"，
还没有真正跑起来。下一阶段实现时建议按下面三处接入，分别对应
`Desktop\项目\中青网项目\材料\青少年意识风险产品形态及方案` 里已经定的
"关键词规则预筛 + 对四库做 RAG + 大模型 API 判别 + 规则后处理"技术路线：

### 1. 关键词规则预筛 → `hit_lexicon.yaml`

参照现有 `_KEYWORDS`/`load_keywords()`（main.py 约第 73-111 行）的启动期
加载模式，新增一个 `load_hit_lexicon()`：把 `hit_lexicon.yaml` 里的
`hit_phrases` 铺平成 `(phrase, subtype_id, tier)` 三元组列表，接入
`match_keywords()`/`rag_cosine_search()` 同一条命中链路。命中后除了现在
的 `tag_id`，还应该带出 `subtype_id`（如 `"3.2"`），供后续大模型判别阶段
把该小类的 `judged_examples` 作为 few-shot 拼进 Prompt。

### 2. 大模型 API 判别 → `scoring_rubric.yaml`

新增一个真正做六维判分的函数（区别于现在名不副实的 `_dimension_score()`），
仿照 `adapt_scripts_sync()` 的写法调用现有 `ai_client.messages.stream`：

- Prompt 里拼入 `scoring_rubric.yaml` 的 `dimensions`（六维定义 + 0-5 分档
  文字）。
- 如果关键词预筛命中了具体小类，把该小类的 `classification_logic` 和
  1-2 条 `judged_examples` 作为 few-shot 附上，帮模型对齐打分尺度。
- 模型输出六维分值（0-5 整数 ×6），替换掉 `_build_structured_result()`
  里现在的启发式拆分逻辑。

### 3. 规则后处理 → `scoring_rubric.yaml` 的 `escalation_rules`

拿到大模型六维分值、求和得到总分之后，在映射到 `risk_levels` 的五级风险
之前，按 `escalation_rules` 里 4 条规则的 `trigger_type` 程序化判断是否
触发（`keyword_pattern`/`channel_context`/`multi_category`/
`content_structure`），再对 `dimension` 或 `total` 做加减分 / 等级平移。
注意 `escalation_rules` 里已经写明应用顺序：先处理 `target: dimension`
的规则、重新求和，再处理 `target: total` 的规则。

## 本轮明确没做的事

- 没有改 `backend/main.py` 任何一行。
- 没有接入 `ai_client` 做实际的大模型调用。
- 没有改数据库（`t_risk_label`/`t_corpus` 等表结构不变），`hit_lexicon.yaml`
  里的 `subtype_id` 目前也没有写回任何数据库字段。
- `judged_examples` 里的六维分值是保留原文整段文本（`reasoning_text`），
  没有把每个判例的六维数字拆成结构化字段——原文档在这部分本身有笔误
  （如 3.1 节判例里 `_orientation=0` 漏了字段名前缀），逐条掰开成结构化
  数字意义不大，够用于 Prompt few-shot 即可。

## 重新生成

```bash
cd backend/rules/historical_nihilism
python build_hit_lexicon.py
```

脚本会打印校验结果（应为 `43/43 个小类均含 3 类命中语料 + 5 条风险判例`）。
如果源文档格式发生变化（比如新增/重命名了某个小类），脚本会在找不到对应
小节标题时直接抛异常，需要先更新脚本里的 `CATEGORIES` 清单。
