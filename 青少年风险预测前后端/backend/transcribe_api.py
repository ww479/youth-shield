# -*- coding: utf-8 -*-
"""视频转写 API — 上传视频，边解析边推送字幕

本模块导出 `router`，由 main.py `include_router` 挂进主服务（8000），这样转写文本
做六维研判时能用上 main.py startup() 建好的 Chroma 语义索引（重大风险样本库）；
独立进程跑的话拿不到索引，M 规则只剩词库精确匹配、召回会掉一截。

也保留独立启动（不带语义索引，仅转写）：
  cd backend && python -m uvicorn transcribe_api:app --host 0.0.0.0 --port 8010 --reload
挂在主服务下时前端地址：http://127.0.0.1:8000/ui/transcribe.html

链路：mp4 --ffmpeg--> 16kHz 单声道 wav --whisper.cpp--> 逐行捕获 stdout --SSE--> 前端
whisper-cli 每识别完一句就往 stdout 打一行，所以按解析速度实时出字，不必等全片跑完。
"""
import os, re, json, asyncio, shutil, subprocess, tempfile, threading, queue, uuid, time
from fastapi import APIRouter, FastAPI, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse

router = APIRouter()

# ── 配置：可用环境变量覆盖 ──
WHISPER_BIN   = os.environ.get("WHISPER_BIN", shutil.which("whisper-cli") or "whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL",
                    os.path.expanduser("~/whisper-models/ggml-large-v3-turbo.bin"))
FFMPEG_BIN    = os.environ.get("FFMPEG_BIN", shutil.which("ffmpeg") or "ffmpeg")
FFPROBE_BIN   = os.environ.get("FFPROBE_BIN", shutil.which("ffprobe") or "ffprobe")
WORK_DIR      = os.environ.get("TRANSCRIBE_WORK_DIR",
                    os.path.join(tempfile.gettempdir(), "youth_shield_transcribe"))
os.makedirs(WORK_DIR, exist_ok=True)

# 压制繁简混输 + 要求输出标点（whisper 的 initial prompt）
# 这句"加上标点符号"是断句的命脉：不带它 whisper 会一个标点都不输出（实测），
# 于是只能靠字数兜底硬切，字幕就变成几句杂糅、没有标点。
PUNCT_PROMPT = "以下是一段普通话视频内容，请使用简体中文输出，并加上标点符号。"
DEFAULT_PROMPT = os.environ.get("WHISPER_PROMPT", PUNCT_PROMPT)


def build_prompt(user_prompt: str = "") -> str:
    """把用户填的提示词**追加**在标点要求之后，而不是整体替换。

    用户在页面上填的多是人名/专有名词（用来降低同音错字），如果直接拿它当
    initial prompt，就把"请加上标点符号"挤掉了 —— 标点一丢，断句立刻退化。
    """
    user = (user_prompt or "").strip()
    if not user:
        return DEFAULT_PROMPT
    if "标点" in user:          # 用户自己写了标点要求，尊重原文
        return user
    return f"{DEFAULT_PROMPT} {user}"

MAX_UPLOAD_MB = int(os.environ.get("TRANSCRIBE_MAX_MB", "500"))

# 句末标点：遇到就断句
_SENT_END = "。！？!?…"
# 句中标点：逗号/顿号/分号/冒号也作为断点，让长句拆成短句、每条各自对应一个时间点。
# 断出来的短句保留末尾符号（"课本省略了历史真相，"），不去掉。
# 但要有最小长度，否则"对，""是的，"这种两三个字的碎片会刷满列表。
_SOFT_END = "，,、；;：:"
SOFT_CUT_LEN = int(os.environ.get("TRANSCRIBE_SOFT_CUT", "8"))
# 兜底切分长度：万一连标点都不给，到这个字数就强制断
FORCE_CUT_LEN = int(os.environ.get("TRANSCRIBE_FORCE_CUT", "40"))
# 静音间隔断句：字级时间戳下，两个字之间的空白就是说话人的停顿。
# 实测停顿点与真实句子边界高度吻合（1.0-1.2s 的间隔正好落在句子之间），
# 所以即便模型一个标点都不给，也能按停顿把句子切开——比纯靠字数硬切可靠得多。
GAP_CUT_SEC = float(os.environ.get("TRANSCRIBE_GAP_CUT", "0.45"))
GAP_MIN_LEN = int(os.environ.get("TRANSCRIBE_GAP_MIN_LEN", "5"))
# 只剩标点的碎片，丢掉
_PUNCT_ONLY = re.compile(r"^[\s。，、；：？！…—\-·「」『』《》（）()\.,;:!?\"']*$")

