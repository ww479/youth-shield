# -*- coding: utf-8 -*-
"""
���ݵ���ű� 5/7�����밸����
��Դ1���Ŀ�Dem.xlsx �� 03_�����⣨81����
��Դ2����������20260525.xlsx��59�������棩
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet

def seed_case(conn):
    """���밸��ά�ȱ�"""
    cur = conn.cursor()
    imported = 0
    errors = []

    # ---- ��Դ1���Ŀ�Dem.xlsx �� 03_������ ----
    rows1 = read_sheet(
        "C:/Users/29488/Desktop/��Ŀ/��������ʶ��̬����ʶ�����ֵ�������Ŀ�Dem.xlsx",
        "03_������"
    )
    for row in rows1:
        try:
            case_id = str(row.get("����ID") or "").strip()
            if not case_id or case_id == "����ID":
                continue
            cur.execute("""
                INSERT INTO dim_case (
                    case_id, case_name, related_tag_id,
                    risk_domain_l1, risk_domain_l2, platform, core_keyword,
                    related_corpus_cnt, total_interaction, avg_risk_score, priority_score,
                    representative_corpus_id, representative_text,
                    propagation_analysis, response_strategy,
                    convertible_script_type, review_status, related_script_ids
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (case_id) DO UPDATE SET
                    response_strategy = EXCLUDED.response_strategy,
                    updated_at = NOW()
            """, (
                case_id,
                str(row.get("��������") or "").strip(),
                str(row.get("������ǩID") or "").strip(),
                str(row.get("һ��������") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("ƽ̨") or "").strip(),
                str(row.get("���Ĺؼ���") or "").strip(),
                _int(row.get("������������")),
                _int(row.get("�ܻ�����")),
                _int(row.get("ƽ�����շ�")),
                _int(row.get("�������ȼ���")),
                str(row.get("��������ID") or "").strip(),
                str(row.get("�����ı�") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("���鴦�ò���") or "").strip(),
                str(row.get("��ת����������") or "").strip(),
                str(row.get("����״̬") or "").strip(),
                str(row.get("��������ID") or "").strip(),
            ))
            imported += 1
        except Exception as e:
            errors.append(f"[{row.get('����ID','')}] {e}")

    # ---- ��Դ2����������20260525.xlsx ----
    try:
        rows2 = read_sheet(
            "C:/Users/29488/Desktop/��Ŀ/��������20260525.xlsx",
            "Sheet1"
        )
        for row in rows2:
            try:
                case_id = str(row.get("����ID") or "").strip()
                if not case_id or case_id in ("����ID", "nan"):
                    continue

                from datetime import datetime
                occ_date = None
                raw_date = row.get("����ʱ��")
                if raw_date and str(raw_date) not in ("nan", ""):
                    try:
                        occ_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d")
                    except:
                        try:
                            occ_date = datetime.strptime(str(raw_date)[:10], "%Y/%m/%d")
                        except:
                            pass

                cur.execute("""
                    INSERT INTO dim_case (
                        case_id, case_title, risk_domain_l1,
                        risk_domain_l2, case_name, content_summary,
                        occurrence_date, spread_platform, spread_path, spread_effect,
                        interaction_feature, data_source_url, risk_description,
                        case_risk_level, platform_action,
                        action_age_6_12, action_age_13_15, action_age_16_18,
                        guiding_script, lesson_learned
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (case_id) DO UPDATE SET
                        guiding_script  = EXCLUDED.guiding_script,
                        lesson_learned  = EXCLUDED.lesson_learned,
                        action_age_13_15 = EXCLUDED.action_age_13_15,
                        updated_at      = NOW()
                """, (
                    case_id,
                    str(row.get("��������") or "").strip(),
                    str(row.get("һ��������") or "").strip(),
                    str(row.get("������ǩ") or "").strip(),
                    str(row.get("��������") or "").strip(),
                    str(row.get("����ժҪ") or "").strip(),
                    occ_date,
                    str(row.get("����ƽ̨") or "").strip(),
                    str(row.get("����·��") or "").strip(),
                    str(row.get("����Ч��") or "").strip(),
                    str(row.get("��������") or "").strip(),
                    str(row.get("������Դ����ҳ��ͼƬ���ӣ�") or "").strip(),
                    str(row.get("��������") or "").strip(),
                    str(row.get("���յȼ�") or "").strip(),
                    str(row.get("���÷�ʽ��ƽ̨��") or "").strip(),
                    str(row.get("���÷�ʽ��6-12�꣩") or "").strip(),
                    str(row.get("���÷�ʽ��13-15�꣩") or "").strip(),
                    str(row.get("���÷�ʽ��16-18�꣩") or "").strip(),
                    str(row.get("������������") or "").strip(),
                    str(row.get("���̽���") or "").strip(),
                ))
                imported += 1
            except Exception as e:
                errors.append(f"[case2 {row.get('����ID','')}] {e}")
    except Exception as e:
        errors.append(f"[��������20260525] {e}")

    conn.commit()
    cur.close()
    return imported, errors


def _int(v):
    try:
        return int(float(str(v or 0)))
    except:
        return 0
