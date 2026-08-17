# -*- coding: utf-8 -*-
"""转写文本的同音错字纠正 — 面向重大风险词库（M1-M11）。

whisper 的错基本都是同音替换（"颠覆"→"颠付"、"历史从来"→"历史丛来"），而
`major_lexicon.detect_major()` 是短语精确匹配，一个字错整条短语就废掉，重大风险
直接漏判。`rules/transcribe_corrections.json` 那张手工表只有 34 条、且全是社保/
工龄话题，跟意识形态词库不搭，补不上这个洞。

这里换成数据驱动：把 major_lexicon 的 STRONG/WEAK 短语按拼音建索引，在转写文本里
找"读音相同但写错了"的片段改回来。

严格门控（拿 t_corpus 真实语料量过，缺一不可）：
  · 只用 >=4 字的短语      —— 3 字短语拼音串太短，撞车严重
  · 拼音串必须完全相同
  · 字面必须恰好差 1 个字  —— 差 2 个字以上多半不是同音错字，而是另一句话

放宽到"全量拼音匹配"实测在低风险语料上新增 18.3% 误报，不可用；
收到上面三条后新增误报 2.0%，而定向同音破坏的 M 命中挽回 33%（56.1%→70.7%）。

用法：
    fixed, fixes = fix_text(转写文本)
    # fixes = [(原片段, 纠正后), ...]，供前端标"已纠正"并展示原文
"""
import re

from pypinyin import lazy_pinyin

import major_lexicon

# 短语最小长度：3 字短语（词库里占多数）拼音串太短，同音撞车率高，只会带来误报。
# 实测放宽到 3：低风险语料误报 2.0% → 11.3%，而真实 whisper 错字（"换谁执政"→"患水执政"）
# 因为错了 2 个字仍纠不回来（只能得到"患谁执政"），纯亏。别再往下调。
MIN_PHRASE_LEN = 4
# 允许的字面差异数：同音错字通常只错 1 个字；放宽到 2 就会把不同的句子改成风险短语
MAX_CHAR_DIFF = 1


def _pinyin_chars(text: str) -> list:
    """逐字拼音，非汉字原样保留 —— 必须和原文严格一对一，否则下面按下标切片会错位。"""
    return lazy_pinyin(text, errors=lambda item: list(item))


def _build_index() -> list:
    """(短语, 拼音串列表) 列表。取 STRONG+WEAK 全量，只留够长的。"""
    seen = set()
    out = []
    for table in (major_lexicon.STRONG_PHRASES, major_lexicon.WEAK_PHRASES):
        for phrases in table.values():
            for p in phrases:
                if len(p) < MIN_PHRASE_LEN or p in seen:
                    continue
                seen.add(p)
                out.append((p, _pinyin_chars(p)))
    return out


_INDEX = _build_index()
# 按首音节分桶：整段文本逐位置比对 385 条短语太慢，先用首音节筛掉绝大多数
_BY_HEAD: dict = {}
for _p, _py in _INDEX:
    _BY_HEAD.setdefault(_py[0], []).append((_p, _py))


def fix_text(text: str) -> tuple:
    """纠正文本里的同音错字，返回 (纠正后文本, [(原片段, 纠正后), ...])。

    命中即整段替换（同一个错法在一段话里往往重复出现），fixes 去重后返回。
    """
    if not text or not text.strip():
        return text, []

    py = _pinyin_chars(text)
    n = len(text)
    fixes = {}
    for i in range(n):
        for phrase, ppy in _BY_HEAD.get(py[i], ()):
            L = len(phrase)
            if i + L > n or py[i:i + L] != ppy:
                continue
            seg = text[i:i + L]
            if seg == phrase:
                continue                      # 本来就是对的
            if sum(a != b for a, b in zip(seg, phrase)) != MAX_CHAR_DIFF:
                continue                      # 差太多，不像同音错字
            fixes.setdefault(seg, phrase)

    fixed = text
    for seg, phrase in fixes.items():
        fixed = fixed.replace(seg, phrase)
    return fixed, list(fixes.items())


def detect_major_tolerant(text: str) -> tuple:
    """先纠正同音错字再跑 M 规则检测，返回 (M编号列表, [(原片段,纠正后)])。

    转写文本走这个而不是直接 detect_major()，否则一个同音错字就吃掉整条重大风险命中。
    """
    fixed, fixes = fix_text(text)
    return major_lexicon.detect_major(fixed), fixes


def stats() -> dict:
    """词库规模自检，供 /health 之类的接口展示。"""
    return {"phrases_indexed": len(_INDEX),
            "min_phrase_len": MIN_PHRASE_LEN,
            "max_char_diff": MAX_CHAR_DIFF,
            "head_buckets": len(_BY_HEAD)}