# ── 同音错字纠正表 ──
# whisper 的错基本都是同音替换（工龄→公邻），改提示词并不总能压住，
# 用一张词组表在输出侧直接改掉更可靠。规则放外部 JSON，加词不用改代码。
CORRECTIONS_FILE = os.environ.get("TRANSCRIBE_CORRECTIONS",
                      os.path.join(os.path.dirname(__file__), "rules",
                                   "transcribe_corrections.json"))

_corr_map: dict = {}
_corr_re = None


def load_corrections(path: str = None) -> int:
    """读纠正表，编译成一个大正则。长词优先，避免 '公邻' 抢在 '公邻满' 前面。"""
    global _corr_map, _corr_re
    path = path or CORRECTIONS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("corrections") or {}
        # 过滤左右相同、空键的无效条目
        _corr_map = {k: v for k, v in raw.items()
                     if k and v and k != v and not k.startswith("_")}
    except FileNotFoundError:
        _corr_map = {}
    except Exception as e:
        print(f"[corrections] 读取失败 {path}: {e}")
        _corr_map = {}

    if _corr_map:
        keys = sorted(_corr_map, key=len, reverse=True)   # 长的先匹配
        _corr_re = re.compile("|".join(re.escape(k) for k in keys))
    else:
        _corr_re = None
    return len(_corr_map)


def apply_corrections(text: str) -> str:
    if not text or not _corr_re:
        return text
    return _corr_re.sub(lambda m: _corr_map[m.group(0)], text)


# whisper 偶尔把中文逗号识别成全角字母（实测输出 "很多人不知道Ｂ其实这件事…"，
# Ｂ=U+FF22 出现在本该是逗号的位置；默认分段和 -ml 模式都会发生）。
# 两种形态都要处理：字级模式下它是独立 token，整段模式下它夹在汉字中间。
# 只认单个字母：连续多个（ＣＥＯ、ＡＩ）可能是真内容，不动。
_FULLWIDTH_LATIN = re.compile(r"^[Ａ-Ｚａ-ｚ]$")
_STRAY_PUNCT = re.compile(r"(?<=[一-鿿])[Ａ-Ｚａ-ｚ](?=[一-鿿])")


def normalize_punct(text: str) -> str:
    """把误识别成全角字母的中文逗号还原，否则断句会失效。"""
    if not text:
        return text
    if _FULLWIDTH_LATIN.match(text.strip()):
        return "，"                      # 字级模式：整个 token 就是那个杂字符
    return _STRAY_PUNCT.sub("，", text)   # 整段模式：夹在汉字之间


# ── 幻觉过滤 ──
# whisper 在没有人声的片段（静音、纯音乐、背景噪声）不会保持沉默，而是硬凑输出。
# 实测三种形态：
#   ① 复读我们自己的 initial prompt —— 静音 40s 输出"中文输出，并加上标点符号。"
#   ② 吐训练数据里的字幕组残留 —— "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"
#   ③ 吐"字幕志愿者 李宗盛"这类署名
# whisper-cli 的 -nth / -sns / -nf / -et / -lpt 都压不掉（实测无效），只能在输出侧滤。
_HALLUCINATION_PAT = [
    r"请不吝(点赞|赐)", r"点赞\s*订阅", r"打赏支持", r"明镜与点点",
    r"字幕志愿者", r"字幕组", r"由.{0,8}字幕", r"翻译[:：]?\s*$",
    r"感谢观看", r"謝謝(收看|觀看)", r"下集再见", r"欢迎订阅",
    r"关注我们的频道", r"更多精彩", r"^\s*(音乐|掌声|笑声|BGM)\s*$",
    r"Amara\.org", r"subtitles? by", r"transcri(bed|ption) by",
]
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATION_PAT), re.I)


