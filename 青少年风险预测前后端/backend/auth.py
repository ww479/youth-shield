#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""平台登录鉴权。

原先密码写在 login.html 的 JS 里，F12 即可看到，且直接访问
/ui/transcribe.html 能完全绕过登录。这里把校验移到后端：

  · 口令只以 salt + PBKDF2 摘要形式存放，明文不落盘、不出现在代码里
  · 登录成功签发 HttpOnly Cookie（JS 读不到），带签名防伪造
  · /ui 下除登录页与静态资源外，一律要求已登录，未登录跳登录页
  · 失败次数超限后按 IP 短暂锁定，防在线爆破

账号存放于 backend/users.json（已在 .gitignore 排除）：
  {"users": [{"name": "admin", "salt": "...", "hash": "...", "display": "管理员"}]}
用 `python auth.py add <用户名>` 交互式添加，口令不回显。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, Response

HERE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.environ.get("YS_USERS_FILE", os.path.join(HERE, "users.json"))

COOKIE = "ys_session"
TTL_REMEMBER = 14 * 24 * 3600      # 勾选「记住我」：14 天
TTL_SESSION = 12 * 3600            # 未勾选：12 小时
PBKDF2_ROUNDS = 260_000

# 登录失败限流：同一 IP 连续失败达上限后锁定一段时间
MAX_FAILS = 6
LOCK_SECS = 300
_fails: dict = {}                  # ip -> [失败次数, 最后一次失败时间]


# ────────────────────────── 口令摘要 ──────────────────────────

def hash_password(pw: str, salt: Optional[str] = None) -> tuple:
    """返回 (salt, hash)，均为 hex。"""
    s = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(s), PBKDF2_ROUNDS)
    return s, h.hex()


def _load_users() -> list:
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f).get("users") or []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[auth] users.json 读取失败：{e}")
        return []


def verify_user(name: str, pw: str) -> Optional[dict]:
    """校验用户名口令。用户不存在时也走一次哈希，避免响应时间泄露账号是否存在。"""
    users = _load_users()
    rec = next((u for u in users if u.get("name") == name), None)
    if rec is None:
        hash_password(pw, secrets.token_hex(16))
        return None
    _, h = hash_password(pw, rec["salt"])
    if hmac.compare_digest(h, rec.get("hash", "")):
        return {"name": rec["name"], "display": rec.get("display") or rec["name"]}
    return None


# ────────────────────────── 会话签名 ──────────────────────────

def _secret() -> bytes:
    """签名密钥。未配置 YS_SECRET 时按 users.json 内容派生，
    这样重启后已签发的 Cookie 依然有效，改口令则自动失效。"""
    env = os.environ.get("YS_SECRET")
    if env:
        return env.encode()
    try:
        with open(USERS_FILE, "rb") as f:
            base = f.read()
    except Exception:
        base = b"youth-shield-fallback"
    return hashlib.sha256(b"ys-session-v1" + base).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(name: str, ttl: int) -> str:
    exp = int(time.time()) + ttl
    body = f"{name}|{exp}"
    b64 = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{b64}.{_sign(body)}"


def read_token(tok: str) -> Optional[str]:
    """校验并返回用户名；签名不符或已过期返回 None。"""
    if not tok or "." not in tok:
        return None
    b64, sig = tok.rsplit(".", 1)
    try:
        body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        name, exp = body.rsplit("|", 1)
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    if int(exp) < time.time():
        return None
    return name


def current_user(request: Request) -> Optional[str]:
    return read_token(request.cookies.get(COOKIE, ""))


# ────────────────────────── 限流 ──────────────────────────

def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else
            (request.client.host if request.client else "?"))


def locked_for(request: Request) -> int:
    """剩余锁定秒数，0 表示未锁定。"""
    n, last = _fails.get(_ip(request), (0, 0.0))
    if n < MAX_FAILS:
        return 0
    left = int(LOCK_SECS - (time.time() - last))
    if left <= 0:
        _fails.pop(_ip(request), None)
        return 0
    return left


def note_fail(request: Request) -> None:
    ip = _ip(request)
    n, _ = _fails.get(ip, (0, 0.0))
    _fails[ip] = (n + 1, time.time())


def clear_fail(request: Request) -> None:
    _fails.pop(_ip(request), None)


# ────────────────────────── 路由注册 ──────────────────────────

# 未登录也可访问：登录页本身与其依赖的静态资源
PUBLIC_PREFIXES = ("/ui/login.html", "/ui/assets/")


