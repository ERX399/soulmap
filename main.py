import json
import re
import os
import time
import errno
import threading
import importlib.util
import builtins
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api import AstrBotConfig, logger
from astrbot.core.message.components import Plain




def _get_runtime_state() -> dict:
    """跨模块热更新共享的运行时状态，不依赖额外 py 文件。"""
    state = getattr(builtins, "_soulmap_runtime_state", None)
    if not isinstance(state, dict):
        state = {"server": None, "thread": None, "port": None}
        setattr(builtins, "_soulmap_runtime_state", state)
    return state


def _register_active_webui(server=None, thread=None, port=None):
    state = _get_runtime_state()
    state["server"] = server
    state["thread"] = thread
    state["port"] = port


def _shutdown_active_webui(timeout: float = 5.0) -> bool:
    state = _get_runtime_state()
    server = state.get("server")
    thread = state.get("thread")
    _register_active_webui(None, None, None)
    if server is None:
        return False
    try:
        if hasattr(server, "stop"):
            server.stop(timeout=timeout)
        else:
            try:
                server.shutdown()
            finally:
                try:
                    server.server_close()
                except Exception:
                    pass
            if thread and thread.is_alive():
                thread.join(timeout=timeout)
        return True
    except Exception:
        return False

class SoulMapManager:
    """
    用户画像管理系统 (SoulMap)
    - 所有字段统一为字符串类型，AI负责数据格式管理
    - 备注字段特殊处理：追加模式，保留最近N条
    """

    def __init__(self, data_path: Path, allowed_fields: list, max_notes_count: int = 5):
        self.data_path = data_path
        self.allowed_fields = allowed_fields
        self.max_notes_count = max_notes_count
        self._init_path()
        self._file_mtime = None
        self._platform_mtime = None
        self._load_failed = False
        self._platform_load_failed = False
        data = self._load_data("user_profiles.json")
        if data is None:
            self.user_data = {}
            self._load_failed = True
            logger.error("[SoulMap] 初始化加载失败，已进入只读保护模式，不会覆盖原文件")
        else:
            self.user_data = data

        platform_data = self._load_data("user_platform.json")
        if platform_data is None:
            self.platform_data = {}
            self._platform_load_failed = True
            logger.error("[SoulMap] 平台数据初始化加载失败，已进入平台数据只读保护模式")
        else:
            self.platform_data = platform_data
        try:
            self._file_mtime = (self.data_path / "user_profiles.json").stat().st_mtime
        except OSError:
            pass
        try:
            self._platform_mtime = (self.data_path / "user_platform.json").stat().st_mtime
        except OSError:
            pass

    def _init_path(self):
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _backup_file(self, path: Path, suffix: str = ".bak") -> Optional[Path]:
        """创建数据文件备份，避免修复或写入时丢失原始数据。"""
        if not path.exists():
            return None
        backup_path = path.with_suffix(path.suffix + suffix)
        try:
            import shutil
            shutil.copy2(path, backup_path)
            logger.info(f"[SoulMap] 已创建数据备份: {backup_path}")
            return backup_path
        except Exception as e:
            logger.warning(f"[SoulMap] 创建数据备份失败: {e}")
            return None

    def _ordered_profile(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """
        按统一规则整理单个画像字段顺序：
        1. allowed_fields 配置顺序
        2. 自定义字段名称排序
        3. _starred / _platform / _last_updated
        4. 其他元字段名称排序
        """
        if not isinstance(profile, dict):
            return profile

        ordered = {}
        for field in self.allowed_fields:
            if field in profile:
                ordered[field] = profile[field]

        custom_fields = sorted(k for k in profile if k not in ordered and not str(k).startswith("_"))
        for field in custom_fields:
            ordered[field] = profile[field]

        preferred_meta = ["_starred", "_platform", "_last_updated"]
        for field in preferred_meta:
            if field in profile:
                ordered[field] = profile[field]

        other_meta_fields = sorted(
            k for k in profile
            if str(k).startswith("_") and k not in preferred_meta
        )
        for field in other_meta_fields:
            ordered[field] = profile[field]

        return ordered

    def _sort_text_key(self, value: str) -> str:
        """
        名称排序 key：中文转拼音后与英文混排。
        例如：Alice、阿明(aming)、Bob、陈七(chenqi) 会自然穿插。
        如果运行环境没有 pypinyin，则回退到原字符串，不影响插件启动。
        """
        text = str(value or "").strip()
        if not text:
            return "~"
        try:
            from pypinyin import lazy_pinyin
            return "".join(lazy_pinyin(text)).casefold()
        except Exception:
            return text.casefold()

    def _profile_name_for_sort(self, key: str, profile: Dict[str, Any]) -> str:
        """排序显示名：优先使用画像称呼，没有称呼时使用 user_key。"""
        if isinstance(profile, dict):
            name = str(profile.get("对用户的称呼") or "").strip()
            if name:
                return name
        return str(key or "")

    def _profile_sort_key(self, key: str, profile: Dict[str, Any]) -> tuple:
        """
        统一用户排序规则 A：
        1. 星标置顶
        2. 有称呼的用户优先
        3. 显示名按拼音/英文混排
        4. user_key 兜底稳定排序

        不使用 _last_updated 排序；最近更新交给专门页面处理。
        """
        name = self._profile_name_for_sort(key, profile)
        has_name_rank = 0 if (isinstance(profile, dict) and str(profile.get("对用户的称呼") or "").strip()) else 1
        return (
            0 if (isinstance(profile, dict) and profile.get("_starred")) else 1,
            has_name_rank,
            self._sort_text_key(name),
            self._sort_text_key(key),
            str(key or "").casefold(),
        )

    def _normalize_data_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """按统一规则整理顶层用户顺序和画像字段顺序。"""
        if not isinstance(data, dict):
            return data

        items = sorted(
            data.items(),
            key=lambda item: self._profile_sort_key(str(item[0]), item[1] if isinstance(item[1], dict) else {})
        )
        ordered = {}
        for k, v in items:
            ordered[k] = self._ordered_profile(v) if isinstance(v, dict) else v
        return ordered

    def _try_repair_json(self, content: str) -> tuple[str, list]:
        """尝试修复常见 JSON 语法错误。"""
        repairs = []
        original = content

        content, count = re.subn(r',\s*([}\]])', r'\1', content)
        if count:
            repairs.append(f"移除尾部多余逗号({count})")

        content, count = re.subn(r'//.*$', '', content, flags=re.MULTILINE)
        if count:
            repairs.append(f"移除单行注释({count})")

        content, count = re.subn(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        if count:
            repairs.append(f"移除多行注释({count})")

        content, count = re.subn(r"'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', content)
        if count:
            repairs.append(f"单引号字符串转双引号({count})")

        content, count = re.subn(r'([}\]"0-9tfe])\s*\n\s*("[^"\n]+"\s*:)', r'\1,\n\2', content)
        if count:
            repairs.append(f"补齐缺失逗号({count})")

        content, count = re.subn(r'([}\]])\s*\n\s*([\[{])', r'\1,\n\2', content)
        if count:
            repairs.append(f"补齐对象/数组间逗号({count})")

        brace_delta = content.count('{') - content.count('}')
        bracket_delta = content.count('[') - content.count(']')
        if brace_delta > 0 or bracket_delta > 0:
            content = re.sub(r'[,\s:]*$', '', content.rstrip())
            content += ']' * max(0, bracket_delta) + '}' * max(0, brace_delta)
            repairs.append(f"补齐闭合括号(]x{max(0, bracket_delta)}, }}x{max(0, brace_delta)})")

        if content == original:
            repairs.append("未发现可自动修复项")
        return content, repairs

    def _load_data(self, filename: str) -> Optional[Dict[str, Any]]:
        """加载数据文件，支持 JSON 语法错误自动修复和备份恢复。"""
        path = self.data_path / filename
        if not path.exists():
            return {}
        try:
            if path.stat().st_size == 0:
                return {}
            content = path.read_text(encoding="utf-8")
            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning(f"[SoulMap] JSON 解析失败，尝试自动修复: {e}")
                self._backup_file(path, ".broken.bak")
                repaired, repairs = self._try_repair_json(content)
                logger.info(f"[SoulMap] JSON 修复动作: {', '.join(repairs)}")
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError as repair_error:
                    logger.error(f"[SoulMap] JSON 自动修复失败: {repair_error}")
                    backup_path = path.with_suffix(path.suffix + ".bak")
                    if backup_path.exists():
                        try:
                            data = json.loads(backup_path.read_text(encoding="utf-8"))
                            logger.info(f"[SoulMap] 已从备份恢复数据: {backup_path}")
                        except Exception as backup_error:
                            logger.error(f"[SoulMap] 备份恢复失败: {backup_error}")
                            return None
                    else:
                        return None
                else:
                    if isinstance(data, dict):
                        data = self._normalize_data_order(data)
                        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        logger.info(f"[SoulMap] JSON 自动修复成功，已恢复 {len(data)} 条记录")
            if not isinstance(data, dict):
                logger.error(f"[SoulMap] 数据格式错误: 期望dict，实际为{type(data).__name__}")
                return None
            normalized = self._normalize_data_order(data)
            normalized_text = json.dumps(normalized, ensure_ascii=False, indent=2)
            if normalized != data or content.strip() != normalized_text.strip():
                self._backup_file(path, ".sort.bak")
                path.write_text(normalized_text, encoding="utf-8")
                logger.info(f"[SoulMap] 已自动整理 JSON 格式与排序: {filename}")
            return normalized
        except (TypeError, IOError, OSError) as e:
            logger.error(f"[SoulMap] 加载数据失败: {e}")
            return None

    def _save_data(self):
        if self._load_failed:
            logger.warning("[SoulMap] 数据加载曾失败，拒绝写入以保护原文件")
            return
        path = self.data_path / "user_profiles.json"
        if not self.user_data and path.exists():
            try:
                if path.stat().st_size > 2:
                    logger.warning("[SoulMap] 内存数据为空但文件非空，拒绝写入以防数据丢失")
                    return
            except OSError:
                pass
        if path.exists():
            try:
                if path.stat().st_size > 2:
                    self._backup_file(path, ".bak")
            except OSError:
                pass
        self.user_data = self._normalize_data_order(self.user_data)
        tmp_path = path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            self._file_mtime = path.stat().st_mtime
        except Exception as e:
            logger.error(f"[SoulMap] 写入文件失败: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def _check_reload(self):
        """检查文件是否被外部修改，如有则重新加载。节流：1秒内不重复检查。"""
        now = time.time()
        if hasattr(self, '_last_reload_check') and now - self._last_reload_check < 1.0:
            return
        self._last_reload_check = now
        path = self.data_path / "user_profiles.json"
        if path.exists():
            try:
                current_mtime = path.stat().st_mtime
            except OSError:
                current_mtime = None
            if current_mtime is not None and (self._file_mtime is None or current_mtime != self._file_mtime):
                logger.info("[SoulMap] 检测到数据文件被外部修改，重新加载")
                new_data = self._load_data("user_profiles.json")
                if new_data is not None:
                    self.user_data = new_data
                    self._file_mtime = current_mtime
                    if self._load_failed:
                        self._load_failed = False
                        logger.info("[SoulMap] 数据重新加载成功，解除只读保护模式")
                else:
                    logger.error("[SoulMap] 重新加载失败，保留内存数据")
        self._check_platform_reload()

    def _get_user_key(self, user_id: str, session_id: Optional[str] = None) -> str:
        return f"{session_id}_{user_id}" if session_id else user_id

    def _get_platform_from_origin(self, unified_msg_origin: str) -> str:
        """从 AstrBot unified_msg_origin 中提取平台名。"""
        text = str(unified_msg_origin or "").strip()
        if not text:
            return ""
        low = text.lower()
        if "gewechat" in low:
            return "gewechat"
        if "telegram" in low or low.startswith("tg"):
            return "telegram"
        if "aiocqhttp" in low or "cqhttp" in low:
            return "aiocqhttp"
        # AstrBot 的 unified_msg_origin 可能是 default:GroupMessage:xxx，
        # default 是平台实例 ID，不是真实平台名，不能记录为平台。
        if low == "default" or low.startswith("default:"):
            return ""
        for sep in ("_", ":", "/", "|"):
            if sep in low:
                head = low.split(sep, 1)[0].strip()
                return "" if head == "default" else head
        return "" if low == "default" else low

    def _platform_label(self, platform: str) -> str:
        p = str(platform or "").lower().strip()
        if not p:
            return ""
        if p == "telegram" or p == "tg":
            return "TG"
        if p == "gewechat" or "wechat" in p:
            return "微信"
        if p == "aiocqhttp" or "cqhttp" in p or p == "qq":
            return "QQ"
        return platform

    def _extract_platform_from_event(self, event: AstrMessageEvent) -> tuple[str, str]:
        """从 AstrBot 事件对象提取平台；不依赖 LLM。返回 (platform, origin)。"""
        origin = str(getattr(event, "unified_msg_origin", "") or "")

        def try_text(value) -> tuple[str, str]:
            text = str(value or "").strip()
            if not text:
                return "", ""
            platform = self._get_platform_from_origin(text)
            return platform, text

        # 1. 优先读取 AstrBot 平台元数据。unified_msg_origin 中的 default 往往只是实例 ID。
        platform_meta = getattr(event, "platform", None)
        platform, text = try_text(getattr(platform_meta, "name", None))
        if platform:
            return platform, origin or text

        fn = getattr(event, "get_platform_name", None)
        if callable(fn):
            try:
                platform, text = try_text(fn())
            except Exception:
                platform, text = "", ""
            if platform:
                return platform, origin or text

        # 2. AstrBot 标准 unified_msg_origin，只有能解析到真实平台时才使用
        platform, text = try_text(origin)
        if platform:
            return platform, origin

        # 3. 事件对象常见属性
        for attr in (
            "platform", "platform_name", "adapter", "adapter_name", "bot_platform",
            "platform_id", "adapter_id", "session_id", "self_id"
        ):
            platform, text = try_text(getattr(event, attr, None))
            if platform:
                return platform, origin or text

        # 3. 事件对象常见方法
        for method in (
            "get_platform_name", "get_platform_id", "get_message_platform",
            "get_adapter_name", "get_adapter_id", "get_self_id", "get_session_id"
        ):
            fn = getattr(event, method, None)
            if callable(fn):
                try:
                    value = fn()
                except Exception:
                    continue
                platform, text = try_text(value)
                if platform:
                    return platform, origin or text

        # 4. message_obj 常见属性/方法
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            for attr in (
                "platform", "platform_name", "adapter", "adapter_name", "bot_platform",
                "platform_id", "adapter_id", "session_id", "self_id", "message_origin",
                "origin", "unified_msg_origin"
            ):
                platform, text = try_text(getattr(message_obj, attr, None))
                if platform:
                    return platform, origin or text

            for method in (
                "get_platform_name", "get_platform_id", "get_message_platform",
                "get_adapter_name", "get_adapter_id", "get_self_id", "get_session_id"
            ):
                fn = getattr(message_obj, method, None)
                if callable(fn):
                    try:
                        value = fn()
                    except Exception:
                        continue
                    platform, text = try_text(value)
                    if platform:
                        return platform, origin or text

        return "", origin

    def _save_platform_data(self):
        if self._platform_load_failed:
            logger.warning("[SoulMap] 平台数据加载曾失败，拒绝写入以保护原文件")
            return
        path = self.data_path / "user_platform.json"
        try:
            # 平台文件使用 user_key 稳定排序；最近更新时间不参与全局排序。
            data = dict(sorted(
                self.platform_data.items(),
                key=lambda item: str(item[0] or "").casefold()
            ))
            tmp_path = path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            self.platform_data = data
            self._platform_mtime = path.stat().st_mtime
        except Exception as e:
            logger.error(f"[SoulMap] 写入平台数据失败: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def _check_platform_reload(self):
        now = time.time()
        if hasattr(self, '_last_platform_reload_check') and now - self._last_platform_reload_check < 2.0:
            return
        self._last_platform_reload_check = now
        path = self.data_path / "user_platform.json"
        if not path.exists():
            return
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return
        if self._platform_mtime is None or current_mtime != self._platform_mtime:
            logger.info("[SoulMap] 检测到平台数据文件被外部修改，重新加载")
            data = self._load_data("user_platform.json")
            if data is not None:
                self.platform_data = data
                self._platform_mtime = current_mtime
                self._platform_load_failed = False

    def record_user_platform(self, user_id: str, session_id: Optional[str], platform: str, origin: str = "") -> bool:
        """保存用户平台信息到 user_platform.json，不创建画像。内存节流：5秒内不重复写盘。"""
        platform = str(platform or "").lower().strip()
        if not user_id or not platform:
            return False
        if not hasattr(self, '_platform_write_times'):
            self._platform_write_times = {}
        key = self._get_user_key(user_id, session_id)
        now = time.time()
        last_write = self._platform_write_times.get(key, 0)
        if now - last_write < 5:
            return False
        if not isinstance(self.platform_data, dict):
            self.platform_data = {}
        self.platform_data[key] = {
            "platform": platform,
            "label": self._platform_label(platform),
            "origin": str(origin or ""),
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self._platform_write_times[key] = now
        self._save_platform_data()
        return True

    def _migrate_platform_data(self):
        """把旧画像中的 _platform 迁移到 user_platform.json，不再写回 user_profiles.json。"""
        if not self.user_data:
            return
        if not isinstance(self.platform_data, dict):
            self.platform_data = {}
        migrated = 0
        for key, profile in list(self.user_data.items()):
            if not isinstance(profile, dict):
                continue
            platform = profile.get("_platform")
            if not platform or key in self.platform_data:
                continue
            p = str(platform).lower().strip()
            self.platform_data[key] = {
                "platform": p,
                "label": self._platform_label(p),
                "origin": "",
                "last_updated": profile.get("_last_updated") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            migrated += 1
        if migrated:
            logger.info(f"[SoulMap] 已迁移 {migrated} 条旧画像平台信息到 user_platform.json")
            self._save_platform_data()

    def get_user_profile(self, user_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        self._check_reload()
        key = self._get_user_key(user_id, session_id)
        return self.user_data.get(key, {}).copy()

    def update_field(self, user_id: str, field: str, value: str, 
                     session_id: Optional[str] = None, save: bool = True) -> tuple:
        """更新字段值，备注字段特殊处理。save=False 时跳过写盘（用于批量操作）"""
        if self._load_failed:
            return False, "数据加载失败，当前为只读保护模式"
        if save:
            self._check_reload()
        if field not in self.allowed_fields:
            return False, f"字段 '{field}' 不在允许列表中"

        key = self._get_user_key(user_id, session_id)
        if key not in self.user_data:
            self.user_data[key] = {}

        value = value.strip()
        
        # 备注字段特殊处理：追加模式，保留最近N条
        if field == "备注":
            existing = self.user_data[key].get("备注", "")
            # 解析现有备注（以顿号或分号分隔）
            if existing:
                notes = [n.strip() for n in re.split(r'[；;]', existing) if n.strip()]
            else:
                notes = []
            # 解析新备注
            new_notes = [n.strip()[:20] for n in re.split(r'[；;]', value) if n.strip()]
            # 去重并追加
            for note in new_notes:
                if note not in notes:
                    notes.append(note)
            # 保留最近N条
            notes = notes[-self.max_notes_count:]
            value = "；".join(notes)
        
        self.user_data[key][field] = value
        self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if save:
            self._save_data()
        return True, f"已更新 {field}"

    def delete_field(self, user_id: str, field: str, session_id: Optional[str] = None, save: bool = True) -> tuple:
        """删除字段或备注条目（支持数字索引）。save=False 时跳过写盘（用于批量操作）"""
        if self._load_failed:
            return False, "数据加载失败，当前为只读保护模式"
        if save:
            self._check_reload()
        key = self._get_user_key(user_id, session_id)
        if key not in self.user_data:
            return False, "没有找到你的画像数据"

        # 1. 精确匹配字段名
        if field in self.user_data[key]:
            del self.user_data[key][field]
            self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if save:
                self._save_data()
            return True, f"已删除字段 {field}"

        # 2. 数字索引：删除备注中的第N条
        if "备注" in self.user_data[key] and field.isdigit():
            idx = int(field) - 1  # 转为0索引
            notes = [n.strip() for n in re.split(r'[；;]', self.user_data[key]["备注"]) if n.strip()]
            if 0 <= idx < len(notes):
                deleted_note = notes.pop(idx)
                if notes:
                    self.user_data[key]["备注"] = "；".join(notes)
                else:
                    del self.user_data[key]["备注"]
                self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if save:
                    self._save_data()
                return True, f"已删除备注第{field}条：{deleted_note}"
            return False, f"备注第{field}条不存在"

        # 3. 模糊匹配：在备注中搜索并删除包含该内容的条目
        if "备注" in self.user_data[key]:
            notes = [n.strip() for n in re.split(r'[；;]', self.user_data[key]["备注"]) if n.strip()]
            new_notes = [n for n in notes if field not in n]
            if len(new_notes) < len(notes):
                if new_notes:
                    self.user_data[key]["备注"] = "；".join(new_notes)
                else:
                    del self.user_data[key]["备注"]
                self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if save:
                    self._save_data()
                return True, f"已从备注中删除包含 '{field}' 的条目"

        return False, f"未找到字段或备注条目 '{field}'"

    def clear_profile(self, user_id: str, session_id: Optional[str] = None) -> tuple:
        if self._load_failed:
            return False, "数据加载失败，当前为只读保护模式"
        self._check_reload()
        key = self._get_user_key(user_id, session_id)
        if key in self.user_data:
            del self.user_data[key]
            self._save_data()
            return True, "已清空"
        return False, "没有画像数据"

    def format_profile_summary(self, user_id: str, session_id: Optional[str] = None) -> str:
        """格式化用户画像摘要"""
        self._check_reload()
        key = self._get_user_key(user_id, session_id)
        profile = self.user_data.get(key, {})
        if not profile:
            return "暂无记录"

        lines = []
        for field in self.allowed_fields:
            if field in profile and profile[field]:
                # 备注字段按条显示：1.xxx 2.xxx
                if field == "备注":
                    notes = [n.strip() for n in re.split(r'[；;]', profile[field]) if n.strip()]
                    notes_display = " ".join([f"{i}.{note}" for i, note in enumerate(notes, 1)])
                    lines.append(f"- 备注：{notes_display}")
                else:
                    lines.append(f"- {field}：{profile[field]}")

        return "\n".join(lines) if lines else "暂无记录"

    def export_all_profiles(self) -> Dict[str, Any]:
        self._check_reload()
        return self.user_data.copy()


@register("SoulMap", "ERX399", "AI驱动的用户画像收集系统，简洁设计，AI负责数据管理", "1.2.2")
class SoulMapPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._webui_server = None
        self._webui_thread = None
        self._webui_port = None

        data_dir = StarTools.get_data_dir()

        # 从配置读取字段列表，直接使用
        allowed_fields = self._cfg("allowed_fields", [
            "对用户的称呼", "性别", "年龄", "所在地", "生日", "爱吃", "忌口",
            "爱好", "职业", "重要节日", "恐惧/弱点", "作息规律", "技能水平",
            "健康状况", "宠物", "备注"
        ])
        max_notes_count = self._cfg("max_notes_count", 5)
        self.support_platforms = {"aiocqhttp", "telegram", "gewechat"}

        self.manager = SoulMapManager(data_dir, allowed_fields, max_notes_count)

        # 正则模式（支持中文字段名）
        self.profile_pattern = re.compile(r"\[Profile:\s*([^\]]+)\]", re.IGNORECASE)
        # 支持多字段删除: [ProfileDelete: 字段1, 字段2] 或 [ProfileDelete: 字段]
        self.delete_pattern = re.compile(r"\[ProfileDelete:\s*([^\]]+)\]", re.IGNORECASE)
        self.block_pattern = re.compile(r"\s*\[(?:Profile|ProfileDelete):[^\]]*\]\s*", re.IGNORECASE)

        # 迁移旧数据中的平台信息
        self.manager._migrate_platform_data()

        # 热更新/重复加载前，先关闭旧 WebUI，避免旧线程继续占用端口
        try:
            if _shutdown_active_webui(timeout=5.0):
                logger.info("[SoulMap] 已关闭旧 WebUI 实例，准备启动新实例")
        except Exception as e:
            logger.warning(f"[SoulMap] 关闭旧 WebUI 实例失败: {e}")

        # 启动 WebUI 服务器
        self._start_webui(data_dir)

    @property
    def session_based(self) -> bool:
        return bool(self._cfg("session_based", False))

    def _get_session_id(self, event: AstrMessageEvent) -> Optional[str]:
        return event.unified_msg_origin if self.session_based else None

    def _get_allowed_fields_display(self) -> str:
        """生成可用字段的显示字符串"""
        return "/".join(self.manager.allowed_fields)

    def _cfg(self, key: str, default=None, section: str = None):
        """读取配置：兼容旧版平铺配置和新版模块化 object/items 配置。"""
        try:
            if key in self.config:
                return self.config.get(key, default)
            if section:
                group = self.config.get(section, {})
                if isinstance(group, dict) and key in group:
                    return group.get(key, default)
            for group_name in ("basic", "profile", "audit", "permission", "webui"):
                group = self.config.get(group_name, {})
                if isinstance(group, dict) and key in group:
                    return group.get(key, default)
        except Exception:
            pass
        return default

    def _is_supported_platform(self, platform_or_origin: str) -> bool:
        if not self.support_platforms:
            return True
        platform = self.manager._get_platform_from_origin(platform_or_origin)
        if not platform:
            platform = str(platform_or_origin or "").lower().strip()
        if not platform:
            return True
        return platform in self.support_platforms

    @filter.on_llm_request()
    async def add_profile_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入画像信息"""
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        profile_summary = self.manager.format_profile_summary(user_id, session_id)
        allowed_fields_display = self._get_allowed_fields_display()
        max_notes_count = str(self._cfg("max_notes_count", 5))
        
        profile_prompt = self._cfg("profile_prompt", "")
        
        if profile_prompt:
            try:
                profile_prompt = profile_prompt.format(
                    profile_summary=profile_summary,
                    allowed_fields_display=allowed_fields_display,
                    max_notes_count=max_notes_count
                )
            except KeyError:
                # 兼容旧格式，使用replace
                profile_prompt = profile_prompt.replace("{profile_summary}", profile_summary)
                profile_prompt = profile_prompt.replace("{allowed_fields_display}", allowed_fields_display)
                profile_prompt = profile_prompt.replace("{max_notes_count}", max_notes_count)
            req.system_prompt += f"\n{profile_prompt}"

    def _audit_profile_value_by_rules(self, field: str, value: str) -> tuple[bool, str, str]:
        """规则层画像审核：返回 (是否通过, 修正值, 原因)。"""
        field = str(field or "").strip()
        value = str(value or "").strip()
        max_len = int(self._cfg("profile_audit_max_value_length", 80) or 80)

        if not field or not value:
            return False, "", "字段或值为空"
        if field not in self.manager.allowed_fields:
            return False, value, "字段不在允许列表中"

        # 清理外层引号和明显控制字符
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip(" \t\r\n\"'“”‘’")
        if not value:
            return False, "", "清理后为空"

        # 过滤明显无意义值
        meaningless = {"无", "未知", "不知道", "不清楚", "none", "", "undefined", "n/a", "暂无", "没有"}
        if value.lower() in meaningless:
            return False, value, "无有效信息"

        # 重复字符/乱码防护
        compact = re.sub(r"\s+", "", value)
        if len(compact) >= 8:
            most = max(compact.count(ch) for ch in set(compact))
            if most / max(1, len(compact)) > 0.65:
                return False, value, "重复字符过多"

        if len(value) > max_len and field != "备注":
            value = value[:max_len].rstrip()

        if field == "年龄":
            m = re.search(r"\d{1,3}", value)
            if not m:
                return False, value, "年龄不是数字"
            age = int(m.group(0))
            if age <= 0 or age > 120:
                return False, value, "年龄超出合理范围"
            value = str(age)

        elif field == "性别":
            low = value.lower()
            if any(x in value for x in ("男", "男性")) or low in {"male", "man", "boy"}:
                value = "男"
            elif any(x in value for x in ("女", "女性")) or low in {"female", "woman", "girl"}:
                value = "女"
            elif any(x in value for x in ("其他", "非二元", "未知", "保密")) or low in {"other", "unknown"}:
                value = "其他"
            else:
                return False, value, "性别值不明确"

        elif field == "生日":
            # 允许 YYYY-MM-DD、MM-DD、中文月日等简短生日信息；过长或纯数字噪声跳过
            if len(value) > 30:
                return False, value, "生日信息过长"
            if re.fullmatch(r"\d{5,}", value):
                return False, value, "生日格式异常"

        elif field == "备注":
            # 备注拆分并限制每条长度，去重保序
            parts = [x.strip()[:20] for x in re.split(r"[;；]", value) if x.strip()]
            seen, cleaned = set(), []
            for part in parts:
                if part and part not in seen:
                    cleaned.append(part)
                    seen.add(part)
            if not cleaned:
                return False, value, "备注为空"
            value = "；".join(cleaned)

        return True, value, "规则审核通过"

    async def _audit_profile_ops_with_llm(self, original_text: str, ops: dict) -> dict:
        """可选 LLM 二次审核。失败时返回原 ops，不阻断主流程。"""
        if not self._cfg("profile_audit_use_llm", False):
            return ops
        if not ops:
            return ops

        audit_items = {
            field: {"op": op, "value": value}
            for field, (op, value) in ops.items()
            if op == "update"
        }
        if not audit_items:
            return ops

        prompt_tmpl = self._cfg("profile_audit_prompt", "")
        if not prompt_tmpl:
            prompt_tmpl = (
                "你是用户画像审核器。请审核候选画像是否合理、明确、无臆测。"
                "只返回JSON对象，格式：{\"字段\":{\"action\":\"keep|modify|drop\",\"value\":\"修正值\",\"reason\":\"原因\"}}。"
                "不确定、无意义、隐私过度推断、格式明显错误时 drop。"
            )

        prompt = (
            f"{prompt_tmpl}\n\n"
            f"原始回复文本：{original_text[:1500]}\n"
            f"允许字段：{self.manager.allowed_fields}\n"
            f"候选画像JSON：{json.dumps(audit_items, ensure_ascii=False)}"
        )

        async def maybe_await(x):
            if hasattr(x, "__await__"):
                return await x
            return x

        try:
            provider = None
            audit_model = str(self._cfg("profile_audit_model", "") or "").strip()
            if audit_model:
                for name in ("get_provider_by_id", "get_provider_by_name", "get_provider"):
                    fn = getattr(self.context, name, None)
                    if callable(fn):
                        try:
                            provider = await maybe_await(fn(audit_model))
                        except TypeError:
                            continue
                        if provider:
                            break

            if provider is None:
                for name in ("get_using_provider", "get_provider", "get_llm_provider"):
                    fn = getattr(self.context, name, None)
                    if callable(fn):
                        try:
                            provider = await maybe_await(fn())
                        except TypeError:
                            continue
                        if provider:
                            break
            if provider is None:
                logger.debug("[SoulMap] 未找到可用 provider，跳过 LLM 画像审核")
                return ops

            result_text = ""
            for name in ("text_chat", "generate", "ask", "chat"):
                fn = getattr(provider, name, None)
                if callable(fn):
                    res = await maybe_await(fn(prompt))
                    result_text = getattr(res, "completion_text", None) or getattr(res, "text", None) or str(res)
                    break
            if not result_text:
                logger.debug("[SoulMap] provider 无可用文本生成方法，跳过 LLM 画像审核")
                return ops

            m = re.search(r"\{.*\}", result_text, re.S)
            if not m:
                logger.warning("[SoulMap] LLM画像审核未返回JSON，使用规则审核结果")
                return ops
            audit = json.loads(m.group(0))
            if not isinstance(audit, dict):
                return ops

            new_ops = dict(ops)
            for field, decision in audit.items():
                if field not in new_ops or not isinstance(decision, dict):
                    continue
                action = str(decision.get("action", "keep")).lower().strip()
                if action == "drop":
                    new_ops.pop(field, None)
                elif action == "modify":
                    new_value = str(decision.get("value", "")).strip()
                    ok, fixed, reason = self._audit_profile_value_by_rules(field, new_value)
                    if ok:
                        new_ops[field] = ("update", fixed)
                    else:
                        logger.info(f"[SoulMap] LLM审核修正值未通过规则，跳过 {field}: {reason}")
                        new_ops.pop(field, None)
            return new_ops
        except Exception as e:
            logger.warning(f"[SoulMap] LLM画像审核失败，使用规则审核结果: {e}")
            return ops

    async def _audit_profile_ops(self, original_text: str, final_ops: dict) -> dict:
        """规则审核 + 可选 LLM 审核。"""
        if not self._cfg("profile_audit_enabled", True):
            return final_ops

        audited = {}
        for field, (op_type, value) in final_ops.items():
            if op_type != "update":
                audited[field] = (op_type, value)
                continue
            ok, fixed, reason = self._audit_profile_value_by_rules(field, value)
            if ok:
                audited[field] = ("update", fixed)
                if fixed != value:
                    logger.info(f"[SoulMap] 规则审核修正: {field}={value} -> {fixed}")
            else:
                logger.info(f"[SoulMap] 规则审核跳过: {field}={value}, 原因: {reason}")

        audited = await self._audit_profile_ops_with_llm(original_text, audited)
        return audited

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """解析并更新画像（合并同一回复中的重复操作，统一写盘一次）"""
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)
        original_text = resp.completion_text or ""

        # 确保使用最新数据，避免覆盖外部修改
        self.manager._check_reload()

        # 自动记录平台信息：平台来自 AstrBot 事件，不依赖 LLM；单独写入 user_platform.json，不创建空画像。
        platform, origin = self.manager._extract_platform_from_event(event)
        if platform and self._is_supported_platform(platform):
            self.manager.record_user_platform(user_id, session_id, platform, origin)

        logger.debug(f"[SoulMap] on_llm_resp 被调用 - 用户: {user_id}, session_id: {session_id}, session_based: {self.session_based}")
        logger.debug(f"[SoulMap] 原始文本长度: {len(original_text)}")

        if not original_text:
            logger.debug("[SoulMap] 原始文本为空，直接返回")
            return

        # ---- 第一步：收集所有操作，按出现顺序记录 ----
        # 用 (操作类型, 位置) 排序来保证按原文顺序执行
        ops = []  # [(pos, 'update'|'delete', field, value|None), ...]

        for m in self.profile_pattern.finditer(original_text):
            match_text = m.group(1)
            pos = m.start()
            pairs = re.findall(
                r'([\w\u4e00-\u9fff/]+)\s*=\s*([^,，]*(?:[,，](?!\s*[\w\u4e00-\u9fff/]+=)[^,，]*)*)',
                match_text
            )
            for field, value in pairs:
                ops.append((pos, 'update', field.strip(), value.strip()))

        for m in self.delete_pattern.finditer(original_text):
            match_text = m.group(1)
            pos = m.start()
            fields = [f.strip() for f in re.split(r'[,，;；、]', match_text) if f.strip()]
            for field in fields:
                ops.append((pos, 'delete', field, None))

        # 按出现位置排序
        ops.sort(key=lambda x: x[0])

        # ---- 第二步：同一字段去重，只保留最后一次操作 ----
        # 对于备注字段：如果最后一个操作是 update 且包含完整内容，前面的中间步骤可以跳过
        final_ops = {}  # field -> (op_type, value)
        for _, op_type, field, value in ops:
            final_ops[field] = (op_type, value)

        if not final_ops:
            # 没有任何画像操作，只清理标签就返回
            resp.completion_text = self.block_pattern.sub('', original_text).strip()
            if resp.result_chain and resp.result_chain.chain:
                for comp in resp.result_chain.chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = self.block_pattern.sub('', comp.text).strip()
            return

        logger.debug(f"[SoulMap] 原始操作数: {len(ops)}, 去重后: {len(final_ops)}")

        # ---- 第三步：画像审核（规则层 + 可选 LLM 二次审核）----
        final_ops = await self._audit_profile_ops(original_text, final_ops)
        if not final_ops:
            logger.info("[SoulMap] 所有画像操作均被审核层过滤，跳过写入")
            resp.completion_text = self.block_pattern.sub('', original_text).strip()
            if resp.result_chain and resp.result_chain.chain:
                for comp in resp.result_chain.chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = self.block_pattern.sub('', comp.text).strip()
            return

        # ---- 第四步：按正确顺序执行（先删后写），不逐次写盘 ----
        has_changes = False

        # 先执行所有删除
        delete_fields = [(f, v) for f, (op, v) in final_ops.items() if op == 'delete']
        # 数字索引从大到小，避免删除后索引错位
        digit_deletes = sorted([f for f, _ in delete_fields if f.isdigit()], key=int, reverse=True)
        other_deletes = [f for f, _ in delete_fields if not f.isdigit()]

        for field in other_deletes + digit_deletes:
            success, msg = self.manager.delete_field(user_id, field, session_id, save=False)
            if success:
                has_changes = True
                logger.info(f"[SoulMap] {user_id} 删除成功: {field}")
            else:
                logger.warning(f"[SoulMap] {user_id} 删除失败: {field}, 原因: {msg}")

        # 再执行所有更新
        for field, (op_type, value) in final_ops.items():
            if op_type != 'update':
                continue
            success, msg = self.manager.update_field(user_id, field, value, session_id, save=False)
            if success:
                has_changes = True
                logger.info(f"[SoulMap] {user_id} 更新成功: {field}={value}")
            else:
                logger.warning(f"[SoulMap] {user_id} 更新失败: {field}={value}, 原因: {msg}")

        # ---- 第五步：统一写盘一次 ----
        if has_changes:
            self.manager._save_data()
            logger.debug(f"[SoulMap] {user_id} 批量操作完成，统一写盘")

        # 清理标签
        resp.completion_text = self.block_pattern.sub('', original_text).strip()
        if resp.result_chain and resp.result_chain.chain:
            for comp in resp.result_chain.chain:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = self.block_pattern.sub('', comp.text).strip()

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """最后清理"""
        result = event.get_result()
        if result is None or not result.chain:
            return

        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                cleaned = self.block_pattern.sub('', comp.text).strip()
                if cleaned != comp.text:
                    comp.text = cleaned

    # ------------------- 用户命令 -------------------

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        """判断是否为群聊消息"""
        origin = event.unified_msg_origin or ""
        # 群聊的unified_msg_origin通常包含group关键字
        return "group" in origin.lower()

    @filter.command("我的画像")
    async def show_my_profile(self, event: AstrMessageEvent):
        # 判断是否为群聊，如果是群聊则检查开关
        if self._is_group_chat(event):
            allow_in_group = self._cfg("allow_profile_in_group", False)
            if not allow_in_group:
                denied_msg = self._cfg("group_profile_denied_msg", "为保护隐私，请私聊我查看你的画像哦~")
                yield event.plain_result(denied_msg)
                return

        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        summary = self.manager.format_profile_summary(user_id, session_id)
        if summary == "暂无记录":
            yield event.plain_result("暂时还没有记录内容，多和我聊聊吧")
            return

        profile = self.manager.get_user_profile(user_id, session_id)
        last_updated = profile.get("_last_updated", "未知")
        yield event.plain_result(f"📋 你的画像：\n{summary}\n\n最后更新：{last_updated}")

    @filter.command("删除画像")
    async def delete_my_field(self, event: AstrMessageEvent, field: str):
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        field = field.strip()

        success, msg = self.manager.delete_field(user_id, field, session_id)
        if success:
            yield event.plain_result(f"✅ 已删除「{field}」")
        else:
            yield event.plain_result(f"❌ {msg}")

    @filter.command("清空画像")
    async def clear_my_profile(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        success, msg = self.manager.clear_profile(user_id, session_id)
        if success:
            yield event.plain_result("✅ 已清空你的所有画像数据")
        else:
            yield event.plain_result(f"❌ {msg}")

    # ------------------- 管理员命令 -------------------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.role == "admin"

    @filter.command("查询画像")
    async def admin_query_profile(self, event: AstrMessageEvent, user_id: str):
        if not self._is_admin(event):
            yield event.plain_result(self._cfg("admin_permission_denied_msg", "错误：此命令仅限管理员使用"))
            return

        session_id = self._get_session_id(event)
        profile = self.manager.get_user_profile(user_id.strip(), session_id)

        if not profile:
            yield event.plain_result(f"用户 {user_id} 没有画像数据")
            return

        summary = self.manager.format_profile_summary(user_id.strip(), session_id)
        last_updated = profile.get("_last_updated", "未知")
        yield event.plain_result(f"📋 用户 {user_id} 的画像：\n{summary}\n\n最后更新：{last_updated}")

    @filter.command("画像统计")
    async def admin_profile_stats(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._cfg("admin_permission_denied_msg", "错误：此命令仅限管理员使用"))
            return

        all_profiles = self.manager.export_all_profiles()
        user_count = len(all_profiles)

        field_counts = {}
        for profile in all_profiles.values():
            for field in profile:
                if not field.startswith("_"):
                    field_counts[field] = field_counts.get(field, 0) + 1

        response = f"📊 画像系统统计\n\n总用户数：{user_count}\n\n字段填充情况：\n"

        for field in self.manager.allowed_fields:
            count = field_counts.get(field, 0)
            rate = (count / user_count * 100) if user_count > 0 else 0
            response += f"• {field}: {count} ({rate:.1f}%)\n"

        yield event.plain_result(response)

    @filter.command("平台调试")
    async def admin_platform_debug(self, event: AstrMessageEvent):
        """输出当前 AstrBot 事件里的平台相关字段，用于适配不同平台/适配器。"""
        if not self._is_admin(event):
            yield event.plain_result(self._cfg("admin_permission_denied_msg", "错误：此命令仅限管理员使用"))
            return

        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)
        platform, origin = self.manager._extract_platform_from_event(event)
        user_key = self.manager._get_user_key(user_id, session_id)

        lines = [
            "🔎 SoulMap 平台调试",
            f"user_id: {user_id!r}",
            f"session_id: {session_id!r}",
            f"user_key: {user_key!r}",
            f"parsed_platform: {platform!r}",
            f"parsed_origin: {origin!r}",
            "",
            f"event_type: {type(event).__name__}",
            "event attrs:"
        ]

        attrs = (
            "unified_msg_origin", "role", "platform", "platform_name", "adapter",
            "adapter_name", "bot_platform", "platform_id", "adapter_id",
            "session_id", "self_id"
        )
        for attr in attrs:
            try:
                lines.append(f"- {attr}: {getattr(event, attr, None)!r}")
            except Exception as e:
                lines.append(f"- {attr}: <error {e}>")

        lines.append("")
        lines.append("event methods:")
        methods = (
            "get_sender_id", "get_session_id", "get_platform_name", "get_platform_id",
            "get_message_platform", "get_adapter_name", "get_adapter_id", "get_self_id"
        )
        for name in methods:
            fn = getattr(event, name, None)
            if not callable(fn):
                lines.append(f"- {name}: not callable")
                continue
            try:
                lines.append(f"- {name}(): {fn()!r}")
            except Exception as e:
                lines.append(f"- {name}(): <error {e}>")

        message_obj = getattr(event, "message_obj", None)
        lines.append("")
        lines.append(f"message_obj_type: {type(message_obj).__name__ if message_obj is not None else 'None'}")
        if message_obj is not None:
            lines.append("message_obj attrs:")
            for attr in attrs + ("message_origin", "origin"):
                try:
                    lines.append(f"- {attr}: {getattr(message_obj, attr, None)!r}")
                except Exception as e:
                    lines.append(f"- {attr}: <error {e}>")

            lines.append("")
            lines.append("message_obj methods:")
            for name in methods:
                fn = getattr(message_obj, name, None)
                if not callable(fn):
                    lines.append(f"- {name}: not callable")
                    continue
                try:
                    lines.append(f"- {name}(): {fn()!r}")
                except Exception as e:
                    lines.append(f"- {name}(): <error {e}>")

        platform_file = self.manager.data_path / "user_platform.json"
        lines.extend([
            "",
            f"platform_file: {str(platform_file)}",
            f"platform_file_exists: {platform_file.exists()}",
            f"support_platforms: {sorted(self.support_platforms) if self.support_platforms else 'ALL'}",
        ])

        yield event.plain_result("\n".join(lines))

    def _load_webui_start_server(self):
        """按插件文件路径加载 webui.py，避免插件环境下导入失败或导入到同名模块。"""
        webui_path = Path(__file__).parent / "webui.py"
        if not webui_path.exists():
            raise FileNotFoundError(f"webui.py 不存在: {webui_path}")

        module_name = f"soulmap_webui_{id(self)}"
        spec = importlib.util.spec_from_file_location(module_name, str(webui_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 webui.py: {webui_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.start_server

    def _start_webui(self, data_dir: Path):
        """在后台线程启动 WebUI 服务器；端口被占用时尝试释放原占用进程。"""
        try:
            start_server = self._load_webui_start_server()
            host = str(self._cfg("webui_host", "0.0.0.0"))
            port = int(self._cfg("webui_port", 3999))
            debug = bool(self._cfg("webui_debug", False))
            password = str(self._cfg("webui_password", "") or "").strip()
            release_occupied_port = bool(self._cfg("webui_release_occupied_port", True))
            plugin_dir = str(Path(__file__).parent)
            auth_state = "on" if password else "off"

            def run():
                try:
                    self._webui_server = start_server(
                        data_dir=str(data_dir),
                        host=host,
                        port=port,
                        plugin_dir=plugin_dir,
                        debug=debug,
                        password=password,
                        release_occupied_port=release_occupied_port
                    )
                    self._webui_port = port
                    _register_active_webui(self._webui_server, self._webui_thread, port)
                    logger.info(f"[SoulMap] 独立 WebUI 已启动: http://{host}:{port} (aiohttp, debug={'on' if debug else 'off'}, auth={auth_state}, release_port={'on' if release_occupied_port else 'off'})")
                except OSError as e:
                    err_no = getattr(e, "errno", None)
                    if isinstance(e, PermissionError) or err_no == errno.EACCES:
                        logger.error(f"[SoulMap] WebUI 端口绑定被系统拒绝 {host}:{port}，请检查端口权限或安全策略: {e}")
                    elif err_no == errno.EADDRINUSE:
                        # 热更新/重复加载时可能已有一个 WebUI 实例成功启动。
                        # 端口占用在这种场景下不影响使用，避免输出误导性错误日志。
                        return
                    else:
                        logger.error(f"[SoulMap] WebUI 启动时发生网络/端口异常 {host}:{port}: {e}")
                except Exception as e:
                    logger.error(f"[SoulMap] WebUI 服务器异常退出: {e}")

            self._webui_thread = threading.Thread(target=run, daemon=True, name="SoulMap-WebUI")
            self._webui_thread.start()
            logger.info(f"[SoulMap] WebUI 启动任务已提交: http://{host}:{port} (debug={'on' if debug else 'off'}, auth={auth_state}, release_port={'on' if release_occupied_port else 'off'})")
        except Exception as e:
            logger.error(f"[SoulMap] 启动 WebUI 失败: {e}")

    async def terminate(self):
        if self._webui_server:
            try:
                logger.info("[SoulMap] 正在关闭 WebUI 服务器...")
                if hasattr(self._webui_server, "stop"):
                    self._webui_server.stop(timeout=5.0)
                else:
                    self._webui_server.shutdown()
                    self._webui_server.server_close()
                    if self._webui_thread and self._webui_thread.is_alive():
                        self._webui_thread.join(timeout=5.0)
                        if self._webui_thread.is_alive():
                            logger.warning("[SoulMap] WebUI 线程未在 5 秒内退出")
                self._webui_server = None
                self._webui_thread = None
                _register_active_webui(None, None, None)
                logger.info("[SoulMap] WebUI 已安全关闭")
            except Exception as e:
                logger.warning(f"[SoulMap] 关闭 WebUI 失败: {e}")

        if not self.manager._load_failed and self.manager.user_data:
            self.manager._save_data()

