#!/usr/bin/env python3
"""AISessionManager（原 ClaudeSessionManager）的本地网页服务。

统一管理 Claude Code / Codex / DSH / WorkBuddy / ZCode 的会话：
    列出 / 详情 / 删除（回收站）/ 恢复 / 批量清理 / 占用统计 / Token 工作台

用法:
    python run.py                   # 启动并自动打开浏览器
    python -m session_manager web   # 同上（命令行入口）

只监听 127.0.0.1，不对外网开放。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from . import core as sm
    from . import pricing
    from . import providers
    from . import usage as usage_mod
    from .providers.base import bucket_total, human_size, session_json, short_path
except ImportError:  # 直接运行本文件
    import core as sm  # type: ignore
    import pricing  # type: ignore
    import providers  # type: ignore
    import usage as usage_mod  # type: ignore
    from providers.base import (bucket_total, human_size, session_json,  # type: ignore
                                short_path)


def _webui_root():
    """界面目录：PyInstaller 单文件包解包在 sys._MEIPASS，开发时就在本文件旁边。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for cand in (os.path.join(base, "webui"), base):
            if os.path.isfile(os.path.join(cand, "index.html")):
                return cand
    return os.path.join(HERE, "webui")


WEBUI = _webui_root()
UI_FILE = os.path.join(WEBUI, "index.html")
VENDOR = os.path.join(WEBUI, "vendor")
STATIC_TYPES = {".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml"}

ROOT = os.path.join(sm.HOME, ".claude")  # 兼容旧入口：只影响 Claude
_TOOLS_TTL = 4.0
_tools_cache = {"at": 0.0, "data": None}
_tools_lock = threading.Lock()
_clean_lock = threading.Lock()


def provider(key, root=None):
    try:
        p = providers.get_provider(key)
    except KeyError as e:
        raise ValueError(str(e))
    if p.key == "claude":
        if root:
            p._root = os.path.abspath(os.path.expanduser(root))
        elif not os.environ.get("AISM_CLAUDE_ROOT"):
            p._root = ROOT
    return p


def _installed_providers():
    out = []
    for key in providers.keys():
        p = provider(key)
        if p.available():
            out.append(p)
    return out


# ---------- 汇总 ----------

def tools_json(root=None, scan=False):
    if scan:
        providers.all_providers(force=True)
        invalidate_tools()
    with _tools_lock:
        if _tools_cache["data"] and time.time() - _tools_cache["at"] < _TOOLS_TTL:
            return _tools_cache["data"]
    cache = usage_mod.load_cache()
    items = []
    for key in providers.keys():
        p = provider(key, root)
        installed = p.available()
        row = {
            "key": p.key, "name": p.name, "color": p.color,
            "hint": p.hint, "root": p.root, "installed": installed,
            "kind": p.kind,
            "primary": p.key in providers.PRIMARY_KEYS,
            "readonly": bool(getattr(p, "readonly", False)),
            "via": getattr(p, "via", ""),
            "empty": bool(getattr(p, "empty", False)),
            "sessions": 0, "size": 0, "size_str": "0B",
            "tokens": None, "models": [],
        }
        if installed:
            try:
                n, size = p.quick_stats()
                row["sessions"] = int(n or 0)
                row["size"] = int(size or 0)
                row["size_str"] = human_size(size or 0)
            except Exception:  # noqa: BLE001
                pass
            try:
                qu = p.quick_usage(cache)
            except Exception:  # noqa: BLE001
                qu = {"total": 0, "models": {}}
            if qu["total"]:
                row["tokens"] = qu["totals"]
                row["models"] = [{"model": m, "tokens": t}
                                 for m, t in sorted(qu["models"].items(),
                                                    key=lambda kv: -kv[1])][:6]
        items.append(row)
    data = {
        "tools": items,
        "live_id": sm.current_sid(),
        "cache_dir": usage_mod.cache_dir(),
        "cache_file": usage_mod.cache_path(),
    }
    with _tools_lock:
        _tools_cache["at"] = time.time()
        _tools_cache["data"] = data
    return data


def invalidate_tools():
    with _tools_lock:
        _tools_cache["data"] = None


