#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从分享链接下载视频，交给现有转写研判链路。

为什么需要这一步：短视频平台的文案靠 JS 动态渲染，还有反爬与登录墙，
服务端直接抓页面拿不到内容 —— 实测抖音链接抓取结果为空，却仍会走完
研判流程给出一个"看着正常"的等级，那是无源之水。所以改为下载视频本体，
走音视频转写这条已验证的链路。

支持：抖音、快手、B站、小红书等 yt-dlp 覆盖的站点。
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional

# 站点判定：仅用于给用户更准确的提示，下载能力由 yt-dlp 决定
SITE_PATTERNS = [
    (r"douyin\.com|iesdouyin\.com", "抖音"),
    (r"kuaishou\.com|gifshow\.com", "快手"),
    (r"bilibili\.com|b23\.tv", "B站"),
    (r"xiaohongshu\.com|xhslink\.com", "小红书"),
    (r"weibo\.com|weibo\.cn", "微博"),
    (r"youtube\.com|youtu\.be", "YouTube"),
]

# 抖音、B站等平台要求请求携带 cookie（实测报 "Fresh cookies are needed"），
# 否则直接拒绝。这里读取导出的 cookie 文件；文件不存在时照常尝试，
# 失败会给出"请下载后上传"的提示，不影响上传文件这条主路径。
COOKIE_FILE = os.environ.get("LINK_COOKIE_FILE",
                             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "dy_cookies.txt"))

MAX_DURATION = int(os.environ.get("LINK_MAX_DURATION", "1800"))   # 单个视频 30 分钟上限
MAX_SIZE_MB = int(os.environ.get("LINK_MAX_MB", "500"))
DL_TIMEOUT = int(os.environ.get("LINK_DL_TIMEOUT", "300"))


def detect_site(url: str) -> str:
    for pat, name in SITE_PATTERNS:
        if re.search(pat, url, re.I):
            return name
    return ""


def extract_url(text: str) -> Optional[str]:
    """从分享文案里抠出链接 —— 用户常直接粘贴整段分享文本。"""
    m = re.search(r"https?://[^\s，。、）\)】]+", text or "")
    return normalize_url(m.group(0)) if m else None


def normalize_url(url: str) -> str:
    """把站内各种入口链接归一成下载工具认得的标准形式。

    抖音在「精选」「推荐」等页面用弹窗展示视频，链接形如
    douyin.com/jingxuan?modal_id=123 —— 实测 yt-dlp 报 Unsupported URL，
    但换成 douyin.com/video/123 就能正常读取。
    """
    u = (url or "").strip()
    if not u:
        return u
    # 抖音：从 modal_id / vid 参数里取出视频 ID，拼成 /video/<id>
    if re.search(r"douyin\.com", u, re.I):
        m = re.search(r"[?&](?:modal_id|vid|aweme_id)=(\d{6,})", u)
        if m:
            return f"https://www.douyin.com/video/{m.group(1)}"
        m = re.search(r"douyin\.com/(?:video|note)/(\d{6,})", u)
        if m:
            return f"https://www.douyin.com/video/{m.group(1)}"
    return u


class DownloadError(Exception):
    """下载失败，message 面向用户，不含内部堆栈。"""


