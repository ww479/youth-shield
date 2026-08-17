# -*- coding: utf-8 -*-
"""
四库 Demo · 语料库导入脚本
把《青少年意识形态风险识别与价值观引导四库Demo.xlsx》的
「02_语料库」(约 5.9 万条) 导入 MySQL 表 t_corpus，供后端 /dashboard/* 实时接口聚合。

用法：
    cd 青少年风险预测前后端
    .venv/bin/python db/import_corpus_demo.py \
        "/绝对路径/青少年意识形态风险识别与价值观引导四库Demo.xlsx"

不传路径时使用下面的 DEFAULT_XLSX。
脏日期(今天/昨天/N天前/N月N日/2025年12/2026-05 等)统一清洗为 datetime，
相对时间锚定到数据采集参考日 REF_DATE；实在无法解析的置 NULL(不参与月度趋势)。
脚本可重复执行：每次先 TRUNCATE t_corpus。
"""
import os
import re
import sys
from datetime import datetime, timedelta

import openpyxl
import mysql.connector

DEFAULT_XLSX = os.environ.get(
    "CORPUS_XLSX",
    "/Users/wangshuai/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
    "xwechat_files/wxid_1vn16raw7d8822_4053/msg/file/2026-07/"
    "青少年意识形态风险识别与价值观引导四库Demo.xlsx",
)
SHEET = "02_语料库"
REF_DATE = datetime(2026, 5, 8)          # 相对时间(今天/昨天/N天前)锚定的数据采集参考日
DEFAULT_YEAR = 2026                       # 只有「M月D日」而缺年份时补的年份

DB = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "root"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "youth_ideology"),
    charset="utf8mb4",
    use_pure=True,
)

COLUMNS = [
    "corpus_id", "platform", "search_keyword", "matched_keyword", "tag_id",
    "risk_domain_l1", "risk_type_l2", "risk_level",
    "total_risk_score", "content_risk_score", "opposition_risk_score",
    "l1_tag_code", "l2_tag_code", "l3_tag_code",
    "guidance_direction", "tag_chain",
    "title", "content_summary", "full_text",
    "author", "publish_time", "url",
    "like_count", "comment_count", "share_count", "collect_count", "interaction_count",
    "is_case_candidate", "desensitize_status", "review_status", "source_file",
]
INSERT_SQL = (
    "INSERT INTO t_corpus (" + ",".join(COLUMNS) + ",created_at) VALUES ("
    + ",".join(["%s"] * len(COLUMNS)) + ",NOW())"
)


def _s(v, default=""):
    if v is None:
        return default
    s = str(v).strip()
    return default if s in ("", "nan", "None") else s