def filter_sessions(rows, q="", project="", untitled=False):
    q = (q or "").strip().lower()
    out = []
    for s in rows:
        title = s.get("title") or ""
        if untitled and title:
            continue
        if project:
            hay = "%s %s" % (s.get("project") or "", s.get("cwd") or "")
            if project.lower() not in hay.lower():
                continue
        if q:
            hay = " ".join([title, s.get("project") or "", s.get("cwd") or "",
                            s.get("id") or ""]).lower()
            if q not in hay:
                continue
        out.append(s)
    return out


def sort_sessions(rows, sort="time"):
    if sort == "size":
        rows.sort(key=lambda s: (-(s.get("size") or 0), -(s.get("mtime") or 0)))
    elif sort == "tokens":
        rows.sort(key=lambda s: (-bucket_total(s.get("tokens") or {}),
                                 -(s.get("mtime") or 0)))
    elif sort == "title":
        rows.sort(key=lambda s: (s.get("title") or "").lower())
    elif sort == "project":
        rows.sort(key=lambda s: ((s.get("project") or "").lower(),
                                 -(s.get("mtime") or 0)))
    else:
        rows.sort(key=lambda s: (-(s.get("mtime") or 0), s.get("id") or ""))
    return rows


def sessions_json(key, root=None, q="", project="", untitled=False, sort="time"):
    p = provider(key, root)
    if not p.available():
        return {"tool": _tool_meta(p), "sessions": [], "total": 0,
                "total_size": 0, "total_size_str": "0B", "installed": False}
    rows = p.sessions()
    rows = filter_sessions(rows, q, project, untitled)
    sort_sessions(rows, sort)
    total_size = sum(s.get("size") or 0 for s in rows)
    total_tokens = sum(bucket_total(s.get("tokens") or {}) for s in rows)
    projects = sorted({(s.get("project") or "") for s in rows if s.get("project")})
    return {
        "tool": _tool_meta(p),
        "installed": True,
        "total": len(rows),
        "total_size": total_size,
        "total_size_str": human_size(total_size),
        "total_tokens": total_tokens,
        "projects": projects,
        "sessions": [session_json(s) for s in rows],
        "live_id": p.live_id(),
    }


def _tool_meta(p):
    return {"key": p.key, "name": p.name, "color": p.color,
            "root": p.root, "installed": p.available(), "kind": p.kind,
            "primary": p.key in providers.PRIMARY_KEYS,
            "readonly": bool(getattr(p, "readonly", False)),
            "empty": bool(getattr(p, "empty", False)),
            "via": getattr(p, "via", "")}


def show_json(key, sid, root=None):
    p = provider(key, root)
    s = p.get(sid)
    if not s:
        raise ValueError("未找到会话")
    d = session_json(s)
    try:
        info = p.detail(s) or {}
    except Exception as e:  # noqa: BLE001
        info = {"error": "%s: %s" % (type(e).__name__, e)}
    d.update(info)
    d["resume"] = p.resume_cmd(s)
    d["tool_name"] = p.name
    d["tool_color"] = p.color
    return d


def trash_json(key, root=None):
    p = provider(key, root)
    items = p.trash_list() if p.available() else []
    total = sum(i.get("size") or 0 for i in items)
    return {"tool": _tool_meta(p), "items": items,
            "total_size": total, "total_size_str": human_size(total)}


def stats_json(key, root=None):
    p = provider(key, root)
    if not p.available():
        return {"tool": _tool_meta(p), "groups": [], "total": 0, "total_size": 0,
                "total_size_str": "0B", "trash_count": 0, "trash_size_str": "0B"}
    rows = p.sessions()
    groups = {}
    for s in rows:
        groups.setdefault(s.get("project") or "(未知项目)", []).append(s)
    items = []
    for name, lst in sorted(groups.items(),
                            key=lambda kv: -sum(x.get("size") or 0 for x in kv[1])):
        size = sum(x.get("size") or 0 for x in lst)
        latest = max((x.get("mtime") or 0) for x in lst)
        items.append({
            "project": name, "project_short": short_path(name, 3),
            "count": len(lst), "size": size, "size_str": human_size(size),
            "tokens": sum(bucket_total(x.get("tokens") or {}) for x in lst),
            "latest": latest,
            "latest_str": sm.fmt_mtime(latest) if latest else "",
            "live": any(x.get("live") for x in lst),
        })
    trash = p.trash_list() if p.available() else []
    total = sum(r.get("size") or 0 for r in rows)
    return {
        "tool": _tool_meta(p), "groups": items,
        "total": len(rows), "total_size": total,
        "total_size_str": human_size(total),
        "trash_count": len(trash),
        "trash_size_str": human_size(sum(t.get("size") or 0 for t in trash)),
    }