def download(url: str, work_dir: str = None) -> dict:
    """下载视频，返回 {path, title, duration, site}。失败抛 DownloadError。"""
    try:
        import yt_dlp
    except ImportError:
        raise DownloadError("服务端未安装视频下载组件，请联系管理员")

    url = normalize_url(url)
    site = detect_site(url) or "该平台"
    work_dir = work_dir or tempfile.mkdtemp(prefix="ys_link_")
    os.makedirs(work_dir, exist_ok=True)
    out_tmpl = os.path.join(work_dir, "%(id)s.%(ext)s")

    opts = {
        "outtmpl": out_tmpl,
        **({"cookiefile": COOKIE_FILE} if os.path.exists(COOKIE_FILE) else {}),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,          # 分享链接可能指向合集，只取当前一条
        "socket_timeout": 20,
        # CDN 节点偶发超时：重试并允许换源，比直接失败让用户重来体验好
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "retry_sleep_functions": {"http": lambda n: min(2 ** n, 8)},
        # 优先中等画质：转写只需要音轨，下 4K 纯属浪费带宽和时间
        "format": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        "merge_output_format": "mp4",
    }

    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            dur = float(info.get("duration") or 0)
            if dur > MAX_DURATION:
                raise DownloadError(
                    f"视频时长 {int(dur//60)} 分钟，超过 {MAX_DURATION//60} 分钟上限")
            ydl.download([url])
    except DownloadError:
        raise
    except Exception as e:
        msg = str(e)
        # 把 yt-dlp 的技术错误翻译成用户能理解的说法
        if "Unsupported URL" in msg:
            raise DownloadError(f"暂不支持从{site}获取视频，请下载后上传文件")
        if "Private" in msg or "login" in msg.lower() or "cookie" in msg.lower():
            raise DownloadError(f"该{site}内容需要登录才能访问，请下载后上传文件")
        if "Unable to download" in msg or "HTTP Error 4" in msg:
            raise DownloadError(f"{site}拒绝了下载请求，可能是链接失效或平台限制")
        raise DownloadError(f"从{site}获取视频失败，请改用上传文件")

    # 找出下载好的文件
    vid = (info or {}).get("id") or ""
    files = [f for f in os.listdir(work_dir) if f.startswith(vid)] if vid else os.listdir(work_dir)
    files = [f for f in files if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise DownloadError("视频下载未完成，请重试或改用上传文件")
    path = os.path.join(work_dir, sorted(files, key=lambda f:
                        os.path.getsize(os.path.join(work_dir, f)))[-1])

    size_mb = os.path.getsize(path) / 1048576
    if size_mb > MAX_SIZE_MB:
        os.remove(path)
        raise DownloadError(f"视频体积 {size_mb:.0f}MB，超过 {MAX_SIZE_MB}MB 上限")

    return {
        "path": path,
        "title": (info or {}).get("title") or "",
        "duration": float((info or {}).get("duration") or 0),
        "site": site,
        "size_mb": round(size_mb, 1),
    }


def probe(url: str) -> dict:
    """只取元信息不下载，供前端先展示标题时长、让用户确认。"""
    try:
        import yt_dlp
    except ImportError:
        raise DownloadError("服务端未安装视频下载组件，请联系管理员")
    url = normalize_url(url)
    try:
        popts = {"quiet": True, "no_warnings": True,
                 "noplaylist": True, "socket_timeout": 20}
        if os.path.exists(COOKIE_FILE):
            popts["cookiefile"] = COOKIE_FILE
        with yt_dlp.YoutubeDL(popts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        msg = str(e)
        site = detect_site(url) or "该平台"
        if "Unsupported URL" in msg:
            raise DownloadError(f"暂不支持从{site}获取视频，请下载后上传文件")
        raise DownloadError(f"无法读取{site}视频信息，请检查链接或改用上传文件")
    return {
        "title": info.get("title") or "",
        "duration": float(info.get("duration") or 0),
        "uploader": info.get("uploader") or "",
        "site": detect_site(url),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法：python link_fetch.py <视频链接>")
        raise SystemExit(1)
    u = extract_url(sys.argv[1]) or sys.argv[1]
    print(f"链接：{u}\n平台：{detect_site(u) or '未识别'}")
    try:
        meta = probe(u)
        print(f"标题：{meta['title']}\n时长：{meta['duration']:.0f} 秒\n作者：{meta['uploader']}")
        t = time.time()
        r = download(u)
        print(f"已下载：{r['path']}（{r['size_mb']}MB，{time.time()-t:.0f}s）")
    except DownloadError as e:
        print(f"失败：{e}")