def _prompt_echo_keys(prompt: str) -> set:
    """把提示词切成片段，用于识别"模型把提示词复读出来"的情况。"""
    keys = set()
    for part in re.split(r"[，,。；;！!？?\s]+", prompt or ""):
        part = part.strip()
        if len(part) >= 4:
            keys.add(part)
    return keys


def is_hallucination(text: str, prompt_keys: set = None) -> bool:
    """判断这条输出是否为无人声段的幻觉，True 则丢弃。"""
    s = (text or "").strip()
    if not s:
        return True
    if _HALLUCINATION_RE.search(s):
        return True
    # 复读提示词：整条内容几乎就是提示词的一个片段
    if prompt_keys:
        bare = re.sub(r"[\s，,。；;！!？?、]", "", s)
        for k in prompt_keys:
            kb = re.sub(r"[\s，,。；;！!？?、]", "", k)
            if kb and len(bare) <= len(kb) + 4 and kb in bare:
                return True
    return False


# ── 轨 A：逐句重大风险红线检测（纯正则词库，实测 ~1.6ms/条，不碰 LLM）──
# 六维 36 项大半是篇章级判据，单句喂进去基本全 0；但 M1-M11 是短语级的，逐句就能判。
# 所以"实时"这一层用 M 规则撑，六维交给 main.py 的窗口研判。
try:
    import homophone_fix
    import major_lexicon
except Exception as e:      # 缺 pypinyin 之类的依赖时不要拖垮转写主功能
    homophone_fix = None
    major_lexicon = None
    print(f"[transcribe] 逐句红线检测不可用（仅转写不受影响）：{e}")


def detect_sentence_risk(text: str) -> dict:
    """返回 {"major": [M..], "major_names": [..], "homophone": [[原,纠正后],..]}。

    先做同音纠正再判：whisper 的同音错字会让短语精确匹配整条落空（实测定向破坏后
    M 命中率 100%→56%，纠正后回到 71%）。
    """
    if not text or not text.strip() or major_lexicon is None:
        return {"major": [], "major_names": [], "homophone": []}
    if homophone_fix is not None:
        mids, fixes = homophone_fix.detect_major_tolerant(text)
    else:
        mids, fixes = major_lexicon.detect_major(text), []
    return {"major": mids,
            "major_names": [major_lexicon.MAJOR_RULE_NAMES.get(m, m) for m in mids],
            "homophone": [list(f) for f in fixes]}


load_corrections()
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".ts", ".m4v",
               ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

# 任务表：task_id -> {"path":..., "name":..., "duration":..., "created":...}
_TASKS: dict = {}

# 上传文件的保留时长：前端 finish() 会主动 DELETE，但浏览器崩了/直接关标签页就不会发，
# 残留文件会一直占着 /tmp。超过这个秒数的一律回收。
TASK_TTL_SEC = int(os.environ.get("TRANSCRIBE_TASK_TTL", "7200"))


def cleanup_stale(ttl: int = None) -> int:
    """回收超时的上传文件 + 任务记录，返回删掉的文件数。

    进程启动时扫一次（清掉上次跑崩留下的），每次 upload 前也扫一次（无需定时器）。
    """
    ttl = TASK_TTL_SEC if ttl is None else ttl
    now = time.time()
    removed = 0
    for tid, task in list(_TASKS.items()):
        if now - float(task.get("created") or 0) > ttl:
            try:
                if os.path.exists(task["path"]):
                    os.remove(task["path"]); removed += 1
            except Exception:
                pass
            _TASKS.pop(tid, None)
    # 目录里可能还有没记在 _TASKS 里的孤儿（上次进程留下的）
    try:
        for name in os.listdir(WORK_DIR):
            fp = os.path.join(WORK_DIR, name)
            try:
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > ttl:
                    os.remove(fp); removed += 1
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return removed

# whisper-cli 的行格式：[00:00:03.200 --> 00:00:08.100]  文本
_SEG_RE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})\]\s*(.*)$")
_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")


