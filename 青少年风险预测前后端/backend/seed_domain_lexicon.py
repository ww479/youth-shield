# -*- coding: utf-8 -*-
"""把五大意识形态风险域的关键词簇 + 句式模板灌入 MySQL（youth_ideology）。
用法：python seed_domain_lexicon.py
表：t_domain_keyword（词簇关键词）、t_domain_template（核心句式模板）。幂等：先清空再插入。"""
import sys
import os

import mysql.connector

from parse_domain_rules import parse

sys.stdout.reconfigure(encoding="utf-8")

DB = dict(host=os.environ.get("DB_HOST", "127.0.0.1"), port=int(os.environ.get("DB_PORT", "3306")),
          user=os.environ.get("DB_USER", "root"), password=os.environ.get("DB_PASSWORD", ""),
          database=os.environ.get("DB_NAME", "youth_ideology"), charset="utf8mb4", use_pure=True)

DDL_KW = """
CREATE TABLE IF NOT EXISTS t_domain_keyword (
  id INT AUTO_INCREMENT PRIMARY KEY,
  domain_code VARCHAR(4) NOT NULL,
  domain_id   VARCHAR(40) NOT NULL,
  domain_name VARCHAR(40) NOT NULL,
  cluster     VARCHAR(60) NOT NULL,
  keyword     VARCHAR(120) NOT NULL,
  KEY idx_kw (keyword),
  KEY idx_domain (domain_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_TPL = """
CREATE TABLE IF NOT EXISTS t_domain_template (
  id INT AUTO_INCREMENT PRIMARY KEY,
  domain_code   VARCHAR(4) NOT NULL,
  domain_id     VARCHAR(40) NOT NULL,
  domain_name   VARCHAR(40) NOT NULL,
  template_code VARCHAR(16) NOT NULL,
  pattern       VARCHAR(255) NOT NULL,
  risk_action   VARCHAR(120),
  KEY idx_domain (domain_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def main():
    domains = parse()
    conn = mysql.connector.connect(**DB)
    cur = conn.cursor()
    cur.execute(DDL_KW)
    cur.execute(DDL_TPL)
    cur.execute("TRUNCATE TABLE t_domain_keyword")
    cur.execute("TRUNCATE TABLE t_domain_template")

    n_kw = n_tpl = 0
    for code in ["A", "B", "C", "D", "E"]:
        d = domains.get(code)
        if not d:
            continue
        for cluster, kws in d["clusters"].items():
            for kw in kws:
                cur.execute(
                    "INSERT INTO t_domain_keyword(domain_code,domain_id,domain_name,cluster,keyword) VALUES(%s,%s,%s,%s,%s)",
                    (code, d["id"], d["name"], cluster, kw))
                n_kw += 1
        for tcode, pat, act in d["templates"]:
            cur.execute(
                "INSERT INTO t_domain_template(domain_code,domain_id,domain_name,template_code,pattern,risk_action) VALUES(%s,%s,%s,%s,%s,%s)",
                (code, d["id"], d["name"], tcode, pat, act))
            n_tpl += 1

    conn.commit()
    cur.close(); conn.close()
    print(f"✓ 灌库完成：关键词 {n_kw} 条，句式模板 {n_tpl} 条")


if __name__ == "__main__":
    main()
