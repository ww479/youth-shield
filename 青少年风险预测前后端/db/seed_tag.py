# -*- coding: utf-8 -*-
"""
���ݵ���ű� 1/7���������ϱ�ǩ��ϵ
��Դ���Ŀ�Dem.xlsx �� 01_���ϱ�ǩ��ϵ��92����
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet

def seed_risk_tag(conn):
    """������ձ�ǩά�ȱ�"""
    rows = read_sheet(
        "C:/Users/29488/Desktop/��Ŀ/��������ʶ��̬����ʶ�����ֵ�������Ŀ�Dem.xlsx",
        "01_���ϱ�ǩ��ϵ"
    )

    cur = conn.cursor()
    imported = 0
    errors = []

    for row in rows:
        try:
            tag_id = str(row.get("��ǩID") or "").strip()
            if not tag_id or tag_id == "��ǩID":
                continue

            cur.execute("""
                INSERT INTO dim_risk_tag (
                    tag_id, keyword, risk_domain_l1, risk_domain_l2, scene, dimension,
                    group_risk_threshold, content_risk_score, opposed_risk_score,
                    opposed_risk_level, combined_score, risk_level,
                    l1_name, l1_code, l1_desc,
                    l2_name, l2_code, l2_desc,
                    l3_name, l3_code, l3_desc,
                    guide_direction, tag_chain, is_crisis, is_active
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (tag_id) DO UPDATE SET
                    guide_direction = EXCLUDED.guide_direction,
                    combined_score = EXCLUDED.combined_score,
                    risk_level     = EXCLUDED.risk_level,
                    updated_at     = NOW()
            """, (
                tag_id,
                str(row.get("�ؼ���") or "").strip(),
                str(row.get("һ��������") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("��Ӧ����") or "").strip(),
                str(row.get("��Ӧά��") or "").strip(),
                _int(row.get("Ⱥ�����������ֵ")),
                _int(row.get("���ݷ��շ�")),
                _int(row.get("�������շ�")),
                str(row.get("�����ȼ�") or "").strip(),
                _int(row.get("�ۺϷ��շ�")),
                str(row.get("�����ȼ���") or "").strip(),   # risk_level ���ö����ȼ���
                str(row.get("L1") or "").strip(),
                tag_id.split("-")[0] if "-" in tag_id else "",
                str(row.get("L1˵��") or "").strip(),
                str(row.get("L2") or "").strip(),
                tag_id.rsplit("-", 1)[0] if tag_id.count("-") >= 1 else "",
                str(row.get("L2˵��") or "").strip(),
                str(row.get("L3") or "").strip(),
                tag_id.rsplit("-", 2)[-1] if tag_id.count("-") >= 2 else tag_id,
                str(row.get("L3˵��") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("��ǩ��") or "").strip(),
                False,
                True,
            ))
            imported += 1
        except Exception as e:
            errors.append(f"[{tag_id}] {e}")

    conn.commit()
    cur.close()
    return imported, errors


def _int(v):
    try:
        return int(float(str(v or 0)))
    except:
        return 0