def _hms_to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _srt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class SentenceAssembler:
    """把 whisper 的字级 token 流组装成短句，每条带真实起止秒。

    断句依据：句末标点（。！？…）+ 句中标点（，、；：）。逗号也断，是为了让长句
    拆成一条条短句、每条各自对应一个时间点，便于逐句定位和跳播；断出来的短句保留
    末尾符号。句中标点要攒够 SOFT_CUT_LEN 字才断，避免"对，""是的，"这种碎片。

    为什么要走字级（-ml 1）：whisper 默认分段的时间戳只在段边界上，段内按逗号拆出
    的短句只能共用段末时间，跳播会偏。字级模式下标点自己也带时间戳，每个短句的起止
    秒都是 whisper 给的真实值，不做插值。
    """

    def __init__(self, emit, force_cut: int = FORCE_CUT_LEN, duration: float = 0.0,
                 prompt: str = ""):
        self._emit = emit
        self._force = force_cut
        self._dur = duration or 0.0   # 用于夹住结尾残句的时间上界
        self._buf = ""          # 当前在攒的短句
        self._start = None      # 当前短句的起始秒（第一个字的开始时间）
        self._last_end = 0.0    # 最近一个 token 的结束时间
        self._pkeys = _prompt_echo_keys(prompt)   # 识别"复读提示词"
        self._recent = []       # 最近吐出的短句，用于压掉连续重复
        self._dropped = 0       # 丢弃的幻觉/重复条数（回传给前端提示）

    def feed(self, tok_start: float, tok_end: float, text: str):
        """喂入一个 token（-ml 1 下通常是单字/单词；给整段也能正常处理）。"""
        text = normalize_punct((text or "").strip())
        if not text:
            return
        # 距上一个字有明显停顿 → 说话人换句了，先把攒着的吐出去。
        # 这条独立于标点：模型不给标点时，它就是唯一可靠的句子边界信号。
        if (self._start is not None and self._buf
                and tok_start - self._last_end >= GAP_CUT_SEC
                and len(self._buf.strip()) >= GAP_MIN_LEN):
            self._flush(self._last_end)
        if self._start is None:
            self._start = tok_start
        self._last_end = tok_end

        # 一个 token 里可能含多个标点，逐字符处理
        for ch in text:
            self._buf += ch
            if ch in _SENT_END:
                self._flush(tok_end)
            elif ch in _SOFT_END and len(self._buf.strip()) >= SOFT_CUT_LEN:
                self._flush(tok_end)
            elif len(self._buf) >= self._force:
                self._flush(tok_end)          # 连标点都不给时的最后兜底

    def _flush(self, end: float):
        s = self._buf.strip()
        self._buf = ""
        start = self._start
        self._start = None
        if not s or _PUNCT_ONLY.match(s):
            return
        if start is None:
            start = max(0.0, end - 1.0)
        if self._dur > 0:
            end = min(end, self._dur)          # 别超出媒体总时长
            start = min(start, end)
        # 无人声段的幻觉（复读提示词 / 字幕组残留）直接丢
        if is_hallucination(s, self._pkeys):
            self._dropped += 1
            return
        # 连续重复：whisper 在静音或长音乐段会把同一句反复吐出来，只留第一条
        norm = re.sub(r"[\s，,。；;！!？?、]", "", s)
        if norm and norm in self._recent[-3:]:
            self._dropped += 1
            return
        if norm:
            self._recent.append(norm)
            if len(self._recent) > 8:
                self._recent.pop(0)
        fixed = apply_corrections(s)
        ev = dict(type="segment", start=round(start, 2),
                  end=round(max(end, start + 0.1), 2), text=fixed)
        if fixed != s:
            ev["raw"] = s                      # 前端据此标记"已纠正"
        # 轨 A：这一句的重大风险红线，随字幕一起推，前端立刻能标红
        risk = detect_sentence_risk(fixed)
        if risk["major"]:
            ev["major"] = risk["major"]
            ev["major_names"] = risk["major_names"]
        if risk["homophone"]:
            ev["homophone"] = risk["homophone"]
        self._emit(**ev)

    def close(self):
        """全片结束：把最后一句没有句末标点的残句也吐出来。"""
        if self._buf.strip():
            self._flush(self._dur or self._last_end)
        if self._dropped:
            # 告诉前端丢了多少条幻觉，否则"识别完成但没几句字"看着像坏了
            self._emit(type="filtered", count=self._dropped)


