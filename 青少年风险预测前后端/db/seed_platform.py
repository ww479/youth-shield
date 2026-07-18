# -*- coding: utf-8 -*-
"""
���ݵ���ű� 7/7��������ƽ̨ԭʼ����
��Դ��0529���ݼ�/0529�ϼ�/xlsx�棨5��ƽ̨��Լ30.5������
֧�ֶϵ��������ٶȹ�����
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.excel_reader import read_sheet
from datetime import datetime

PLATFORM_FILES = {
    "weibo":       "΢���ϼ�0529.xlsx",
    "douyin":      "�����ϼ�0529.xlsx",
    "xiaohongshu": "С����ϼ�0529.xlsx",
    "bilibili":    "���������ϼ�0529.xlsx",
    "baidu":       "�ٶȺϼ�0529.xlsx",
}
PLATFORM_DIR = "C:/Users/29488/Desktop/��Ŀ/0529���ݼ�/0529�ϼ�/xlsx��"
CHUNK_SIZE = 3000


def seed_platform_raw(conn):
    """������ƽ̨ԭʼ����"""
    cur = conn.cursor()
    results = {}
    total_all = 0
    skip_ad = 0

    for platform, fname in PLATFORM_FILES.items():
        fpath = os.path.join(PLATFORM_DIR, fname)
        if not os.path.exists(fpath):
            print(f"  [WARN] �ļ������ڣ�{fname}")
            results[platform] = {"status": "file_not_found", "imported": 0}
            continue

        print(f"\n  ���ڵ��� {fname}��{platform}��...")
        try:
            imported, skipped, errs = _import_single_platform(cur, conn, fpath, platform)
            total_all += imported
            skip_ad += skipped
            results[platform] = {
                "status": "success",
                "imported": imported,
                "skip_ad": skipped,
                "errors": errs[:5],
            }
        except Exception as e:
            results[platform] = {"status": "error", "error": str(e)}

    cur.close()
    return {"total": total_all, "skip_ad": skip_ad, "details": results}


def _import_single_platform(cur, conn, fpath, platform):
    rows = read_sheet(fpath, None)  # ����һ��sheet
    imported = 0
    skip_ad = 0
    errors = []
    batch = []

    # ��ȡ��ͷ
    if not rows:
        return 0, 0, []
    headers = rows[0]

    for ri, row in enumerate(rows[1:], 2):
        try:
            rec = dict(zip(headers, row))
            raw = {}

            # �ٶȹ�����
            if platform == "baidu":
                is_ad = str(rec.get("�Ƿ���") or "").strip()
                if is_ad == "��":
                    skip_ad += 1
                    continue

            # ��������
            post_date = _parse_post_date(rec.get("����ʱ��") or rec.get("�ʼǷ���ʱ��") or "")

            # ƽ̨ͨ���ֶ�
            record = [
                platform,
                str(rec.get("�ؼ���") or rec.get("������") or "").strip(),
                str(rec.get("����") or "").strip(),
                str(rec.get("��������") or rec.get("��������ҳ����") or rec.get("��Ƶ����") or rec.get("ժҪ") or rec.get("����") or "").strip(),
                str(rec.get("�����ǳ�") or rec.get("������") or rec.get("����") or rec.get("С��������") or rec.get("��������") or rec.get("BվUP��") or "").strip(),
                post_date,
                str(rec.get("ҳ����ַ") or rec.get("��������") or rec.get("��Ƶ����") or rec.get("URL") or rec.get("��������ҳ����") or "").strip(),
                _int(rec.get("������") or rec.get("�ܵ�����") or 0),
                _int(rec.get("������") or rec.get("�ܵ�Ļ��") or 0),
                _int(rec.get("ת����") or rec.get("������") or 0),
                _int(rec.get("�ղ���") or 0),
                _int(rec.get("Ӳ��") or 0),
                _int(rec.get("�ܲ�����") or 0),
                _int(rec.get("��Ļ��") or 0),
                0,  # follower_count
                str(rec.get("��Ƶ����") or rec.get("��ƵURL") or "").strip(),
                _int(rec.get("��Ƶ����") or 0),
                None,  # is_collection
                str(rec.get("�Ƿ���") or "��").strip() == "��",  # is_ad
                _int(rec.get("ҳ��") or 0),
                str(rec.get("ժҪ") or "").strip(),
                str(rec.get("�ʼ�����ҳ����") or "").strip(),
                str(rec.get("�������ӵ�ַ") or "").strip(),
                str(rec.get("������ҳ") or "").strip(),
                str(rec.get("�ʼǷ���ʱ��") or "").strip(),
                str(rec.get("��Ƶʱ��") or "").strip(),
                str(rec.get("��Ƶ��ǩ") or "").strip(),
                str(rec.get("������ͷ������") or "").strip(),
                _int(rec.get("Ͷ������") or 0),
                str(rec.get("ԭ�����ǳ�") or "").strip(),
                str(rec.get("ͼƬ����") or "").strip(),
                str(rec.get("��Ƶ����") or "").strip(),
                json.dumps({k: str(v) for k, v in rec.items() if v}, ensure_ascii=False),
            ]

            batch.append(record)

            if len(batch) >= CHUNK_SIZE:
                _insert_batch(cur, batch)
                imported += len(batch)
                batch = []
                print(f"    {platform}: �ѵ��� {imported} ��")

        except Exception as e:
            if len(errors) < 5:
                errors.append(f"[row {ri}] {e}")

    if batch:
        _insert_batch(cur, batch)
        imported += len(batch)

    conn.commit()
    return imported, skip_ad, errors


def _insert_batch(cur, batch):
    cur.executemany("""
        INSERT INTO fact_platform_raw (
            platform, keyword_search, title, content, author, post_date, url,
            like_count, comment_count, repost_count, favorite_count,
            collect_count, view_count, danmu_count, follower_count,
            video_url, video_duration, is_collection, is_ad,
            page_no, abstract, note_url, cover_url, author_page, note_date,
            video_duration_str, video_tags, author_avatar, publish_count,
            original_author, image_urls, video_urls, raw_data
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, batch)


def _int(v):
    try:
        return int(float(str(v or 0)))
    except:
        return 0


def _parse_post_date(v):
    if not v or str(v) in ("", "nan", "NaT"):
        return None
    v = str(v).strip()[:19]
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"]:
        try:
            return datetime.strptime(v, fmt)
        except:
            continue
    return None