def _f(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _i(v):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return 0


_REL_DAYS = re.compile(r"^(\d+)\s*天前")
_REL_HRS = re.compile(r"^(\d+)\s*小时前")
_REL_MIN = re.compile(r"^(\d+)\s*(?:分钟|秒)前")
_YMD = re.compile(r"(\d{4})[-/年.](\d{1,2})(?:[-/月.](\d{1,2}))?")   # 2026-05-08 / 2025年12 / 2019/02
_MD = re.compile(r"^(\d{1,2})[-/月](\d{1,2})日?")                     # 5月8日 / 05-08(无年)
_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def parse_dt(v):
    """把各种脏日期串清洗为 datetime；无法解析返回 None。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s or s in ("nan", "None"):
        return None

    # 相对时间：锚定到 REF_DATE
    if s.startswith("今天"):
        m = _TIME.search(s)
        return REF_DATE.replace(hour=int(m.group(1)), minute=int(m.group(2))) if m else REF_DATE
    if s.startswith("昨天"):
        d = REF_DATE - timedelta(days=1)
        m = _TIME.search(s)
        return d.replace(hour=int(m.group(1)), minute=int(m.group(2))) if m else d
    m = _REL_DAYS.match(s)
    if m:
        return REF_DATE - timedelta(days=int(m.group(1)))
    if _REL_HRS.match(s) or _REL_MIN.match(s):
        return REF_DATE

    # 绝对：YYYY-MM(-DD)
    m = _YMD.search(s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        d = int(m.group(3)) if m.group(3) else 1
        try:
            dt = datetime(y, min(max(mo, 1), 12), min(max(d, 1), 28))
        except Exception:
            return None
        t = _TIME.search(s[m.end():])
        if t:
            try:
                dt = dt.replace(hour=int(t.group(1)), minute=int(t.group(2)))
            except Exception:
                pass
        return dt

    # 只有 M月D日 / MM-DD，缺年份 → 补 DEFAULT_YEAR
    m = _MD.match(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            return datetime(DEFAULT_YEAR, min(max(mo, 1), 12), min(max(d, 1), 28))
        except Exception:
            return None
    return None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    if not os.path.exists(path):
        print("找不到 Excel 文件：", path)
        raise SystemExit(1)

    print("读取 Excel(只读流式)：", path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[SHEET]
    it = ws.iter_rows(values_only=True)
    header = next(it)
    idx = {h: i for i, h in enumerate(header)}

    def col(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    conn = mysql.connector.connect(**DB)
    cur = conn.cursor()
    cur.execute("SET NAMES utf8mb4")
    # 安全保护：默认只对空表插入，绝不擅自清空已有数据。
    # 如需重导，显式设置环境变量 FORCE_CLEAR=1（会先删除本脚本导入的 Demo 语料）。
    cur.execute("SELECT COUNT(*) FROM t_corpus")
    existing = cur.fetchone()[0]
    if existing > 0:
        if os.environ.get("FORCE_CLEAR") == "1":
            print(f"FORCE_CLEAR=1：删除 t_corpus 现有 {existing} 条 Demo 语料后重导 ...")
            cur.execute("DELETE FROM t_corpus")
        else:
            print(f"t_corpus 已有 {existing} 条数据，为避免误删已中止。"
                  f"如确认要重导，请以 FORCE_CLEAR=1 重新运行。")
            cur.close(); conn.close()
            raise SystemExit(2)

    batch, total, no_date, no_score = [], 0, 0, 0
    BATCH = 2000
    for row in it:
        cid = _s(col(row, "语料ID"))
        if not cid:
            continue
        score = _f(col(row, "综合风险分"))
        if score is None:
            no_score += 1
        dt = parse_dt(col(row, "发布时间"))
        if dt is None:
            no_date += 1
        rec = (
            cid[:30],
            _s(col(row, "平台"))[:30],
            _s(col(row, "搜索关键词"))[:200],
            _s(col(row, "匹配关键词"))[:200],
            _s(col(row, "标签ID"))[:20],
            _s(col(row, "一级风险域"))[:50],
            _s(col(row, "二级风险类型"))[:100],
            _s(col(row, "风险等级"))[:10],
            score,
            _f(col(row, "内容风险分")),
            _f(col(row, "对立风险分")),
            _s(col(row, "L1"))[:20],
            _s(col(row, "L2"))[:20],
            _s(col(row, "L3"))[:20],
            _s(col(row, "建议引导方向"))[:500],
            _s(col(row, "标签链"))[:200],
            _s(col(row, "标题"))[:500],
            _s(col(row, "正文摘要")),
            _s(col(row, "文本内容")),
            _s(col(row, "作者"))[:100] or None,
            dt,
            _s(col(row, "URL"))[:1000],
            _i(col(row, "点赞数")),
            _i(col(row, "评论数")),
            _i(col(row, "转发数")),
            _i(col(row, "收藏数")),
            _i(col(row, "互动量")),
            1 if _s(col(row, "是否进入案例候选")) == "是" else 0,
            0,
            0,
            _s(col(row, "原始来源文件"))[:200],
        )
        batch.append(rec)
        if len(batch) >= BATCH:
            cur.executemany(INSERT_SQL, batch)
            total += len(batch)
            batch = []
            print(f"  已写入 {total} 条 ...", end="\r")
    if batch:
        cur.executemany(INSERT_SQL, batch)
        total += len(batch)
    conn.commit()
    wb.close()

    print(f"\n完成：写入 t_corpus {total} 条（无日期 {no_date} 条置NULL / 无风险分 {no_score} 条）")
    # 验证
    cur.execute("SELECT risk_domain_l1, COUNT(*) FROM t_corpus GROUP BY risk_domain_l1 ORDER BY COUNT(*) DESC")
    print("风险域分布：", dict(cur.fetchall()))
    cur.execute("SELECT platform, COUNT(*) FROM t_corpus GROUP BY platform ORDER BY COUNT(*) DESC")
    print("平台分布：", dict(cur.fetchall()))
    cur.execute("SELECT risk_level, COUNT(*) FROM t_corpus GROUP BY risk_level ORDER BY COUNT(*) DESC")
    print("等级分布：", dict(cur.fetchall()))
    cur.execute("SELECT COUNT(*) FROM t_corpus WHERE publish_time IS NOT NULL")
    print("有发布时间的记录：", cur.fetchone()[0])
    cur.execute("SELECT DATE_FORMAT(MIN(publish_time),'%Y-%m'), DATE_FORMAT(MAX(publish_time),'%Y-%m') FROM t_corpus")
    print("时间范围(月)：", cur.fetchone())
    cur.close()
    conn.close()
    print("\n完成！后端 /dashboard/* 接口现在即可服务这份语料。")


if __name__ == "__main__":
    main()
