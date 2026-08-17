# -*- coding: utf-8 -*-
"""导入《重大意识形态风险语料样本》到 t_major_risk_sample（M1-M11 标注短句，供语义样本库/评测/词库挖掘）。
用法: .venv/bin/python db/import_major_samples.py "/绝对路径/重大意识形态风险语料样本-20260723.xlsx"
insert-only；表已有数据则中止（FORCE_CLEAR=1 可重导）。"""
import os, re, sys, openpyxl, mysql.connector
DEFAULT=os.environ.get("MAJOR_XLSX","/Users/wangshuai/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_1vn16raw7d8822_4053/msg/file/2026-07/重大意识形态风险语料样本-20260723.xlsx")
DB=dict(host="127.0.0.1",port=3306,user="root",password="",database="youth_ideology",charset="utf8mb4",use_pure=True)
DDL="""CREATE TABLE IF NOT EXISTS t_major_risk_sample (
  id INT NOT NULL AUTO_INCREMENT,
  rule_code VARCHAR(6) NOT NULL COMMENT '重大风险规则 M1-M11',
  rule_name VARCHAR(100) COMMENT '规则名称',
  subtype VARCHAR(200) COMMENT '二级类型',
  sample_text VARCHAR(600) NOT NULL COMMENT '代表性话语样本',
  PRIMARY KEY(id), KEY idx_rule(rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='重大意识形态风险标注样本(M1-M11)';"""
def main():
    path=sys.argv[1] if len(sys.argv)>1 else DEFAULT
    wb=openpyxl.load_workbook(path,read_only=True,data_only=True); ws=wb['Sheet1']
    it=ws.iter_rows(values_only=True); next(it); rows=[]
    for r in it:
        if not r or r[0] is None: continue
        c1=str(r[1] or ''); m=re.search(r'规则\s*(\d+)',c1); s=str(r[3] or '').strip()
        if not (m and s): continue
        rows.append(('M'+m.group(1), re.sub(r'规则\s*\d+[：:]\s*','',c1).strip(), str(r[2] or '').strip(), s[:600]))
    wb.close()
    c=mysql.connector.connect(**DB); cur=c.cursor(); cur.execute("SET NAMES utf8mb4")
    cur.execute(DDL)
    cur.execute("SELECT COUNT(*) FROM t_major_risk_sample"); n=cur.fetchone()[0]
    if n>0 and os.environ.get("FORCE_CLEAR")!="1":
        print(f"t_major_risk_sample 已有 {n} 行，已中止（FORCE_CLEAR=1 可重导）"); return
    if n>0: cur.execute("DELETE FROM t_major_risk_sample")
    cur.executemany("INSERT INTO t_major_risk_sample(rule_code,rule_name,subtype,sample_text) VALUES(%s,%s,%s,%s)", rows)
    c.commit()
    cur.execute("SELECT rule_code,rule_name,COUNT(*) FROM t_major_risk_sample GROUP BY rule_code,rule_name ORDER BY COUNT(*) DESC")
    print(f"导入 {len(rows)} 条。分布:")
    for a,b,k in cur.fetchall(): print(f"  {a} {b}: {k}")
    c.close()
if __name__=="__main__": main()