def install(app) -> None:
    """在 FastAPI 应用上装载登录接口与 /ui 访问闸门。"""

    @app.post("/auth/login")
    async def login(request: Request, response: Response):
        left = locked_for(request)
        if left:
            raise HTTPException(429, f"尝试过于频繁，请 {left} 秒后再试")
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = (body.get("username") or "").strip()
        pw = body.get("password") or ""
        remember = bool(body.get("remember"))
        if not name or not pw:
            raise HTTPException(400, "请输入用户名和密码")

        user = verify_user(name, pw)
        if not user:
            note_fail(request)
            n, _ = _fails.get(_ip(request), (0, 0.0))
            left_try = max(0, MAX_FAILS - n)
            msg = "用户名或密码不正确"
            if left_try <= 2:
                msg += f"（还可尝试 {left_try} 次）"
            raise HTTPException(401, msg)

        clear_fail(request)
        ttl = TTL_REMEMBER if remember else TTL_SESSION
        response.set_cookie(
            COOKIE, make_token(user["name"], ttl),
            max_age=ttl if remember else None,   # 不勾记住我则为会话 Cookie
            httponly=True, samesite="lax", path="/",
        )
        return {"ok": True, "user": user["display"]}

    @app.post("/auth/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/auth/me")
    async def me(request: Request):
        name = current_user(request)
        if not name:
            return {"authed": False}
        rec = next((u for u in _load_users() if u.get("name") == name), None)
        return {"authed": True, "user": (rec or {}).get("display") or name}

    @app.middleware("http")
    async def gate(request: Request, call_next):
        """拦住 /ui 下的模块页面 —— 否则直接输入网址即可绕过登录。"""
        path = request.url.path
        if path.startswith("/ui") and not path.startswith(PUBLIC_PREFIXES):
            if not current_user(request):
                from fastapi.responses import RedirectResponse
                # 记下原始去向，登录后可跳回
                nxt = path.replace("/ui/", "", 1).replace(".html", "")
                return RedirectResponse(f"/ui/login.html#m={nxt}", status_code=302)
        resp = await call_next(request)
        # HTML 页面不缓存：改完前端刷新即生效，免去手动强刷。
        # 图片/字体等静态资源仍走浏览器默认缓存，不影响加载速度。
        if path.startswith("/ui") and path.endswith((".html", "/")):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
        return resp


# ────────────────────────── 命令行：管理账号 ──────────────────────────

def _cli() -> None:
    import getpass
    import sys

    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    def save(users):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": users}, f, ensure_ascii=False, indent=2)
        os.chmod(USERS_FILE, 0o600)

    if cmd == "add":
        if len(args) < 2:
            print("用法：python auth.py add <用户名> [显示名]"); return
        name = args[1]
        display = args[2] if len(args) > 2 else name
        users = _load_users()
        if any(u["name"] == name for u in users):
            print(f"用户 {name} 已存在，如需改密请用 passwd"); return
        pw = getpass.getpass("设置口令：")
        if len(pw) < 6:
            print("口令至少 6 位"); return
        if pw != getpass.getpass("再次输入："):
            print("两次输入不一致"); return
        s, h = hash_password(pw)
        users.append({"name": name, "display": display, "salt": s, "hash": h})
        save(users)
        print(f"已添加 {name}（{display}），共 {len(users)} 个账号")

    elif cmd == "passwd":
        if len(args) < 2:
            print("用法：python auth.py passwd <用户名>"); return
        users = _load_users()
        rec = next((u for u in users if u["name"] == args[1]), None)
        if not rec:
            print("用户不存在"); return
        pw = getpass.getpass("新口令：")
        if len(pw) < 6:
            print("口令至少 6 位"); return
        if pw != getpass.getpass("再次输入："):
            print("两次输入不一致"); return
        rec["salt"], rec["hash"] = hash_password(pw)
        save(users)
        print(f"已重置 {args[1]} 的口令（此前签发的登录状态将失效）")

    elif cmd == "del":
        if len(args) < 2:
            print("用法：python auth.py del <用户名>"); return
        users = [u for u in _load_users() if u["name"] != args[1]]
        save(users)
        print(f"已删除 {args[1]}，剩余 {len(users)} 个账号")

    else:
        users = _load_users()
        if not users:
            print(f"暂无账号（{USERS_FILE}）\n用 python auth.py add <用户名> 添加")
            return
        print(f"共 {len(users)} 个账号（{USERS_FILE}）：")
        for u in users:
            print(f"  {u['name']:<16} {u.get('display') or ''}")


if __name__ == "__main__":
    _cli()
