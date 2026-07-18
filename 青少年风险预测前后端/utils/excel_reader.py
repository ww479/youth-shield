# -*- coding: utf-8 -*-
import openpyxl


def read_sheet(filepath: str, sheet_name: str) -> list[dict]:
    """
    读取 Excel 指定 Sheet，返回字典列表（第一行为列头）。
    空行自动跳过。
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb[sheet_name]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]

    result = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        result.append({headers[i]: row[i] for i in range(len(headers))})

    return result
