# -*- coding: utf-8 -*-
"""
���ݵ���ű� 4/7�����뻰����
��Դ1���Ŀ�Dem.xlsx �� 04_AI�����⣨181����
��Դ2����������20260525.xlsx���������������У�
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet

def seed_script(conn):
    """���뻰��ά�ȱ�"""
    rows = read_sheet(
        "C:/Users/29488/Desktop/��Ŀ/��������ʶ��̬����ʶ�����ֵ�������Ŀ�Dem.xlsx",
        "04_AI������"
    )

    cur = conn.cursor()
    imported = 0
    errors = []

    for row in rows:
        try:
            sid = str(row.get("����ID") or "").strip()
            if not sid or sid == "����ID":
                continue

            script_type = str(row.get("��������") or "normal").strip()
            script_phase = str(row.get("�����׶�") or "emotion").strip()
            age_group = str(row.get("���������") or "13-15��").strip()

            # ͳһ����θ�ʽ
            age_group = _normalize_age(age_group)

            cur.execute("""
                INSERT INTO dim_script (
                    script_code, related_case_id, tag_id,
                    risk_domain_l1, risk_domain_l2, core_keyword,
                    applicable_platform, applicable_age, applicable_risk,
                    script_type, script_phase, trigger_condition,
                    script_content, follow_up, audit_status, is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (script_code) DO UPDATE SET
                    script_content = EXCLUDED.script_content,
                    follow_up     = EXCLUDED.follow_up,
                    updated_at    = NOW()
            """, (
                f"SCR-{sid}",
                str(row.get("��������ID") or "").strip(),
                str(row.get("������ǩID") or "").strip(),
                str(row.get("һ��������") or "").strip(),
                str(row.get("������������") or "").strip(),
                str(row.get("���Ĺؼ���") or "").strip(),
                str(row.get("����ƽ̨") or "").strip(),
                age_group,
                str(row.get("���÷��յȼ�") or "").strip(),
                script_type,
                script_phase,
                str(row.get("��������") or "").strip(),
                str(row.get("��������") or "").strip(),
                str(row.get("��������") or "").strip(),
                str(row.get("���״̬") or "pending").strip(),
                True,
            ))
            imported += 1
        except Exception as e:
            errors.append(f"[{row.get('����ID','')}] {e}")

    conn.commit()

    # ---- ���䣺�Ӱ�����������ȡ������������ ----
    try:
        case_rows = read_sheet(
            "C:/Users/29488/Desktop/��Ŀ/��������20260525.xlsx",
            "Sheet1"
        )
        for row in case_rows:
            case_id = str(row.get("����ID") or "").strip()
            guiding_script = str(row.get("������������") or "").strip()
            if not guiding_script or guiding_script in ("������������", "", "nan"):
                continue
            age_groups = ["6-12��", "13-15��", "16-18��"]
            for ag in age_groups:
                action = str(row.get(f"���÷�ʽ��{ag}��") or "").strip()
                if not action:
                    continue
                try:
                    cur.execute("""
                        INSERT INTO dim_script (
                            script_code, related_case_id, tag_id,
                            applicable_age, script_type, script_phase,
                            script_content, follow_up, audit_status, is_active
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (
                        f"SCR-CASE-{case_id}-{ag}",
                        case_id,
                        str(row.get("������ǩ") or "").strip(),
                        ag,
                        "normal",
                        "emotion",
                        action,
                        guiding_script,
                        "approved",
                        True,
                    ))
                except Exception as e:
                    errors.append(f"[case {case_id}] {e}")
        conn.commit()
    except Exception as e:
        errors.append(f"[��������] {e}")

    cur.close()
    return imported, errors


def _normalize_age(age_str):
    """ͳһ����θ�ʽ"""
    age_str = str(age_str).strip()
    if "13-15" in age_str or "����" in age_str:
        return "13-15��"
    elif "16-18" in age_str or "����" in age_str:
        return "16-18��"
    elif "19-24" in age_str or "��ѧ" in age_str:
        return "19-24��"
    elif "6-12" in age_str or "Сѧ" in age_str:
        return "6-12��"
    return age_str if age_str else "13-15��"