def clean_candidates(key, body, root=None):
    p = provider(key, root)
    rows = p.sessions() if p.available() else []
    days = float(body.get("older_days") or 0)
    bigger_mb = float(body.get("bigger_mb") or 0)
    keepn = int(body.get("keep_last") or 0)
    project = body.get("project") or ""
    if not (days or bigger_mb or keepn):
        raise ValueError("至少设置一个条件（天数 / 大小 / 每项目保留数）")
    if project:
        rows = [s for s in rows if project.lower() in (s.get("project") or "").lower()]
    now = time.time()
    protected = {p.live_id()}
    if keepn:
        byproj = {}
        for s in rows:
            byproj.setdefault(s.get("project") or "", []).append(s)
        for lst in byproj.values():
            lst.sort(key=lambda s: -(s.get("mtime") or 0))
            for s in lst[:keepn]:
                protected.add(s["id"])
    cands = []
    for s in rows:
        if s["id"] in protected:
            continue
        if days and now - (s.get("mtime") or 0) < days * 86400:
            continue
        if bigger_mb and (s.get("size") or 0) < bigger_mb * 1024 * 1024:
            continue
        cands.append(s)
    return cands


def usage_json(root=None, force=False):
    return usage_mod.collect(_installed_providers(), force=force)


def prices_json():
    """「价目」页：内置价目 + 自定义价目 + 谷时窗口。"""
    cache_dir = usage_mod.cache_dir()
    cfg = pricing.load(cache_dir)
    overrides = cfg["models"]
    return {
        "file": pricing.prices_path(cache_dir),
        "peak": cfg["peak"],
        "cny_rate": pricing.cny_rate(),
        "overrides": overrides,
        "effective": {name: pricing.price_of(name, overrides) for name in overrides},
        "effective_cny": {name: pricing.price_cny(name, overrides) for name in overrides},
        "builtin": pricing.builtin_rows(),
        "fallback": dict(pricing.FALLBACK),
    }


def save_prices(body):
    if not isinstance(body, dict):
        raise ValueError("请求体不合法")
    models = body.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError("models 必须是对象")
    peak = body.get("peak")
    weekdays = body.get("peak_weekdays")
    path = pricing.save(usage_mod.cache_dir(), models, peak,
                        True if weekdays is None else bool(weekdays))
    invalidate_tools()
    return {"ok": True, "file": path, "peak": pricing.peak_key()}


# ---------- HTTP ----------

LAST_HIT = [0.0]  # 最后一次请求时间（客户端模式用来判断窗口是不是已经关了）
CLOSE_AT = [0.0]  # 页面离开时间（客户端窗口关闭时由页面发 /api/bye）
APP_MODE = [False]  # 是否以「客户端窗口」方式启动


