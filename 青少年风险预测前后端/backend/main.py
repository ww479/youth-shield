# -*- coding: utf-8 -*-
# reload trigger
"""
青少年意识形态风险识别平台 — 后端 API
启动：cd backend && python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import os, re, json, asyncio, time
import io, datetime
try:
    from dotenv import load_dotenv
    load_dotenv()   # 读取 backend/.env（若存在），把配置注入环境变量
except Exception:
    pass
os.environ.setdefault("HF_HUB_OFFLINE", "1")   # 强制用本地缓存，跳过 HuggingFace 连接检查
import concurrent.futures
import jieba
import numpy as np
import requests
import httpx
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from bs4 import BeautifulSoup
import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from anthropic import Anthropic
from urllib.parse import quote, unquote
import chromadb
from sentence_transformers import SentenceTransformer
import nihilism_scorer
import six_dim_scorer

app = FastAPI(title="风险识别平台 API", version="1.0")

# ── Claude 客户端（中转/官方）：密钥与地址从环境变量读取，绝不写死在代码里 ──
_anthropic_key  = os.environ.get("ANTHROPIC_API_KEY", "")
_anthropic_base = os.environ.get("ANTHROPIC_BASE_URL", "")
_anthropic_kwargs = {"api_key": _anthropic_key or "sk-placeholder-set-ANTHROPIC_API_KEY",
                     "http_client": httpx.Client(trust_env=False, timeout=30)}  # 绕过系统代理 + 放宽超时
if _anthropic_base:
    _anthropic_kwargs["base_url"] = _anthropic_base
ai_client = Anthropic(**_anthropic_kwargs)
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(FRONTEND):
    app.mount("/ui", StaticFiles(directory=FRONTEND, html=True), name="frontend")

# ── 数据库连接池（连接信息从环境变量读取）──
DB = dict(host=os.environ.get("DB_HOST", "127.0.0.1"),
          port=int(os.environ.get("DB_PORT", "3306")),
          user=os.environ.get("DB_USER", "root"),
          password=os.environ.get("DB_PASSWORD", ""),
          database=os.environ.get("DB_NAME", "youth_ideology"),
          charset="utf8mb4", use_pure=True)

_db_pool: MySQLConnectionPool = None

def _init_pool():
    global _db_pool
    _db_pool = MySQLConnectionPool(pool_name="yipool", pool_size=8,
                                   pool_reset_session=True, **DB)

def get_conn():
    return _db_pool.get_connection()

def qone(sql, params=()):
    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute(sql, params); row = cur.fetchone()
    cur.close(); conn.close(); return row

def qall(sql, params=()):
    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute(sql, params); rows = cur.fetchall()
    cur.close(); conn.close(); return rows

# ── 关键词缓存 ──
_KEYWORDS: list = []

# ── 话术全量内存缓存（启动时加载，避免每次查 DB）──
_ALL_SCRIPTS: list = []

# ── 话术适配缓存（case_id → adapted_scripts dict，进程内持久）──
_script_adapt_cache: dict = {}

# ── 分析结果缓存（URL → result, TTL=30min）──
_result_cache: dict = {}
_RESULT_TTL = 1800

# 临时清空缓存（测试用）
_result_cache.clear()
_script_adapt_cache.clear()

# ── Chroma 向量库 ──
_embed_model  = None   # SentenceTransformer
_chroma_client = None
_kw_collection = None

def load_keywords():
    global _KEYWORDS
    _KEYWORDS = qall("""
        SELECT r.tag_id, r.keyword, r.risk_domain_l1, r.risk_type_l2,
               r.total_risk_score, r.content_risk_score, r.opposition_risk_score,
               r.l1_tag_code, r.l2_tag_code, r.l3_tag_code,
               r.tag_chain, r.guidance_direction,
               l1.l1_name, l2.l2_name, l3.l3_name
        FROM t_risk_label r
        LEFT JOIN t_l1_taxonomy l1 ON r.l1_tag_code = l1.l1_code
        LEFT JOIN t_l2_taxonomy l2 ON r.l2_tag_code = l2.l2_code
        LEFT JOIN t_l3_taxonomy l3 ON r.l3_tag_code = l3.l3_code
        WHERE r.keyword != ''
    """)
    for kw in _KEYWORDS:
        if kw["keyword"]:
            jieba.add_word(kw["keyword"], freq=10000)
    print(f"✓ 关键词库加载：{len(_KEYWORDS)} 条")

# ── 五大意识形态风险域词库（关键词簇 + 句式模板，seed_domain_lexicon.py 灌入）──
_DOMAIN_KW: list = []
_DOMAIN_TPL: list = []

def load_domain_lexicon():
    global _DOMAIN_KW, _DOMAIN_TPL
    try:
        _DOMAIN_KW = qall("SELECT domain_code,domain_id,domain_name,cluster,keyword FROM t_domain_keyword")
        _DOMAIN_TPL = qall("SELECT domain_code,domain_id,domain_name,template_code,pattern,risk_action FROM t_domain_template")
        for kw in _DOMAIN_KW:
            if kw.get("keyword"):
                jieba.add_word(kw["keyword"], freq=8000)
        print(f"✓ 五大风险域词库加载：关键词 {len(_DOMAIN_KW)} 条 / 句式模板 {len(_DOMAIN_TPL)} 条")
    except Exception as e:
        _DOMAIN_KW, _DOMAIN_TPL = [], []
        print(f"[五域词库] 加载失败（未灌库？先跑 seed_domain_lexicon.py）: {e}")

def match_five_domains(text: str) -> list:
    """子串匹配五大域关键词簇，按域聚合，返回识别标签用的域命中列表（按命中关键词数降序）。"""
    if not text or not _DOMAIN_KW:
        return []
    agg = {}   # domain_id -> {"domain_id","domain_name","clusters":set,"keywords":[]}
    for rec in _DOMAIN_KW:
        kw = rec.get("keyword") or ""
        if kw and kw in text:
            did = rec["domain_id"]
            a = agg.setdefault(did, {"domain_id": did, "domain_name": rec["domain_name"],
                                     "clusters": set(), "keywords": []})
            a["clusters"].add(rec.get("cluster") or "")
            if kw not in a["keywords"]:
                a["keywords"].append(kw)
    out = []
    for a in agg.values():
        out.append({"domain_id": a["domain_id"], "domain_name": a["domain_name"],
                    "clusters": sorted(c for c in a["clusters"] if c),
                    "keywords": a["keywords"][:12]})
    out.sort(key=lambda x: len(x["keywords"]), reverse=True)
    return out

# ── 案例索引（启动时构建，内存驻留）──
_case_collection = None
_case_records    = []

# ── 正向案例索引（中青网权威案例，独立于风险案例，启动时构建）──
_positive_case_collection = None
_positive_case_records    = []

def build_positive_case_index():
    """t_case 里 case_id 以 POS- 开头的记录是中青网正向案例，
    与 ZQW% 风险案例完全隔离，走独立的 Chroma collection。"""
    global _positive_case_collection, _positive_case_records
    rows = qall("""
        SELECT case_id, case_name, risk_domain_l1, risk_type_l2,
               core_keywords, representative_text, source_url
        FROM t_case WHERE case_name != '' AND case_id LIKE 'POS-%'
    """)
    if not rows or _embed_model is None or _chroma_client is None:
        return
    _positive_case_records = rows

    def index_text(row):
        parts = [
            row.get("case_name") or "",
            row.get("risk_type_l2") or "",
            row.get("core_keywords") or "",
            row.get("representative_text") or "",
        ]
        return " ".join(filter(None, parts))[:1200]

    texts = [index_text(r) for r in rows]
    embs  = _embed_model.encode(texts, batch_size=32, show_progress_bar=False).tolist()
    _positive_case_collection = _chroma_client.get_or_create_collection(
        "positive_cases", metadata={"hnsw:space": "cosine"}
    )
    _positive_case_collection.upsert(
        ids        = [f"pos_{r['case_id']}" for r in rows],
        embeddings = embs,
        documents  = texts,
        metadatas  = [{"idx": i, "risk_domain_l1": r.get("risk_domain_l1") or ""} for i, r in enumerate(rows)],
    )
    print(f"✓ 正向案例向量库构建：{len(rows)} 条")

def build_case_index():
    global _case_collection, _case_records
    rows = qall("""
        SELECT case_id, case_name, risk_domain_l1, risk_type_l2,
               risk_level, avg_risk_score, case_priority_score,
               platform,
               spread_mechanism, spread_path, disposal_strategy_6_12,
               disposal_strategy_13_15, disposal_strategy_16_18,
               representative_text, review_conclusion,
               CAST(total_interaction AS SIGNED) AS total_interaction,
               core_keywords, source_url
        FROM t_case WHERE case_name != '' AND case_id LIKE 'ZQW%'
    """)
    if not rows or _embed_model is None or _chroma_client is None:
        return
    _case_records = rows

    def index_text(row):
        parts = [
            row.get("case_name") or "",
            row.get("risk_domain_l1") or "",
            row.get("risk_type_l2") or "",
            row.get("core_keywords") or "",
            row.get("representative_text") or "",
            row.get("spread_path") or "",
            row.get("spread_mechanism") or "",
            row.get("review_conclusion") or "",
        ]
        return " ".join(filter(None, parts))[:1200]

    texts = [index_text(r) for r in rows]
    embs  = _embed_model.encode(texts, batch_size=32, show_progress_bar=False).tolist()
    _case_collection = _chroma_client.get_or_create_collection(
        "cases", metadata={"hnsw:space": "cosine"}
    )
    _case_collection.upsert(
        ids        = [f"case_{r['case_id']}" for r in rows],
        embeddings = embs,
        documents  = texts,
        metadatas  = [{"idx": i, "risk_domain_l1": r.get("risk_domain_l1") or ""} for i, r in enumerate(rows)],
    )
    print(f"✓ Chroma案例向量库构建：{len(rows)} 条")

def build_keyword_collection():
    """将关键词库 embed 后存入内存 Chroma collection，供语义检索使用。"""
    global _embed_model, _chroma_client, _kw_collection
    if not _KEYWORDS:
        return
    _embed_model   = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    _chroma_client = chromadb.Client()
    _kw_collection = _chroma_client.get_or_create_collection(
        "keywords", metadata={"hnsw:space": "cosine"}
    )

    texts = [kw["keyword"] for kw in _KEYWORDS]
    embs  = _embed_model.encode(texts, batch_size=32, show_progress_bar=False).tolist()
    _kw_collection.upsert(
        ids        = [f"kw_{i}" for i in range(len(_KEYWORDS))],
        embeddings = embs,
        documents  = texts,
        metadatas  = [{"idx": i} for i in range(len(_KEYWORDS))],
    )
    print(f"✓ Chroma关键词向量库构建：{len(_KEYWORDS)} 条")


def load_scripts():
    global _ALL_SCRIPTS
    _ALL_SCRIPTS = qall("""
        SELECT script_id, related_case_id, risk_domain_l1, age_group,
               script_type, script_phase, script_content,
               follow_up_action, applicable_risk_level, use_count
        FROM t_ai_script
        WHERE review_status IN (0,1)
        ORDER BY use_count DESC
    """)
    print(f"✓ 话术库加载：{len(_ALL_SCRIPTS)} 条")

@app.on_event("startup")
async def startup():
    _init_pool()                 # 连接池（优先初始化）
    load_keywords()
    load_domain_lexicon()        # 五大风险域词库（识别标签召回）
    load_scripts()               # 话术全量载入内存
    build_keyword_collection()   # embed model + chroma
    build_case_index()
    build_positive_case_index()  # 中青网正向案例向量库
    nihilism_scorer.load_rules() # 历史虚无主义命中规则 + 六维评分标准
    nihilism_scorer.init_semantic(_embed_model, _chroma_client)  # 复用现有向量库做命中短语语义匹配

# ── 请求模型 ──
class AnalyzeRequest(BaseModel):
    url: str
    age_group: str = "13-15"
    page_text: str = ""   # 由浏览器插件直接传入的页面文本，有则跳过爬取
    no_cache: bool = False   # 插件"点图标重新分析"时置 True，跳过结果缓存强制重算

class CaseSearchRequest(BaseModel):
    query: str
    domain: str = ""
    top_k: int = 5

class CorpusSearchRequest(BaseModel):
    query: str
    domain: str = ""
    platform: str = ""
    risk_level: str = ""
    top_k: int = 10

class VisualAIRequest(BaseModel):
    prompt: str
    preferred_visual: str = ""
    time_range: str = "近30天"
    data_source: str = "demo"

# ── 工具 ──
PLATFORM_MAP = {"weibo":"微博","xiaohongshu":"小红书","xhslink":"小红书",
                "douyin":"抖音","tiktok":"抖音","bilibili":"B站",
                "b23.tv":"B站","baidu":"百度","baijiahao":"百家号",
                "zhihu":"知乎","qq.com":"腾讯"}
def detect_platform(url):
    u = url.lower()
    for k,v in PLATFORM_MAP.items():
        if k in u: return v
    return "未知平台"

def get_embed_info(url: str) -> dict:
    """
    返回 { embed_url, thumb_url, embed_type }
    embed_type: 'iframe' | 'thumb' | 'none'
    """
    u = url.lower()

    # ── 抖音：提取 video_id → open player ──
    if "douyin.com/video/" in u:
        m = re.search(r"/video/(\d+)", url)
        if m:
            vid = m.group(1)
            return {
                "embed_url": f"https://open.douyin.com/player/video?vid={vid}&autoplay=1",
                "thumb_url": "",
                "embed_type": "iframe",
                "video_id": vid,
            }

    # ── B站：提取 BV 号 → 官方 player ──
    if "bilibili.com" in u or "b23.tv" in u:
        m = re.search(r"BV\w+", url)
        if m:
            bv = m.group(0)
            return {
                "embed_url": f"//player.bilibili.com/player.html?bvid={bv}&page=1&high_quality=1&autoplay=1",
                "thumb_url": "",
                "embed_type": "iframe",
                "video_id": bv,
            }

    # ── 其余：抓 OG 封面图 ──
    thumb = _fetch_og_image(url)
    return {
        "embed_url": url,
        "thumb_url": thumb,
        "embed_type": "thumb",
        "video_id": "",
    }

def _fetch_og_image(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name":"og:image"})
        if og and og.get("content"):
            return og["content"]
        # 兜底：找第一张图
        img = soup.find("img", src=True)
        if img:
            src = img["src"]
            if src.startswith("http"): return src
    except:
        pass
    return ""

AGE_MAP = {"6-12":"6-12岁","13-15":"13-15岁","16-18":"16-18岁"}

RISK_LEVELS = [(4.1,"L4","极高风险"),(3.1,"L3","高风险"),
               (2.1,"L2","中度风险"),(1.1,"L1","轻度风险"),(0.0,"L0","低风险")]
def score_to_level(score):
    for t,code,label in RISK_LEVELS:
        if score >= t: return code, label
    return "L0","暂无风险"

# ── 《五大风险域统一规则》：六维加权 → RiskPercent(0-100) → 1-5 级五色灯 ──
# 维度权重（新规则）：事实0.20 叙事0.20 情绪0.15 价值0.20 包装0.10 青少年0.15
SIX_DIM_WEIGHTS = {
    "fact_handling": 0.20, "narrative_frame": 0.20, "emotional_mobilization": 0.15,
    "value_orientation": 0.20, "expression_packaging": 0.10, "youth_impact": 0.15,
}
WARNING_LIGHTS = {1: "green", 2: "yellow", 3: "orange", 4: "red", 5: "purple"}
LEVEL_NUM_TO_CODE = {1: "L0", 2: "L1", 3: "L2", 4: "L3", 5: "L4"}
CODE_TO_LEVEL_NUM = {"L0": 1, "L1": 2, "L2": 3, "L3": 4, "L4": 5}

def weighted_risk_percent(dimensions: dict, dim_scale: float = 5.0) -> float:
    """六维加权求和 ×100。dim_scale=5 表示传入维度分是 0-5（现状）；
    未来 0/1 命中版维度分为 0-1，传 dim_scale=1 即可。未命中/缺失计 0。"""
    total = 0.0
    for key, w in SIX_DIM_WEIGHTS.items():
        v = _safe_float(dimensions.get(key), 0.0)
        norm = max(0.0, min(1.0, v / dim_scale if dim_scale else v))
        total += norm * w
    return round(total * 100, 1)

def percent_to_level(pct: float) -> int:
    """RiskPercent → 1-5 级：0-20/21-40/41-60/61-80/81-100。"""
    pct = max(0.0, min(100.0, _safe_float(pct)))
    if pct <= 20: return 1
    if pct <= 40: return 2
    if pct <= 60: return 3
    if pct <= 80: return 4
    return 5

NARRATIVE_RULES = [
    {"domain":"历史认知风险","pattern":"历史理想化 + 时代对比叙事",
     "desc":"将特定历史时期片面美化，与当下形成负向对比，属于发展道路否定类型",
     "level":"danger","icon":"🔴","badge":"历史虚无化叙事"},
    {"domain":"制度认同风险","pattern":"制度合法性隐性质疑",
     "desc":"以自由、民主等话语框架对现行制度进行隐性否定，属于制度合法性质疑类型",
     "level":"danger","icon":"🔴","badge":"制度质疑框架"},
    {"domain":"认知闭合风险","pattern":"伪中立知识化叙事",
     "desc":"借助知识性、专业性话语包裹意识形态倾向，属于价值软包装策略",
     "level":"warning","icon":"🟠","badge":"伪中立包装"},
    {"domain":"心理韧性风险","pattern":"情感共鸣驱动传播",
     "desc":"以群体身份认同为情感锚点，激发共鸣后引导快速传播，二次扩散风险较高",
     "level":"warning","icon":"🟠","badge":"情感渗透手法"},
    {"domain":"网络素养风险","pattern":"平台算法偏置内容",
     "desc":"内容借助平台推荐机制精准触达青少年，算法放大效应显著",
     "level":"info","icon":"🔵","badge":"算法渗透路径"},
]

# ── 精确关键词匹配 ──
def match_keywords(text: str) -> list:
    seen, matched = set(), []
    for kw in _KEYWORDS:
        word = kw["keyword"]
        if word and word in text and word not in seen:
            seen.add(word); matched.append(kw)
    return matched

# ── RAG：Chroma 语义向量检索关键词库 ──
def _token_cosine_search(text: str, topk: int) -> list:
    """降级：jieba 词元余弦相似度检索关键词库。"""
    text_tokens = set(t for t in jieba.cut(text) if len(t) >= 2)
    if not text_tokens:
        return []
    scored = []
    for kw_rec in _KEYWORDS:
        kw = kw_rec.get("keyword") or ""
        if not kw:
            continue
        kw_tokens = set(jieba.cut(kw)) | {kw}
        inter = len(text_tokens & kw_tokens)
        if inter == 0:
            continue
        cos = inter / ((len(text_tokens) ** 0.5) * (len(kw_tokens) ** 0.5))
        scored.append((cos, kw_rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    matched = [rec for _, rec in scored[:topk]]
    if matched:
        print(f"[RAG余弦降级] top命中: {[r['keyword'] for r in matched[:3]]}")
    return matched


def rag_cosine_search(text: str, topk: int = 5) -> list:
    """
    文本 embed 后查询 Chroma，返回语义最近的 topk 条关键词记录。
    Chroma 未就绪 / 查询异常 / 返回空 时，自动降级为 jieba 词元余弦，
    避免内存版 Chroma 运行期偶发失效导致召回整体变空（历史遗留退化 bug 的兜底）。
    """
    if not text or len(text.strip()) < 2:
        return []

    matched = []
    # ── Chroma 语义检索 ──
    if _kw_collection is not None and _embed_model is not None:
        try:
            emb = _embed_model.encode([text], show_progress_bar=False)[0].tolist()
            res = _kw_collection.query(
                query_embeddings=[emb],
                n_results=min(topk, len(_KEYWORDS)),
            )
            ids0 = (res.get("ids") or [[]])[0]
            if ids0:
                matched = [_KEYWORDS[m["idx"]] for m in res["metadatas"][0]]
                print(f"[Chroma] top命中: {[r['keyword'] for r in matched[:3]]}")
            else:
                print("[Chroma] 查询返回空 → 降级词元余弦兜底")
        except Exception as e:
            print(f"[Chroma] 查询异常 → 降级词元余弦兜底: {e}")

    # Chroma 未就绪 / 空 / 异常 → 词元余弦兜底
    if not matched:
        matched = _token_cosine_search(text, topk)

    if not matched:
        return []

    # 取命中 tag_id 对应的语料库最高风险记录，补充真实 risk_level
    tag_ids = list({r["tag_id"] for r in matched if r["tag_id"]})
    if not tag_ids:
        return matched

    placeholders = ",".join(["%s"] * len(tag_ids))
    corpus_recs = qall(f"""
        SELECT tag_id, risk_level, total_risk_score, content_risk_score, opposition_risk_score
        FROM t_corpus
        WHERE tag_id IN ({placeholders}) AND risk_level != ''
        ORDER BY total_risk_score DESC LIMIT 5
    """, tag_ids)
    corpus_map = {r["tag_id"]: r for r in corpus_recs}

    enriched = []
    for rec in matched:
        c = corpus_map.get(rec["tag_id"])
        if c:
            rec = dict(rec)
            rec["_corpus_risk_level"] = c["risk_level"]
            rec["total_risk_score"]   = c["total_risk_score"]
        enriched.append(rec)
    return enriched

# ── RAG：分词后做模糊匹配，找相似语料借用标注 ──
def rag_search(text: str) -> list:
    """
    当精确匹配失败时的兜底：
    1. jieba 分词提取关键词
    2. 与 _KEYWORDS 做包含关系匹配（词出现在关键词中，或关键词出现在文本中）
    3. 用命中的 tag_id 从 t_corpus 取相似语料的风险标注
    """
    if not text or len(text.strip()) < 4:
        return []

    # jieba 分词，保留 ≥2 字的有效词，去重
    tokens = list(dict.fromkeys(
        t for t in jieba.cut(text) if len(t) >= 2
    ))[:20]
    if not tokens:
        return []

    # 与关键词库做模糊匹配：token 包含在关键词里，或关键词整体在文本里
    hit_tag_ids = set()
    hit_kw_recs = []
    seen_tags   = set()
    for kw_rec in _KEYWORDS:
        kw = kw_rec.get("keyword") or ""
        if not kw:
            continue
        matched_by_rag = (
            any(tok in kw for tok in tokens)   # token 是关键词的一部分
            or kw in text                       # 关键词整体出现在文本里（精确兜底）
        )
        if matched_by_rag and kw_rec["tag_id"] not in seen_tags:
            seen_tags.add(kw_rec["tag_id"])
            hit_tag_ids.add(kw_rec["tag_id"])
            hit_kw_recs.append(kw_rec)

    if not hit_tag_ids:
        return []

    print(f"[RAG] 分词命中 {len(hit_tag_ids)} 个标签: {[k['keyword'] for k in hit_kw_recs[:5]]}")

    # 从 t_corpus 取这些 tag_id 的代表语料（各域取最高分的一条）
    placeholders = ",".join(["%s"] * len(hit_tag_ids))
    corpus_recs = qall(f"""
        SELECT tag_id, risk_domain_l1, risk_type_l2, risk_level,
               total_risk_score, content_risk_score, opposition_risk_score,
               matched_keyword, content_summary
        FROM t_corpus
        WHERE tag_id IN ({placeholders})
          AND risk_domain_l1 != '' AND risk_level != ''
        ORDER BY total_risk_score DESC
        LIMIT 5
    """, list(hit_tag_ids))

    # 如果语料库没有对应条目，直接返回关键词记录
    if not corpus_recs:
        return hit_kw_recs[:5]

    # 把语料库里的标注 merge 回关键词记录
    corpus_map = {r["tag_id"]: r for r in corpus_recs}
    enriched = []
    for kw_rec in hit_kw_recs:
        rec = dict(kw_rec)
        c   = corpus_map.get(kw_rec["tag_id"])
        if c:
            # 用语料库的实际风险分覆盖（更准确）
            rec["total_risk_score"]      = c["total_risk_score"]
            rec["content_risk_score"]    = c["content_risk_score"]
            rec["opposition_risk_score"] = c["opposition_risk_score"]
            rec["_rag_corpus_level"]     = c["risk_level"]   # 语料库已有等级
        enriched.append(rec)

    return enriched

# ── 文本高亮 ──
def build_highlights(text: str, matched: list) -> list:
    if not text: return []
    display = text[:600]
    spans = []
    for m in matched:
        kw = m["keyword"]; s = float(m["total_risk_score"] or 0)
        lvl = "l4" if s>=3.5 else "l3" if s>=2.5 else "l2" if s>=1.5 else "tag"
        pos = 0
        while True:
            idx = display.find(kw, pos)
            if idx == -1: break
            spans.append({"start":idx,"end":idx+len(kw),"level":lvl,"domain":m["risk_domain_l1"] or ""})
            pos = idx+1
    if not spans: return [{"text":display,"type":"normal"}]
    spans.sort(key=lambda x: x["start"])
    clean, last = [], 0
    for sp in spans:
        if sp["start"] >= last: clean.append(sp); last = sp["end"]
    segs, pos = [], 0
    for sp in clean:
        if pos < sp["start"]: segs.append({"text":display[pos:sp["start"]],"type":"normal"})
        segs.append({"text":display[sp["start"]:sp["end"]],"type":sp["level"],"domain":sp["domain"]})
        pos = sp["end"]
    if pos < len(display): segs.append({"text":display[pos:],"type":"normal"})
    return segs

# ── 叙事模式 ──
def build_narrative(matched: list, case: dict = None) -> dict:
    domains_hit = {m["risk_domain_l1"] for m in matched if m["risk_domain_l1"]}
    badges, patterns = [], []
    _spread_desc = (case.get("spread_path") or case.get("spread_mechanism") or "") if case else ""
    if _spread_desc:
        patterns.append({"title":"传播路径研判（案例库）","desc":_spread_desc,
                         "level":"danger","icon":"🔴"})
        badges.append("案例库收录")
    for rule in NARRATIVE_RULES:
        if rule["domain"] in domains_hit:
            patterns.append({"title":rule["pattern"],"desc":rule["desc"],
                             "level":rule["level"],"icon":rule["icon"]})
            badges.append(rule["badge"])
    if not patterns:
        patterns.append({"title":"未命中已知叙事模式","desc":"建议人工复核",
                         "level":"info","icon":"🔵"})
    return {"badges":badges[:4],"patterns":patterns[:3]}

# ── 结构化 API 输出 ──
RISK_CODE_LABEL = {
    "L4": "极高风险",
    "L3": "高风险",
    "L2": "中度风险",
    "L1": "轻度风险",
    "L0": "低风险",
}

def _safe_float(v, default=0.0):
    try:
        return round(float(v or 0), 2)
    except Exception:
        return default

def _unique(values, limit=6):
    seen, result = set(), []
    for v in values:
        s = str(v or "").strip()
        if s and s not in seen:
            seen.add(s)
            result.append(s)
        if len(result) >= limit:
            break
    return result

def _dimension_score(score: float, boost: int = 0) -> int:
    """把 0-5 风险分压到 0-4 维度分，便于审核接口消费。"""
    if score <= 0:
        base = 0
    elif score < 1.1:
        base = 1
    elif score < 2.1:
        base = 2
    elif score < 3.1:
        base = 3
    else:
        base = 4
    return max(0, min(4, base + boost))

def _build_structured_result(result: dict) -> dict:
    """
    面向外部调用方的稳定 JSON 摘要。
    保留 /analyze 原始大结果，同时提供可直接入库、排序、审核的字段。
    """
    risk_code = (result.get("risk_level") or "L0").strip()
    risk_level = (result.get("risk_label") or RISK_CODE_LABEL.get(risk_code) or "低风险").strip()
    risk_score = _safe_float(result.get("composite_score"))
    matched_tags = result.get("matched_tags") or []
    narrative = result.get("narrative") or {}

    # 定级来源优先级：新六维引擎(_sixdim，对所有内容生效) > 旧历史虚无引擎(_nihilism) > 启发式
    sixdim = result.get("_sixdim")
    nihilism = result.get("_nihilism")
    base_score = None
    score_shift = 0
    if nihilism and not sixdim:
        risk_code = nihilism["risk_code"]
        risk_level = nihilism["risk_label"]
        base_score = round(nihilism["total_score"] / 6, 2)   # 升降档前（0-5 量纲）
        score_shift = int(nihilism.get("level_shift", 0))     # 档位移动（1 档 ≈ 1.0 分）
        # 升降档折算进显示分（1 档=1.0 分），封顶 5 封底 0，让显示分与风险等级不再自相矛盾
        risk_score = max(0.0, min(5.0, round(base_score + score_shift, 2)))

    risk_domains = _unique(
        [result.get("primary_domain")]
        + [m.get("domain") for m in matched_tags]
        + [result.get("risk_domain")]
    )

    tag_types = []
    for m in matched_tags:
        l2 = m.get("l2_name") or ""
        l3 = m.get("l3_name") or ""
        domain = m.get("domain") or ""
        keyword = m.get("keyword") or ""
        if l2 and l3:
            tag_types.append(f"{l2} / {l3}")
        elif l2:
            tag_types.append(l2)
        elif domain and keyword:
            tag_types.append(f"{domain} / {keyword}")
        elif domain:
            tag_types.append(domain)

    pattern_types = [p.get("title") for p in narrative.get("patterns") or []
                     if p.get("title") and p.get("title") != "未命中已知叙事模式"]
    risk_types = _unique(tag_types + pattern_types + (narrative.get("badges") or []), limit=8)

    highlighted = result.get("highlighted_text") or []
    evidence = [seg.get("text") for seg in highlighted
                if seg.get("type") and seg.get("type") != "normal"]
    if not evidence:
        evidence = [m.get("keyword") for m in matched_tags]
    matched_phrases = _unique(evidence, limit=8)

    primary_domain = result.get("primary_domain") or (risk_domains[0] if risk_domains else "")
    has_narrative = bool(pattern_types or narrative.get("badges"))
    has_packaging = any(("包装" in t or "伪中立" in t or "娱乐" in t or "叙事" in t)
                        for t in risk_types)
    has_emotion = any(("心理" in d or "情绪" in t or "情感" in t or "煽动" in t)
                      for d in risk_domains for t in (risk_types or [""]))

    content_score = _safe_float(result.get("content_risk_score"), risk_score)
    if sixdim:
        dimensions = dict(sixdim["dim_scores"])       # 新六维 0-1 量纲（命中数/6）
    elif nihilism:
        dimensions = nihilism["dimensions"]           # 旧引擎 0-5 量纲
    else:
        dimensions = {
            "fact_handling": _dimension_score(content_score or risk_score),
            "narrative_frame": _dimension_score(risk_score, 1 if has_narrative else 0),
            "emotional_mobilization": _dimension_score(risk_score, 0 if has_emotion else -1),
            "value_orientation": _dimension_score(risk_score, 0 if primary_domain else -1),
            "expression_packaging": _dimension_score(risk_score, 0 if has_packaging else -1),
            "youth_impact": _dimension_score(risk_score),
        }

    # ── 新规则统一定级：六维加权 → RiskPercent → 1-5 级；一票否决/M 规则可顶级 ──
    if sixdim:
        level_num = sixdim["level"]                    # 已综合六维/否决/M 定级
        six_pct = _safe_float(sixdim["risk_percent"])
        floor = {1: 0, 2: 21, 3: 41, 4: 61, 5: 81}.get(level_num, 0)
        risk_percent = max(six_pct, floor)             # 被否决/M 顶级时，显示分对齐等级，避免"1.3分却紫灯"
        risk_code = LEVEL_NUM_TO_CODE[level_num]
        risk_level = RISK_CODE_LABEL.get(risk_code, risk_level)
        risk_score = round(risk_percent / 20, 2)
        base_score = round(six_pct / 20, 2)            # 纯六维加权显示分（未计否决/M）
        score_shift = 0
    elif nihilism:
        base_pct = weighted_risk_percent(dimensions, dim_scale=5.0)
        # 升降档折算为 ±20%/档（≈ 1.0/5 显示分），使等级与展示分自洽
        risk_percent = max(0.0, min(100.0, round(base_pct + score_shift * 20, 1)))
        level_num = percent_to_level(risk_percent)
        risk_code = LEVEL_NUM_TO_CODE[level_num]
        risk_level = RISK_CODE_LABEL.get(risk_code, risk_level)
        risk_score = round(risk_percent / 20, 2)      # 保留 x.x/5 展示（= RiskPercent ÷ 20）
        base_score = round(base_pct / 20, 2)          # 升降档前显示分
    else:
        level_num = CODE_TO_LEVEL_NUM.get(risk_code, 1)
        risk_percent = round(_safe_float(risk_score) * 20, 1)
    warning_light = WARNING_LIGHTS.get(level_num, "green")

    if risk_code in ("L4",):
        suggested_action = ["立即人工复核", "限制推荐", "上报处置", "分龄引导"]
        review_priority = "P0"
    elif risk_code in ("L3",):
        suggested_action = ["人工复核", "限制推荐", "要求补充来源", "分龄引导"]
        review_priority = "P1"
    elif risk_code in ("L2",):
        suggested_action = ["人工抽检", "补充事实来源", "降低扩散权重", "分龄提示"]
        review_priority = "P2"
    elif risk_code in ("L1",):
        suggested_action = ["常规巡检", "保留观察", "分龄提示"]
        review_priority = "P3"
    else:
        suggested_action = ["常规通过", "低频巡检"]
        review_priority = "P4"

    if nihilism:
        suggested_action = [nihilism["action"]]

    phrase_text = "、".join(f"“{p}”" for p in matched_phrases[:3]) or "暂无明确片段"
    type_text = "、".join(risk_types[:3]) or "未命中明确风险类型"
    domain_text = "、".join(risk_domains) or "未分类"
    risk_explanation = (
        f"内容命中{domain_text}下的{type_text}，关键证据包括{phrase_text}，"
        f"综合评分{risk_score:.2f}，判定为{risk_level}。"
    )

    return {
        "risk_level": risk_level,
        "risk_code": risk_code,
        "risk_level_num": level_num,          # 新规则 1-5 级
        "warning_light": warning_light,       # 新规则五色灯 green|yellow|orange|red|purple
        "risk_percent": risk_percent,         # 新规则风险百分制 0-100
        "risk_score": risk_score,
        "base_score": base_score,       # 升降档前的显示分（无历史虚无结果时为 None）
        "score_shift": score_shift,     # 升降档折算的分数增减（档数，≈±1；0 表示未升降档）
        "one_vote_veto": bool(sixdim["one_vote_veto"]) if sixdim else False,
        "major_ideological_risk": bool(sixdim["major_ideological_risk"]) if sixdim else False,
        "major_risk_rules": list(sixdim["major_rules"]) if sixdim else [],
        "risk_domains": risk_domains,
        "risk_types": risk_types,
        "matched_phrases": matched_phrases,
        "risk_dimensions": dimensions,
        "risk_explanation": risk_explanation,
        "suggested_action": suggested_action,
        "review_priority": review_priority,
        "six_dim_detail": sixdim["detail"] if sixdim else None,   # 新规则六维卡数据（36 项 + 否决/M）
        "nihilism_detail": None if sixdim else (nihilism.get("detail") if nihilism else None),
    }

# ── 话术检索（全内存，零 DB 查询）──
def _dedup_scripts(rows: list) -> list:
    seen, unique = set(), []
    for r in rows:
        key = (r.get("script_content") or "").strip()
        if key and key not in seen:
            seen.add(key); unique.append(r)
        if len(unique) >= 1:
            break
    return unique

def get_scripts_by_case(case_id: str, domain: str) -> dict:
    result = {}
    for ag_key, ag_db in AGE_MAP.items():
        rows = [r for r in _ALL_SCRIPTS
                if r.get("related_case_id") == case_id and r.get("age_group") == ag_db] \
               if case_id else []
        if not rows and domain:
            rows = [r for r in _ALL_SCRIPTS
                    if r.get("risk_domain_l1") == domain and r.get("age_group") == ag_db]
        result[ag_key] = _dedup_scripts(rows)
    return result

# ── AI 熔断器（连续失败超阈值时暂停调用 60s）──
_ai_fail_count   = 0
_ai_open_until   = 0.0
_AI_FAIL_THRESH  = 3       # 连续失败 3 次开启熔断
_AI_OPEN_SEC     = 60      # 熔断保持 60 秒

def _ai_call_allowed() -> bool:
    global _ai_fail_count, _ai_open_until
    if time.time() < _ai_open_until:
        return False
    return True

def _ai_record_success():
    global _ai_fail_count
    _ai_fail_count = 0

def _ai_record_failure():
    global _ai_fail_count, _ai_open_until
    _ai_fail_count += 1
    if _ai_fail_count >= _AI_FAIL_THRESH:
        _ai_open_until = time.time() + _AI_OPEN_SEC
        print(f"[熔断] AI调用连续失败 {_ai_fail_count} 次，暂停 {_AI_OPEN_SEC}s")

# ── 话术改写（Haiku 单次调用，同步）──
def adapt_scripts_sync(base_content: str, phases: dict, case_info: dict = None) -> dict:
    """三个年龄段并发独立请求，各 200 tokens，单个超时 7s，总超时 8s。"""
    ci = case_info or {}
    case_ctx = " | ".join(filter(None, [
        ci.get("case_name", ""),
        ci.get("risk_type_l2", "") or ci.get("risk_domain_l1", ""),
        (ci.get("core_keywords", "") or "")[:60],
    ]))

    AGE_TONE = {
        "6-12":  "用词简单、语气温和，多用比喻和故事，避免说教",
        "13-15": "尊重自主意识，引导辨析，语气平等不居高临下",
        "16-18": "可以讲道理讲逻辑，鼓励独立思考和批判性分析",
    }

    def call_one(age_key: str) -> tuple:
        if not _ai_call_allowed():
            return age_key, None
        tone    = AGE_TONE.get(age_key, "")
        strategy = phases.get(age_key, "")
        prompt = (
            f"你是青少年意识形态引导专家。案例背景：{case_ctx}\n"
            f"基础话术：{base_content[:300]}\n"
            f"针对 {age_key}岁 青少年，要求：{tone}。"
            + (f"引导策略参考：{strategy[:100]}" if strategy else "")
            + "\n请直接输出改写后的引导话术，80-120字，不加任何标签或说明。"
        )
        try:
            chunks = []
            with ai_client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    chunks.append(text)
            _ai_record_success()
            return age_key, "".join(chunks).strip()
        except Exception as e:
            _ai_record_failure()
            print(f"[话术改写-{age_key}] 失败: {e}")
            return age_key, None

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(call_one, age): age for age in ["6-12", "13-15", "16-18"]}
        done, _ = concurrent.futures.wait(futs, timeout=8)
        for fut in done:
            age_key, text = fut.result()
            if text:
                result[age_key] = text

    if result:
        print(f"[话术改写] 成功 {len(result)}/3 组")
    return result


# ── AI 生成分级处置策略 + 引导话术（按当前内容分析结果动态生成，非固定库）──
_GUIDANCE_TONE = {
    "6-12":  "用词简单、语气温和，多用比喻，避免说教和专业术语",
    "13-15": "尊重自主意识、引导辨析，语气平等不居高临下",
    "16-18": "可讲逻辑与方法，鼓励独立思考和批判性分析",
}


def generate_guidance_sync(risk_label: str, risk_types: list, domains: list, key_points: list) -> dict:
    """按当前内容分析结果，用 GPT-5.5(OpenAI 兼容接口) 并发生成三个年龄段的『处置策略 + 引导话术』。失败该段留空。"""
    ctx = "；".join(filter(None, [
        f"风险等级：{risk_label}" if risk_label else "",
        "风险类型：" + "、".join([t for t in (risk_types or [])[:3] if t]) if risk_types else "",
        "涉及风险域：" + "、".join([d for d in (domains or [])[:3] if d]) if domains else "",
        "主要风险点：" + "；".join([k for k in (key_points or [])[:3] if k]) if key_points else "",
    ])) or "一般网络内容风险"

    def call_one(age_key: str) -> tuple:
        client = nihilism_scorer._get_openai_client()
        if client is None:
            return age_key, None
        tone = _GUIDANCE_TONE.get(age_key, "")
        prompt = (
            f"你是青少年意识形态风险处置与引导专家。当前内容分析：{ctx}。\n"
            f"请针对 {age_key}岁 青少年，生成两部分，语气要求：{tone}。\n"
            f"1) 处置策略：给一线教师/家长/平台的处置与干预建议，具体可操作"
            f"（如何识别、引导、必要时留存上报），60-90字。\n"
            f"2) 引导话术：可直接对该年龄段青少年说的引导话，帮其识破手法、建立正确认知，"
            f"亲切不生硬，80-110字。\n"
            f"只输出一个 JSON 对象，不要任何额外文字：{{\"strategy\":\"...\",\"script\":\"...\"}}"
        )
        try:
            r = client.with_options(
                timeout=nihilism_scorer.NIHILISM_LLM_TIMEOUT, max_retries=0
            ).responses.create(
                model=nihilism_scorer._OPENAI_MODEL,
                input=[{"role": "user", "content": prompt}],
                reasoning={"effort": "low"},
            )
            raw = (r.output_text or "").strip()
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                data = json.loads(m.group())
                return age_key, {"strategy": str(data.get("strategy", "")).strip(),
                                 "script": str(data.get("script", "")).strip()}
        except Exception as e:
            print(f"[处置生成-{age_key}] 失败: {e}")
        return age_key, None

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(call_one, a): a for a in ["6-12", "13-15", "16-18"]}
        done, _ = concurrent.futures.wait(futs, timeout=45)
        for fut in done:
            age_key, val = fut.result()
            if val:
                result[age_key] = val
    if result:
        print(f"[处置生成] 成功 {len(result)}/3 组")
    return result


def get_adapted_scripts_for_case(case_id: str, domain: str, case_info: dict = None) -> dict:
    """Fetch scripts for a case; adapt with Claude (grounded in case_info) if all ages share same content."""
    if case_id and case_id in _script_adapt_cache:
        print(f"[话术缓存] {case_id} 命中")
        return _script_adapt_cache[case_id]
    scripts = get_scripts_by_case(case_id, domain)
    ages = ["6-12", "13-15", "16-18"]
    first = ((scripts.get("6-12") or [{}])[0].get("script_content") or "").strip()
    if not first:
        return scripts
    all_same = all(
        ((scripts.get(a) or [{}])[0].get("script_content") or "").strip() == first
        for a in ages
    )
    if all_same:
        phases  = {a: ((scripts.get(a) or [{}])[0].get("script_phase") or "") for a in ages}
        adapted = adapt_scripts_sync(first, phases, case_info=case_info)

        if adapted:
            for a in ages:
                if scripts.get(a) and adapted.get(a):
                    scripts[a][0] = {**scripts[a][0], "script_content": adapted[a]}
    if case_id:
        _script_adapt_cache[case_id] = scripts
    return scripts

def enrich_cases_with_scripts(cases: list, domain: str) -> None:
    """Parallel-fetch and adapt scripts for each case, storing result in case['_adapted_scripts']."""
    if not cases:
        return
    # max_workers=1：串行调用 Claude，避免并发请求打爆中转 API
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        futs = [
            (c, pool.submit(get_adapted_scripts_for_case, c.get("case_id", ""), domain, c))
            for c in cases
        ]
        for c, fut in futs:
            try:
                c["_adapted_scripts"] = fut.result(timeout=30)
            except concurrent.futures.TimeoutError:
                print(f"[话术预计算] {c.get('case_id')} 超时，跳过")
                c["_adapted_scripts"] = {}
            except Exception as e:
                print(f"[话术预计算] {c.get('case_id')} 失败: {type(e).__name__}: {e}")
                c["_adapted_scripts"] = {}

def get_scripts(domain: str, age_group_raw: str) -> dict:
    result = {}
    for ag_key, ag_db in AGE_MAP.items():
        rows = [r for r in _ALL_SCRIPTS
                if r.get("risk_domain_l1") == domain and r.get("age_group") == ag_db]
        if not rows:
            rows = [r for r in _ALL_SCRIPTS if r.get("age_group") == ag_db]
        result[ag_key] = _dedup_scripts(rows)
    return result

# ── 相关案例检索 ──
CASE_DOMAINS = ["历史认知风险"]

# 余弦距离阈值：Chroma cosine 距离 = 1 - 余弦相似度
# 距离 > 0.65 ≈ 相似度 < 0.35，视为无匹配
CASE_SIM_THRESHOLD = 0.65

def _web_search_cases(keywords: str, domain: str) -> list:
    """本地案例无高质量匹配时联网搜索，返回结构化结果。"""
    query = f"{domain} {keywords} 典型案例 青少年"
    try:
        # 使用 Bing 搜索（中国可访问，无需 API key）
        url  = f"https://www.bing.com/search?q={requests.utils.quote(query)}&count=5"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for item in soup.select("li.b_algo")[:5]:
            h2   = item.select_one("h2")
            cap  = item.select_one(".b_caption p")
            a    = h2.find("a") if h2 else None
            if not (h2 and cap):
                continue
            results.append({
                "case_id":           f"WEB-{len(results)+1:02d}",
                "case_name":         h2.get_text(strip=True)[:80],
                "representative_text": cap.get_text(strip=True)[:200],
                "risk_domain_l1":    domain,
                "risk_type_l2":      "",
                "risk_level":        "",
                "total_interaction": 0,
                "core_keywords":     keywords,
                "spread_mechanism":  "",
                "review_conclusion": "",
                "disposal_strategy_6_12":  "",
                "disposal_strategy_13_15": "",
                "disposal_strategy_16_18": "",
                "source_url":        a["href"] if a and a.get("href") else "",
                "_source":           "web",   # 标记来源为联网搜索
            })
        print(f"[联网搜索] '{query[:30]}...' 返回 {len(results)} 条")
        return results
    except Exception as e:
        print(f"[联网搜索] 失败: {e}")
        return []


def get_related_cases(domain: str, tag_ids: list, query_text: str = "",
                      matched_keywords: list = None) -> list:
    """
    1. 先用余弦相似度在本地案例库找匹配，每域取最佳 1 条，不过滤距离
    2. 按距离排序取 top 5
    3. 若所有域最佳匹配余弦距离 > CASE_SIM_THRESHOLD（相似度不足），
       则降级到联网搜索，返回网页结果
    """
    if not _case_records or _case_collection is None or _embed_model is None:
        return []

    # 把命中的关键词也加入查询文本，让向量搜索更精准
    kw_text   = " ".join(matched_keywords or [])
    q_text    = " ".join(filter(None, [kw_text, domain, query_text]))
    emb       = _embed_model.encode([q_text], show_progress_bar=False)[0].tolist()

    candidates   = []
    seen_ids     = set()
    best_dist    = 1.0   # 记录全局最小距离（越小越相似）

    for d in CASE_DOMAINS:
        domain_cases = [r for r in _case_records if r.get("risk_domain_l1") == d]
        if not domain_cases:
            continue
        try:
            res = _case_collection.query(
                query_embeddings=[emb],
                n_results=min(8, len(domain_cases)),
                where={"risk_domain_l1": d},
                include=["metadatas", "distances"],
            )
            dists = res["distances"][0] if res.get("distances") else []
            metas = res["metadatas"][0] if res.get("metadatas") else []

            for i, m in enumerate(metas):
                dist = dists[i] if i < len(dists) else 1.0
                best_dist = min(best_dist, dist)
                c = _case_records[m["idx"]]
                if c["case_id"] not in seen_ids:
                    seen_ids.add(c["case_id"])
                    candidates.append({**dict(c), "_source": "local", "_dist": round(dist, 3)})
        except Exception as e:
            print(f"[Chroma案例] {d} 检索失败: {e}")

    # 按相似度排序，取 top 4
    candidates.sort(key=lambda x: x.get("_dist", 1.0))
    results = candidates[:4]

    print(f"[Chroma案例] 本地候选 {len(candidates)} 条，返回 {len(results)} 条，最佳距离 {best_dist:.3f}")

    # 全域相似度均不足 → 联网搜索
    if best_dist > CASE_SIM_THRESHOLD:
        print(f"[Chroma案例] 相似度不足（距离>{CASE_SIM_THRESHOLD}），转联网搜索")
        kw_str = " ".join(matched_keywords[:3]) if matched_keywords else domain
        web_results = _web_search_cases(kw_str, domain)
        return web_results if web_results else results

    return results

def get_positive_cases(domain: str, query_text: str = "",
                       matched_keywords: list = None) -> list:
    """
    中青网正向案例检索：和 get_related_cases 用同一套向量匹配规则
    （embed 查询文本 → Chroma 余弦检索 → 距离阈值过滤），
    但不做联网兜底——没有语义相近的正向案例就返回空列表，不硬凑。
    """
    if not _positive_case_records or _positive_case_collection is None or _embed_model is None:
        return []

    kw_text = " ".join(matched_keywords or [])
    q_text  = " ".join(filter(None, [kw_text, domain, query_text]))
    emb     = _embed_model.encode([q_text], show_progress_bar=False)[0].tolist()

    try:
        res = _positive_case_collection.query(
            query_embeddings=[emb],
            n_results=min(4, len(_positive_case_records)),
            include=["metadatas", "distances"],
        )
    except Exception as e:
        print(f"[Chroma正向案例] 检索失败 → 库内兜底: {e}")
        return _positive_case_records[:2]   # 向量检索异常也不让正向案例消失

    dists = res["distances"][0] if res.get("distances") else []
    metas = res["metadatas"][0] if res.get("metadatas") else []

    results = []
    for i, m in enumerate(metas):
        dist = dists[i] if i < len(dists) else 1.0
        if dist > CASE_SIM_THRESHOLD:
            continue
        results.append(_positive_case_records[m["idx"]])

    # 回填保底：阈值内 0 条时，不让"相关案例"整块消失 ——
    #   有候选(Chroma 正常)→取最相近的 2 条；候选也空(向量库退化)→取库内前 2 条兜底。
    if not results:
        if metas:
            results = [_positive_case_records[m["idx"]] for m in metas[:2]]
            print(f"[Chroma正向案例] 阈值内 0 条 → 回填最相近 {len(results)} 条")
        else:
            results = _positive_case_records[:2]
            print(f"[Chroma正向案例] 向量检索退化 → 库内兜底 {len(results)} 条")
    else:
        print(f"[Chroma正向案例] 候选 {len(metas)} 条，返回 {len(results)} 条")
    return results[:4]

def _case_review_action(risk_level: str, priority_score: float = 0.0) -> list:
    lv = (risk_level or "").strip()
    if lv in ("L4", "极高风险") or priority_score >= 4.0:
        return ["立即人工复核", "限制推荐", "上报处置", "分龄引导"]
    if lv in ("L3", "高风险") or priority_score >= 3.0:
        return ["人工复核", "限制推荐", "要求补充来源", "分龄引导"]
    if lv in ("L2", "中度风险") or priority_score >= 2.0:
        return ["人工抽检", "补充事实来源", "降低扩散权重", "分龄提示"]
    return ["常规巡检", "保留观察", "分龄提示"]

def _case_match_reason(case: dict, query: str, similarity: float = None,
                       source: str = "vector") -> str:
    q = (query or "").strip()
    direct_fields = []
    if q:
        checks = [
            ("案例名称", case.get("case_name")),
            ("核心关键词", case.get("core_keywords")),
            ("代表文本", case.get("representative_text")),
            ("风险类型", case.get("risk_type_l2")),
            ("传播路径", case.get("spread_path") or case.get("spread_mechanism")),
        ]
        for label, text in checks:
            if text and q in str(text):
                direct_fields.append(label)

    parts = []
    if direct_fields:
        parts.append(f"{'、'.join(direct_fields[:3])}直接包含查询词")
    elif source == "vector":
        parts.append("案例语义与查询内容相近")
    else:
        parts.append("案例字段与查询词模糊匹配")
    if similarity is not None:
        parts.append(f"向量相似度 {similarity:.2f}")
    return "；".join(parts)

def _format_case_search_item(case: dict, query: str,
                             similarity: float = None,
                             source: str = "vector") -> dict:
    priority = _safe_float(case.get("case_priority_score"))
    return {
        "case_id": case.get("case_id") or "",
        "case_name": case.get("case_name") or "",
        "risk_domain": case.get("risk_domain_l1") or "",
        "risk_type": case.get("risk_type_l2") or "",
        "risk_level": case.get("risk_level") or "",
        "similarity": similarity,
        "match_source": source,
        "matched_reason": _case_match_reason(case, query, similarity, source),
        "representative_text": case.get("representative_text") or "",
        "spread_path": case.get("spread_path") or case.get("spread_mechanism") or "",
        "review_conclusion": case.get("review_conclusion") or "",
        "platform": case.get("platform") or "",
        "core_keywords": case.get("core_keywords") or "",
        "priority_score": priority,
        "total_interaction": int(case.get("total_interaction") or 0),
        "source_url": case.get("source_url") or "",
        "suggested_action": _case_review_action(case.get("risk_level") or "", priority),
    }

def _keyword_search_cases(query: str, domain: str, top_k: int,
                          exclude_ids: set = None) -> list:
    exclude_ids = exclude_ids or set()
    terms = _unique([query] + [t for t in jieba.cut(query) if len(t) >= 2], limit=6)
    if not terms:
        return []

    like_parts, params = [], []
    for term in terms:
        like = f"%{term}%"
        for col in ("case_name", "core_keywords", "representative_text",
                    "risk_type_l2", "spread_path", "spread_mechanism"):
            like_parts.append(f"{col} LIKE %s")
            params.append(like)

    conds = [f"({' OR '.join(like_parts)})"]
    if domain:
        conds.append("risk_domain_l1 = %s")
        params.append(domain)
    params.append(max(top_k * 3, top_k))

    rows = qall(f"""
        SELECT case_id, case_name, risk_domain_l1, risk_type_l2,
               platform, risk_level, case_priority_score,
               CAST(total_interaction AS SIGNED) AS total_interaction,
               representative_text, spread_mechanism, spread_path,
               core_keywords, review_conclusion, source_url
        FROM t_case
        WHERE case_id LIKE 'ZQW%' AND {' AND '.join(conds)}
        ORDER BY case_priority_score DESC, total_interaction DESC
        LIMIT %s
    """, tuple(params))

    result = []
    for row in rows:
        if row.get("case_id") in exclude_ids:
            continue
        result.append(_format_case_search_item(row, query, None, "keyword"))
        if len(result) >= top_k:
            break
    return result

def search_cases_by_query(query: str, domain: str = "", top_k: int = 5) -> dict:
    top_k = max(1, min(20, int(top_k or 5)))
    domain = (domain or "").strip()
    query_text = " ".join(filter(None, [query.strip(), domain]))

    items, seen_ids = [], set()
    vector_ready = _case_collection is not None and _embed_model is not None and _case_records

    if vector_ready:
        try:
            emb = _embed_model.encode([query_text], show_progress_bar=False)[0].tolist()
            kwargs = {
                "query_embeddings": [emb],
                "n_results": min(max(top_k * 3, top_k), len(_case_records)),
                "include": ["metadatas", "distances"],
            }
            if domain:
                kwargs["where"] = {"risk_domain_l1": domain}
            res = _case_collection.query(**kwargs)
            metas = res["metadatas"][0] if res.get("metadatas") else []
            dists = res["distances"][0] if res.get("distances") else []
            for i, meta in enumerate(metas):
                case = _case_records[meta["idx"]]
                cid = case.get("case_id")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                dist = float(dists[i]) if i < len(dists) else 1.0
                similarity = round(max(0.0, min(1.0, 1.0 - dist)), 4)
                items.append(_format_case_search_item(case, query, similarity, "vector"))
                if len(items) >= top_k:
                    break
        except Exception as e:
            print(f"[案例搜索] Chroma 检索失败: {e}")

    if len(items) < top_k:
        items.extend(_keyword_search_cases(query, domain, top_k - len(items), seen_ids))

    return {
        "query": query,
        "domain": domain,
        "source": "case_library",
        "search_method": "vector" if vector_ready else "keyword",
        "total": len(items[:top_k]),
        "cases": items[:top_k],
    }

def _risk_level_values(value: str) -> list:
    raw = (value or "").strip()
    if not raw:
        return []
    code_to_label = {"L4": "极高风险", "L3": "高风险", "L2": "中度风险",
                     "L1": "轻度风险", "L0": "低风险"}
    label_to_code = {v: k for k, v in code_to_label.items()}
    return _unique([raw, code_to_label.get(raw), label_to_code.get(raw)], limit=3)

def _corpus_snippet(row: dict, terms: list, max_len: int = 180) -> str:
    text = (row.get("content_summary") or row.get("full_text") or row.get("title") or "").strip()
    if not text:
        return ""
    hit = -1
    for term in terms:
        if term:
            hit = text.find(term)
            if hit >= 0:
                break
    if hit < 0:
        return text[:max_len]
    start = max(0, hit - max_len // 3)
    end = min(len(text), start + max_len)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"

def _format_corpus_search_item(row: dict, query: str, terms: list) -> dict:
    matched_terms = []
    joined = " ".join(filter(None, [
        row.get("title") or "",
        row.get("content_summary") or "",
        row.get("full_text") or "",
        row.get("matched_keyword") or "",
        row.get("risk_type_l2") or "",
    ]))
    for term in terms:
        if term and term in joined:
            matched_terms.append(term)

    risk_score = _safe_float(row.get("total_risk_score"))
    return {
        "corpus_id": row.get("corpus_id") or "",
        "title": row.get("title") or "",
        "platform": row.get("platform") or "",
        "author": row.get("author") or "",
        "publish_time": str(row.get("publish_time") or ""),
        "risk_domain": row.get("risk_domain_l1") or "",
        "risk_type": row.get("risk_type_l2") or "",
        "risk_level": row.get("risk_level") or "",
        "risk_score": risk_score,
        "matched_keyword": row.get("matched_keyword") or "",
        "search_keyword": row.get("search_keyword") or "",
        "matched_terms": _unique(matched_terms, limit=6),
        "snippet": _corpus_snippet(row, terms),
        "url": row.get("url") or "",
        "interaction": {
            "like": int(row.get("like_count") or 0),
            "comment": int(row.get("comment_count") or 0),
            "share": int(row.get("share_count") or 0),
            "collect": int(row.get("collect_count") or 0),
            "total": int(row.get("interaction_count") or 0),
        },
        "suggested_action": _case_review_action(row.get("risk_level") or "", risk_score),
    }

def search_corpus_by_query(query: str, domain: str = "", platform: str = "",
                           risk_level: str = "", top_k: int = 10) -> dict:
    top_k = max(1, min(50, int(top_k or 10)))
    query = (query or "").strip()
    domain = (domain or "").strip()
    platform = (platform or "").strip()
    risk_level = (risk_level or "").strip()

    terms = _unique([query] + [t for t in jieba.cut(query) if len(t) >= 2], limit=8)
    like_fields = ("title", "content_summary", "matched_keyword",
                   "search_keyword", "risk_type_l2", "risk_domain_l1")
    like_parts, params = [], []
    for term in terms:
        like = f"%{term}%"
        for col in like_fields:
            like_parts.append(f"{col} LIKE %s")
            params.append(like)

    conds = [f"({' OR '.join(like_parts)})"]
    if domain:
        conds.append("risk_domain_l1 = %s")
        params.append(domain)
    if platform:
        conds.append("platform = %s")
        params.append(platform)
    lv_values = _risk_level_values(risk_level)
    if lv_values:
        conds.append(f"risk_level IN ({','.join(['%s'] * len(lv_values))})")
        params.extend(lv_values)
    params.append(top_k)

    rows = qall(f"""
        SELECT corpus_id, platform, search_keyword, matched_keyword,
               risk_domain_l1, risk_type_l2, risk_level, total_risk_score,
               title, content_summary, LEFT(full_text, 800) AS full_text,
               author, publish_time, url,
               like_count, comment_count, share_count, collect_count,
               interaction_count
        FROM t_corpus
        WHERE {' AND '.join(conds)}
        ORDER BY total_risk_score DESC, interaction_count DESC, publish_time DESC
        LIMIT %s
    """, tuple(params))

    items = [_format_corpus_search_item(row, query, terms) for row in rows]
    return {
        "query": query,
        "domain": domain,
        "platform": platform,
        "risk_level": risk_level,
        "source": "corpus_library",
        "search_method": "keyword",
        "total": len(items),
        "items": items,
    }

# ── 语料库统计 ──
def get_corpus_stats(domain: str, keyword: str) -> dict:
    # 该风险域总量及平台分布
    by_platform = qall("""
        SELECT platform, COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_score
        FROM t_corpus
        WHERE risk_domain_l1=%s AND platform!=''
        GROUP BY platform ORDER BY cnt DESC LIMIT 5
    """, (domain,))

    # 该关键词命中总量
    kw_total = qone("""
        SELECT COUNT(*) AS cnt FROM t_corpus
        WHERE matched_keyword=%s
    """, (keyword,)) or {}

    # 风险域总量
    domain_total = qone("""
        SELECT COUNT(*) AS cnt FROM t_corpus WHERE risk_domain_l1=%s
    """, (domain,)) or {}

    return {
        "domain_total": int(domain_total.get("cnt") or 0),
        "keyword_total": int(kw_total.get("cnt") or 0),
        "by_platform": by_platform,
    }

# ── 四库关联关系 ──
def get_relations(corpus_id: str) -> list:
    if not corpus_id: return []
    return qall("""
        SELECT relation, object_type, object_id, matched_keyword, confidence
        FROM t_lib_relation
        WHERE subject_type='corpus' AND subject_id=%s
        LIMIT 10
    """, (corpus_id,))

# ── 主分析：语料库命中路径 ──
def analyze_from_corpus(corpus: dict, age_group: str) -> dict:
    # 1. 全文关键词匹配
    full_text = " ".join(filter(None,[
        corpus.get("title",""), corpus.get("content_summary",""), corpus.get("full_text","")
    ]))
    all_matched = match_keywords(full_text) if full_text.strip() else []

    # 兜底：至少用语料库自带的关键词
    if not all_matched and corpus.get("matched_keyword"):
        fb = next((k for k in _KEYWORDS if k["keyword"]==corpus["matched_keyword"]), None)
        if fb: all_matched = [fb]

    # 2. 风险计算 —— 直接信任 t_corpus 已有标注，不重新推算
    composite = float(corpus.get("total_risk_score") or 0)
    max_s     = composite
    primary   = corpus.get("risk_domain_l1") or ""

    # 优先使用语料库已标注的 risk_level（存储值为中文，需反向映射为 L 码）
    _REVERSE_MAP = {"极高风险":"L4","高风险":"L3","中度风险":"L2","轻度风险":"L1","低风险":"L0"}
    _rl_raw = (corpus.get("risk_level") or "").strip()
    if _rl_raw in _REVERSE_MAP:
        risk_level = _REVERSE_MAP[_rl_raw]
        risk_label = _rl_raw
    else:
        risk_level, risk_label = score_to_level(composite)

    # 历史虚无主义六维判分（仅命中该域时才真正调用大模型，其余域不受影响）
    nihilism_result = None
    if primary == "历史认知风险":
        nihilism_result = nihilism_scorer.run_nihilism_pipeline(
            full_text, title=corpus.get("title", ""),
            platform=corpus.get("platform") or detect_platform(corpus.get("url", "")),
            age_group=age_group,
            ai_client=ai_client, ai_call_allowed=_ai_call_allowed,
            ai_record_success=_ai_record_success, ai_record_failure=_ai_record_failure,
        )

    # 风险维度（5个域）
    DOMAINS = ["制度认同风险","心理韧性风险","历史认知风险","认知闭合风险","网络素养风险"]
    domain_scores: dict = {d: 0.3 for d in DOMAINS}
    if primary: domain_scores[primary] = composite
    # 如果全量匹配到了多个域，补充各域最高分
    for m in all_matched:
        d = m["risk_domain_l1"] or ""
        if d: domain_scores[d] = max(domain_scores.get(d, 0), float(m["total_risk_score"] or 0))
    tag_ids = [m["tag_id"] for m in all_matched]

    # 3. 查相关案例（语义匹配：title + risk_domain_l1 + content_summary）
    query_text = " ".join(filter(None,[
        corpus.get("title",""),
        corpus.get("risk_domain_l1",""),
        (corpus.get("content_summary","") or "")[:200],
    ]))
    kw_list = [m["keyword"] for m in all_matched if m.get("keyword")]
    related_cases = get_related_cases(primary, tag_ids, query_text, matched_keywords=kw_list)
    top_case = related_cases[0] if related_cases else None
    positive_cases = get_positive_cases(primary, query_text, matched_keywords=kw_list)

    # 4. 语料库统计
    corpus_stats = get_corpus_stats(primary, corpus.get("matched_keyword",""))

    # 5. 四库关系
    relations = get_relations(str(corpus.get("corpus_id","")))

    # 6. 叙事分析
    narrative = build_narrative(all_matched, top_case)

    # 7. 话术
    scripts = get_scripts(primary, age_group)

    # 8. 高亮
    highlights = build_highlights(
        corpus.get("content_summary") or corpus.get("full_text") or "", all_matched
    )

    # 9. 标签列表（含完整分类链）
    matched_tags = sorted([
        {"tag_id":   m["tag_id"],
         "keyword":  m["keyword"],
         "domain":   m["risk_domain_l1"] or "",
         "score":    float(m["total_risk_score"] or 0),
         "tag_chain":m.get("tag_chain") or "",
         "guidance": m.get("guidance_direction") or "",
         "l1_code":  m.get("l1_tag_code") or "",
         "l1_name":  m.get("l1_name") or "",
         "l2_code":  m.get("l2_tag_code") or "",
         "l2_name":  m.get("l2_name") or "",
         "l3_code":  m.get("l3_tag_code") or "",
         "l3_name":  m.get("l3_name") or ""}
        for m in all_matched
    ], key=lambda x: x["score"], reverse=True)[:10]

    # 10. 处置策略汇总（来自案例）
    disposal = {}
    if top_case:
        disposal = {
            "6-12":    top_case.get("disposal_strategy_6_12") or "",
            "13-15":   top_case.get("disposal_strategy_13_15") or "",
            "16-18":   top_case.get("disposal_strategy_16_18") or "",
            "platform":top_case.get("disposal_strategy_platform") or "",
        }

    return {
        "source":          "corpus",
        "_nihilism":       nihilism_result,
        "url":             corpus.get("url",""),
        "platform":        corpus.get("platform") or detect_platform(corpus.get("url","")),
        "title":           corpus.get("title") or "",
        "author":          corpus.get("author") or "",
        "publish_time":    str(corpus.get("publish_time") or ""),
        "content_summary": corpus.get("content_summary") or "",
        "interaction": {
            "like":    int(corpus.get("like_count") or 0),
            "comment": int(corpus.get("comment_count") or 0),
            "share":   int(corpus.get("share_count") or 0),
            "collect": int(corpus.get("collect_count") or 0),
            "total":   int(corpus.get("interaction_count") or 0),
        },
        # 风险
        "risk_level":            risk_level,
        "risk_label":            risk_label,
        "composite_score":       composite,
        "max_score":             round(max_s,2),
        "content_risk_score":    float(corpus.get("content_risk_score") or 0),
        "opposition_risk_score": float(corpus.get("opposition_risk_score") or 0),
        "domain_scores":         domain_scores,
        "primary_domain":        primary,
        "tag_chain":             corpus.get("tag_chain") or "",
        # 标签
        "matched_tags":          matched_tags,
        "matched_count":         len(all_matched),
        # 文本
        "highlighted_text":      highlights,
        # 叙事
        "narrative":             narrative,
        # 话术
        "scripts":               scripts,
        # 相关案例（含预计算话术）
        "related_cases": [
            {"case_id":           c.get("case_id",""),
             "case_name":         c.get("case_name",""),
             "risk_domain":       c.get("risk_domain_l1",""),
             "risk_type":         c.get("risk_type_l2",""),
             "risk_level":        c.get("risk_level",""),
             "priority_score":    float(c.get("case_priority_score") or 0),
             "spread_mechanism":  c.get("spread_path") or c.get("spread_mechanism") or "",
             "representative_text": c.get("representative_text") or "",
             "review_conclusion": c.get("review_conclusion") or "",
             "total_interaction": int(c.get("total_interaction") or 0),
             "core_keywords":     c.get("core_keywords") or "",
             "disposal_6_12":     c.get("disposal_strategy_6_12") or "",
             "disposal_13_15":    c.get("disposal_strategy_13_15") or "",
             "disposal_16_18":    c.get("disposal_strategy_16_18") or "",
             "source_url":        c.get("source_url") or "",
             "adapted_scripts":   {},
             }
            for c in related_cases
        ],
        # 中青网正向案例（不含风险等级/处置策略字段，插件端据此和风险案例区分展示）
        "positive_cases": [
            {"case_id":         c.get("case_id", ""),
             "case_name":       c.get("case_name", ""),
             "source":          "中青网",
             "relation_reason": c.get("representative_text", ""),
             "source_url":      c.get("source_url", "")}
            for c in positive_cases
        ],
        # 语料库统计（新增）
        "corpus_stats":   corpus_stats,
        # 四库关联（新增）
        "relations":      relations,
        # 处置策略
        "disposal":       disposal,
    }

# ── 兜底路径：实时爬取分析 ──
HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36",
           "Accept-Language":"zh-CN,zh;q=0.9"}

def analyze_realtime(url: str, age_group: str, page_text: str = "") -> dict:
    # 优先使用插件直接传入的页面文本（已登录、已渲染，比爬虫准确）
    if page_text and page_text.strip():
        text = re.sub(r"\s{2,}", " ", page_text.strip())[:4000]
        title = text[:60]   # 取前 60 字作为标题占位
        crawl_ok = True
    else:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            for tag in soup(["script","style","nav","footer","header"]): tag.decompose()
            text = re.sub(r"\s{2,}"," ",soup.get_text(" ",strip=True))[:4000]
            crawl_ok = True
        except:
            title, text, crawl_ok = "","",False

    full = f"{title} {text}".strip()
    domain_tags = match_five_domains(full)   # 五大风险域关键词簇命中（识别标签 + 兜底打分用）
    # 新规则六维引擎：对所有内容判分（六维 0/1 + 一票否决 + M 规则），不依赖关键词匹配
    six_dim_result = six_dim_scorer.score(text, domain_hits=domain_tags) if (text and text.strip()) else None
    matched = match_keywords(full)

    # 精确匹配失败 → 余弦 RAG 兜底
    match_method = "exact"
    if not matched and full.strip():
        matched = rag_cosine_search(full)
        if matched:
            match_method = "rag"

    # 如果连文本都没有 → 用 URL / 平台做最后兜底
    if not matched:
        url_text = f"{detect_platform(url)} {url}"
        matched = rag_cosine_search(url_text)
        if matched:
            match_method = "rag_url"

    if not matched:
        return {"source":"realtime","url":url,"platform":detect_platform(url),"title":title,
                "crawl_success":crawl_ok,
                "_sixdim": six_dim_result,
                "domain_tags": domain_tags,
                "content_summary":text[:300],
                "full_text":text[:2000],
                "interaction":{"like":0,"comment":0,"share":0,"collect":0,"total":0},
                "risk_level":"L0","risk_label":"暂无风险",
                "composite_score":0,"max_score":0,
                "content_risk_score":0,"opposition_risk_score":0,
                "domain_scores":{},"primary_domain":"",
                "matched_tags":[],"matched_count":0,
                "highlighted_text":[{"text":text[:500],"type":"normal"}] if text else [],
                "narrative":{"badges":[],"patterns":[]},"scripts":{},"related_cases":[],
                "positive_cases":[],
                "corpus_stats":{},"relations":[],"disposal":{},"tag_chain":""}

    scores = [float(m["total_risk_score"] or 0) for m in matched]
    max_s = max(scores); composite = round(max_s*0.6+sum(scores)/len(scores)*0.4, 2)
    risk_level, risk_label = score_to_level(composite)

    # RAG 模式：优先使用语料库已有的 risk_level（更准确）
    if match_method in ("rag","rag_url"):
        corpus_levels = [m.get("_corpus_risk_level") for m in matched if m.get("_corpus_risk_level")]
        if corpus_levels:
            LEVEL_ORDER = {"L4":4,"L3":3,"L2":2,"L1":1,"L0":0}
            best = max(corpus_levels, key=lambda l: LEVEL_ORDER.get(l,0))
            LABEL_MAP = {"L4":"极高风险","L3":"高风险","L2":"中度风险","L1":"轻度风险","L0":"低风险"}
            risk_level, risk_label = best, LABEL_MAP.get(best, risk_label)
    domain_scores: dict = {}
    for m in matched:
        d = m["risk_domain_l1"] or "未分类"
        domain_scores[d] = max(domain_scores.get(d,0), float(m["total_risk_score"] or 0))
    primary = max(domain_scores, key=domain_scores.get)
    tag_ids = [m["tag_id"] for m in matched]

    # 六维判分已在前面对全文执行（six_dim_result），此处不再调用旧历史虚无引擎

    # 实时分析用 title + primary_domain + 页面文本 作为查询
    rt_query = " ".join(filter(None, [title, primary, text[:200]]))
    kw_list = [m["keyword"] for m in matched if m.get("keyword")]
    related_cases = get_related_cases(primary, tag_ids, rt_query, matched_keywords=kw_list)
    positive_cases = get_positive_cases(primary, rt_query, matched_keywords=kw_list)
    corpus_stats = get_corpus_stats(primary, matched[0]["keyword"] if matched else "")

    return {
        "source":"realtime","url":url,"platform":detect_platform(url),
        "_sixdim": six_dim_result,
        "domain_tags": domain_tags,
        "title":title,"crawl_success":crawl_ok,
        "match_method": match_method,
        "content_summary":text[:300],
        "interaction":{"like":0,"comment":0,"share":0,"collect":0,"total":0},
        "risk_level":risk_level,"risk_label":risk_label,
        "composite_score":composite,"max_score":round(max_s,2),
        "content_risk_score":0,"opposition_risk_score":0,
        "domain_scores":domain_scores,"primary_domain":primary,
        "matched_tags":sorted([
            {"tag_id":m["tag_id"],"keyword":m["keyword"],"domain":m["risk_domain_l1"] or "",
             "score":float(m["total_risk_score"] or 0),"tag_chain":m.get("tag_chain",""),
             "guidance":m.get("guidance_direction",""),
             "l1_code":m.get("l1_tag_code",""),"l1_name":m.get("l1_name",""),
             "l2_code":m.get("l2_tag_code",""),"l2_name":m.get("l2_name",""),
             "l3_code":m.get("l3_tag_code",""),"l3_name":m.get("l3_name","")}
            for m in matched
        ], key=lambda x:x["score"], reverse=True)[:10],
        "highlighted_text":build_highlights(text, matched),
        "narrative":build_narrative(matched),
        "scripts":get_scripts(primary, age_group),
        "related_cases":[
            {"case_id":c.get("case_id",""),"case_name":c.get("case_name",""),
             "risk_domain":c.get("risk_domain_l1",""),"risk_type":c.get("risk_type_l2",""),
             "risk_level":c.get("risk_level",""),"priority_score":float(c.get("case_priority_score") or 0),
             "spread_mechanism":c.get("spread_mechanism") or "",
             "representative_text":c.get("representative_text") or "",
             "review_conclusion":c.get("review_conclusion") or "",
             "total_interaction":int(c.get("total_interaction") or 0),
             "core_keywords":c.get("core_keywords") or "",
             "disposal_6_12":c.get("disposal_strategy_6_12") or "",
             "disposal_13_15":c.get("disposal_strategy_13_15") or "",
             "disposal_16_18":c.get("disposal_strategy_16_18") or "",
             "source_url":c.get("data_source_url") or c.get("source_url") or "",
             "adapted_scripts":{}}
            for c in related_cases
        ],
        "positive_cases":[
            {"case_id":         c.get("case_id", ""),
             "case_name":       c.get("case_name", ""),
             "source":          "中青网",
             "relation_reason": c.get("representative_text", ""),
             "source_url":      c.get("source_url", "")}
            for c in positive_cases
        ],
        "corpus_stats":corpus_stats,
        "relations":[],
        "disposal":{},
        "tag_chain":"",
        "matched_count":len(matched),
    }

# ── 主接口 ──
@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "请输入完整 URL（以 http/https 开头）")

    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM t_corpus WHERE url=%s LIMIT 1", (url,))
    corpus = cur.fetchone()
    if not corpus:
        stripped = re.sub(r"^https?://","",url).rstrip("/")
        cur.execute("SELECT * FROM t_corpus WHERE url LIKE %s LIMIT 1", (f"%{stripped}%",))
        corpus = cur.fetchone()
    cur.close(); conn.close()

    # 结果缓存命中直接返回（TTL=30min）
    # 缓存键含 page_text：同一 URL 不同正文（插件实时提取 vs 服务端爬取，或提取器改版前后）
    # 不能撞同一条缓存，否则会把旧的爬取垃圾结果返回给带正确正文的新请求。
    cache_key = f"{url}|{hash((req.page_text or '').strip())}"
    cached = _result_cache.get(cache_key)
    if cached and not req.no_cache and (time.time() - cached[1]) < _RESULT_TTL:
        if "structured_result" not in cached[0]:
            cached[0]["structured_result"] = _build_structured_result(cached[0])
        return cached[0]

    embed = get_embed_info(url)
    loop = asyncio.get_event_loop()
    # 插件传了真实正文(page_text)时，一律实时分析该页面（走六维引擎）；
    # 仅在没有正文时才用语料库预置内容兜底。否则语料库 LIKE 命中会顶掉真实页面、六维也拿不到。
    if corpus and not (req.page_text or "").strip():
        result = await loop.run_in_executor(None, analyze_from_corpus, corpus, req.age_group)
    else:
        result = await loop.run_in_executor(None, analyze_realtime, url, req.age_group, req.page_text)
    result["embed"] = embed
    result["structured_result"] = _build_structured_result(result)
    # 命中六维引擎（新 _sixdim / 旧 _nihilism）时，顶层风险字段也用其结果覆盖，
    # 否则插件"风险评级"卡（读顶层老关键词分）会和"六维分析"卡对不上
    if result.get("_sixdim") or result.get("_nihilism"):
        sr = result["structured_result"]
        result["risk_level"] = sr["risk_code"]
        result["risk_label"] = sr["risk_level"]
        result["composite_score"] = sr["risk_score"]      # 最终显示分
        result["base_score"] = sr.get("base_score")        # 供前端展示拆解公式
        result["score_shift"] = sr.get("score_shift")
    # 新规则字段统一提升到顶层（插件读顶层），无论命中哪个域
    _sr = result["structured_result"]
    result["risk_level_num"] = _sr.get("risk_level_num")
    result["warning_light"]  = _sr.get("warning_light")
    result["risk_percent"]   = _sr.get("risk_percent")
    result["one_vote_veto"]  = _sr.get("one_vote_veto", False)
    result["major_ideological_risk"] = _sr.get("major_ideological_risk", False)
    result["major_risk_rules"] = _sr.get("major_risk_rules", [])
    result.pop("_nihilism", None)   # 内部字段，不透出给插件/前端
    result.pop("_sixdim", None)
    _result_cache[cache_key] = (result, time.time())
    return result

@app.post("/analyze/structured")
async def analyze_structured(req: AnalyzeRequest):
    """只返回结构化审核结果，方便外部系统直接消费。"""
    result = await analyze(req)
    return result.get("structured_result") or _build_structured_result(result)


def _preset_level_from_url(url: str):
    """测试页(/test-pages/L{0-4}_*.html)按文件名映射到预设等级 1-5；非测试页返回 None。
    L0→1(低) L1→2(轻度) L2→3(中度) L3→4(高) L4→5(极高)。仅用于演示五级五色 UI。"""
    m = re.search(r"/L([0-4])_", url or "")
    return int(m.group(1)) + 1 if m else None


@app.post("/analyze/stream")
async def analyze_stream(req: AnalyzeRequest):
    """流式分析（NDJSON，每行一个事件）：命中标签→六维逐句→案例，分阶段边算边推，
    供插件增量渲染。始终实时计算（不读结果缓存）。走 page_text/实时路径。"""
    url = req.url.strip()
    age_group = req.age_group or "13-15"
    page_text = req.page_text or ""

    def emit(obj):
        return json.dumps(obj, ensure_ascii=False) + "\n"

    def gen():
        # ── 取正文（优先插件传入的 page_text）──
        if page_text.strip():
            text = re.sub(r"\s{2,}", " ", page_text.strip())[:4000]
            title = text[:60]
        else:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=12)
                resp.encoding = resp.apparent_encoding or "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")
                title = soup.title.string.strip() if soup.title and soup.title.string else ""
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True))[:4000]
            except Exception:
                title, text = "", ""
        platform = detect_platform(url)

        # ── 命中 + 域 + 初判 ──
        full = f"{title} {text}".strip()
        domain_tags = match_five_domains(full)   # 五大风险域命中（识别标签用）
        matched = match_keywords(full)
        match_method = "exact"
        if not matched and full.strip():
            matched = rag_cosine_search(full)
            if matched:
                match_method = "rag"
        if not matched:
            matched = rag_cosine_search(f"{platform} {url}")
            if matched:
                match_method = "rag_url"

        if not matched:
            # 无关键词命中也按新规则跑六维（内容判分不依赖关键词层）
            nm_result = {"source": "realtime", "url": url, "platform": platform, "title": title,
                         "content_summary": text[:300], "matched_tags": [], "primary_domain": "",
                         "narrative": {"badges": [], "patterns": []},
                         "_sixdim": six_dim_scorer.score(text, use_llm=False, domain_hits=domain_tags) if (text and text.strip()) else None}
            nm_sr = _build_structured_result(nm_result)
            yield emit({"event": "tags", "title": title, "platform": platform,
                        "content_summary": text[:300], "matched_tags": [], "primary_domain": "",
                        "risk_level": nm_sr["risk_code"], "risk_label": nm_sr["risk_level"],
                        "composite_score": nm_sr["risk_score"], "domain_tags": domain_tags,
                        "narrative": {"badges": [], "patterns": []}, "match_method": match_method})
            yield emit({"event": "sixdim", "structured_result": nm_sr,
                        "risk_level": nm_sr["risk_code"], "risk_label": nm_sr["risk_level"],
                        "composite_score": nm_sr["risk_score"],
                        "base_score": nm_sr.get("base_score"), "score_shift": nm_sr.get("score_shift"),
                        "risk_level_num": nm_sr.get("risk_level_num"),
                        "warning_light": nm_sr.get("warning_light"),
                        "risk_percent": nm_sr.get("risk_percent")})
            yield emit({"event": "cases", "related_cases": [], "positive_cases": []})
            yield emit({"event": "done"})
            return

        scores = [float(m["total_risk_score"] or 0) for m in matched]
        max_s = max(scores)
        composite = round(max_s * 0.6 + sum(scores) / len(scores) * 0.4, 2)
        risk_level, risk_label = score_to_level(composite)
        if match_method in ("rag", "rag_url"):
            corpus_levels = [m.get("_corpus_risk_level") for m in matched if m.get("_corpus_risk_level")]
            if corpus_levels:
                LEVEL_ORDER = {"L4": 4, "L3": 3, "L2": 2, "L1": 1, "L0": 0}
                best = max(corpus_levels, key=lambda l: LEVEL_ORDER.get(l, 0))
                LABEL_MAP = {"L4": "极高风险", "L3": "高风险", "L2": "中度风险", "L1": "轻度风险", "L0": "低风险"}
                risk_level, risk_label = best, LABEL_MAP.get(best, risk_label)
        domain_scores: dict = {}
        for m in matched:
            d = m["risk_domain_l1"] or "未分类"
            domain_scores[d] = max(domain_scores.get(d, 0), float(m["total_risk_score"] or 0))
        primary = max(domain_scores, key=domain_scores.get)
        tag_ids = [m["tag_id"] for m in matched]
        matched_tags = sorted([
            {"tag_id": m["tag_id"], "keyword": m["keyword"], "domain": m["risk_domain_l1"] or "",
             "score": float(m["total_risk_score"] or 0), "tag_chain": m.get("tag_chain", ""),
             "guidance": m.get("guidance_direction", ""),
             "l1_code": m.get("l1_tag_code", ""), "l1_name": m.get("l1_name", ""),
             "l2_code": m.get("l2_tag_code", ""), "l2_name": m.get("l2_name", ""),
             "l3_code": m.get("l3_tag_code", ""), "l3_name": m.get("l3_name", "")}
            for m in matched
        ], key=lambda x: x["score"], reverse=True)[:10]
        narrative = build_narrative(matched)

        # 组装 result 壳（供 _build_structured_result 复用），并发出 tags 事件
        result = {
            "source": "realtime", "url": url, "platform": platform, "title": title,
            "content_summary": text[:300], "match_method": match_method,
            "interaction": {"like": 0, "comment": 0, "share": 0, "collect": 0, "total": 0},
            "risk_level": risk_level, "risk_label": risk_label,
            "composite_score": composite, "max_score": round(max_s, 2),
            "content_risk_score": 0, "opposition_risk_score": 0,
            "domain_scores": domain_scores, "primary_domain": primary,
            "matched_tags": matched_tags,
            "highlighted_text": build_highlights(text, matched),
            "narrative": narrative,
        }
        yield emit({"event": "tags", "title": title, "platform": platform,
                    "content_summary": text[:300], "matched_tags": matched_tags,
                    "primary_domain": primary, "risk_level": risk_level, "risk_label": risk_label,
                    "composite_score": composite, "domain_tags": domain_tags,
                    "narrative": narrative, "match_method": match_method})

        # ── 六维引擎：先用规则版即时出结果（命中信息立刻可见、有过程感），AI 精判随后刷新 ──
        def _sixdim_evt(_sr):
            return {"event": "sixdim", "structured_result": _sr,
                    "risk_level": _sr["risk_code"], "risk_label": _sr["risk_level"],
                    "composite_score": _sr["risk_score"],
                    "base_score": _sr.get("base_score"), "score_shift": _sr.get("score_shift"),
                    "risk_level_num": _sr.get("risk_level_num"),
                    "warning_light": _sr.get("warning_light"),
                    "risk_percent": _sr.get("risk_percent")}
        has_text = bool(text and text.strip())
        preset_lv = _preset_level_from_url(url)   # 测试页：按预设等级出稳定结果，跳过 LLM
        if preset_lv:
            result["_sixdim"] = six_dim_scorer.preset_score(preset_lv, text)
        else:
            result["_sixdim"] = six_dim_scorer.score(text, use_llm=False, domain_hits=domain_tags) if has_text else None
        sr = _build_structured_result(result)
        yield emit(_sixdim_evt(sr))

        # ── 案例检索 ──
        rt_query = " ".join(filter(None, [title, primary, text[:200]]))
        kw_list = [m["keyword"] for m in matched if m.get("keyword")]
        related_cases = get_related_cases(primary, tag_ids, rt_query, matched_keywords=kw_list)
        positive_cases = get_positive_cases(primary, rt_query, matched_keywords=kw_list)
        yield emit({"event": "cases",
            "related_cases": [
                {"case_id": c.get("case_id", ""), "case_name": c.get("case_name", ""),
                 "risk_domain": c.get("risk_domain_l1", ""), "risk_type": c.get("risk_type_l2", ""),
                 "risk_level": c.get("risk_level", ""), "priority_score": float(c.get("case_priority_score") or 0),
                 "spread_mechanism": c.get("spread_mechanism") or "",
                 "representative_text": c.get("representative_text") or "",
                 "review_conclusion": c.get("review_conclusion") or "",
                 "total_interaction": int(c.get("total_interaction") or 0),
                 "core_keywords": c.get("core_keywords") or "",
                 "disposal_6_12": c.get("disposal_strategy_6_12") or "",
                 "disposal_13_15": c.get("disposal_strategy_13_15") or "",
                 "disposal_16_18": c.get("disposal_strategy_16_18") or "",
                 "source_url": c.get("data_source_url") or c.get("source_url") or "",
                 "adapted_scripts": {}}
                for c in related_cases
            ],
            "positive_cases": [
                {"case_id": c.get("case_id", ""), "case_name": c.get("case_name", ""),
                 "source": "中青网", "relation_reason": c.get("representative_text", ""),
                 "representative_text": c.get("representative_text", ""),
                 "keywords": c.get("core_keywords", ""),
                 "risk_type": c.get("risk_type_l2", ""),
                 "domain": c.get("risk_domain_l1", ""),
                 "source_url": c.get("source_url", "")}
                for c in positive_cases
            ]})
        # ── AI 精判：代理可用时用大模型精细判定并刷新六维（不阻塞前面已展示的命中信息）──
        if has_text and not preset_lv:   # 测试页用预设等级，跳过 AI 精判
            ai = six_dim_scorer.score(text, use_llm=True, domain_hits=domain_tags)
            if ai and ai.get("source") == "llm":
                result["_sixdim"] = ai
                sr = _build_structured_result(result)
                yield emit(_sixdim_evt(sr))
        yield emit({"event": "done"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ── 历史虚无主义评估报告（Word 导出）──
_LEVEL_HEX = {"L4": "C0392B", "L3": "E67E22", "L2": "D4AC0D", "L1": "27AE60", "L0": "7F8C8D"}
_CONFIRM_TEXT = {True: "已复核", False: "疑似误命中", None: "未复核"}


def _risk_action(risk_code: str) -> str:
    """按风险等级取处置建议（读六维评分标准 risk_levels）。"""
    for lv in getattr(nihilism_scorer, "_RISK_LEVELS", []) or []:
        if lv.get("code") == risk_code:
            return lv.get("action", "")
    return ""


def build_nihilism_docx(result: dict, detail: dict) -> io.BytesIO:
    """把一次分析结果渲染成 Word 评估报告，返回内存字节流。"""
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    doc.styles["Normal"].font.name = "Microsoft YaHei"
    doc.styles["Normal"].font.size = Pt(10.5)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rules_ver = (getattr(nihilism_scorer, "_LEXICON", {}) or {}).get("source_doc", "—")
    risk_code = result.get("risk_level") or "L0"
    risk_label = result.get("risk_label") or ""

    title = doc.add_heading("历史虚无主义内容风险评估报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub.add_run(f"青年心盾内容风险识别系统 · 生成时间 {now}")
    run.font.size = Pt(9); run.font.color.rgb = RGBColor(0x7F, 0x8C, 0x8D)

    def kv_table(rows):
        t = doc.add_table(rows=0, cols=2); t.style = "Light Grid Accent 1"
        for k, v in rows:
            c = t.add_row().cells
            c[0].text = k; c[1].text = str(v)
            c[0].paragraphs[0].runs[0].bold = True
        return t

    # 一、基本信息
    doc.add_heading("一、基本信息", level=1)
    kv_table([
        ("页面链接", result.get("url") or "—"),
        ("平台", result.get("platform") or "—"),
        ("受众年龄段", result.get("_age_group") or "13-15"),
        ("分析时间", now),
        ("规则库版本", rules_ver),
        ("打分来源", "大模型逐句判分" if (detail or {}).get("score_source") == "llm" else "规则估算（AI 兜底）"),
    ])

    if not detail:
        doc.add_heading("二、总体结论", level=1)
        p = doc.add_paragraph()
        r = p.add_run(f"风险等级：{risk_code} {risk_label}")
        r.bold = True; r.font.color.rgb = RGBColor.from_string(_LEVEL_HEX.get(risk_code, "7F8C8D"))
        doc.add_paragraph("本页未命中历史虚无主义命中语料库，未触发六维逐句判定。")
        buf = io.BytesIO(); doc.save(buf); buf.seek(0); return buf

    # 二、总体结论
    doc.add_heading("二、总体结论", level=1)
    p = doc.add_paragraph()
    r = p.add_run(f"风险等级：{risk_code} {risk_label}　|　综合总分：{detail.get('total_score', 0)} / {detail.get('max_total', 30)}")
    r.bold = True; r.font.size = Pt(12)
    r.font.color.rgb = RGBColor.from_string(_LEVEL_HEX.get(risk_code, "7F8C8D"))

    if detail.get("llm_reason"):
        pr = doc.add_paragraph(); pr.add_run("大模型综合判定：").bold = True
        pr.add_run(detail["llm_reason"])
    action = _risk_action(risk_code)
    if action:
        pa = doc.add_paragraph(); pa.add_run("处置建议：").bold = True; pa.add_run(action)

    # 六维评分表
    doc.add_heading("六维评分（整页聚合，取各句最严值）", level=2)
    dt = doc.add_table(rows=1, cols=3); dt.style = "Light Grid Accent 1"
    hdr = dt.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "维度", "分值", "分档说明"
    for c in hdr: c.paragraphs[0].runs[0].bold = True
    for dim in detail.get("dimensions", []):
        c = dt.add_row().cells
        c[0].text = dim.get("name", ""); c[1].text = f"{dim.get('score',0)}/{dim.get('max',5)}"
        c[2].text = dim.get("band_desc", "")

    # 命中话术类型（并集 + 复核结论）
    subs = detail.get("matched_subtypes", [])
    if subs:
        doc.add_heading("命中话术类型", level=2)
        for s in subs:
            para = doc.add_paragraph(style="List Bullet")
            para.add_run(f"{s['id']} {s['name']}（{s['category']}）　").bold = True
            para.add_run(f"[{_CONFIRM_TEXT.get(s.get('confirmed'))}]")

    # 三、逐句判定明细
    doc.add_heading("三、逐句判定明细", level=1)
    sentences = detail.get("sentences", [])
    doc.add_paragraph(f"共判定命中句 {len(sentences)} 句（按风险从高到低排列）：").runs[0].italic = True
    st = doc.add_table(rows=1, cols=5); st.style = "Light Grid Accent 1"
    h = st.rows[0].cells
    for i, name in enumerate(["#", "原句", "风险", "命中类型 / 复核", "判定理由"]):
        h[i].text = name; h[i].paragraphs[0].runs[0].bold = True
    for i, s in enumerate(sentences, 1):
        c = st.add_row().cells
        c[0].text = str(i)
        c[1].text = s.get("text", "")
        rc = s.get("risk_code", "L0")
        c[2].text = f"{rc} {s.get('risk_label','')}"
        if c[2].paragraphs[0].runs:
            c[2].paragraphs[0].runs[0].font.color.rgb = RGBColor.from_string(_LEVEL_HEX.get(rc, "7F8C8D"))
        types = "；".join(f"{x['id']} {x['name']}[{_CONFIRM_TEXT.get(x.get('confirmed'))}]"
                          for x in s.get("matched_subtypes", []))
        c[3].text = types
        reason = s.get("llm_reason", "")
        evid = s.get("llm_evidence") or []
        c[4].text = reason + (("\n证据：" + "；".join(evid)) if evid else "")

    # 四、附注
    doc.add_heading("四、附注", level=1)
    doc.add_paragraph(
        "1. 本报告由「规则词典初筛 + 大模型逐句语义复核 + 六维评分」自动生成，"
        "命中类型经大模型在规则候选内复核，标「疑似误命中」者为规则命中但大模型判不成立。")
    doc.add_paragraph(
        "2. 六维为整页聚合值（各维取所有命中句的最大值），逐句明细见上表；"
        "风险等级依据六维总分区间并叠加升降档规则得出。")
    doc.add_paragraph("3. 结论供人工复核参考，不作为唯一处置依据。")

    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    return buf


# 新规则五色灯 → 文本色（docx，hex 不带 #）
_LIGHT_HEX = {"green": "2E9E5B", "yellow": "C99A00", "orange": "E67E22",
              "red": "D64541", "purple": "8E44AD"}
_LEVEL_CN = {1: "1级·绿灯（正常）", 2: "2级·黄灯（关注）", 3: "3级·橙灯（误导）",
             4: "4级·红灯（高危）", 5: "5级·紫灯（重大）"}


def build_six_dim_docx(result: dict, sd: dict) -> io.BytesIO:
    """按《五大意识形态风险域统一命中规则》把一次分析结果渲染成 Word 报告。"""
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    doc.styles["Normal"].font.name = "Microsoft YaHei"
    doc.styles["Normal"].font.size = Pt(10.5)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    level_num = int(result.get("risk_level_num") or 1)
    light = result.get("warning_light") or "green"
    pct = result.get("risk_percent")
    hexc = _LIGHT_HEX.get(light, "7F8C8D")

    title = doc.add_heading("青少年意识形态内容风险评估报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph(); sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = sub.add_run(f"青年心盾内容风险识别系统 · 生成时间 {now}")
    r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x7F, 0x8C, 0x8D)

    def kv_table(rows):
        t = doc.add_table(rows=0, cols=2); t.style = "Light Grid Accent 1"
        for k, v in rows:
            c = t.add_row().cells
            c[0].text = k; c[1].text = str(v)
            c[0].paragraphs[0].runs[0].bold = True

    # 一、基本信息
    doc.add_heading("一、基本信息", level=1)
    domains = "、".join(d.get("name", "") for d in sd.get("domains", [])) or "—"
    kv_table([
        ("页面链接", result.get("url") or "—"),
        ("平台", result.get("platform") or "—"),
        ("受众年龄段", result.get("_age_group") or "13-15"),
        ("分析时间", now),
        ("命中风险域", domains),
        ("评分规则", "六维 0/1 加权 + 一票否决 + 重大风险 M1-M11（统一命中规则）"),
    ])

    # 二、总体结论
    doc.add_heading("二、总体结论", level=1)
    p = doc.add_paragraph()
    rr = p.add_run(f"风险等级：{_LEVEL_CN.get(level_num, level_num)}　|　风险指数：{pct} / 100")
    rr.bold = True; rr.font.size = Pt(12); rr.font.color.rgb = RGBColor.from_string(hexc)
    if sd.get("summary"):
        ps = doc.add_paragraph(); ps.add_run("总体判定：").bold = True; ps.add_run(sd["summary"])
    if sd.get("adjustments"):
        pa = doc.add_paragraph(); pa.add_run("定级修正：").bold = True; pa.add_run("、".join(sd["adjustments"]))
    if result.get("one_vote_veto"):
        doc.add_paragraph().add_run("⚠ 命中一票否决，按最高风险处置。").bold = True
    if result.get("major_ideological_risk"):
        doc.add_paragraph().add_run("⚠ 命中重大意识形态风险。").bold = True
    act = _risk_action(result.get("risk_level") or "L0")
    if act:
        pc = doc.add_paragraph(); pc.add_run("处置建议：").bold = True; pc.add_run(act)

    # 三、六维评分
    doc.add_heading("三、六维 0/1 命中评分（加权）", level=1)
    dt = doc.add_table(rows=1, cols=4); dt.style = "Light Grid Accent 1"
    hdr = dt.rows[0].cells
    for i, name in enumerate(["维度", "命中项数", "权重", "命中编号"]):
        hdr[i].text = name; hdr[i].paragraphs[0].runs[0].bold = True
    for dim in sd.get("dimensions", []):
        c = dt.add_row().cells
        c[0].text = dim.get("name", "")
        c[1].text = f"{dim.get('hit_count', 0)}/{dim.get('total', 6)}"
        c[2].text = f"{int((dim.get('weight') or 0) * 100)}%"
        c[3].text = "、".join(it["id"] for it in dim.get("items", []) if it.get("hit")) or "—"

    # 四、命中详情与逐句证据
    doc.add_heading("四、命中详情与逐句证据", level=1)
    ev = sd.get("evidence", [])
    if ev:
        et = doc.add_table(rows=1, cols=3); et.style = "Light Grid Accent 1"
        h = et.rows[0].cells
        for i, name in enumerate(["原文片段", "命中编号", "风险说明"]):
            h[i].text = name; h[i].paragraphs[0].runs[0].bold = True
        for e in ev:
            c = et.add_row().cells
            c[0].text = e.get("span", "")
            ids = [x.get("id") for x in e.get("item_descs", [])] or e.get("items", [])
            c[1].text = "、".join(ids)
            c[2].text = e.get("note", "")
    else:
        doc.add_paragraph("未提取到明显风险片段。")

    # 五、规则命中
    veto = sd.get("veto_rules", []); major = sd.get("major_rules", [])
    if veto or major:
        doc.add_heading("五、规则命中（一票否决 / 重大风险）", level=1)
        for v in veto:
            para = doc.add_paragraph(style="List Bullet")
            para.add_run("一票否决　").bold = True; para.add_run(v.get("desc", v.get("id", "")))
        for m in major:
            para = doc.add_paragraph(style="List Bullet")
            para.add_run(f"{m.get('id','')}　").bold = True; para.add_run(m.get("desc", ""))

    # 附注
    doc.add_heading("附注", level=1)
    doc.add_paragraph("1. 本报告依据《青少年五大意识形态风险域统一命中规则》，由六维 0/1 命中加权、一票否决、重大风险 M1-M11 与传播/语境修正综合生成。")
    doc.add_paragraph("2. 六维加权得 RiskPercent，映射 1-5 级五色灯；一票否决/重大风险可将等级顶至 4/5 级。")
    doc.add_paragraph("3. 结论供人工复核参考，不作为唯一处置依据。")

    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    return buf


@app.post("/report/nihilism.docx")
async def report_nihilism_docx(req: AnalyzeRequest):
    """对一次分析结果生成意识形态风险评估报告（Word 下载）。复用分析缓存，不重复调模型。"""
    result = await analyze(req)
    result["_age_group"] = req.age_group
    sr = result.get("structured_result") or {}
    six = sr.get("six_dim_detail")
    if six:
        buf = build_six_dim_docx(result, six)   # 新规则六维报告
    else:
        buf = build_nihilism_docx(result, sr.get("nihilism_detail"))  # 旧格式兜底
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    fname = f"意识形态风险评估报告_{result.get('platform','') or '页面'}_{stamp}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@app.get("/scripts")
async def scripts_api(case_id: str = "", domain: str = ""):
    return {"scripts": get_scripts_by_case(case_id, domain)}


class GuidanceReq(BaseModel):
    risk_label: str = ""
    risk_types: list = []
    domains: list = []
    key_points: list = []


@app.post("/guidance/generate")
async def guidance_generate(req: GuidanceReq):
    """按当前内容分析结果，AI 生成三个年龄段的处置策略 + 引导话术（非固定库）。"""
    loop = asyncio.get_event_loop()
    guidance = await loop.run_in_executor(
        None, generate_guidance_sync,
        req.risk_label, req.risk_types, req.domains, req.key_points
    )
    return {"guidance": guidance}

@app.get("/scripts/adapted")
async def scripts_adapted_api(case_id: str = "", domain: str = ""):
    """后台预取：带 Claude 适配，有 case_id 内存缓存，重复调用秒返回。"""
    case_info = next((c for c in _case_records if c.get("case_id") == case_id), None)
    loop = asyncio.get_event_loop()
    scripts = await loop.run_in_executor(
        None, get_adapted_scripts_for_case, case_id, domain, case_info
    )
    return {"scripts": scripts}

class AdaptScriptReq(BaseModel):
    base_content: str
    phases: dict   # {"6-12": "简短提示+...", "13-15": "...", "16-18": "..."}
    follow_up: str = ""
    domain: str = ""

@app.post("/scripts/adapt")
async def adapt_scripts(req: AdaptScriptReq):
    if not _ai_call_allowed():
        return {"ok": False, "adapted": {}, "error": "AI服务暂时不可用，请稍后重试"}
    prompt = (
        "你是青少年意识形态引导专家。以下是一段引导话术的基础内容：\n\n"
        f"{req.base_content}\n\n"
        "请根据以下三个年龄段的引导策略，分别改写为适合该年龄段学生的版本"
        "（语气、用词、举例均需贴合年龄特点，每版80-120字）：\n"
        f"- 6-12岁 引导策略：{req.phases.get('6-12','')}\n"
        f"- 13-15岁 引导策略：{req.phases.get('13-15','')}\n"
        f"- 16-18岁 引导策略：{req.phases.get('16-18','')}\n\n"
        "只输出JSON对象，格式：{\"6-12\":\"...\",\"13-15\":\"...\",\"16-18\":\"...\"}"
    )
    try:
        import json as _json, re as _re
        chunks = []
        with ai_client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for t in stream.text_stream:
                chunks.append(t)
        text = "".join(chunks).strip()
        m = _re.search(r'\{.*\}', text, _re.DOTALL)
        if m:
            data = _json.loads(m.group())
            return {"ok": True, "adapted": data}
        return {"ok": False, "adapted": {}}
    except Exception as e:
        return {"ok": False, "adapted": {}, "error": str(e)}

@app.get("/cases")
async def get_cases(domain: str = "", level: str = ""):
    conds, params = ["1=1"], []
    if domain:
        conds.append("risk_domain_l1 = %s"); params.append(domain)
    if level:
        conds.append("risk_level = %s"); params.append(level)
    rows = qall(f"""
        SELECT case_id, case_name, risk_domain_l1, risk_type_l2,
               platform, risk_level, case_priority_score,
               CAST(total_interaction AS SIGNED) AS total_interaction,
               representative_text, spread_mechanism, spread_path, core_keywords,
               disposal_strategy_6_12, disposal_strategy_13_15,
               disposal_strategy_16_18, review_conclusion
        FROM t_case
        WHERE case_id LIKE 'ZQW%' AND {' AND '.join(conds)}
        ORDER BY case_priority_score DESC
    """, params)
    return {"cases": rows, "total": len(rows)}

@app.post("/search/cases")
async def search_cases(req: CaseSearchRequest):
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "请输入要检索的关键词或短句")
    return search_cases_by_query(query, req.domain, req.top_k)

@app.post("/search/corpus")
async def search_corpus(req: CorpusSearchRequest):
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "请输入要检索的关键词或短句")
    return search_corpus_by_query(
        query=query,
        domain=req.domain,
        platform=req.platform,
        risk_level=req.risk_level,
        top_k=req.top_k,
    )

def _detect_visual_ai_intent(prompt: str, preferred: str = "") -> dict:
    text = f"{preferred} {prompt}".lower()
    if any(k in text for k in ["地域", "地区", "城市", "地图", "热度", "流场", "等高线", "密度", "geo", "contour", "flow"]):
        visual_type = "flow"
    elif any(k in text for k in ["层级", "矩阵", "分发", "星系", "组织", "父子", "树", "hierarchy", "galaxy"]):
        visual_type = "galaxy"
    else:
        visual_type = "force"

    titles = {
        "force": "传播关系星云图",
        "flow": "全域热度流场图",
        "galaxy": "矩阵分发星系图",
    }
    chart_names = {
        "force": "force-nebula",
        "flow": "density-flow",
        "galaxy": "orbit-hierarchy",
    }
    return {
        "visual_type": visual_type,
        "title": titles[visual_type],
        "chart": chart_names[visual_type],
    }

def _visual_ai_metrics() -> list:
    return [
        {"label": "覆盖平台", "value": "6", "unit": "个", "trend": "+2"},
        {"label": "传播热度", "value": "87.6", "unit": "", "trend": "+12.4%"},
        {"label": "核心节点", "value": "18", "unit": "个", "trend": "+5"},
        {"label": "协同指数", "value": "73", "unit": "", "trend": "+8.1%"},
    ]

def _visual_ai_force_data() -> dict:
    nodes = [
        {"id": "center", "label": "总台主账号", "type": "source", "platform": "央视频", "cluster": "核心", "value": 96, "sentiment": "positive"},
        {"id": "douyin", "label": "短视频矩阵", "type": "platform", "platform": "抖音", "cluster": "视频", "value": 88, "sentiment": "positive"},
        {"id": "weibo", "label": "微博话题场", "type": "platform", "platform": "微博", "cluster": "社交", "value": 82, "sentiment": "neutral"},
        {"id": "wechat", "label": "公众号矩阵", "type": "platform", "platform": "微信", "cluster": "图文", "value": 76, "sentiment": "positive"},
        {"id": "bili", "label": "B站二创圈", "type": "platform", "platform": "B站", "cluster": "视频", "value": 69, "sentiment": "positive"},
        {"id": "local", "label": "地方号联动", "type": "account", "platform": "地方媒体", "cluster": "协同", "value": 74, "sentiment": "neutral"},
        {"id": "topic1", "label": "热点议题A", "type": "topic", "platform": "全平台", "cluster": "议题", "value": 84, "sentiment": "positive"},
        {"id": "topic2", "label": "服务效能", "type": "topic", "platform": "全平台", "cluster": "议题", "value": 71, "sentiment": "neutral"},
        {"id": "kol1", "label": "头部创作者", "type": "account", "platform": "抖音", "cluster": "视频", "value": 78, "sentiment": "positive"},
        {"id": "kol2", "label": "评论扩散层", "type": "account", "platform": "微博", "cluster": "社交", "value": 64, "sentiment": "neutral"},
        {"id": "content1", "label": "重点内容1", "type": "content", "platform": "央视频", "cluster": "内容", "value": 91, "sentiment": "positive"},
        {"id": "content2", "label": "重点内容2", "type": "content", "platform": "微信", "cluster": "内容", "value": 67, "sentiment": "neutral"},
    ]
    links = [
        {"source": "center", "target": "content1", "weight": 0.95},
        {"source": "center", "target": "douyin", "weight": 0.86},
        {"source": "center", "target": "weibo", "weight": 0.81},
        {"source": "center", "target": "wechat", "weight": 0.78},
        {"source": "center", "target": "local", "weight": 0.72},
        {"source": "douyin", "target": "kol1", "weight": 0.76},
        {"source": "douyin", "target": "bili", "weight": 0.58},
        {"source": "weibo", "target": "kol2", "weight": 0.69},
        {"source": "weibo", "target": "topic1", "weight": 0.82},
        {"source": "wechat", "target": "content2", "weight": 0.63},
        {"source": "content1", "target": "topic1", "weight": 0.88},
        {"source": "content2", "target": "topic2", "weight": 0.67},
        {"source": "local", "target": "topic2", "weight": 0.61},
        {"source": "bili", "target": "topic1", "weight": 0.54},
    ]
    return {"nodes": nodes, "links": links}

def _visual_ai_flow_data() -> dict:
    points = [
        {"name": "北京", "x": 0.58, "y": 0.32, "value": 96, "delta": 14},
        {"name": "上海", "x": 0.66, "y": 0.58, "value": 82, "delta": 9},
        {"name": "广州", "x": 0.55, "y": 0.78, "value": 76, "delta": 7},
        {"name": "成都", "x": 0.38, "y": 0.61, "value": 68, "delta": 11},
        {"name": "武汉", "x": 0.52, "y": 0.58, "value": 64, "delta": 5},
        {"name": "西安", "x": 0.43, "y": 0.46, "value": 58, "delta": 4},
        {"name": "杭州", "x": 0.64, "y": 0.55, "value": 71, "delta": 8},
        {"name": "重庆", "x": 0.42, "y": 0.66, "value": 61, "delta": 6},
    ]
    timeline = [
        {"time": "09:00", "value": 42},
        {"time": "11:00", "value": 56},
        {"time": "13:00", "value": 63},
        {"time": "15:00", "value": 88},
        {"time": "17:00", "value": 74},
        {"time": "19:00", "value": 91},
    ]
    return {"points": points, "timeline": timeline}

def _visual_ai_galaxy_data() -> dict:
    return {
        "name": "总台传播主阵地",
        "value": 100,
        "children": [
            {
                "name": "视频分发",
                "value": 88,
                "children": [
                    {"name": "短视频账号", "value": 72},
                    {"name": "直播切片", "value": 66},
                    {"name": "二创内容", "value": 58},
                ],
            },
            {
                "name": "社交扩散",
                "value": 82,
                "children": [
                    {"name": "微博话题", "value": 77},
                    {"name": "评论互动", "value": 61},
                    {"name": "达人转发", "value": 68},
                ],
            },
            {
                "name": "地方协同",
                "value": 74,
                "children": [
                    {"name": "地方媒体号", "value": 69},
                    {"name": "区域专题", "value": 55},
                ],
            },
            {
                "name": "图文沉淀",
                "value": 70,
                "children": [
                    {"name": "公众号", "value": 64},
                    {"name": "长图解读", "value": 52},
                ],
            },
        ],
    }

def _build_visual_ai_response(req: VisualAIRequest) -> dict:
    prompt = (req.prompt or "").strip()
    intent = _detect_visual_ai_intent(prompt, req.preferred_visual)
    visual_type = intent["visual_type"]
    datasets = {
        "force": _visual_ai_force_data,
        "flow": _visual_ai_flow_data,
        "galaxy": _visual_ai_galaxy_data,
    }
    summaries = {
        "force": "识别到关系网络类需求，优先展示账号、内容、议题之间的传播关联和核心节点。",
        "flow": "识别到地域热度或趋势类需求，优先展示传播热度的空间分布和时序变化。",
        "galaxy": "识别到层级矩阵类需求，优先展示主阵地、分发渠道与下级节点的层级关系。",
    }
    return {
        "ok": True,
        "mode": "demo",
        "request": {
            "prompt": prompt,
            "time_range": req.time_range,
            "data_source": req.data_source,
        },
        "intent": {
            "analysis_goal": "效能屏可视化分析",
            "visual_type": visual_type,
            "recommended_chart": intent["chart"],
            "reason": summaries[visual_type],
        },
        "chart": {
            "title": intent["title"],
            "type": visual_type,
            "theme": "command-screen",
        },
        "metrics": _visual_ai_metrics(),
        "data": datasets[visual_type](),
        "insights": [
            summaries[visual_type],
            "当前为通用模拟数据版本，已预留字段映射位置，后续可替换为真实数据库查询结果。",
            "建议下一步补充数据字典、指标口径和大屏接入方式，以便完成真实数据适配。",
        ],
        "next_questions": [
            "需要展示哪个业务主题或专题？",
            "指标更关注传播热度、互动效率还是协同覆盖？",
            "大屏侧希望以嵌入页面还是组件插件方式接入？",
        ],
    }

@app.post("/visual-ai/generate")
async def visual_ai_generate(req: VisualAIRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "请输入可视化分析需求")
    return _build_visual_ai_response(req)

@app.get("/health")
async def health():
    return {"status":"ok","keywords_loaded":len(_KEYWORDS)}

class NihilismDebugReq(BaseModel):
    text: str
    title: str = ""
    platform: str = ""
    age_group: str = ""

@app.post("/debug/nihilism_score")
async def debug_nihilism_score(req: NihilismDebugReq):
    """联调用：直接对一段文本跑历史虚无主义六维判分，不经过数据库/案例检索。"""
    result = nihilism_scorer.run_nihilism_pipeline(
        req.text, title=req.title, platform=req.platform, age_group=req.age_group,
        ai_client=ai_client, ai_call_allowed=_ai_call_allowed,
        ai_record_success=_ai_record_success, ai_record_failure=_ai_record_failure,
    )
    return result or {"matched": False}

# ══════════════════════════════════════════════════════
#  Dashboard 接口
# ══════════════════════════════════════════════════════

# 固定展示的两个代表账号
_DEMO_ACCOUNTS = [
    {"author": "用**", "platform": "微博"},
    {"author": "小**", "platform": "微博"},
]

HIGH_RISK_LEVELS = ("高风险", "极高风险", "L3", "L4")

def _account_grade(avg_score: float) -> str:
    if avg_score >= 3.5:
        return "D"
    if avg_score >= 2.5:
        return "C"
    if avg_score >= 1.5:
        return "B"
    return "A"

def _account_profile(grade: str) -> str:
    return {
        "D": "优先复核主体",
        "C": "重点关注主体",
        "B": "持续观察主体",
        "A": "常规观察主体",
    }.get(grade, "常规观察主体")

def _format_account_rank(row: dict) -> dict:
    total = int(row.get("cnt") or row.get("total") or 0)
    high_count = int(row.get("high_cnt") or row.get("high_count") or 0)
    avg_score = float(row.get("avg_s") or row.get("avg_score") or 0)
    grade = _account_grade(avg_score)
    return {
        "author": row.get("author") or "",
        "platform": row.get("platform") or "",
        "total": total,
        "avg_score": avg_score,
        "max_score": float(row.get("max_s") or row.get("max_score") or 0),
        "high_count": high_count,
        "high_rate": round(high_count / total, 4) if total else 0,
        "domain_count": int(row.get("domain_cnt") or row.get("domain_count") or 0),
        "risk_profile": _account_profile(grade),
        "grade": grade,
    }

def _account_scope(author: str = "", platform: str = "", domain: str = ""):
    filters, params = [], []
    if author:
        filters.append("author = %s"); params.append(author)
    else:
        filters.extend(["author IS NOT NULL", "author != ''"])
    if platform:
        filters.append("platform = %s"); params.append(platform)
    else:
        filters.extend(["platform IS NOT NULL", "platform != ''"])
    if domain:
        filters.append("risk_domain_l1 = %s"); params.append(domain)
    return " AND ".join(filters), params

def _query_account_rank(limit: int = 0, offset: int = 0, platform: str = "", domain: str = "") -> list:
    limit = max(0, min(2000, int(limit or 0)))
    offset = max(0, int(offset or 0))
    where_sql, params = _account_scope(platform=platform, domain=domain)
    limit_sql = " LIMIT %s OFFSET %s" if limit else ""
    if limit:
        params = [*params, limit, offset]
    rows = qall(f"""
        SELECT author, platform,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s,
               MAX(total_risk_score) AS max_s,
               SUM(CASE WHEN risk_level IN ('高风险','极高风险','L3','L4')
                         OR total_risk_score >= 3.0 THEN 1 ELSE 0 END) AS high_cnt,
               COUNT(DISTINCT CASE
                    WHEN risk_domain_l1 != '' AND risk_domain_l1 != '未匹配'
                    THEN risk_domain_l1 END) AS domain_cnt
        FROM t_corpus
        WHERE {where_sql}
        GROUP BY author, platform
        ORDER BY
            CASE
                WHEN AVG(total_risk_score) >= 3.5 THEN 4
                WHEN AVG(total_risk_score) >= 2.5 THEN 3
                WHEN AVG(total_risk_score) >= 1.5 THEN 2
                ELSE 1
            END DESC,
            (SUM(CASE WHEN risk_level IN ('高风险','极高风险','L3','L4')
                      OR total_risk_score >= 3.0 THEN 1 ELSE 0 END) / COUNT(*)) DESC,
            avg_s DESC,
            high_cnt DESC,
            max_s DESC,
            cnt DESC
        {limit_sql}
    """, tuple(params))
    return [_format_account_rank(r) for r in rows]

def _account_summary(accounts: list, platform: str = "", domain: str = "") -> dict:
    where_sql, params = _account_scope(platform=platform, domain=domain)
    account_row = qone(f"""
        SELECT COUNT(*) AS account_cnt
        FROM (
            SELECT author, platform
            FROM t_corpus
            WHERE {where_sql}
            GROUP BY author, platform
        ) grouped_accounts
    """, tuple(params)) or {}
    base = qone(f"""
        SELECT COUNT(*) AS total,
               ROUND(AVG(total_risk_score),2) AS avg_s,
               SUM(CASE WHEN risk_level IN ('高风险','极高风险','L3','L4')
                         OR total_risk_score >= 3.0 THEN 1 ELSE 0 END) AS high_cnt,
               COUNT(DISTINCT CASE
                    WHEN risk_domain_l1 != '' AND risk_domain_l1 != '未匹配'
                    THEN risk_domain_l1 END) AS domain_cnt
        FROM t_corpus
        WHERE {where_sql}
    """, tuple(params)) or {}
    return {
        "account_count": int(account_row.get("account_cnt") or len(accounts)),
        "content_count": int(base.get("total") or 0),
        "avg_score": float(base.get("avg_s") or 0),
        "high_count": int(base.get("high_cnt") or 0),
        "domain_count": int(base.get("domain_cnt") or 0),
    }

def _build_account_detail(author: str, platform: str, domain: str = "") -> dict:
    where_sql, params = _account_scope(author=author, platform=platform, domain=domain)

    base = qone(f"""
        SELECT COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s,
               MAX(total_risk_score) AS max_s,
               SUM(CASE WHEN risk_level IN ('高风险','极高风险','L3','L4')
                         OR total_risk_score >= 3.0 THEN 1 ELSE 0 END) AS high_cnt,
               COUNT(DISTINCT CASE
                    WHEN risk_domain_l1 != '' AND risk_domain_l1 != '未匹配'
                    THEN risk_domain_l1 END) AS domain_cnt
        FROM t_corpus
        WHERE {where_sql}
    """, tuple(params)) or {}

    if not int(base.get("cnt") or 0):
        raise HTTPException(404, "未找到该发布主体的画像记录")

    domains = qall(f"""
        SELECT risk_domain_l1 AS domain,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s
        FROM t_corpus
        WHERE {where_sql}
          AND risk_domain_l1 != '' AND risk_domain_l1 != '未匹配'
        GROUP BY risk_domain_l1
        ORDER BY cnt DESC, avg_s DESC
    """, tuple(params))

    levels = qall(f"""
        SELECT risk_level AS lv, COUNT(*) AS cnt
        FROM t_corpus
        WHERE {where_sql}
          AND risk_level IS NOT NULL AND risk_level != ''
        GROUP BY risk_level
        ORDER BY cnt DESC
    """, tuple(params))

    trend = qall(f"""
        SELECT DATE_FORMAT(publish_time,'%Y-%m') AS month,
               ROUND(AVG(total_risk_score),2) AS avg_s,
               COUNT(*) AS cnt
        FROM t_corpus
        WHERE {where_sql}
          AND publish_time IS NOT NULL
          AND publish_time >= '2000-01-01'
        GROUP BY DATE_FORMAT(publish_time,'%Y-%m')
        ORDER BY month ASC
        LIMIT 12
    """, tuple(params))

    types = qall(f"""
        SELECT risk_type_l2 AS rtype,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s
        FROM t_corpus
        WHERE {where_sql}
          AND risk_type_l2 IS NOT NULL AND risk_type_l2 != ''
        GROUP BY risk_type_l2
        ORDER BY cnt DESC, avg_s DESC
        LIMIT 5
    """, tuple(params))

    posts = qall(f"""
        SELECT corpus_id,
               title,
               LEFT(content_summary, 220) AS summary,
               risk_domain_l1 AS domain,
               risk_type_l2 AS rtype,
               risk_level AS lv,
               ROUND(total_risk_score,2) AS score,
               DATE_FORMAT(publish_time,'%Y-%m-%d') AS pub_date,
               like_count,
               comment_count,
               share_count,
               collect_count,
               interaction_count,
               url
        FROM t_corpus
        WHERE {where_sql}
        ORDER BY total_risk_score DESC, interaction_count DESC, publish_time DESC
        LIMIT 5
    """, tuple(params))

    detail = _format_account_rank({
        "author": author,
        "platform": platform,
        "cnt": base.get("cnt"),
        "avg_s": base.get("avg_s"),
        "max_s": base.get("max_s"),
        "high_cnt": base.get("high_cnt"),
        "domain_cnt": base.get("domain_cnt"),
    })
    detail.update({
        "domains": domains,
        "levels": levels,
        "trend": trend,
        "top_types": types,
        "posts": posts,
    })
    return detail

@app.get("/dashboard/accounts/rank")
async def dashboard_account_rank(limit: int = 120, offset: int = 0, platform: str = "", domain: str = ""):
    """发布主体风险榜。limit=0 表示返回当前筛选下的全部发布主体。"""
    accounts = _query_account_rank(limit=limit, offset=offset, platform=platform, domain=domain)
    summary = _account_summary(accounts, platform=platform, domain=domain)
    return {
        "accounts": accounts,
        "summary": summary,
        "page": {
            "limit": max(0, min(2000, int(limit or 0))),
            "offset": max(0, int(offset or 0)),
            "returned": len(accounts),
            "has_more": bool(limit and max(0, int(offset or 0)) + len(accounts) < summary["account_count"]),
        },
        "filters": {"platform": platform, "domain": domain},
    }

@app.get("/dashboard/accounts/detail")
async def dashboard_account_detail(author: str = Query(...), platform: str = Query(...), domain: str = ""):
    """单个发布主体画像详情。"""
    return {
        "account": _build_account_detail(author=author, platform=platform, domain=domain),
        "filters": {"platform": platform, "domain": domain},
    }

@app.get("/dashboard/accounts")
async def dashboard_accounts(limit: int = 30, platform: str = "", domain: str = ""):
    """兼容旧页面：返回带详情的发布主体画像列表。"""
    accounts = _query_account_rank(limit=limit, platform=platform, domain=domain)
    result = [
        _build_account_detail(author=a["author"], platform=a["platform"], domain=domain)
        for a in accounts
    ]
    return {"accounts": result}


@app.get("/dashboard/trends")
async def dashboard_trends(platform: str = "", domain: str = ""):
    """月度风险漂移曲线（各平台）。"""
    conds, params = [], []
    if platform:
        conds.append("platform = %s"); params.append(platform)
    if domain:
        conds.append("risk_domain_l1 = %s"); params.append(domain)
    cond = ("AND " + " AND ".join(conds)) if conds else ""
    rows = qall(f"""
        SELECT DATE_FORMAT(publish_time,'%Y-%m') AS month,
               platform,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s,
               SUM(CASE WHEN risk_level IN ('高风险','极高风险','L3','L4')
                         OR total_risk_score >= 3.0 THEN 1 ELSE 0 END) AS high_cnt
        FROM t_corpus
        WHERE publish_time IS NOT NULL {cond}
          AND platform IN ('微博','抖音','小红书','百度')
        GROUP BY DATE_FORMAT(publish_time,'%Y-%m'), platform
        ORDER BY month ASC, platform
    """, tuple(params))
    return {"series": rows, "filters": {"platform": platform, "domain": domain}}


@app.get("/dashboard/matrix")
async def dashboard_matrix(domain: str = "", platform: str = ""):
    """风险域 × 平台 数量与均分矩阵。"""
    conds, params = ["risk_domain_l1 != ''", "risk_domain_l1 != '未匹配'",
                     "platform IN ('微博','抖音','小红书','百度')"], []
    if domain:
        conds.append("risk_domain_l1 = %s"); params.append(domain)
    if platform:
        conds.append("platform = %s"); params.append(platform)
    where_sql = " AND ".join(conds)

    kpi = qone(f"""
        SELECT COUNT(*) AS total,
               ROUND(AVG(total_risk_score),2) AS avg_s,
               SUM(CASE WHEN risk_level IN ('高风险','极高风险','L3','L4')
                         OR total_risk_score >= 3.0 THEN 1 ELSE 0 END) AS high_cnt,
               COUNT(DISTINCT platform) AS platform_cnt,
               COUNT(DISTINCT risk_domain_l1) AS domain_cnt
        FROM t_corpus
        WHERE {where_sql}
    """, tuple(params)) or {}

    rows = qall(f"""
        SELECT risk_domain_l1 AS domain, platform,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s
        FROM t_corpus
        WHERE {where_sql}
        GROUP BY risk_domain_l1, platform
        ORDER BY risk_domain_l1, cnt DESC
    """, tuple(params))

    # 叙事聚类：L1 下的 L2 分布
    clusters = qall(f"""
        SELECT risk_domain_l1 AS domain, risk_type_l2 AS rtype,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s
        FROM t_corpus
        WHERE {where_sql}
          AND risk_type_l2 IS NOT NULL AND risk_type_l2 != ''
        GROUP BY risk_domain_l1, risk_type_l2
        ORDER BY risk_domain_l1, cnt DESC
    """, tuple(params))

    collaboration = qall(f"""
        SELECT risk_type_l2 AS rtype,
               COUNT(DISTINCT platform) AS platform_cnt,
               GROUP_CONCAT(DISTINCT platform ORDER BY platform SEPARATOR '、') AS platforms,
               COUNT(*) AS cnt,
               ROUND(AVG(total_risk_score),2) AS avg_s
        FROM t_corpus
        WHERE {where_sql}
          AND risk_type_l2 IS NOT NULL AND risk_type_l2 != ''
        GROUP BY risk_type_l2
        HAVING platform_cnt >= 2
        ORDER BY platform_cnt DESC, avg_s DESC, cnt DESC
        LIMIT 8
    """, tuple(params))

    return {
        "kpi": {
            "total": int(kpi.get("total") or 0),
            "high_count": int(kpi.get("high_cnt") or 0),
            "avg_score": float(kpi.get("avg_s") or 0),
            "platform_count": int(kpi.get("platform_cnt") or 0),
            "domain_count": int(kpi.get("domain_cnt") or 0),
        },
        "matrix": rows,
        "clusters": clusters,
        "collaboration": collaboration,
        "filters": {"platform": platform, "domain": domain},
    }


@app.get("/dashboard/hotspots")
async def dashboard_hotspots(domain: str = "", platform: str = "", limit: int = 15):
    """近期高风险内容预警列表。"""
    conds, params_list = [], []
    if domain:
        conds.append("risk_domain_l1 = %s"); params_list.append(domain)
    if platform:
        conds.append("platform = %s"); params_list.append(platform)
    cond = ("AND " + " AND ".join(conds)) if conds else ""
    params_list.append(limit)
    rows = qall(f"""
        SELECT title, risk_domain_l1 AS domain, risk_type_l2 AS rtype,
               total_risk_score AS score, risk_level AS lv,
               platform, author,
               DATE_FORMAT(publish_time,'%Y-%m-%d') AS pub_date,
               like_count, comment_count, url
        FROM t_corpus
        WHERE total_risk_score >= 3.0
          AND title IS NOT NULL AND title != ''
          AND publish_time IS NOT NULL
          {cond}
        ORDER BY total_risk_score DESC, publish_time DESC
        LIMIT %s
    """, tuple(params_list))
    return {"hotspots": rows}


@app.get("/dashboard/alerts")
async def dashboard_alerts(domain: str = "", platform: str = "",
                            risk_level: str = "", limit: int = 30):
    """预警内容：中风险（score>=2.0）及以上，按风险分降序。"""
    # 基础条件：中风险及以上（score >= 2.0）
    conds = ["total_risk_score >= 2.0",
             "(title IS NOT NULL AND title != '' OR content_summary IS NOT NULL AND content_summary != '')"]
    params_list = []
    # risk_level 过滤：同时匹配中文标签和 L 码
    LEVEL_LABELS = {
        "L4": ("极高风险", "L4"),
        "极高风险": ("极高风险", "L4"),
        "L3": ("高风险", "L3"),
        "高风险": ("高风险", "L3"),
        "L2": ("中度风险", "中风险", "L2"),
        "中度风险": ("中度风险", "中风险", "L2"),
    }
    if risk_level and risk_level in LEVEL_LABELS:
        labels = LEVEL_LABELS[risk_level]
        placeholders = ",".join(["%s"] * len(labels))
        conds.append(f"risk_level IN ({placeholders})")
        params_list.extend(labels)
    if domain:
        conds.append("risk_domain_l1 = %s"); params_list.append(domain)
    if platform:
        conds.append("platform = %s"); params_list.append(platform)
    count_params = list(params_list)
    params_list.append(max(1, min(100, int(limit))))
    rows = qall(f"""
        SELECT COALESCE(NULLIF(title,''), LEFT(content_summary,60)) AS title,
               risk_domain_l1 AS domain, risk_type_l2 AS rtype,
               ROUND(total_risk_score, 2) AS score, risk_level AS lv,
               platform, author,
               DATE_FORMAT(publish_time, '%Y-%m-%d') AS pub_date,
               like_count, comment_count, url
        FROM t_corpus
        WHERE {' AND '.join(conds)}
        ORDER BY total_risk_score DESC
        LIMIT %s
    """, tuple(params_list))
    total_row = qone(f"""
        SELECT COUNT(*) AS cnt FROM t_corpus
        WHERE {' AND '.join(conds)}
    """, tuple(count_params)) or {}
    return {
        "alerts": rows,
        "total": int(total_row.get("cnt") or len(rows)),
        "filters": {"domain": domain, "platform": platform, "risk_level": risk_level},
    }


# ── 页面扫描：爬取中青网等页面，提取文章并做风险评估 ──

def _crawl_html(url: str) -> str:
    """抓取 URL 返回解码后 HTML，自动检测编码。"""
    import urllib.request as _ur
    req = _ur.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://news.youth.cn/",
    })
    raw = _ur.urlopen(req, timeout=12).read()
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")

def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()

def _extract_article_urls(html: str, base_url: str) -> list:
    """从列表页提取文章链接和标题。"""
    from urllib.parse import urljoin
    pattern = re.compile(
        r'href=["\']([^"\']*(?:youth\.cn|中青网)[^"\']*t\d{6,8}_\d+\.htm)["\'][^>]*>(.*?)</a>',
        re.S,
    )
    seen, items = set(), []
    for m in pattern.finditer(html):
        href = m.group(1)
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            from urllib.parse import urlparse
            p = urlparse(base_url)
            href = f"{p.scheme}://{p.netloc}{href}"
        elif not href.startswith("http"):
            href = urljoin(base_url, href)
        title_raw = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        title = re.sub(r"\s+", " ", title_raw)
        if href not in seen and len(title) > 4:
            seen.add(href)
            items.append({"url": href, "title": title})
    return items

def _extract_article_body(html: str) -> dict:
    """从文章页提取标题和正文（针对中青网结构）。"""
    def div_content(cls):
        m = re.search(
            rf'<div[^>]+class=["\'][^"\']*{cls}[^"\']*["\'][^>]*>(.*?)</div>',
            html, re.S,
        )
        return _clean_html(m.group(1)) if m else ""

    title = div_content("page_bt")
    # page_bt 通常含"标题 + 时间 + 来源"，只取第一行
    title = title.split("发稿时间")[0].strip() if "发稿时间" in title else title

    # 兜底用 <title> 标签
    if not title:
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        title = _clean_html(m.group(1)).split("_")[0].strip() if m else ""

    body = div_content("page_nr")
    if not body:
        # 兜底：拼接所有 <p> 正文
        paras = [_clean_html(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S)]
        body = " ".join(
            p for p in paras
            if len(p) > 20 and not re.search(r"(ICP|版权|举报|许可证|公网安备)", p)
        )

    pub_m = re.search(r"发稿时间[：:]\s*([\d\-\s:]+)", html)
    pub_time = pub_m.group(1).strip() if pub_m else ""

    return {"title": title, "body": body[:800], "pub_time": pub_time}

def _score_article(title: str, body: str) -> dict:
    """用内存关键词库对文章标题+正文做快速风险评估。"""
    text = f"{title} {body}"
    matched = match_keywords(text)
    if not matched:
        matched = rag_search(text)

    if not matched:
        return {
            "risk_level": "L0", "risk_label": "低风险",
            "score": 0.0, "domain": "", "matched_tags": [],
        }

    scores = [float(m.get("total_risk_score") or 0) for m in matched]
    max_s = max(scores)
    composite = round(max_s * 0.6 + sum(scores) / len(scores) * 0.4, 2)
    risk_level, risk_label = score_to_level(composite)
    domain_map: dict = {}
    for m in matched:
        d = m.get("risk_domain_l1") or "未分类"
        domain_map[d] = max(domain_map.get(d, 0), float(m.get("total_risk_score") or 0))
    primary = max(domain_map, key=domain_map.get) if domain_map else ""

    return {
        "risk_level": risk_level,
        "risk_label": risk_label,
        "score": composite,
        "domain": primary,
        "matched_tags": [m.get("keyword", "") for m in matched[:3]],
    }

@app.get("/dashboard/scan_page")
async def scan_page(url: str = Query(...), limit: int = 15):
    """
    扫描中青网（或其他）页面：
    - 列表页：提取所有文章链接，对每篇做风险评估
    - 文章页：直接评估该篇文章
    返回文章列表（按出现顺序），每条含风险评估结果。
    """
    if not url.startswith("http"):
        raise HTTPException(400, "请输入完整 URL（以 http/https 开头）")

    loop = asyncio.get_event_loop()
    try:
        html = await loop.run_in_executor(None, _crawl_html, url)
    except Exception as e:
        raise HTTPException(502, f"页面抓取失败：{e}")

    # 判断是列表页还是文章页
    is_article = bool(re.search(r"t\d{8}_\d+\.htm", url))
    articles = []

    if is_article:
        # 单篇文章
        art = _extract_article_body(html)
        risk = _score_article(art["title"], art["body"])
        articles = [{
            "url": url, "title": art["title"],
            "pub_time": art["pub_time"], "summary": art["body"][:120],
            "body": art["body"],
            **risk,
        }]
    else:
        # 列表页：提取子链接，逐篇评估（最多 limit 篇）
        items = _extract_article_urls(html, url)[:limit]
        if not items:
            raise HTTPException(404, "未从该页面提取到文章链接")

        def process_item(item):
            try:
                art_html = _crawl_html(item["url"])
                art = _extract_article_body(art_html)
                title = art["title"] or item["title"]
            except Exception:
                title = item["title"]
                art = {"body": "", "pub_time": ""}
            risk = _score_article(title, art.get("body", ""))
            return {
                "url": item["url"], "title": title,
                "pub_time": art.get("pub_time", ""),
                "summary": art.get("body", "")[:120],
                "body": art.get("body", ""),
                **risk,
            }

        # 并发爬取（最多 4 个线程）
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(process_item, item) for item in items]
            articles = [f.result() for f in concurrent.futures.as_completed(futs)]
        # 按原始顺序排列
        url_order = {item["url"]: i for i, item in enumerate(items)}
        articles.sort(key=lambda a: url_order.get(a["url"], 999))

    # 统计
    risk_order = {"L4": 4, "L3": 3, "L2": 2, "L1": 1, "L0": 0}
    high_count = sum(1 for a in articles if risk_order.get(a["risk_level"], 0) >= 2)
    first_risk = next((a for a in articles if risk_order.get(a["risk_level"], 0) >= 2), None)

    return {
        "source_url": url,
        "is_article": is_article,
        "total": len(articles),
        "high_count": high_count,
        "first_risk_index": articles.index(first_risk) if first_risk else -1,
        "articles": articles,
    }


@app.post("/admin/clear-cache")
async def clear_cache():
    _result_cache.clear()
    _script_adapt_cache.clear()
    _video_cache.clear()
    return {"ok": True, "msg": "已清空 result_cache、script_adapt_cache、video_cache"}

# ── Playwright 持久化浏览器（启动一次，复用页面，不重建进程）──
import threading as _threading
from playwright.sync_api import sync_playwright

_pw_instance  = None          # playwright 进程句柄
_pw_browser   = None          # 持久化 Chromium 实例
_pw_lock      = _threading.Lock()
_video_cache: dict = {}       # url → result，命中直接返回
_pw_executor  = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="pw"
)

def _get_pw_browser():
    global _pw_instance, _pw_browser
    with _pw_lock:
        try:
            if _pw_browser and _pw_browser.is_connected():
                return _pw_browser
        except:
            pass
        try:
            if _pw_instance:
                _pw_instance.stop()
        except:
            pass
        _pw_instance = sync_playwright().start()
        _pw_browser  = _pw_instance.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                  "--no-first-run","--mute-audio"]
        )
        print("✓ Playwright 浏览器已启动（持久化）")
        return _pw_browser

def _thread_run(url: str) -> dict:
    if url in _video_cache:
        print(f"[Playwright] 缓存命中: {url[:50]}")
        return _video_cache[url]

    found = []
    page_title, page_desc = "", ""
    try:
        browser = _get_pw_browser()
        ctx  = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Referer": "https://www.douyin.com/"}
        )
        page = ctx.new_page()

        def on_req(req):
            u = req.url
            if any(d in u for d in ["douyinvod.com","snssdk.com","bytecdn.cn","volces.com"]):
                if "video" in u or ".mp4" in u:
                    found.append(u)
        page.on("request", on_req)

        page.goto(url, timeout=8000, wait_until="domcontentloaded")

        # 找到第一个视频 URL 立即退出，最多再等 3s（30×100ms）
        for _ in range(30):
            if found:
                break
            page.wait_for_timeout(100)

        try:
            page_title = page.title() or ""
        except:
            pass
        for sel in ['[class*="desc"]', '[class*="title"]', 'h1']:
            try:
                el = page.query_selector(sel)
                if el:
                    t = (el.inner_text() or "").strip()
                    if len(t) > 5:
                        page_desc = t[:300]; break
            except:
                pass
        if not found:
            try:
                src = page.eval_on_selector("video", "el => el.src")
                if src and src.startswith("http"):
                    found.append(src)
            except:
                pass

        page.close()
        ctx.close()
    except Exception as e:
        print(f"[Playwright] 提取失败: {e}")
        global _pw_browser
        _pw_browser = None      # 强制下次重建

    clean = [u for u in found if not any(
        x in u for x in ["thumbnail","cover","avatar","jpeg","png","webp"]
    )]
    video_url = clean[0] if clean else ""
    print(f"[Playwright] URL: {video_url[:60] if video_url else '无'}  标题: {page_title[:30]}")
    result = {"video_url": video_url, "title": page_title, "description": page_desc}
    if video_url:
        _video_cache[url] = result
    return result

async def _playwright_extract(url: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pw_executor, _thread_run, url)

# ── 视频提取接口（前端异步调用）──
@app.get("/video/extract")
async def video_extract(url: str = Query(...)):
    from urllib.parse import quote
    u = url.lower()

    # 只有明确的视频平台才走 Playwright
    if "douyin.com/video/" in u:
        info = await _playwright_extract(url)
        if info.get("video_url"):
            proxy = f"/video/proxy?url={quote(info['video_url'], safe='')}&ref={quote('https://www.douyin.com/', safe='')}"
            return {
                "video_url": proxy,
                "method": "playwright",
                "title": info.get("title",""),
                "description": info.get("description",""),
            }

    # B站直接用官方 iframe，不需要 Playwright
    if "bilibili.com" in u or "b23.tv" in u:
        m = re.search(r"BV\w+", url)
        if m:
            bv = m.group(0)
            return {"embed_url": f"//player.bilibili.com/player.html?bvid={bv}&page=1", "method": "bilibili"}

    # 其余链接：只抓 OG 封面图，不启动浏览器
    thumb = _fetch_og_image(url)
    return {"thumb_url": thumb, "method": "og"}

# ── 视频代理（加 Referer 绕防盗链）──
@app.get("/video/proxy")
async def video_proxy(url: str = Query(...), ref: str = Query(default="https://www.douyin.com/")):
    from urllib.parse import unquote
    real_url = unquote(url)
    referer  = unquote(ref)
    def stream():
        with requests.get(real_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
            "Referer": referer,
        }, stream=True, timeout=30) as r:
            for chunk in r.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk
    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={"Accept-Ranges":"bytes","Access-Control-Allow-Origin":"*"}
    )

# ── AI 结构化分析（未收录内容）──
class AIAnalyzeReq(BaseModel):
    url:      str
    platform: str = ""
    title:    str = ""
    summary:  str = ""
    page_text: str = ""   # 插件直接传入的完整页面文本

@app.post("/ai_analyze")
async def ai_analyze(req: AIAnalyzeReq):
    # ── 关键词库兜底（无论 AI 是否可用都先跑，AI 成功则覆盖）──
    def _kw_fallback():
        text = " ".join(filter(None, [req.title, req.summary, req.page_text[:400]]))
        matched = match_keywords(text) if text.strip() else []
        if not matched:
            matched = rag_search(text) if text.strip() else []
        if not matched:
            return None
        scores = [float(m.get("total_risk_score") or 0) for m in matched]
        max_s = max(scores)
        composite = round(max_s * 0.6 + sum(scores) / len(scores) * 0.4, 2)
        lv, label = score_to_level(composite)
        tags = [{"keyword": m["keyword"], "domain": m.get("risk_domain_l1", "")}
                for m in matched[:3] if m.get("keyword")]
        return {"ok": True, "tags": tags, "risk_level": lv, "risk_word": label, "source": "kw"}

    if not _ai_call_allowed():
        return _kw_fallback() or {"ok": False, "tags": [], "risk_level": "L0", "risk_word": "暂无"}

    content_ctx = (req.page_text[:800] if req.page_text.strip()
                   else req.summary[:300]) or "(内容无法获取)"
    prompt = (
        "你是青少年意识形态风险分析专家。根据以下信息分析风险，必须输出JSON，不能为空。\n\n"
        f"URL: {req.url}\n"
        f"平台: {req.platform or '未知'}\n"
        f"标题: {req.title or '(无标题)'}\n"
        f"页面内容: {content_ctx}\n\n"
        "【必须遵守】\n"
        "1. 无论内容是否可获取，都必须输出1-2个语义标签，标签词要贴合内容\n"
        "2. 若内容为空，根据URL、平台特征推断最可能的风险领域\n"
        "3. 只输出JSON对象，不加markdown代码块\n\n"
        "输出格式：\n"
        '{"tags":[{"keyword":"标签词","domain":"风险域"}],"risk_level":"L2","risk_word":"中度风险"}\n\n'
        "domain只能是：制度认同风险/历史认知风险/心理韧性风险/认知闭合风险/网络素养风险\n"
        "risk_level: L0低风险/L1轻度风险/L2中度风险/L3高风险/L4极高风险（内容为空时默认L0）\n"
        "risk_word: 低风险/轻度风险/中度风险/高风险/极高风险"
    )
    try:
        import json as _json, re as _re
        chunks = []
        with ai_client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role":"user","content":prompt}],
        ) as stream:
            for t in stream.text_stream:
                chunks.append(t)
        text = "".join(chunks).strip()
        m = _re.search(r'\{.*\}', text, _re.DOTALL)
        if m:
            data = _json.loads(m.group())
            return {"ok": True, **data}
        return _kw_fallback() or {"ok": False, "tags": [], "risk_level": "L0", "risk_word": "暂无"}
    except Exception:
        _ai_record_failure()
        return _kw_fallback() or {"ok": False, "tags": [], "risk_level": "L0", "risk_word": "暂无"}

# ── 流式引导建议接口 ──
class NarrativeReq(BaseModel):
    keywords: str
    domain:   str
    summary:  str
    risk_level: str

@app.post("/narrative/stream")
async def narrative_stream(req: NarrativeReq, request: Request):
    ctx = "\n".join(filter(None, [
        f"风险域：{req.domain}" if req.domain else None,
        f"风险等级：{req.risk_level}" if req.risk_level else None,
        f"关键词：{req.keywords}" if req.keywords else None,
        f"内容摘要：{req.summary[:200]}" if req.summary else None,
    ])) or "（内容信息有限）"
    prompt = (
        f"你是青少年价值观引导专家。根据以下信息，"
        f"用40字以内给出简洁具体的引导建议，直接输出建议文字，不加标题。\n"
        f"无论信息是否完整，必须给出建议。\n{ctx}"
    )

    FALLBACK_GUIDE = {
        "制度认同风险": "引导青少年从多角度理解社会现象，区分个案与制度整体，培养系统性思维，避免情绪化、简单化归因。",
        "历史认知风险": "以史实为依据，帮助青少年建立正确历史观，识别娱乐化、解构化对历史认知的干扰，增强对英烈精神的认同感。",
        "心理韧性风险": "关注青少年情绪变化，疏导消极认知，引导建立成长型思维和积极人生目标，增强抗挫折能力。",
        "认知闭合风险": "培养批判性思维，教会青少年辨别阴谋论与事实，保持开放心态，通过多元渠道获取信息，避免认知封闭。",
        "网络素养风险": "加强网络素养教育，引导青少年理性参与网络讨论，识别情绪煽动，理性表达，文明上网。",
    }

    async def generate():
        import asyncio as _asyncio
        domain = req.domain or ""
        fallback = FALLBACK_GUIDE.get(domain, "引导青少年保持理性思考，辨别网络信息真伪，树立正确价值观，遇到疑惑及时与老师、家长沟通。")
        try:
            loop = _asyncio.get_event_loop()
            def _stream_sync():
                chunks = []
                with ai_client.messages.stream(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    for text in stream.text_stream:
                        chunks.append(text)
                return chunks
            # 8 秒超时，超时直接走兜底
            chunks = await _asyncio.wait_for(
                loop.run_in_executor(None, _stream_sync), timeout=8
            )
            for chunk in chunks:
                if await request.is_disconnected():
                    return
                yield f"data: {chunk}\n\n"
            yield "data: [DONE]\n\n"
        except Exception:
            yield f"data: {fallback}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ══════════════════════════════════════════════════════
#  智能问答接口  /chat
# ══════════════════════════════════════════════════════

DOMAIN_KEYWORDS = {
    "心理韧性风险": ["心理", "情绪", "焦虑", "抑郁", "压力", "躺平", "厌学", "自卑", "孤独",
                   "迷茫", "消极", "轻生", "挫折", "青春期", "自尊", "自信", "韧性", "恐惧",
                   "紧张", "担忧", "失眠", "崩溃", "丧", "内耗", "emo"],
    "网络素养风险": ["网络", "游戏", "网红", "直播", "短视频", "抖音", "小红书", "B站", "沉迷",
                   "成瘾", "手机", "网课", "谣言", "虚假信息", "隐私", "上网", "网友", "私信"],
    "历史认知风险": ["历史", "革命", "英雄", "爱国", "抗战", "红色", "历史虚无", "传统文化",
                   "文化自信", "民族精神"],
    "制度认同风险": ["制度", "政治", "民主", "自由", "西方", "体制", "价值观", "意识形态",
                   "国家", "社会主义"],
    "认知闭合风险": ["阴谋论", "假新闻", "极端", "偏激", "谣言", "封闭", "批判性", "辨别"],
}

class ChatRequest(BaseModel):
    question: str
    age_group: str = "13-15"


def _chat_detect_domain(question: str, tokens: list) -> str:
    all_text = question + " " + " ".join(tokens)
    scores: dict = {}
    for domain, kws in DOMAIN_KEYWORDS.items():
        s = sum(1 for kw in kws if kw in all_text)
        if s > 0:
            scores[domain] = s
    return max(scores, key=scores.get) if scores else "心理韧性风险"


def _chat_scripts_by_domain(domain: str, tokens: list) -> dict:
    """从内存话术缓存中按域+年龄分组取最佳一条。"""
    result = {}
    for age_label in ("6-12岁", "13-15岁", "16-18岁"):
        candidates = [s for s in _ALL_SCRIPTS
                      if s.get("age_group") == age_label
                      and s.get("risk_domain_l1") == domain]
        if not candidates and tokens:
            candidates = [s for s in _ALL_SCRIPTS
                          if s.get("age_group") == age_label
                          and any(t in (s.get("script_content") or "") for t in tokens)]
        if not candidates:
            candidates = [s for s in _ALL_SCRIPTS if s.get("age_group") == age_label]
        result[age_label] = candidates[0] if candidates else None
    return result


def _chat_tags_by_tokens(tokens: list, domain: str) -> list:
    """从内存关键词缓存匹配相关标签。"""
    matched, seen = [], set()
    for kw in _KEYWORDS:
        word = kw.get("keyword") or ""
        if not word or word in seen:
            continue
        if (any(t in word or word in t for t in tokens)
                or kw.get("risk_domain_l1") == domain):
            seen.add(word)
            matched.append(kw)
        if len(matched) >= 10:
            break
    return matched


def _build_fallback_chat(domain: str, question: str, scripts: dict, cases: list) -> dict:
    ANSWERS = {
        "心理韧性风险": (
            "青少年心理韧性培育需要关注情绪调节、挫折应对和积极心态三个层面。"
            "建议从家庭支持、学校引导和自我成长三个维度共同发力，"
            "帮助青少年建立稳定的心理基础，增强面对困难时的抗压能力。"
            "同时，要识别并及时干预高风险情绪信号，避免问题恶化。"
        ),
        "网络素养风险": (
            "网络素养教育应引导青少年理性参与网络活动，识别虚假信息，"
            "保护个人隐私，培养批判性思维，避免网络成瘾，"
            "在数字空间中健康成长。关键是帮助青少年建立自律能力，"
            "合理分配线上线下时间。"
        ),
        "历史认知风险": (
            "帮助青少年建立正确历史观，以史实为基础，理解历史发展规律，"
            "识别历史虚无主义的危害，增强民族文化自信和爱国情感。"
            "可借助生动的历史案例和正面人物榜样，激发青少年的历史责任感。"
        ),
        "制度认同风险": (
            "引导青少年全面客观地认识国家制度，从多角度理解社会现象，"
            "培养系统性思维，避免情绪化、片面化的认知，增强制度自信。"
            "建议结合身边的发展成就，帮助青少年形成理性认知框架。"
        ),
        "认知闭合风险": (
            "培养批判性思维，教会青少年辨别信息真伪，保持开放心态，"
            "通过多元渠道获取信息，避免认知封闭和极端化思维。"
            "引导青少年学会质疑与验证，而不是简单接受或拒绝所有信息。"
        ),
    }
    answer = ANSWERS.get(domain, "建议结合青少年实际情况，从心理、家庭、学校三个维度进行综合引导。")
    key_points = ["关注情绪变化，及时疏导", "建立支持性环境", "鼓励积极参与社会活动"]
    return {
        "question": question,
        "answer": answer,
        "key_points": key_points,
        "domain": domain,
        "guidance_tip": "建议与专业心理咨询师合作，制定个性化引导方案",
        "scripts": _format_scripts_output(scripts),
        "cases": cases[:3],
        "tags": [],
        "domain_chart": [],
    }


def _format_scripts_output(scripts: dict) -> dict:
    out = {}
    for age_label, s in scripts.items():
        if s:
            out[age_label] = {
                "content": s.get("script_content") or "",
                "follow_up": s.get("follow_up_action") or "",
                "phase": s.get("script_phase") or "",
                "script_type": s.get("script_type") or "",
            }
        else:
            out[age_label] = None
    return out


# ── Chat Function Calling 工具定义 ──
CHAT_TOOLS = [
    {
        "name": "query_cases_ranked",
        "description": (
            "从语料库（59,412条真实内容）按互动量或风险评分排序并返回。"
            "当用户询问'播放量最多'、'互动量最高'、'点赞最多'、'风险最高'等数据统计类问题时必须调用此工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_by": {
                    "type": "string",
                    "enum": ["interaction_count", "total_risk_score", "like_count"],
                    "description": "interaction_count=总互动量，total_risk_score=风险评分，like_count=点赞量"
                },
                "order": {
                    "type": "string",
                    "enum": ["DESC", "ASC"],
                    "description": "DESC=降序（默认），ASC=升序"
                },
                "platform": {
                    "type": "string",
                    "description": "平台过滤，可选：微博、抖音、小红书、B站、百度"
                },
                "risk_level": {
                    "type": "string",
                    "description": "风险等级过滤，可选：高风险、极高风险、中度风险"
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认5，最多10"
                }
            },
            "required": ["order_by"]
        }
    },
    {
        "name": "search_cases_by_topic",
        "description": (
            "根据关键词语义搜索相关典型案例，返回案例名称、风险等级、摘要等。"
            "当用户询问特定主题相关案例时调用，例如'焦虑相关案例'、'网络沉迷典型案例'。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如'青少年焦虑'、'网络游戏沉迷'"
                },
                "domain": {
                    "type": "string",
                    "description": "风险域过滤（可选）：心理韧性风险、网络素养风险、历史认知风险、制度认同风险、认知闭合风险"
                },
                "limit": {"type": "integer", "description": "返回条数，默认3"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_guidance_scripts",
        "description": (
            "检索针对特定心理问题的专业引导话术。"
            "当用户询问如何帮助、引导、干预青少年（游戏沉迷、厌学、焦虑、消极情绪等）时必须调用。"
            "话术内容作为专业参考，用于生成回答，不会直接展示。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "主题关键词，如'游戏沉迷'、'厌学'、'焦虑'、'历史观'"
                },
                "domain": {
                    "type": "string",
                    "description": "风险域（可选）：心理韧性风险、网络素养风险、历史认知风险、制度认同风险、认知闭合风险"
                }
            },
            "required": ["topic"]
        }
    },
    {
        "name": "get_risk_domain_info",
        "description": (
            "获取特定风险域的知识标签、关键词和语料统计信息。"
            "当用户询问某类风险的特征、表现或背景知识时调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "风险域名称：心理韧性风险、网络素养风险、历史认知风险、制度认同风险、认知闭合风险"
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "相关关键词列表（可选）"
                }
            },
            "required": ["domain"]
        }
    }
]


def _exec_chat_tool(name: str, tool_input: dict) -> str:
    """执行 Chat 工具调用，返回 JSON 字符串。"""
    import json as _j

    if name == "query_cases_ranked":
        # 字段名兼容旧枚举值
        _field_map = {"total_interaction": "interaction_count",
                      "avg_risk_score":    "total_risk_score",
                      "case_priority_score": "interaction_count"}
        allowed = {"interaction_count", "total_risk_score", "like_count"}
        raw_field = tool_input.get("order_by", "interaction_count")
        order_by = _field_map.get(raw_field, raw_field)
        if order_by not in allowed:
            order_by = "interaction_count"
        order    = "ASC" if str(tool_input.get("order", "DESC")).upper() == "ASC" else "DESC"
        platform = tool_input.get("platform")
        risk_lv  = tool_input.get("risk_level")
        limit    = min(int(tool_input.get("limit") or 5), 10)

        conds = [
            "(title IS NOT NULL AND title != '' OR content_summary IS NOT NULL AND content_summary != '')",
            f"{order_by} IS NOT NULL AND {order_by} > 0",
        ]
        params: list = []
        if platform: conds.append("platform = %s");   params.append(platform)
        if risk_lv:  conds.append("risk_level = %s"); params.append(risk_lv)
        params.append(limit)

        rows = qall(f"""
            SELECT corpus_id                                    AS case_id,
                   COALESCE(NULLIF(title,''), LEFT(content_summary,100)) AS case_name,
                   platform,
                   risk_domain_l1,
                   risk_type_l2,
                   risk_level,
                   total_risk_score AS avg_risk_score,
                   interaction_count AS total_interaction,
                   LEFT(content_summary, 120) AS representative_text,
                   author,
                   url              AS source_url
            FROM t_corpus
            WHERE {' AND '.join(conds)}
            ORDER BY {order_by} {order}
            LIMIT %s
        """, tuple(params))
        return _j.dumps({"order_by": order_by, "order": order,
                          "total": len(rows), "cases": rows or []},
                         ensure_ascii=False, default=str)

    if name == "search_cases_by_topic":
        query  = tool_input.get("query", "")
        domain = tool_input.get("domain", "")
        limit  = int(tool_input.get("limit") or 3)
        result = search_cases_by_query(query, domain=domain, top_k=limit)
        return _j.dumps(result, ensure_ascii=False, default=str)

    if name == "get_guidance_scripts":
        topic  = tool_input.get("topic", "")
        domain = tool_input.get("domain", "")
        tokens = list(dict.fromkeys(t for t in jieba.cut(topic) if len(t) >= 2))[:10]
        if not domain:
            domain = _chat_detect_domain(topic, tokens)
        raw_scripts = _chat_scripts_by_domain(domain, tokens)
        formatted = {age: ({"content": s.get("script_content", ""),
                             "follow_up": s.get("follow_up_action", ""),
                             "phase": s.get("script_phase", "")} if s else None)
                     for age, s in raw_scripts.items()}
        return _j.dumps({"domain": domain, "topic": topic, "scripts": formatted},
                         ensure_ascii=False)

    if name == "get_risk_domain_info":
        domain   = tool_input.get("domain", "")
        keywords = tool_input.get("keywords") or []
        tags     = _chat_tags_by_tokens(keywords, domain)
        stats    = get_corpus_stats(domain, keywords[0] if keywords else "")
        return _j.dumps({"domain": domain,
                          "tags": [{"keyword": t.get("keyword",""),
                                    "domain": t.get("risk_domain_l1",""),
                                    "l2": t.get("l2_name",""),
                                    "score": float(t.get("total_risk_score") or 0)}
                                   for t in tags[:8]],
                          "corpus_stats": stats},
                         ensure_ascii=False, default=str)

    return _j.dumps({"error": f"unknown tool: {name}"})


@app.post("/chat")
async def chat_qa(req: ChatRequest):
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "请输入问题")

    def _fallback():
        tokens = list(dict.fromkeys(t for t in jieba.cut(q) if len(t) >= 2))[:15]
        # 数据查询降级
        if _is_data_query(q):
            qr = _execute_data_query(q)
            rows = qr.get("rows", [])
            return {
                "type": "data_query",
                "question": q,
                "summary": f"已按{qr['order_label']}排序，共 {len(rows)} 条。",
                "order_label": qr["order_label"],
                "order_field": qr["order_field"],
                "platform_filter": qr.get("platform_filter"),
                "top_cases": rows,
            }
        domain  = _chat_detect_domain(q, tokens)
        scripts = _chat_scripts_by_domain(domain, tokens)
        cases   = search_cases_by_query(q, domain=domain, top_k=3).get("cases", [])
        return _build_fallback_chat(domain, q, scripts, cases)

    if not _ai_call_allowed():
        return _fallback()

    try:
        import json as _json
        loop = asyncio.get_event_loop()

        def _tool_loop():
            collected = {"cases": [], "scripts": {}, "tags": [], "domain": ""}
            calls_log: list = []

            sys_prompt = (
                "你是专注于青少年心理健康的引导专家，熟悉青少年意识形态风险识别工作。"
                "请根据用户问题调用合适的工具从数据库获取真实信息，再给出专业、温和的回答。\n"
                "规则：\n"
                "- 数据统计/排行类问题（播放量、互动量、排名等）→ 必须调用 query_cases_ranked\n"
                "- 引导/干预类问题（如何帮助、如何引导、如何干预）→ 必须调用 get_guidance_scripts，可同时调用 search_cases_by_topic\n"
                "- 案例/事件类问题 → 调用 search_cases_by_topic\n"
                "- 风险知识类问题 → 调用 get_risk_domain_info"
            )
            msgs = [{"role": "user", "content": q}]

            def _call(m):
                return ai_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1000,
                    system=sys_prompt,
                    tools=CHAT_TOOLS,
                    messages=m,
                )

            resp = _call(msgs)

            for _round in range(4):
                if resp.stop_reason != "tool_use":
                    break
                tool_results = []
                for blk in resp.content:
                    if blk.type != "tool_use":
                        continue
                    result_str = _exec_chat_tool(blk.name, blk.input)
                    calls_log.append({"name": blk.name, "input": blk.input})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": blk.id,
                        "content": result_str,
                    })
                    # 聚合结果
                    try:
                        rd = _json.loads(result_str)
                        if blk.name in ("query_cases_ranked", "search_cases_by_topic"):
                            collected["cases"].extend(rd.get("cases", []))
                        elif blk.name == "get_guidance_scripts":
                            for age, s in rd.get("scripts", {}).items():
                                if s and age not in collected["scripts"]:
                                    collected["scripts"][age] = s
                            if rd.get("domain") and not collected["domain"]:
                                collected["domain"] = rd["domain"]
                        elif blk.name == "get_risk_domain_info":
                            collected["tags"].extend(rd.get("tags", []))
                            if rd.get("domain") and not collected["domain"]:
                                collected["domain"] = rd["domain"]
                    except Exception:
                        pass

                msgs = msgs + [
                    {"role": "assistant", "content": resp.content},
                    {"role": "user",      "content": tool_results},
                ]
                resp = _call(msgs)

            answer = "".join(
                blk.text for blk in resp.content
                if hasattr(blk, "type") and blk.type == "text"
            ).strip()
            return answer, calls_log, collected

        answer, calls_log, collected = await asyncio.wait_for(
            loop.run_in_executor(None, _tool_loop), timeout=28
        )
        _ai_record_success()

    except Exception as e:
        _ai_record_failure()
        print(f"[Chat tool-use] 失败: {e}")
        return _fallback()

    # ── 根据工具调用记录判断响应类型 ──
    is_data_q = any(c["name"] == "query_cases_ranked" for c in calls_log)

    if is_data_q:
        order_info = next(
            (c["input"] for c in calls_log if c["name"] == "query_cases_ranked"), {}
        )
        _ORDER_LABELS = {
            "total_interaction": "互动量/播放量",
            "avg_risk_score":    "风险评分",
            "case_priority_score": "优先级",
        }
        order_label = _ORDER_LABELS.get(order_info.get("order_by", ""), "互动量")
        return {
            "type": "data_query",
            "question": q,
            "summary": answer or f"已按{order_label}排序，共 {len(collected['cases'])} 条。",
            "order_label": order_label,
            "order_field": order_info.get("order_by", "total_interaction"),
            "platform_filter": order_info.get("platform"),
            "top_cases": collected["cases"][:10],
        }

    # 引导问答响应
    domain = collected["domain"] or _chat_detect_domain(
        q, list(dict.fromkeys(t for t in jieba.cut(q) if len(t) >= 2))[:15]
    )
    domain_scores: dict = {}
    for tag in collected["tags"]:
        d = tag.get("domain") or ""
        if d:
            domain_scores[d] = round(domain_scores.get(d, 0) + float(tag.get("score") or 1), 2)

    return {
        "question": q,
        "answer": answer,
        "key_points": [],
        "domain": domain,
        "guidance_tip": "",
        "cases": collected["cases"][:3],
        "tags": collected["tags"][:8],
        "domain_chart": [{"domain": k, "score": v}
                          for k, v in sorted(domain_scores.items(),
                                             key=lambda x: -x[1])[:5]],
    }


_DATA_QUERY_TRIGGERS = [
    "播放量", "互动量", "热度", "观看量", "传播量",
    "排名", "排行", "最多", "最高", "最大", "最少", "最低",
    "哪个案例", "哪些案例", "多少案例", "几条", "多少条",
    "统计", "前三", "前五", "前十", "数量", "一共", "总共",
    "什么案例", "哪个", "哪些", "查一下", "查询", "列举",
]

def _is_data_query(question: str) -> bool:
    return any(kw in question.lower() for kw in _DATA_QUERY_TRIGGERS)


def _execute_data_query(question: str) -> dict:
    """将自然语言数据查询翻译为 SQL 并执行，返回结构化结果。"""
    import re as _re
    q = question.lower()

    # 排序字段
    if any(k in q for k in ["播放量", "互动", "热度", "观看", "传播", "阅读"]):
        order_field, order_label = "interaction_count", "互动量"
    elif any(k in q for k in ["点赞"]):
        order_field, order_label = "like_count", "点赞量"
    elif any(k in q for k in ["风险分", "风险评分", "危险", "评分", "分数", "风险"]):
        order_field, order_label = "total_risk_score", "风险评分"
    else:
        order_field, order_label = "interaction_count", "互动量"

    # 升降序
    order_dir = "ASC" if any(k in q for k in ["最少", "最低", "最小", "最差"]) else "DESC"

    # 平台过滤
    platform = None
    for k, v in {"微博": "微博", "抖音": "抖音", "小红书": "小红书",
                  "b站": "B站", "bilibili": "B站", "百度": "百度"}.items():
        if k in q:
            platform = v; break

    # 风险等级过滤
    risk_vals = None
    if "高风险" in q or "极高" in q:
        risk_vals = ["高风险", "极高风险", "L3", "L4"]
    elif "中度" in q or "中风险" in q:
        risk_vals = ["中度风险", "L2"]

    # 返回条数
    limit = 5
    m = _re.search(r'前\s*(\d+)', question)
    if m:
        limit = min(int(m.group(1)), 10)

    # 查 t_corpus（有真实互动数据）
    conds = [
        "(title IS NOT NULL AND title != '' OR content_summary IS NOT NULL AND content_summary != '')",
        f"{order_field} IS NOT NULL AND {order_field} > 0",
    ]
    params: list = []
    if platform:
        conds.append("platform = %s"); params.append(platform)
    if risk_vals:
        conds.append(f"risk_level IN ({','.join(['%s']*len(risk_vals))})")
        params.extend(risk_vals)

    params.append(limit)
    rows = qall(f"""
        SELECT corpus_id                                    AS case_id,
               COALESCE(NULLIF(title,''), LEFT(content_summary,100)) AS case_name,
               platform,
               risk_domain_l1,
               risk_type_l2,
               risk_level,
               total_risk_score AS avg_risk_score,
               interaction_count AS total_interaction,
               LEFT(content_summary, 120) AS representative_text,
               author,
               url              AS source_url
        FROM t_corpus
        WHERE {' AND '.join(conds)}
        ORDER BY {order_field} {order_dir}
        LIMIT %s
    """, tuple(params))

    return {
        "order_field": order_field,
        "order_label": order_label,
        "order_dir": order_dir,
        "platform_filter": platform,
        "rows": rows or [],
    }


# ══════════════════════════════════════════════════════
#  流式思维链问答  /chat/stream
# ══════════════════════════════════════════════════════

_TOOL_CN = {
    "query_cases_ranked":    "案例排行查询",
    "search_cases_by_topic": "主题案例搜索",
    "get_guidance_scripts":  "引导话术检索",
    "get_risk_domain_info":  "风险知识库查询",
}


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, request: Request):
    import json as _js

    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "请输入问题")

    ev_q: asyncio.Queue = asyncio.Queue()
    cur_loop = asyncio.get_event_loop()

    def emit(**kw):
        cur_loop.call_soon_threadsafe(ev_q.put_nowait, kw)

    def worker():
        try:
            collected = {"cases": [], "scripts": {}, "tags": [], "domain": ""}
            emit(type="thinking", text=f"解析问题：「{q[:35]}{'…' if len(q)>35 else ''}」")

            if not _ai_call_allowed():
                emit(type="thinking", text="AI 服务暂不可用，启用规则引擎兜底…")
                tokens = list(dict.fromkeys(t for t in jieba.cut(q) if len(t) >= 2))[:15]
                if _is_data_query(q):
                    qr = _execute_data_query(q)
                    rows = qr.get("rows", [])
                    emit(type="thinking_done")
                    emit(type="answer_chunk",
                         text=f"已按{qr['order_label']}排序，共找到 {len(rows)} 条案例。")
                    emit(type="result", data={"type": "data_query", "question": q,
                         "summary": f"找到 {len(rows)} 条", "order_label": qr["order_label"],
                         "order_field": qr["order_field"],
                         "platform_filter": qr.get("platform_filter"), "top_cases": rows})
                else:
                    domain = _chat_detect_domain(q, tokens)
                    scripts = _chat_scripts_by_domain(domain, tokens)
                    cases = search_cases_by_query(q, domain=domain, top_k=3).get("cases", [])
                    fb = _build_fallback_chat(domain, q, scripts, cases)
                    emit(type="thinking_done")
                    emit(type="answer_chunk", text=fb.get("answer", ""))
                    emit(type="result", data=fb)
                emit(type="done"); return

            emit(type="thinking", text="匹配数据库工具，确定查询策略…")

            sys_p = (
                "你是专注于青少年心理健康的引导专家，熟悉青少年意识形态风险识别工作。"
                "请根据用户问题选择合适的工具查询数据库，给出专业、温和的回答。\n"
                "规则：数据统计/排行 → query_cases_ranked；"
                "引导/干预类问题 → get_guidance_scripts（可同时调 search_cases_by_topic）；"
                "案例/事件 → search_cases_by_topic；"
                "风险知识 → get_risk_domain_info"
            )
            msgs = [{"role": "user", "content": q}]
            tool_log: list = []

            for _r in range(4):
                if _r == 0:
                    emit(type="thinking", text="Claude 分析问题，选择工具…")
                resp = ai_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=600,
                    system=sys_p,
                    tools=CHAT_TOOLS,
                    messages=msgs,
                )
                if resp.stop_reason != "tool_use":
                    break

                tool_results = []
                for blk in resp.content:
                    if blk.type != "tool_use":
                        continue
                    cn = _TOOL_CN.get(blk.name, blk.name)
                    preview = _js.dumps(blk.input, ensure_ascii=False)
                    emit(type="thinking_tool", text=f"调用「{cn}」", tool=blk.name,
                         preview=preview[:80])

                    rs = _exec_chat_tool(blk.name, blk.input)
                    tool_log.append({"name": blk.name, "input": blk.input})
                    try:
                        rd = _js.loads(rs)
                        if blk.name in ("query_cases_ranked", "search_cases_by_topic"):
                            n = len(rd.get("cases", []))
                            emit(type="thinking", text=f"✓ 获得 {n} 条案例数据")
                            collected["cases"].extend(rd.get("cases", []))
                        elif blk.name == "get_guidance_scripts":
                            dom = rd.get("domain", "")
                            emit(type="thinking", text=f"✓ 检索到「{dom}」引导话术")
                            for age, s in rd.get("scripts", {}).items():
                                if s and age not in collected["scripts"]:
                                    collected["scripts"][age] = s
                            if dom and not collected["domain"]:
                                collected["domain"] = dom
                        elif blk.name == "get_risk_domain_info":
                            n = len(rd.get("tags", []))
                            emit(type="thinking", text=f"✓ 匹配到 {n} 个风险标签")
                            collected["tags"].extend(rd.get("tags", []))
                            if rd.get("domain") and not collected["domain"]:
                                collected["domain"] = rd["domain"]
                    except Exception:
                        pass
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": blk.id, "content": rs
                    })

                msgs = msgs + [
                    {"role": "assistant", "content": resp.content},
                    {"role": "user",      "content": tool_results},
                ]

            emit(type="thinking", text="综合数据库信息，生成专业回答…")
            emit(type="thinking_done")

            is_dq = any(c["name"] == "query_cases_ranked" for c in tool_log)
            _OL = {"interaction_count": "互动量",
                   "total_risk_score":  "风险评分",
                   "like_count":        "点赞量",
                   # 兼容旧字段名
                   "total_interaction": "互动量",
                   "avg_risk_score":    "风险评分"}

            if is_dq:
                oi = next((c["input"] for c in tool_log
                           if c["name"] == "query_cases_ranked"), {})
                ol = _OL.get(oi.get("order_by", ""), "互动量")
                rows_txt = "\n".join(
                    f"{i+1}.《{r.get('case_name','')}》互动量:{r.get('total_interaction',0)}"
                    for i, r in enumerate(collected["cases"][:3])
                ) or "未找到案例"
                sp = (f"用户问：{q}\n结果（按{ol}排序）：\n{rows_txt}\n"
                      "用1-2句简洁说明结果，直接输出。")
                try:
                    with ai_client.messages.stream(
                        model="claude-haiku-4-5-20251001", max_tokens=100,
                        messages=[{"role": "user", "content": sp}]
                    ) as s:
                        for t in s.text_stream:
                            emit(type="answer_chunk", text=t)
                    _ai_record_success()
                except Exception:
                    _ai_record_failure()
                    emit(type="answer_chunk",
                         text=f"已按{ol}排序，共 {len(collected['cases'])} 条案例。")
                emit(type="result", data={
                    "type": "data_query", "question": q, "order_label": ol,
                    "order_field": oi.get("order_by", "total_interaction"),
                    "platform_filter": oi.get("platform"),
                    "top_cases": collected["cases"][:10],
                })
            else:
                domain = collected["domain"] or _chat_detect_domain(
                    q, list(dict.fromkeys(t for t in jieba.cut(q) if len(t) >= 2))[:15])
                sc_ctx = "\n".join(
                    f"【{a}引导话术】{s['content'][:150]}"
                    for a, s in collected["scripts"].items() if s and s.get("content"))
                ca_ctx = "\n".join(
                    f"案例《{c.get('case_name','')}》：{(c.get('representative_text') or '')[:80]}"
                    for c in collected["cases"][:2])
                gp = (f"你是青少年心理引导专家。\n用户问：{q}\n\n"
                      + (f"专业引导话术参考（请基于此给出建议，勿直接引用原文）：\n{sc_ctx}\n\n" if sc_ctx else "")
                      + f"相关案例：\n{ca_ctx or '（无相关案例）'}\n"
                      "请用200-250字给出专业回答，直接输出正文。")
                full: list = []
                try:
                    with ai_client.messages.stream(
                        model="claude-haiku-4-5-20251001", max_tokens=450,
                        messages=[{"role": "user", "content": gp}]
                    ) as s:
                        for t in s.text_stream:
                            full.append(t); emit(type="answer_chunk", text=t)
                    _ai_record_success()
                except Exception:
                    _ai_record_failure()
                    fb = _build_fallback_chat(domain, q, {}, collected["cases"])
                    emit(type="answer_chunk", text=fb.get("answer", ""))
                    full = [fb.get("answer", "")]

                ds: dict = {}
                for tag in collected["tags"]:
                    d = tag.get("domain") or ""
                    if d:
                        ds[d] = round(ds.get(d, 0) + float(tag.get("score") or 1), 2)
                emit(type="result", data={
                    "question": q, "answer": "".join(full), "key_points": [],
                    "domain": domain, "guidance_tip": "",
                    "cases": collected["cases"][:3],
                    "tags": collected["tags"][:8],
                    "domain_chart": [{"domain": k, "score": v}
                                     for k, v in sorted(ds.items(), key=lambda x: -x[1])[:5]],
                })

        except Exception as e:
            _ai_record_failure()
            print(f"[chat/stream worker] {e}")
            emit(type="error", text=str(e))
        finally:
            emit(type="done")

    async def gen():
        fut = cur_loop.run_in_executor(None, worker)
        try:
            while True:
                if await request.is_disconnected():
                    fut.cancel(); return
                try:
                    ev = await asyncio.wait_for(ev_q.get(), timeout=0.5)
                    yield f"data: {_js.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") == "done":
                        break
                except asyncio.TimeoutError:
                    if fut.done():
                        break
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
        finally:
            if not fut.done():
                fut.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )

