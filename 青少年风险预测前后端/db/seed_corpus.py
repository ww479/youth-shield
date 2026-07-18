# -*- coding: utf-8 -*-
"""
���ݵ���ű� 3/7���������Ͽ�
��Դ���Ŀ�Dem.xlsx �� 02_���Ͽ⣨59412����
֧�ֶϵ�������ÿ10000��һ�ύ��
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet
import time

CHUNK_SIZE = 5000  # ÿ5000���ύһ�Σ������ڴ�ռ��


def seed_corpus_dict(conn):
    """�������Ͽ�ά�ȱ�"""
    rows = read_sheet(
        "C:/Users/29488/Desktop/��Ŀ/��������ʶ��̬����ʶ�����ֵ�������Ŀ�Dem.xlsx",
        "02_���Ͽ�"
    )

    cur = conn.cursor()
    total = 0
    errors = []

    cols = [
        "����ID", "ƽ̨", "�����ؼ���", "ƥ��ؼ���", "��ǩID",
        "һ��������", "������������", "���յȼ�", "�ۺϷ��շ�",
        "���ݷ��շ�", "�������շ�", "�����ȼ���",
        "L1", "L2", "L3", "������������",
        "����", "����ժҪ", "�ı�����",
        "����", "����ʱ��", "URL",
        "������", "������", "ת����", "�ղ���", "������",
        "�Ƿ���밸����ѡ", "����״̬", "����״̬", "ԭʼ��Դ�ļ�", "��ǩ��"
    ]

    batch = []
    for ri, row in enumerate(rows):
        if ri == 0:
            continue  # ������ͷ
        tag_id = str(row.get("��ǩID") or "").strip()
        if tag_id in ("��ǩID", "", "nan"):
            continue

        try:
            post_date_raw = row.get("����ʱ��")
            post_date = _parse_date(post_date_raw)

            batch.append((
                str(row.get("����ID") or "").strip(),
                str(row.get("ƽ̨") or "").strip(),
                str(row.get("�����ؼ���") or "").strip(),
                str(row.get("ƥ��ؼ���") or "").strip(),
                tag_id,
                str(row.get("һ��������") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("���յȼ�") or "").strip(),
                _int(row.get("�ۺϷ��շ�")),
                _int(row.get("���ݷ��շ�")),
                _int(row.get("�������շ�")),
                str(row.get("�����ȼ���") or "").strip(),
                str(row.get("L1") or "").strip(),
                str(row.get("L2") or "").strip(),
                str(row.get("L3") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("����") or "").strip(),
                str(row.get("����ժҪ") or "").strip(),
                str(row.get("�ı�����") or "").strip(),
                str(row.get("����") or "").strip(),
                post_date,
                str(row.get("URL") or "").strip(),
                _int(row.get("������")),
                _int(row.get("������")),
                _int(row.get("ת����")),
                _int(row.get("�ղ���")),
                _int(row.get("������")),
                str(row.get("�Ƿ���밸����ѡ") or "").strip(),
                str(row.get("����״̬") or "").strip() == "������",
                str(row.get("����״̬") or "").strip(),
                str(row.get("ԭʼ��Դ�ļ�") or "").strip(),
                str(row.get("��ǩ��") or "").strip(),
            ))

            if len(batch) >= CHUNK_SIZE:
                _insert_batch(cur, batch)
                total += len(batch)
                print(f"  �ѵ��� {total} ��...")
                batch = []

        except Exception as e:
            errors.append(f"[row {ri}] {e}")

    # ʣ������
    if batch:
        _insert_batch(cur, batch)
        total += len(batch)

    conn.commit()
    cur.close()
    return total, errors


def _insert_batch(cur, batch):
    cur.executemany("""
        INSERT INTO dim_corpus_dict (
            original_id, platform, search_keyword, matched_keyword, tag_id,
            risk_domain_l1, risk_domain_l2, risk_level, combined_score,
            content_score, opposed_score, l1_name, l2_name, l3_name,
            guide_direction, title, content_text, full_text,
            author, post_date, url,
            like_count, comment_count, repost_count, favorite_count, interaction_total,
            candidate_status, desensitized, review_status, source_file, tag_chain
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
    """, batch)


def _int(v):
    try:
        return int(float(str(v or 0)))
    except:
        return 0


def _parse_date(v):
    from datetime import datetime
    if not v or str(v) in ("", "nan", "NaT"):
        return None
    try:
        if isinstance(v, datetime):
            return v
        return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except:
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d")
        except:
            return None