class Handler(BaseHTTPRequestHandler):
    server_version = "AISessionManager/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默，避免刷屏；只记活跃时间
        if not self.path.startswith("/api/bye"):
            LAST_HIT[0] = time.time()

    # -- helpers --
    def _json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, text):
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, path, ctype):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 1_000_000:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _origin_ok(self):
        st = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if st and st not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.startswith(("http://127.0.0.1", "http://localhost",
                                  "http://[::1]"))

    # -- routes --
    def do_GET(self):
        if not self._local():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(UI_FILE, encoding="utf-8") as f:
                    return self._html(f.read())
            if u.path.startswith("/webui/vendor/"):
                name = u.path[len("/webui/vendor/"):]
                ext = os.path.splitext(name)[1].lower()
                if name and os.path.basename(name) == name and ext in STATIC_TYPES:
                    return self._static(os.path.join(VENDOR, name), STATIC_TYPES[ext])
                if name and os.path.basename(name) == name and ext in (".woff2", ".woff"):
                    return self._json({"error": "not found"}, 404)
                return self._json({"error": "not found"}, 404)
            if u.path == "/api/tools":
                return self._json(tools_json(scan=qs.get("scan") == "1"))
            if u.path == "/api/sessions":
                return self._json(sessions_json(
                    qs.get("tool") or "claude",
                    q=qs.get("q", ""), project=qs.get("project", ""),
                    untitled=qs.get("untitled") == "1",
                    sort=qs.get("sort", "time")))
            if u.path == "/api/trash":
                return self._json(trash_json(qs.get("tool") or "claude"))
            if u.path == "/api/stats":
                return self._json(stats_json(qs.get("tool") or "claude"))
            if u.path == "/api/show":
                return self._json(show_json(qs.get("tool") or "claude",
                                            qs.get("id", "")))
            if u.path == "/api/usage":
                return self._json(usage_json(force=qs.get("refresh") == "1"))
            if u.path == "/api/prices":
                return self._json(prices_json())
            return self._json({"error": "not found"}, 404)
        except (ValueError, sm.UserError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        if not self._local() or not self._origin_ok():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/bye":
                # 客户端窗口关闭时页面发的信标；只认客户端窗口自己的页面
                q = parse_qs(u.query)
                if APP_MODE[0] and (q.get("app") or [""])[-1] == "1":
                    CLOSE_AT[0] = time.time()
                return self._json({"ok": True})
            if u.path == "/api/prices":
                return self._json(save_prices(body))
            key = body.get("tool") or "claude"
            p = provider(key)
            if u.path == "/api/delete":
                ids = body.get("ids") or []
                if not ids:
                    raise ValueError("没有选择会话")
                res = p.delete(ids, purge=bool(body.get("purge")),
                               force=bool(body.get("force")))
                invalidate_tools()
                return self._json(res)
            if u.path == "/api/restore":
                tokens = body.get("tokens") or body.get("names") or []
                if not tokens:
                    raise ValueError("没有选择要恢复的条目")
                res = p.trash_restore(tokens)
                invalidate_tools()
                return self._json(res)
            if u.path == "/api/empty-trash":
                res = p.trash_empty()
                invalidate_tools()
                return self._json(res)
            if u.path == "/api/clean":
                with _clean_lock:
                    cands = clean_candidates(key, body)
                    if body.get("dry_run"):
                        total = sum(s.get("size") or 0 for s in cands)
                        return self._json({
                            "dry_run": True, "count": len(cands),
                            "total_size": total,
                            "total_size_str": human_size(total),
                            "sessions": [session_json(s) for s in cands],
                        })
                    if not cands:
                        return self._json({"deleted": [], "freed": 0,
                                           "freed_str": "0B", "errors": [],
                                           "skipped_live": [], "count": 0})
                    res = p.delete([s["id"] for s in cands],
                                   purge=bool(body.get("purge")))
                    res["count"] = len(cands)
                    invalidate_tools()
                    return self._json(res)
            if u.path == "/api/usage/refresh":
                return self._json(usage_json(force=True))
            return self._json({"error": "not found"}, 404)
        except (ValueError, sm.UserError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


def _say(msg):
    """窗口版 exe 没有 stdout，打印要能安静跳过。"""
    try:
        print(msg)
    except Exception:  # noqa: BLE001
        pass


def _chromium():
    """找支持 --app 的 Chromium 内核浏览器（Edge / Chrome 都行）。"""
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands.append(os.path.join(base, "Microsoft", "Edge", "Application",
                                      "msedge.exe"))
            cands.append(os.path.join(base, "Google", "Chrome", "Application",
                                      "chrome.exe"))
    elif sys.platform == "darwin":
        cands += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for name in ("msedge", "chrome", "chromium", "google-chrome",
                 "microsoft-edge", "chromium-browser"):
        try:
            found = shutil.which(name)
        except Exception:  # noqa: BLE001
            found = None
        if found:
            cands.append(found)
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def open_app_window(url, width=1280, height=860):
    """用浏览器的 --app 模式开一个没有地址栏/标签栏的窗口，像桌面客户端。
    找不到 Chromium 内核浏览器时返回 None。"""
    exe = _chromium()
    if not exe:
        return None
    tag = url.rsplit(":", 1)[-1]
    profile = os.path.join(usage_mod.cache_dir(), "app-profile-%s" % tag)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        profile = tempfile.mkdtemp(prefix="aism-app-")
    # 这个窗口只渲染本地一个静态页面，用不到 GPU 加速、扩展、同步、
    # 组件更新、崩溃上报和 Edge 的后台服务；关掉它们能把独立 profile
    # 拉起的浏览器进程树从 ~330MB 压到 ~185MB 私有内存（实测）。
    # 如果哪天窗口出现黑屏/排版异常，先去掉 --disable-gpu 再排查。
    lean_flags = [
        "--disable-gpu",
        "--disable-extensions",
        "--disable-sync",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--no-service-autorun",
        "--metrics-recording-only",
        "--disable-features=msEdgeSearchIndexer,msEdgeSidebarV2,Translate,"
        "OptimizationHints,MediaRouter",
    ]
    args = [
        exe, "--app=" + url + "?app=1",
        "--user-data-dir=" + profile,
        "--window-size=%d,%d" % (width, height),
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-mode",
    ] + lean_flags
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def serve(root=None, port=8765, open_browser=True, app_window=False,
          idle_exit=1800):
    global ROOT
    if root:
        ROOT = os.path.abspath(os.path.expanduser(root))
    sm.setup_stdio()
    if not os.path.isfile(UI_FILE):
        _say("缺少界面文件: %s" % UI_FILE)
        return 2
    httpd = None
    last_err = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError as e:
            last_err = e
    if not httpd:
        _say("端口都被占用: %s" % last_err)
        return 1
    url = "http://127.0.0.1:%d" % port
    _say("会话管理页面: %s" % url)
    _say("Claude 数据: %s" % ROOT)
    _say("缓存目录:   %s" % usage_mod.cache_dir())
    _say("按 Ctrl+C 退出（直接关闭这个窗口也可以）")

    proc = None
    if app_window:
        APP_MODE[0] = True
        proc = open_app_window(url)
        if proc is None:
            # 没有 Edge / Chrome：退回默认浏览器
            open_browser = True
        else:
            open_browser = False  # 已经有客户端窗口了，别再开一个浏览器

        # 页面关闭时会发 /api/bye；3 秒内没有新请求才真的退出（刷新页面不算关闭）
        def _watch_close():
            while True:
                time.sleep(1)
                t = CLOSE_AT[0]
                if t and time.time() - t > 3 and LAST_HIT[0] <= t:
                    try:
                        httpd.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                    return
        threading.Thread(target=_watch_close, daemon=True).start()

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    if proc is not None:
        # 窗口一关，浏览器进程就退出 → 服务跟着停，不留后台进程
        def _watch_window():
            time.sleep(2.5)
            # Edge 已经在运行时，新窗口会交给已有进程，Popen 会立刻退出：
            # 这种情况交给上面的 /api/bye 判断，别据此关服务
            if proc.poll() is not None:
                return
            try:
                proc.wait()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_watch_window, daemon=True).start()
    elif app_window and idle_exit:
        # 退回默认浏览器时感知不到窗口关闭：长时间没人访问就自己退出
        LAST_HIT[0] = time.time()

        def _watch_idle():
            while True:
                time.sleep(60)
                if time.time() - LAST_HIT[0] > idle_exit:
                    try:
                        httpd.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                    return
        threading.Thread(target=_watch_idle, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _say("\n已退出")
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="session_web", description="AI 会话管理器网页界面（本地）")
    ap.add_argument("--root", default=None,
                    help="覆盖默认根目录 ~/.claude（测试用）")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--app", action="store_true",
                    help="用独立客户端窗口打开（Chromium 的 --app 模式）")
    a = ap.parse_args(argv)
    return serve(a.root, a.port, not a.no_browser, app_window=a.app)


if __name__ == "__main__":
    sys.exit(main())
