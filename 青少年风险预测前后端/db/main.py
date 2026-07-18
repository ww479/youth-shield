# -*- coding: utf-8 -*-
"""
���ݵ���������
�÷���python -m db.main
���Զ����⡢������������������
"""
import os, sys, time
from datetime import datetime

# ȷ�� utils �� db ��·����
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

DB_NAME = "youth_risk_db"
DB_USER = os.environ.get("PGUSER", "postgres")
DB_HOST = os.environ.get("PGHOST", "localhost")
DB_PORT = os.environ.get("PGPORT", "5432")
DB_PASS = os.environ.get("PGPASSWORD", "postgres")


def get_conn(dbname=DB_NAME):
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, dbname=dbname
    )


def create_database():
    """�������ݿ⣨��������ڣ�"""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, dbname="postgres"
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
    if not cur.fetchone():
        cur.execute(f"CREATE DATABASE {DB_NAME}")
        print(f"  [OK] ���ݿ� {DB_NAME} �����ɹ�")
    else:
        print(f"  [SKIP] ���ݿ� {DB_NAME} �Ѵ���")
    cur.close()
    conn.close()


def run_schema():
    """ִ�� schema.sql"""
    schema_path = os.path.join(BASE_DIR, "db", "schema.sql")
    print("\nִ�� schema.sql ...")
    conn = get_conn()
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()
    print("  [OK] ���ṹ�������")


def run_seed(name: str, func, *args, **kwargs):
    """���е��� seed ����"""
    start = time.time()
    print(f"\n���� {name} ...")
    conn = get_conn()
    try:
        result = func(conn, *args, **kwargs)
        elapsed = time.time() - start
        if isinstance(result, tuple):
            total, errors = result
            print(f"  [OK] {name} ������ɣ�{total} ������ʱ {elapsed:.1f}s")
            if errors:
                print(f"  [WARN] ǰ3������{errors[:3]}")
        else:
            print(f"  [OK] {name} ��ɣ���ʱ {elapsed:.1f}s")
    except Exception as e:
        print(f"  [FAIL] {name} ʧ�ܣ�{e}")
    finally:
        conn.close()


def main():
    print("=" * 55)
    print("��������ʶ��̬����ʶ��ϵͳ - ���ݵ������")
    print(f"��ʼʱ�䣺{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    # Step 1: ����
    print("\n[Step 1] �������ݿ�")
    create_database()

    # Step 2: ����
    print("\n[Step 2] �������ṹ")
    run_schema()

    # Step 3: ����ά�ȱ���������˳��
    from db.seed_tag import seed_risk_tag
    from db.seed_keyword import seed_keyword
    from db.seed_script import seed_script
    from db.seed_case import seed_case
    from db.seed_corpus import seed_corpus_dict
    from db.seed_relation import seed_relation
    from db.seed_platform import seed_platform_raw

    print("\n[Step 3] ����ά�ȱ�")
    run_seed("���ձ�ǩ��dim_risk_tag��", seed_risk_tag)
    run_seed("�ؼ��ʣ�dim_keyword��", seed_keyword)
    run_seed("������dim_script��", seed_script)
    run_seed("������dim_case��", seed_case)

    print("\n[Step 4] ������ʵ��")
    run_seed("���Ͽ⣨dim_corpus_dict��", seed_corpus_dict)
    run_seed("�Ŀ��ϵ��dim_relation��", seed_relation)
    run_seed("��ƽ̨ԭʼ���ݣ�fact_platform_raw��", seed_platform_raw)

    print("\n" + "=" * 55)
    print("ȫ��������ɣ�")
    print(f"����ʱ�䣺{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    # ��ӡ��������
    print("\n[��������]")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT table_name, row_count FROM v_data_summary ORDER BY row_count DESC")
    for row in cur.fetchall():
        print(f"  {row[0]:30s} {row[1]:>10,} ��")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