def _probe_duration(path: str) -> float:
    """取媒体时长，失败返回 0（前端进度条据此估算）。"""
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return round(float(out.stdout.strip()), 3)
    except Exception:
        return 0.0


def _env_check() -> dict:
    """自检依赖是否就位，前端开页时提示缺什么。"""
    return {
        "ffmpeg":  bool(shutil.which(FFMPEG_BIN) or os.path.exists(FFMPEG_BIN)),
        "whisper": bool(shutil.which(WHISPER_BIN) or os.path.exists(WHISPER_BIN)),
        "model":   os.path.exists(WHISPER_MODEL),
        "model_path": WHISPER_MODEL,
    }


@router.get("/api/transcribe/health")
def transcribe_health():
    env = _env_check()
    env["ok"] = all([env["ffmpeg"], env["whisper"], env["model"]])
    missing = []
    if not env["ffmpeg"]:
        missing.append("ffmpeg（brew install ffmpeg）")
    if not env["whisper"]:
        missing.append("whisper-cli（brew install whisper-cpp）")
    if not env["model"]:
        missing.append(f"模型文件 {WHISPER_MODEL}")
    env["missing"] = missing
    return env


@router.post("/api/transcribe/upload")
async def transcribe_upload(file: UploadFile = File(...)):
    """接收上传，落盘到临时目录，返回 task_id。真正的解析在 /stream 里做。"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的格式 {ext or '（无扩展名）'}")

    cleanup_stale()          # 顺手回收上次没删掉的残留，避免 /tmp 越堆越大
    task_id = uuid.uuid4().hex[:16]
    dst = os.path.join(WORK_DIR, f"{task_id}{ext}")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(dst, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)     # 1MB 一块，避免大文件占满内存
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    f.close(); os.remove(dst)
                    raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
                f.write(chunk)
    finally:
        await file.close()

    duration = _probe_duration(dst)
    if duration <= 0:
        os.remove(dst)
        raise HTTPException(400, "无法解析该文件的媒体信息，可能已损坏或不是音视频")

    _TASKS[task_id] = {"path": dst, "name": file.filename or task_id,
                       "duration": duration, "size": size, "created": time.time()}
    return {"task_id": task_id, "name": file.filename,
            "duration": duration, "size": size}


def run_whisper(wav: str, prompt: str, timeout: int = 3600) -> str:
    """跑一次 whisper 返回 stdout。带幻觉自动重试：

    实测提示词本身会在某些素材上**诱发**幻觉 —— 同一段音频带提示词输出
    「Zither Harp」或「字幕志愿者杨茜茜」无限复读，去掉提示词却能正常识别出
    中文台词。所以首轮若判定为整体幻觉，就去掉提示词重跑一次。
    代价是这一轮没有"加标点"要求，断句退化到靠静音间隔 —— 但有内容总比全丢好。
    """
    def _run(p: str) -> str:
        cmd = [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-l", "zh", "-t", "4", "-ml", "1"]
        if p:
            cmd[6:6] = ["--prompt", p]
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout

    out = _run(prompt)
    if prompt and _looks_hallucinated(out, prompt):
        print("[transcribe] 首轮输出疑为幻觉，去掉提示词重试")
        alt = _run("")
        if not _looks_hallucinated(alt, ""):
            return alt
    return out


def _looks_hallucinated(out: str, prompt: str) -> bool:
    """整段输出是否基本都是幻觉（用于决定要不要去掉提示词重试）。"""
    texts = []
    for ln in (out or "").splitlines():
        m = _SEG_RE.match(ln.strip())
        if m:
            t = (m.groups()[8] or "").strip()
            if t:
                texts.append(t)
    joined = "".join(texts)
    # 有音频却几乎没吐出内容，也是提示词把模型带跑了的典型表现
    # （实测带提示词时整段只输出一个不带时间戳的 "Zither Harp"）
    if len(joined) < 8:
        return True
    pk = _prompt_echo_keys(prompt)
    bad = sum(1 for t in texts if is_hallucination(t, pk))
    # 单一片段高度重复（如"字幕志愿者XX"刷满全片）也算
    uniq = len(set(texts))
    return bad / len(texts) >= 0.8 or uniq <= max(2, len(texts) // 20)


def _worker(task: dict, ev_q: queue.Queue, stop: threading.Event, prompt: str):
    """后台线程：抽音频 → 跑 whisper → 逐行解析 stdout 塞进队列。"""
    def emit(**ev):
        ev_q.put(ev)

    src = task["path"]
    dur = float(task.get("duration") or 0)     # 算进度用
    wav = os.path.splitext(src)[0] + ".16k.wav"
    try:
        # ① 抽音频：-vn 去视频，-ac 1 单声道，-ar 16000 重采样（whisper 原生输入格式）
        #    用 Popen + 轮询而不是 subprocess.run：run 是阻塞的，抽长视频时用户
        #    关掉页面也杀不掉 ffmpeg，进程会一直占着 CPU 跑到 timeout。
        emit(type="stage", stage="extract", text="正在抽取音频…")
        ff = subprocess.Popen(
            [FFMPEG_BIN, "-y", "-v", "error", "-i", src,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 1800
        while True:
            try:
                ff.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if stop.is_set():
                ff.kill(); ff.wait()
                return
            if time.time() > deadline:
                ff.kill(); ff.wait()
                emit(type="error", text="音频抽取超时")
                return
        if ff.returncode != 0 or not os.path.exists(wav):
            err = ""
            try:
                err = (ff.stderr.read() or "")[:400]
            except Exception:
                pass
            emit(type="error", text=f"音频抽取失败：{err}")
            return
        emit(type="stage", stage="transcribe",
             text=f"音频就绪（{os.path.getsize(wav) // 1024 // 1024}MB），开始识别…")

        if stop.is_set():
            return

        # ② 跑 whisper：不加 -otxt/-osrt，纯靠 stdout 逐行捕获，实现边跑边出字。
        #    用 -ml 1 拿字级时间戳（标点自己也带时间），再由 SentenceAssembler 按
        #    标点组成短句。默认分段模式下时间戳只在段边界上，段内按逗号拆出的短句
        #    只能共用段末时间、跳播会偏；字级模式下每条短句的起止秒都是真实值。
        #    仍然不加 -pp：它会把多句合并成大段、时间戳退化成整秒。
        cmd = [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav,
               "-l", "zh", "--prompt", prompt, "-t", "4", "-ml", "1"]
        asm = SentenceAssembler(emit, duration=dur, prompt=prompt)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, encoding="utf-8", errors="replace")
        try:
            last_pct = -1
            for line in proc.stdout:
                if stop.is_set():
                    proc.terminate()
                    return
                line = line.rstrip("\n")

                m = _SEG_RE.match(line.strip())
                if m:
                    g = m.groups()
                    end = _hms_to_sec(*g[4:8])
                    asm.feed(_hms_to_sec(*g[0:4]), end, g[8] or "")
                    # 没有 -pp 就没有 progress 输出，用解析位置 / 总时长算进度
                    # （百分比没变就不重复推，省掉一串无意义的 SSE 事件）
                    if dur > 0:
                        pct = min(99, int(end / dur * 100))
                        if pct != last_pct:
                            last_pct = pct
                            emit(type="progress", percent=pct)
                    continue

                p = _PROGRESS_RE.search(line)
                if p:
                    emit(type="progress", percent=int(p.group(1)))
        finally:
            proc.stdout.close()
            code = proc.wait()

        if stop.is_set():
            return
        if code != 0:
            emit(type="error", text=f"whisper 退出码 {code}")
            return
        asm.close()          # 吐出最后一句（结尾常没有句末标点）
        emit(type="progress", percent=100)

    except subprocess.TimeoutExpired:
        emit(type="error", text="处理超时")
    except FileNotFoundError as e:
        emit(type="error", text=f"依赖缺失：{e}")
    except Exception as e:
        emit(type="error", text=str(e))
    finally:
        for p in (wav,):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        emit(type="done")


@router.get("/api/transcribe/stream")
async def transcribe_stream(request: Request, task_id: str, prompt: str = ""):
    """SSE：把后台线程解析出的每一句实时推给前端。"""
    task = _TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "task_id 不存在或已过期")
    env = _env_check()
    if not all([env["ffmpeg"], env["whisper"], env["model"]]):
        raise HTTPException(503, "转写依赖未就位，请先访问 /api/transcribe/health 查看")

    ev_q: queue.Queue = queue.Queue()
    stop = threading.Event()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(
        None, _worker, task, ev_q, stop, build_prompt(prompt))

    async def gen():
        head = {"type": "meta", "name": task["name"], "duration": task["duration"]}
        yield f"data: {json.dumps(head, ensure_ascii=False)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    stop.set(); return
                try:
                    ev = await asyncio.wait_for(
                        loop.run_in_executor(None, ev_q.get, True, 0.5), timeout=1.0)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") == "done":
                        break
                except (asyncio.TimeoutError, queue.Empty):
                    if fut.done() and ev_q.empty():
                        break
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
        finally:
            stop.set()

    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no",           # 关掉反代缓冲，否则 SSE 会被攒住
                 "Access-Control-Allow-Origin": "*"})


class _ExportBody(dict):
    pass


@router.post("/api/transcribe/export")
async def transcribe_export(payload: dict):
    """前端把累积的 segments 传回来，换成 srt/txt 文本（不落盘，直接回内容）。"""
    segs = payload.get("segments") or []
    fmt = (payload.get("format") or "txt").lower()
    if fmt == "srt":
        lines = []
        for i, s in enumerate(segs, 1):
            lines.append(str(i))
            lines.append(f"{_srt_ts(float(s.get('start') or 0))} --> "
                         f"{_srt_ts(float(s.get('end') or 0))}")
            lines.append((s.get("text") or "").strip())
            lines.append("")
        return JSONResponse({"format": "srt", "content": "\n".join(lines)})
    text = "\n".join((s.get("text") or "").strip()
                     for s in segs if (s.get("text") or "").strip())
    return JSONResponse({"format": "txt", "content": text})


@router.delete("/api/transcribe/task/{task_id}")
def transcribe_cleanup(task_id: str):
    """删掉上传的临时文件。"""
    task = _TASKS.pop(task_id, None)
    if not task:
        return {"ok": True, "removed": False}
    try:
        if os.path.exists(task["path"]):
            os.remove(task["path"])
    except Exception:
        pass
    return {"ok": True, "removed": True}


@router.get("/api/transcribe/corrections")
def transcribe_corrections():
    """查看当前生效的纠正表。"""
    return {"count": len(_corr_map), "file": CORRECTIONS_FILE,
            "corrections": _corr_map}


@router.post("/api/transcribe/corrections/reload")
def transcribe_corrections_reload():
    """改完 JSON 不用重启后端，调这个热加载。"""
    n = load_corrections()
    return {"ok": True, "count": n, "file": CORRECTIONS_FILE}


# ── 独立启动用的 app（挂进 main.py 时不会执行到这里的路由，用的是上面的 router）──
# 独立跑只有转写能力：六维研判要靠 main.py 的 /analyze/transcript，语义索引也在那边。
app = FastAPI(title="视频转写 API", version="1.1")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(FRONTEND):
    app.mount("/ui", StaticFiles(directory=FRONTEND, html=True), name="frontend")


@app.on_event("startup")
def _standalone_startup():
    n = cleanup_stale()
    if n:
        print(f"[transcribe] 启动清理残留上传文件 {n} 个")


@app.get("/")
def root():
    return {"service": "视频转写 API", "ui": "/ui/transcribe.html",
            "health": "/api/transcribe/health",
            "note": "六维研判请用主服务 (main.py) 的 /analyze/transcript"}

