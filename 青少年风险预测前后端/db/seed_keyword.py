# -*- coding: utf-8 -*-
"""
���ݵ���ű� 2/7������ؼ���ӳ���
��Դ1���ؼ���ӳ���ϵ-���������0529.xlsx �� Sheet1��215����
��Դ2���Ŀ�Dem.xlsx �� 01_���ϱ�ǩ��ϵ���ؼ����У�
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet

def seed_keyword(conn):
    """����ؼ���ά�ȱ�"""
    kw_file = "C:/Users/29488/Desktop/��Ŀ/0529���ݼ�/�ؼ���ӳ���ϵ-���������0529.xlsx"

    cur = conn.cursor()
    imported = 0
    errors = []

    # ---- Sheet1���ؼ���ӳ���ϵ��215����----
    rows = read_sheet(kw_file, "Sheet1")
    for row in rows:
        try:
            kw = str(row.get("�ؼ���") or "").strip()
            if not kw or kw == "�ؼ���":
                continue
            cur.execute("""
                INSERT INTO dim_keyword (
                    keyword, keyword_type, tag_id,
                    opposed_level, scene, dimension, content_nature,
                    group_threshold, l1_name, l2_name, l3_name,
                    domain_category, is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (
                kw,
                "exact",
                _build_tag_id(row.get("L1"), row.get("L2"), row.get("L3")),
                str(row.get("�����ȼ�") or "").strip(),
                str(row.get("��Ӧ����") or "").strip(),
                str(row.get("��Ӧά��") or "").strip(),
                str(row.get("��������") or "").strip(),
                _int(row.get("Ⱥ�����������ֵ")),
                str(row.get("L1") or "").strip(),
                str(row.get("L2") or "").strip(),
                str(row.get("L3") or "").strip(),
                str(row.get("�������") or "").strip(),
                True,
            ))
            imported += 1
        except Exception as e:
            errors.append(f"[{row.get('�ؼ���','')}] {e}")

    # ---- ���Ŀ�Dem.xlsx 01_���ϱ�ǩ��ϵ ����ؼ��� ----
    dem_rows = read_sheet(
        "C:/Users/29488/Desktop/��Ŀ/��������ʶ��̬����ʶ�����ֵ�������Ŀ�Dem.xlsx",
        "01_���ϱ�ǩ��ϵ"
    )
    for row in dem_rows:
        tag_id = str(row.get("��ǩID") or "").strip()
        keywords_raw = str(row.get("�ؼ���") or "").strip()
        if not keywords_raw or tag_id == "��ǩID":
            continue
        for kw in keywords_raw.replace("��", ",").replace("��", ",").split(","):
            kw = kw.strip()
            if not kw:
                continue
            try:
                cur.execute("""
                    INSERT INTO dim_keyword (keyword, keyword_type, tag_id, l1_name, l2_name, l3_name, is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, (
                    kw, "exact", tag_id,
                    str(row.get("һ��������") or "").strip(),
                    str(row.get("������������") or "").strip(),
                    str(row.get("L3") or "").strip(),
                    True
                ))
                imported += 1
            except Exception as e:
                errors.append(f"[{kw}] {e}")

    conn.commit()
    cur.close()
    return imported, errors


def _build_tag_id(l1, l2, l3):
    """���� L1/L2/L3 ���ƹ��� tag_id"""
    parts = [str(v).strip() for v in [l1, l2, l3] if str(v).strip()]
    return "-".join(parts)


def _int(v):
    try:
        return int(float(str(v or 0)))
    except:
        return 0
