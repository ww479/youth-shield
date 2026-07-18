# -*- coding: utf-8 -*-
"""
一次性种子脚本：写入"中青网正向案例"7 条数据到 t_case。

数据来源：材料/数据集及案例/历史认知风险域案例.xlsx（与 ZQW-001..007 是同一批事件，
但这里换成中青网权威定性/纠偏角度的正向表述，用于插件"中青网正向案例"区块）。

用 POS- 前缀与风险案例（ZQW% 等）区分，不影响 build_case_index()/get_related_cases()
现有的 `case_id LIKE 'ZQW%'` 检索路径。可重复执行（ON DUPLICATE KEY UPDATE）。

运行：python seed_positive_cases.py
"""
import os
import mysql.connector

DB = dict(host=os.environ.get("DB_HOST", "127.0.0.1"), port=int(os.environ.get("DB_PORT", "3306")),
          user=os.environ.get("DB_USER", "root"), password=os.environ.get("DB_PASSWORD", ""),
          database=os.environ.get("DB_NAME", "youth_ideology"), charset="utf8mb4", use_pure=True)

# (case_id, case_name, risk_type_l2, relation_reason, source_url)
POSITIVE_CASES = [
    (
        "POS-H01",
        "公安机关网安部门依法查处6起侮辱英烈案",
        "英烈亵渎 / 娱乐化",
        "中青网跟踪报道公安机关依法查处侮辱英烈内容，体现平台与执法机关协同治理网络乱象的正向路径。",
        "https://t.m.youth.cn/transfer/index/url/news.youth.cn/gn/202507/t20250707_16102673.htm",
    ),
    (
        "POS-H02",
        "罗昌平侵害英雄烈士名誉、荣誉暨刑事附带民事公益诉讼一案一审宣判",
        "英烈亵渎 / 娱乐化",
        "中青网关注该案司法宣判结果，展示法律对亵渎英烈行为的刚性约束，是历史认知类议题的权威处置参考。",
        "https://m.youth.cn/qwtx/xxl/202205/t20220505_13668597.htm",
    ),
    (
        "POS-H03",
        "琉球跨越百年的苦难与抗争：民族的伤口与未竟的正义",
        "近现代史虚无化、旧时代美化和反向叙事",
        "中青网系统梳理琉球百年殖民历史，以权威史料对冲境外反向叙事，是历史认知风险议题的正向叙事范本。",
        "https://news.youth.cn/gj/202606/t20260623_16726996.htm",
    ),
    (
        "POS-H04",
        "双面《戏台》：老祖宗与旧时代",
        "近现代史虚无化、旧时代美化和反向叙事",
        "中青网文艺评论理性辨析影视作品对旧时代的美化倾向，引导受众用完整史料看待近现代历史。",
        "https://fun.youth.cn/gnzx/202508/t20250806_16159741.htm",
    ),
    (
        "POS-H05",
        "篡改戏唱国歌的主播不能仅止于凉凉",
        "国家符号亵渎和轻慢化使用",
        "中青网评论批评主播戏唱国歌行为，呼吁社会严肃对待国家符号，是国家符号类议题的权威表态。",
        "https://pinglun.youth.cn/ttst/201810/t20181011_11751644.htm",
    ),
    (
        "POS-H06",
        "把“旭日旗”当日本国旗，错了就是错了",
        "国家符号亵渎和轻慢化使用",
        "中青网评论纠正旭日旗与日本国旗混淆的错误认知，示范如何理性辨析历史符号争议。",
        "https://pinglun.youth.cn/wztt/202011/t20201121_12585039.htm",
    ),
    (
        "POS-H07",
        "宣传栏里国旗四颗小星星平行排列",
        "国家符号亵渎和轻慢化使用",
        "中青网报道推动纠正公共场所错版国旗问题，体现媒体监督对国家符号规范使用的正向作用。",
        "https://news.youth.cn/sh/201608/t20160829_8603124.htm",
    ),
]


def main():
    conn = mysql.connector.connect(**DB)
    cur = conn.cursor()
    for case_id, case_name, risk_type_l2, relation_reason, source_url in POSITIVE_CASES:
        core_keywords = f"{risk_type_l2}、历史认知风险、中青网正向案例"
        cur.execute(
            """
            INSERT INTO t_case (
                case_id, case_name, risk_domain_l1, risk_type_l2,
                core_keywords, representative_text, source_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                case_name = VALUES(case_name),
                risk_domain_l1 = VALUES(risk_domain_l1),
                risk_type_l2 = VALUES(risk_type_l2),
                core_keywords = VALUES(core_keywords),
                representative_text = VALUES(representative_text),
                source_url = VALUES(source_url)
            """,
            (case_id, case_name, "历史认知风险", risk_type_l2,
             core_keywords, relation_reason, source_url),
        )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM t_case WHERE case_id LIKE 'POS-%%'")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    print(f"[OK] 正向案例写入完成，t_case 中 POS- 前缀记录共 {total} 条")


if __name__ == "__main__":
    main()
