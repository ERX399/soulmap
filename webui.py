"""
SoulMap 独立 WebUI 后端 - aiohttp 版
参考 astrbot_plugin_stealer 早期独立 WebUI 思路：
- 独立端口服务，不嵌入 AstrBot Dashboard
- aiohttp AppRunner + TCPSite
- 显式 start/stop 生命周期
- 独立登录页 + HttpOnly Cookie Session
"""
import argparse
import asyncio
import json
import os
import re
import re
import secrets
import signal
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from aiohttp import web

DATA_DIR = Path(os.environ.get("SOULMAP_DATA_DIR", "./data"))
PROFILES_FILE = DATA_DIR / "user_profiles.json"
PLATFORM_FILE = DATA_DIR / "user_platform.json"
CACHE_FILE = DATA_DIR / "user_profiles.cache.json"
HOST = os.environ.get("SOULMAP_HOST", "0.0.0.0")
DEFAULT_PORT = 3999
WEBUI_DEBUG = os.environ.get("SOULMAP_DEBUG", "0").lower() in ("1", "true", "yes", "debug")
WEBUI_TOKEN = os.environ.get("SOULMAP_WEBUI_TOKEN", "").strip()

DEFAULT_FIELDS = [
    "对用户的称呼", "性别", "年龄", "所在地", "生日", "爱吃", "忌口",
    "爱好", "职业", "重要节日", "恐惧/弱点", "作息规律", "技能水平",
    "健康状况", "宠物", "备注"
]


