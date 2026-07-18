# -*- coding: utf-8 -*-
"""
���ݵ���ű� 6/7�������Ŀ��ϵ��
��Դ���Ŀ�Dem.xlsx �� 05_�Ŀ��ϵ����20841����
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet

CHUNK_SIZE = 5000


def seed_relation(conn):
    """�����Ŀ��ϵ��"""
    rows = read_sheet(
        "C:/Users/29488/Desktop/��Ŀ/��������ʶ��̬����ʶ�����ֵ�������Ŀ�Dem.xlsx",
        "05_�Ŀ��ϵ��"
    )

    cur = conn.cursor()
    total = 0
    errors = []
    batch = []

    for ri, row in enumerate(rows):
        if ri == 0:
            continue
        rel_id = str(row.get("��ϵID") or "").strip()
        if not rel_id or rel_id in ("��ϵID", "nan"):
            continue

        try:
            conf = row.get("���Ŷ�")
            if conf is not None:
                try:
                    conf = float(str(conf))
                except:
                    conf = 1.0
            else:
                conf = 1.0

            batch.append((
                str(row.get("��ϵID") or "").strip(),
                str(row.get("��������") or "").strip(),
                str(row.get("����ID") or "").strip(),
                str(row.get("��ϵ") or "").strip(),
                str(row.get("��������") or "").strip(),
                str(row.get("����ID") or "").strip(),
                str(row.get("ƥ��ؼ���") or "").strip(),
                conf,
                str(row.get("˵��") or "").strip(),
            ))

            if len(batch) >= CHUNK_SIZE:
                _insert(cur, batch)
                total += len(batch)
                print(f"  �ѵ��� {total} ��...")
                batch = []

        except Exception as e:
            errors.append(f"[row {ri}] {e}")

    if batch:
        _insert(cur, batch)
        total += len(batch)

    conn.commit()
    cur.close()
    return total, errors


def _insert(cur, batch):
    cur.executemany("""
        INSERT INTO dim_relation (
            relation_id, relation_type, subject_type, subject_id,
            relation, object_type, object_id,
            matched_keyword, confidence, description
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING
    """, batch)