def log(level: str, msg: str):
    if level == "DEBUG" and not WEBUI_DEBUG:
        return
    print(f"[SoulMap WebUI][{level}] {datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def parse_port(value, default=DEFAULT_PORT) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _find_port_pids(port: int) -> list[int]:
    pids = set()
    commands = [
        ["fuser", f"{port}/tcp"],
        ["lsof", "-ti", f":{port}"],
        ["sh", "-c", f"ss -ltnp 'sport = :{port}' 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2"],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        output = f"{result.stdout} {result.stderr}"
        for item in output.replace(",", " ").split():
            if item.isdigit():
                pid = int(item)
                if pid != os.getpid():
                    pids.add(pid)
        if pids:
            break
    return sorted(pids)


def release_port(port: int, timeout: float = 3.0) -> list[int]:
    pids = _find_port_pids(port)
    if not pids:
        return []
    log("WARNING", f"检测到端口 {port} 被进程占用，尝试释放: pids={pids}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            log("DEBUG", f"无权限结束进程 {pid}，可能不是本插件进程，继续尝试启动: {e}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _find_port_pids(port):
            log("INFO", f"端口 {port} 已释放")
            return pids
        time.sleep(0.1)
    for pid in _find_port_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            log("DEBUG", f"无权限强制结束进程 {pid}，可能不是本插件进程，继续尝试启动: {e}")
    time.sleep(0.2)
    return pids


def _load_json_file(path: Path) -> dict:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log("ERROR", f"读取JSON失败: {path}, error={e}")
        if WEBUI_DEBUG:
            traceback.print_exc()
        return {}


def _write_json_file(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _save_cache(data: dict):
    if data:
        try:
            _write_json_file(CACHE_FILE, data)
        except OSError as e:
            log("WARNING", f"缓存写入失败: {CACHE_FILE}, error={e}")


class ProfileCache:
    def __init__(self):
        self._data = {}
        self._mtime = None
        self._sorted_keys = []

    def get_data(self) -> dict:
        source_path = PROFILES_FILE if PROFILES_FILE.exists() else CACHE_FILE
        if not source_path.exists():
            self._data = {}
            self._mtime = None
            self._sorted_keys = []
            return self._data
        try:
            current_mtime = source_path.stat().st_mtime
        except OSError:
            return self._data
        if current_mtime != self._mtime:
            self._reload()
            self._mtime = current_mtime
        return self._data

    def get_sorted_keys(self) -> list:
        self.get_data()
        return self._sorted_keys

    def invalidate(self):
        self._mtime = None

    def _reload(self):
        data = _load_json_file(PROFILES_FILE)
        if data:
            _save_cache(data)
        elif not PROFILES_FILE.exists():
            data = _load_json_file(CACHE_FILE)
        self._data = data
        self._sorted_keys = sort_profile_keys(list(self._data.keys()), self._data)


cache = ProfileCache()


def save_profiles(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_file(PROFILES_FILE, data)
    _save_cache(data)
    cache.invalidate()


def load_platforms() -> dict:
    return _load_json_file(PLATFORM_FILE)


def merge_platforms(profiles: dict, keys=None) -> dict:
    """合并平台信息到画像，只对含平台的用户创建浅拷贝，减少内存分配。"""
    platforms = load_platforms()
    if not platforms:
        if keys is not None:
            return {k: profiles[k] for k in keys if k in profiles}
        return profiles
    source_keys = list(keys) if keys is not None else list((profiles or {}).keys())
    merged = {}
    for key in source_keys:
        profile = profiles.get(key)
        if profile is None:
            continue
        platform_info = platforms.get(key)
        platform_name = None
        if isinstance(platform_info, dict):
            platform_name = platform_info.get("platform")
        elif isinstance(platform_info, str):
            platform_name = platform_info
        if platform_name and isinstance(profile, dict):
            item = dict(profile)
            item["_platform"] = platform_name
            merged[key] = item
        else:
            merged[key] = profile
    return merged


def get_all_fields(profiles: dict) -> list:
    fields = list(DEFAULT_FIELDS)
    seen = set(fields)
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for field in profile:
            if field.startswith("_") or field in seen:
                continue
            fields.append(field)
            seen.add(field)
    return fields


def _safe_time_score(value: str) -> int:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    try:
        return int(digits[:14] or 0)
    except ValueError:
        return 0


def _sort_text_rank(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 3
    ch = text[0]
    if ch.isalpha() and ch.isascii():
        return 0  # a-z 最前
    if "\u4e00" <= ch <= "\u9fff":
        return 1  # 中文次之
    if ch.isdigit():
        return 2  # 数字最后
    return 1


def _profile_display_name(key: str, profile: dict) -> str:
    if isinstance(profile, dict):
        name = str(profile.get("对用户的称呼") or "").strip()
        if name:
            return name
    return str(key or "")


def sort_profile_keys(keys: list, profiles: dict) -> list:
    return sorted(
        list(keys or []),
        key=lambda k: (
            _sort_text_rank(_profile_display_name(k, profiles.get(k, {}))),
            _profile_display_name(k, profiles.get(k, {})).lower(),
            0 if (isinstance(profiles.get(k), dict) and profiles[k].get("_platform")) else 1,
            -_safe_time_score(profiles.get(k, {}).get("_last_updated", "") if isinstance(profiles.get(k), dict) else ""),
        )
    )




class SoulMapWebServer:
    SESSION_COOKIE = "soulmap_webui_session"
    SESSION_TIMEOUT = 12 * 3600

    def __init__(self, data_dir: str, host="0.0.0.0", port=3999, plugin_dir=None,
                 debug=False, token="", release_occupied_port=True):
        self.data_dir = Path(data_dir)
        self.host = str(host)
        self.port = parse_port(port)
        self.plugin_dir = Path(plugin_dir) if plugin_dir else Path(__file__).parent
        self.debug = bool(debug)
        self.token = str(token or os.environ.get("SOULMAP_WEBUI_TOKEN", "")).strip()
        self.release_occupied_port = bool(release_occupied_port)
        self.sessions: dict[str, float] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.started = threading.Event()
        self.start_error: Optional[BaseException] = None
        self.app = web.Application(middlewares=[self._error_middleware, self._auth_middleware], client_max_size=20 * 1024 * 1024)
        self._setup_routes()

    def _setup_routes(self):
        r = self.app.router
        r.add_get("/", self.handle_index)
        r.add_get("/index.html", self.handle_index)
        r.add_get("/login.html", self.handle_login)
        r.add_get("/web/app.js", self.handle_js)
        r.add_get("/webui.js", self.handle_js)  # 兼容旧路径
        r.add_get("/auth/info", self.handle_auth_info)
        r.add_post("/auth/login", self.handle_auth_login)
        r.add_post("/auth/logout", self.handle_auth_logout)
        r.add_get("/api/profiles", self.handle_profiles)
        r.add_post("/api/batch-clean", self.handle_batch_clean)
        r.add_post("/api/batch-delete", self.handle_batch_delete)
        r.add_get("/api/stats", self.handle_stats)
        r.add_get("/api/debug", self.handle_debug)
        r.add_get(r"/api/profile/{user_key:.*}", self.handle_get_profile)
        r.add_put(r"/api/profile/{user_key:.*}", self.handle_put_profile)
        r.add_delete(r"/api/profile/{tail:.*}", self.handle_delete_profile)

    @web.middleware
    async def _error_middleware(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            log("ERROR", f"{request.method} {request.path} 处理失败: {e}")
            if WEBUI_DEBUG or self.debug:
                traceback.print_exc()
            return web.json_response({"error": str(e)}, status=500)

    def _is_authenticated(self, request) -> bool:
        if not self.token:
            return True
        sid = request.cookies.get(self.SESSION_COOKIE, "")
        exp = self.sessions.get(sid)
        if exp and exp > time.time():
            return True
        if sid:
            self.sessions.pop(sid, None)
        return False

    @web.middleware
    async def _auth_middleware(self, request, handler):
        path = request.path
        public = path in ("/login.html", "/auth/info", "/auth/login")
        static = path in ("/web/app.js", "/webui.js")
        if not self.token or public or static or self._is_authenticated(request):
            return await handler(request)
        if path.startswith("/api/") or path.startswith("/auth/"):
            return web.json_response({"error": "未授权，请先登录 WebUI"}, status=401)
        raise web.HTTPFound("/login.html")

    @staticmethod
    def _json(data: dict, status=200):
        return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False))

    async def _read_json(self, request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def handle_index(self, request):
        if self.token and not self._is_authenticated(request):
            raise web.HTTPFound("/login.html")
        path = self.plugin_dir / "web" / "index.html"
        content = path.read_text(encoding="utf-8").replace("__SOULMAP_WEBUI_DEBUG__", "true" if (WEBUI_DEBUG or self.debug) else "false")
        return web.Response(text=content, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"})

    async def handle_js(self, request):
        path = self.plugin_dir / "web" / "app.js"
        content = path.read_text(encoding="utf-8")
        return web.Response(text=content, content_type="application/javascript", charset="utf-8", headers={"Cache-Control": "no-store"})

    async def handle_login(self, request):
        if not self.token or self._is_authenticated(request):
            raise web.HTTPFound("/")
        path = self.plugin_dir / "web" / "login.html"
        return web.Response(
            text=path.read_text(encoding="utf-8"),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def handle_auth_info(self, request):
        return self._json({"requires_auth": bool(self.token), "authenticated": self._is_authenticated(request)})

    async def handle_auth_login(self, request):
        if not self.token:
            return self._json({"success": True, "requires_auth": False})
        body = await self._read_json(request)
        password = str(body.get("password") or "")
        if not secrets.compare_digest(password, self.token):
            return self._json({"success": False, "error": "Unauthorized"}, 401)
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = time.time() + self.SESSION_TIMEOUT
        resp = self._json({"success": True})
        resp.set_cookie(self.SESSION_COOKIE, sid, max_age=self.SESSION_TIMEOUT, httponly=True, samesite="Lax")
        return resp

    async def handle_auth_logout(self, request):
        sid = request.cookies.get(self.SESSION_COOKIE, "")
        if sid:
            self.sessions.pop(sid, None)
        resp = self._json({"success": True})
        resp.del_cookie(self.SESSION_COOKIE)
        return resp

    async def handle_profiles(self, request):
        params = request.rel_url.query
        page = max(1, int(params.get("page", "1") or 1))
        size = max(1, min(int(params.get("size", "20") or 20), 100))
        q = params.get("q", "").lower().strip()
        profiles = cache.get_data()
        fields = get_all_fields(profiles)
        keys = cache.get_sorted_keys()
        if q:
            keys = [k for k in keys if q in k.lower() or (isinstance(profiles.get(k), dict) and any(q in str(v).lower() for v in profiles[k].values()))]
        # 先合并平台数据，再排序（平台优先级需要 _platform 字段）
        profiles = merge_platforms(profiles, keys)
        keys = sort_profile_keys(keys, profiles)
        total = len(keys)
        if total <= 100:
            return self._json({"profiles": {k: profiles[k] for k in keys}, "fields": fields, "pagination": {"page": 1, "size": total, "total": total, "total_pages": 1}})
        total_pages = max(1, (total + size - 1) // size)
        page = max(1, min(page, total_pages))
        page_keys = keys[(page - 1) * size:(page - 1) * size + size]
        page_profiles = {k: profiles[k] for k in page_keys}
        return self._json({"profiles": page_profiles, "fields": fields, "pagination": {"page": page, "size": size, "total": total, "total_pages": total_pages}})

    async def handle_batch_delete(self, request):
        body = await self._read_json(request)
        keys = body.get("keys", [])
        if not isinstance(keys, list):
            return self._json({"error": "keys 必须是数组"}, 400)
        keys = [str(k).strip() for k in keys if str(k).strip()]
        if not keys:
            return self._json({"error": "请选择要删除的画像卡"}, 400)
        if len(keys) > 500:
            return self._json({"error": "一次最多删除 500 个画像"}, 400)

        profiles = cache.get_data()
        deleted = []
        for key in keys:
            if key in profiles:
                del profiles[key]
                deleted.append(key)

        if deleted:
            save_profiles(profiles)

        return self._json({
            "success": True,
            "deleted": len(deleted),
            "requested": len(keys),
            "message": f"已删除 {len(deleted)} 个画像卡"
        })

    async def handle_batch_clean(self, request):
        body = await self._read_json(request)
        keyword = str(body.get("keyword", "")).strip()
        if not keyword:
            return self._json({"error": "关键词不能为空"}, 400)
        if len(keyword) > 80:
            return self._json({"error": "关键词过长，请缩短后重试"}, 400)

        profiles = cache.get_data()
        affected_users = set()
        removed_fields = 0
        removed_notes = 0
        details = []

        for user_key, profile in list(profiles.items()):
            if not isinstance(profile, dict):
                continue
            changed = False

            for field in list(profile.keys()):
                if str(field).startswith("_"):
                    continue
                value = profile.get(field)
                if value is None:
                    continue
                text = str(value)

                if field == "备注":
                    notes = [n.strip() for n in re.split(r"[；;]", text) if n.strip()]
                    if not notes:
                        continue
                    kept = [n for n in notes if keyword not in n]
                    removed = len(notes) - len(kept)
                    if removed > 0:
                        removed_notes += removed
                        changed = True
                        affected_users.add(user_key)
                        details.append({"user": user_key, "field": field, "removed": removed})
                        if kept:
                            profile[field] = "；".join(kept)
                        else:
                            profile.pop(field, None)
                else:
                    if keyword in text:
                        profile.pop(field, None)
                        removed_fields += 1
                        changed = True
                        affected_users.add(user_key)
                        details.append({"user": user_key, "field": field, "removed": 1})

            if changed:
                profile["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if affected_users:
            save_profiles(profiles)

        return self._json({
            "success": True,
            "keyword": keyword,
            "affected_users": len(affected_users),
            "removed_fields": removed_fields,
            "removed_notes": removed_notes,
            "details": details[:50],
            "message": f"已清理包含「{keyword}」的词条：影响用户 {len(affected_users)}，删除字段 {removed_fields}，删除备注 {removed_notes} 条"
        })

    async def handle_stats(self, request):
        profiles = cache.get_data()
        fields = get_all_fields(profiles)
        user_count = len(profiles)
        field_counts = {}
        filled_total = 0
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            for field in profile:
                if not field.startswith("_"):
                    field_counts[field] = field_counts.get(field, 0) + 1
                    filled_total += 1
        platforms = load_platforms()
        return self._json({"user_count": user_count, "platform_count": len(platforms), "field_counts": field_counts, "fields": fields, "avg_fields": round(filled_total / user_count, 1) if user_count else 0})

    async def handle_debug(self, request):
        profiles = cache.get_data()
        return self._json({"data_dir": str(DATA_DIR.resolve()), "profiles_file": str(PROFILES_FILE.resolve()), "profiles_file_exists": PROFILES_FILE.exists(), "profiles_file_size": PROFILES_FILE.stat().st_size if PROFILES_FILE.exists() else 0, "platform_file": str(PLATFORM_FILE.resolve()), "platform_file_exists": PLATFORM_FILE.exists(), "platform_file_size": PLATFORM_FILE.stat().st_size if PLATFORM_FILE.exists() else 0, "platform_count": len(load_platforms()), "cache_file": str(CACHE_FILE.resolve()), "cache_file_exists": CACHE_FILE.exists(), "cache_file_size": CACHE_FILE.stat().st_size if CACHE_FILE.exists() else 0, "user_count": len(profiles), "server": "aiohttp"})

    async def handle_get_profile(self, request):
        user_key = unquote(request.match_info.get("user_key", ""))
        profiles = cache.get_data()
        profile = merge_platforms(profiles, [user_key]).get(user_key, {})
        return self._json({"user_key": user_key, "profile": profile})

    async def handle_put_profile(self, request):
        user_key = unquote(request.match_info.get("user_key", ""))
        body = await self._read_json(request)
        field = str(body.get("field", "")).strip()
        value = str(body.get("value", "")).strip()
        if not user_key or not field or not value:
            return self._json({"error": "user_key、field 和 value 不能为空"}, 400)
        profiles = cache.get_data()
        # 浅拷贝单个用户，避免深拷贝整个 profiles
        if user_key in profiles and isinstance(profiles[user_key], dict):
            profiles[user_key][field] = value
            profiles[user_key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            profiles[user_key] = {field: value, "_last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        save_profiles(profiles)
        return self._json({"success": True, "message": f"已更新 {field}"})

    async def handle_delete_profile(self, request):
        tail = request.match_info.get("tail", "")
        parts = tail.split("/", 1)
        profiles = cache.get_data()
        if len(parts) == 2 and parts[1]:
            user_key, field = unquote(parts[0]), unquote(parts[1])
            if user_key in profiles and field in profiles[user_key]:
                del profiles[user_key][field]
                profiles[user_key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_profiles(profiles)
                return self._json({"success": True, "message": f"已删除 {field}"})
            return self._json({"error": "未找到"}, 404)
        user_key = unquote(parts[0])
        if user_key in profiles:
            del profiles[user_key]
            save_profiles(profiles)
            return self._json({"success": True, "message": f"已删除用户 {user_key}"})
        return self._json({"error": "未找到用户"}, 404)

    async def _async_start(self):
        global DATA_DIR, PROFILES_FILE, PLATFORM_FILE, CACHE_FILE, WEBUI_DEBUG, WEBUI_TOKEN
        WEBUI_DEBUG = self.debug or os.environ.get("SOULMAP_DEBUG", "0").lower() in ("1", "true", "yes", "debug")
        WEBUI_TOKEN = self.token
        DATA_DIR = self.data_dir
        PROFILES_FILE = DATA_DIR / "user_profiles.json"
        PLATFORM_FILE = DATA_DIR / "user_platform.json"
        CACHE_FILE = DATA_DIR / "user_profiles.cache.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.release_occupied_port:
            release_port(self.port)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port, reuse_address=True)
        await self.site.start()
        log("INFO", f"启动于 http://{self.host}:{self.port} (aiohttp, auth={'on' if self.token else 'off'})")

    def start(self):
        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._async_start())
                self.started.set()
                self.loop.run_forever()
            except BaseException as e:
                self.start_error = e
                self.started.set()
            finally:
                try:
                    if self.runner:
                        self.loop.run_until_complete(self.runner.cleanup())
                except Exception:
                    pass
                self.loop.close()
        self.thread = threading.Thread(target=runner, daemon=True, name="SoulMap-Aiohttp-WebUI")
        self.thread.start()
        self.started.wait(timeout=5)
        if self.start_error:
            raise self.start_error
        return self

    async def _async_stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.loop:
            self.loop.call_soon(self.loop.stop)

    def stop(self, timeout: float = 5.0):
        if self.loop and self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._async_stop(), self.loop)
            try:
                fut.result(timeout=timeout)
            except Exception as e:
                log("WARNING", f"WebUI stop 等待失败: {e}")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def shutdown(self):
        self.stop()

    def server_close(self):
        pass


def start_server(data_dir: str, host: str = "0.0.0.0", port: int = 3999, plugin_dir: str = None, debug: bool = False, token: str = "", release_occupied_port: bool = True):
    return SoulMapWebServer(data_dir, host, port, plugin_dir, debug, token, release_occupied_port).start()


def main():
    parser = argparse.ArgumentParser(description="SoulMap WebUI")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", default=os.environ.get("SOULMAP_PORT", str(DEFAULT_PORT)))
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--token", default=os.environ.get("SOULMAP_WEBUI_TOKEN", ""))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    server = start_server(args.data_dir, args.host, parse_port(args.port), str(Path(__file__).parent), args.debug, args.token, True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[SoulMap WebUI] 正在停止...")
        server.stop()


if __name__ == "__main__":
    main()
