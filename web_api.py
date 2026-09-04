"""Genie TTS 语音合成工作台的 WebUI 后端接口。

设计约定（与 AstrBot Dashboard 的插件页桥接层对齐，勿随意改动）：

* 鉴权完全依赖 Dashboard 自身的登录态，插件侧不再做二次校验；因此不要把
  Dashboard 暴露在公网上。
* 只使用 GET / POST 两种方法，且每个路径只绑定一种方法——AstrBot 的路由表
  按 (路径, 方法) 精确匹配，同路径双方法容易踩坑。
* 常规接口统一返回 HTTP 200 + envelope::

      {"status": "ok", "message": None, "data": <payload>}
      {"status": "error", "message": "中文原因", "data": None}

  Dashboard 前端会自动解包一层 ``data``，并在 ``status == "error"`` 时抛出
  ``message``。返回 4xx 会被前端的 axios 吞掉，所以失败也要用 200。
* payload 里不要出现顶层 ``status`` 键，否则前端的防御性解包会误判。
* 感情包导出接口是唯一例外：直接返回裸 JSON 文本 + Content-Disposition，
  这样前端的 blob 下载才能拿到干净的文件内容。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from . import emotion_pack
from . import run_log as run_log_mod
from .emotion_pack import EmotionPackError
from .tts_engine import (
    BYTES_PER_SAMPLE,
    CHANNELS,
    DEFAULT_CHUNK_GAP_MS,
    DEFAULT_MAX_TEXT_LENGTH,
    DEFAULT_TAIL_PADDING_MS,
    MAX_CHUNK_GAP_MS,
    MAX_CUSTOM_PAUSE_MS,
    MAX_TAIL_PADDING_MS,
    PAUSE_MARKER_PATTERN,
    SAMPLE_RATE,
    auto_pause_budget_seconds,
    count_pronounceable,
    has_pronounceable,
    pause_budget_seconds,
)

try:  # pragma: no cover - 取决于宿主 AstrBot 的运行环境
    from quart import jsonify, request
    from quart import Response as QuartResponse

    QUART_READY = True
except Exception:  # pragma: no cover
    jsonify = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    QuartResponse = None  # type: ignore[assignment]
    QUART_READY = False


PLUGIN_NAME = "astrbot_plugin_genie_tts_llm"
ROUTE_PREFIX = "/" + PLUGIN_NAME + "/"
PAGE_NAME = "studio"

PACK_DIR_NAME = "emotion_packs"
PREFS_KV_KEY = "webui_prefs_v1"

# 配置页回显密钥时用的占位符：保存时收到它就表示"保持原值不动"。
MASKED_SECRET = "__astrbot_masked__"
SECRET_FIELDS = ("api_key", "token", "secret", "password")

MAX_INLINE_AUDIO_BYTES = 12 * 1024 * 1024
MAX_SYNTH_TEXT_LENGTH = 1500
MAX_PREVIEW_TEXT_LENGTH = 4000
MAX_PACK_FILES = 300
MAX_HISTORY_ROWS = 40


# --------------------------------------------------------------------- 主题

# galgame 质感的 6 套配色。真正的颜色写在 style.css 的 [data-theme] 变量里，
# 这里只提供给下拉框用的元数据（顺序即下拉框顺序）。
THEMES: Tuple[Dict[str, Any], ...] = (
    {"id": "moonlit", "name": "月夜", "hint": "深靛蓝夜色 · 月光银蓝", "dark": True},
    {"id": "sakura", "name": "樱色", "hint": "暖玫白 · 樱粉柔光", "dark": False},
    {"id": "twilight", "name": "黄昏", "hint": "深紫 · 落日琥珀", "dark": True},
    {"id": "aoi", "name": "苍海", "hint": "深板岩 · 青碧水蓝", "dark": True},
    {"id": "usuyuki", "name": "薄雪", "hint": "雪白灰 · 冰蓝极简", "dark": False},
    {"id": "hiyo", "name": "绯夜", "hint": "近黑 · 绯红酒红", "dark": True},
)
THEME_IDS = tuple(theme["id"] for theme in THEMES)
DEFAULT_THEME = "moonlit"

DENSITIES = ("comfortable", "compact")
DEFAULT_DENSITY = "comfortable"


# --------------------------------------------------------------- 配置分组

# 配置页按业务含义分组渲染，而不是按 _conf_schema.json 的原始顺序——39 个键
# 平铺出来没人看得懂。未列出的键会被收进"其它"分组，所以以后加配置项也不会
# 在页面上凭空消失。
CONFIG_GROUPS: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    (
        "voice",
        "默认音色",
        "没有单独指定时，所有会话都会落到这套默认角色与感情上。",
        (
            "default_character",
            "default_emotion_name",
            "tts_default_language",
            "tts_max_text_length",
            "tts_timeout_seconds",
            "tts_max_retries",
        ),
    ),
    (
        "servers",
        "服务器与保活",
        "多个地址会自动轮询；HuggingFace Space 建议开启保活避免冷启动。",
        (
            "tts_servers",
            "enable_space_keepalive",
            "space_keepalive_url",
            "space_keepalive_interval_minutes",
        ),
    ),
    (
        "scope",
        "生效范围与触发",
        "控制哪些会话会自动配音，以及自动配音的触发频率。",
        (
            "enable_group_tts_by_default",
            "group_whitelist",
            "group_blacklist",
            "tts_trigger_mode",
            "tts_trigger_interval_seconds",
            "tts_trigger_probability",
        ),
    ),
    (
        "output",
        "输出形态",
        "决定语音与文本怎么发出去，以及失败时是否提示。",
        (
            "auto_tts_output_mode",
            "llm_tool_tts_output_mode",
            "send_text_with_audio",
            "enable_tts_failure_notice",
        ),
    ),
    (
        "pause",
        "分段与停顿",
        "长句拆成多段分别合成再拼接，段间补静音，避免一口气念完的机械感。",
        (
            "enable_sentence_splitting",
            "sentences_per_chunk",
            "sentence_split_regex",
            "chunk_gap_ms",
            "enable_custom_pause_marker",
            "custom_pause_prompt",
        ),
    ),
    (
        "text",
        "文本处理与翻译",
        "送进 TTS 之前的清洗与翻译流程。",
        (
            "enable_tts_text_cleaning",
            "tts_text_clean_regex",
            "enable_translation",
            "enable_translation_debug_log",
        ),
    ),
    (
        "llm",
        "LLM 注入",
        "注入给主对话模型的提示词与工具开关。",
        ("llm_injection_settings",),
    ),
    (
        "translation",
        "翻译接口",
        "独立的 OpenAI 兼容接口，用于把回复译成目标语言。",
        ("translation_api",),
    ),
    (
        "guard",
        "音质防护与持久化",
        "拦截「参考音频被拼进结果」和「句段被漏掉导致尾音截断」两类异常，并在重启后恢复开关状态。",
        (
            "enable_tts_leak_guard",
            "tts_leak_guard_seconds_per_char",
            "tts_leak_guard_min_seconds",
            "enable_tts_truncation_guard",
            "tts_truncation_guard_seconds_per_char",
            "tts_tail_padding_ms",
            "enable_state_persistence",
        ),
    ),
    (
        "diagnostics",
        "日志与诊断",
        "内存里的运行日志与合成轨迹，供「日志」页排查情感选得好不好。",
        (
            "enable_run_log",
            "run_log_capacity",
            "run_log_synth_capacity",
            "run_log_full_text",
        ),
    ),
)

# 这几个键由后台常驻任务读取，改完需要重载插件才会真正生效。
RESTART_REQUIRED_KEYS = frozenset(
    {
        "enable_space_keepalive",
        "space_keepalive_url",
        "space_keepalive_interval_minutes",
        "enable_run_log",
        "run_log_capacity",
        "run_log_synth_capacity",
        "run_log_full_text",
    }
)


# --------------------------------------------------------------- 指令速查表

COMMAND_TABLE: Tuple[Dict[str, str], ...] = (
    {"group": "开关", "usage": "/tts-llm", "alias": "开启语音合成", "desc": "开启当前会话的自动配音"},
    {"group": "开关", "usage": "/tts-q", "alias": "关闭语音合成", "desc": "关闭当前会话的自动配音"},
    {"group": "开关", "usage": "/ttg", "alias": "开启群语音", "desc": "开启本群自动配音（仅群聊）"},
    {"group": "开关", "usage": "/ttg-q", "alias": "关闭群语音", "desc": "关闭本群自动配音（仅群聊）"},
    {"group": "开关", "usage": "/tts-w", "alias": "开启自动情感识别", "desc": "开启 W 模式（旁白/心声风格）"},
    {"group": "开关", "usage": "/tts-w-q", "alias": "关闭自动情感识别", "desc": "关闭 W 模式"},
    {"group": "音色", "usage": "/sw 角色 感情", "alias": "切换感情", "desc": "切换当前会话的角色与感情"},
    {"group": "音色", "usage": "/sw-w 角色", "alias": "切换w角色", "desc": "切换 W 模式使用的角色与感情"},
    {"group": "音色", "usage": "/注册感情 角色 感情 相对路径 参考文本 [语言]", "alias": "", "desc": "登记一条参考音频"},
    {"group": "音色", "usage": "/删除感情 角色 感情", "alias": "", "desc": "删除一条参考音频"},
    {"group": "音色", "usage": "/查看感情 [角色]", "alias": "", "desc": "列出已登记的角色与感情"},
    {"group": "合成", "usage": "/合成 角色 感情 文本", "alias": "", "desc": "按指定角色与感情直接合成一条语音"},
    {"group": "合成", "usage": "/tts-again", "alias": "重发语音", "desc": "重发上一条语音"},
    {"group": "感情包", "usage": "/感情导出 [角色]", "alias": "导出感情", "desc": "导出感情包 JSON（省略角色导出全部）"},
    {"group": "感情包", "usage": "/感情导入 [模式] [试运行]", "alias": "导入感情", "desc": "导入感情包：附 JSON / 引用消息 / 上传 .json 附件"},
    {"group": "感情包", "usage": "/感情包", "alias": "感情包列表", "desc": "列出服务器上保存的感情包快照"},
    {"group": "感情包", "usage": "/感情包保存 [文件名]", "alias": "", "desc": "把当前感情库存成一份快照"},
    {"group": "感情包", "usage": "/感情包恢复 文件名 [模式] [试运行]", "alias": "", "desc": "从快照恢复感情库（试运行只预演）"},
    {"group": "诊断", "usage": "/tts-status", "alias": "语音状态", "desc": "查看开关、音色、队列与服务器状态"},
    {"group": "诊断", "usage": "/tts-help", "alias": "语音帮助", "desc": "显示指令帮助"},
)

COMMAND_GROUP_ORDER = ("开关", "音色", "合成", "感情包", "诊断")


# ------------------------------------------------------------------ 小工具


def _as_text(value: Any, limit: int = 0) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "否", "关", ""):
        return False
    return default


def _clamp_int(value: Any, low: int, high: int) -> int:
    """把整数夹到 [low, high]。日志接口的 limit 都要过一遍，避免一次拉爆。"""
    number = _as_int(value, low)
    if number < low:
        return low
    if number > high:
        return high
    return number


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _split_csv(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [_as_text(item) for item in value if _as_text(item)]
    text = _as_text(value)
    if not text:
        return []
    return [chunk.strip() for chunk in text.split(",") if chunk.strip()]


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in SECRET_FIELDS)


def _wav_duration_seconds(size_bytes: int) -> float:
    """按裸 PCM 估算时长；44 字节 WAV 头的误差可以忽略。"""
    frame = max(1, SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)
    return round(max(0, size_bytes - 44) / frame, 3)


# --------------------------------------------------------------------- 主体


class GenieWebApi:
    """把插件内部能力包装成 Dashboard 插件页可以调用的 HTTP 接口。

    所有 handler 都是 ``async``，签名为无参（参数从 quart 的 ``request`` 里取），
    这与 AstrBot ``register_web_api`` 的调用方式一致。
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.available = QUART_READY
        # WebUI 可能同时开好几个标签页，写 emotions.json 必须串行化。
        self._write_lock = asyncio.Lock()
        self._history: List[Dict[str, Any]] = []
        self._registered = 0

    # ------------------------------------------------------------ 注册

    def _routes(self) -> Tuple[Tuple[str, str, str, str], ...]:
        return (
            ("overview", "GET", "_handle_overview", "工作台总览"),
            ("emotions", "GET", "_handle_emotions", "读取感情库"),
            ("emotions/upsert", "POST", "_handle_emotion_upsert", "新增或修改感情"),
            ("emotions/delete", "POST", "_handle_emotion_delete", "删除感情或角色"),
            ("emotions/copy", "POST", "_handle_emotion_copy", "复制或移动感情"),
            (
                "emotions/rename-character",
                "POST",
                "_handle_character_rename",
                "重命名角色",
            ),
            ("emotions/export", "GET", "_handle_emotion_export", "下载感情包"),
            (
                "emotions/export-preview",
                "POST",
                "_handle_emotion_export_preview",
                "预览感情包文本",
            ),
            ("emotions/import", "POST", "_handle_emotion_import", "导入感情包"),
            ("packs", "GET", "_handle_packs", "列出感情包快照"),
            ("packs/save", "POST", "_handle_pack_save", "保存感情包快照"),
            ("packs/delete", "POST", "_handle_pack_delete", "删除感情包快照"),
            ("packs/restore", "POST", "_handle_pack_restore", "从快照恢复感情库"),
            ("packs/download", "GET", "_handle_pack_download", "下载感情包快照"),
            ("servers", "GET", "_handle_servers", "探测 TTS 服务器"),
            ("synthesize", "POST", "_handle_synthesize", "合成试听"),
            ("preview", "POST", "_handle_preview", "分段与停顿预览"),
            ("config", "GET", "_handle_config", "读取插件配置"),
            ("config/save", "POST", "_handle_config_save", "保存插件配置"),
            ("sessions", "GET", "_handle_sessions", "读取会话状态"),
            ("sessions/toggle", "POST", "_handle_session_toggle", "切换会话开关"),
            ("commands", "GET", "_handle_commands", "指令速查表"),
            ("prefs", "GET", "_handle_prefs", "读取界面偏好"),
            ("prefs/save", "POST", "_handle_prefs_save", "保存界面偏好"),
            ("logs", "GET", "_handle_logs", "运行日志"),
            ("logs/synths", "GET", "_handle_synth_logs", "合成记录与情感统计"),
            ("logs/clear", "POST", "_handle_logs_clear", "清空日志缓冲"),
            ("logs/export", "GET", "_handle_logs_export", "下载日志文本"),
        )

    def register(self, context: Any) -> int:
        """把所有接口注册到 AstrBot。返回成功注册的条数。"""
        if not self.available:
            logger.warning("Genie TTS: 未检测到 quart，WebUI 工作台接口不会注册。")
            return 0
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "Genie TTS: 当前 AstrBot 不支持 register_web_api，WebUI 工作台已跳过。"
            )
            return 0

        count = 0
        for endpoint, method, attr, desc in self._routes():
            handler = getattr(self, attr, None)
            if handler is None:
                continue
            try:
                register(ROUTE_PREFIX + endpoint, handler, [method], desc)
                count += 1
            except Exception as exc:
                logger.error(
                    "Genie TTS: 注册 WebUI 接口 " + endpoint + " 失败: " + str(exc)
                )
        self._registered = count
        logger.info("Genie TTS: 已注册 " + str(count) + " 个 WebUI 工作台接口。")
        return count

    # -------------------------------------------------------- envelope

    @staticmethod
    def _ok(payload: Any = None):
        return jsonify({"status": "ok", "message": None, "data": payload})

    @staticmethod
    def _err(message: str):
        # 故意用 200：Dashboard 前端只看 envelope 里的 status，4xx 会被吞掉。
        return jsonify({"status": "error", "message": message, "data": None})

    @staticmethod
    async def _read_body() -> Dict[str, Any]:
        try:
            data = await request.get_json(force=True, silent=True)
        except Exception:
            data = None
        if data is None:
            try:
                raw = await request.get_data(as_text=True)
                data = json.loads(raw) if raw else None
            except Exception:
                data = None
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _query(name: str, default: str = "") -> str:
        try:
            return _as_text(request.args.get(name, default))
        except Exception:
            return default

    # ------------------------------------------------------- 内部读写

    @property
    def _manager(self):
        return self.plugin.emotion_manager

    def _characters(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        data = getattr(self._manager, "emotions_data", None)
        return data if isinstance(data, dict) else {}

    def _clean_characters(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """拿到通过校验的感情数据，脏行会被丢掉（导出时用）。"""
        try:
            cleaned, _invalid = emotion_pack.normalize_characters(self._characters())
        except EmotionPackError:
            return {}
        return cleaned

    def _emotion_rows(self) -> Tuple[List[Dict[str, Any]], int]:
        """把感情库摊平成表格行；坏行保留原值并附 warning，方便在页面上修。"""
        rows: List[Dict[str, Any]] = []
        warnings = 0
        for character in sorted(self._characters().keys(), key=lambda x: str(x)):
            emotions = self._characters().get(character) or {}
            if not isinstance(emotions, dict):
                rows.append(
                    {
                        "character": str(character),
                        "emotion": "",
                        "ref_audio_path": "",
                        "ref_audio_text": "",
                        "language": "",
                        "warning": "该角色的数据不是对象结构",
                    }
                )
                warnings += 1
                continue
            for emotion in sorted(emotions.keys(), key=lambda x: str(x)):
                raw = emotions.get(emotion)
                entry, reason = emotion_pack.normalize_entry(raw)
                if entry is None:
                    fallback = raw if isinstance(raw, dict) else {}
                    rows.append(
                        {
                            "character": str(character),
                            "emotion": str(emotion),
                            "ref_audio_path": _as_text(fallback.get("ref_audio_path")),
                            "ref_audio_text": _as_text(fallback.get("ref_audio_text")),
                            "language": _as_text(fallback.get("language")),
                            "warning": reason or "记录不合法",
                        }
                    )
                    warnings += 1
                    continue
                rows.append(
                    {
                        "character": str(character),
                        "emotion": str(emotion),
                        "ref_audio_path": entry.get("ref_audio_path", ""),
                        "ref_audio_text": entry.get("ref_audio_text", ""),
                        "language": entry.get("language", ""),
                        "warning": "",
                    }
                )
        return rows, warnings

    async def _commit(self, characters: Dict[str, Dict[str, Dict[str, str]]]) -> bool:
        """写回 emotions.json；失败时回滚到磁盘上的旧值，避免内存脏数据。"""
        manager = self._manager
        manager.emotions_data = characters
        ok = False
        try:
            ok = bool(manager._save_emotions_to_file())
        except Exception as exc:
            logger.error("Genie TTS WebUI: 写入 emotions.json 异常: " + str(exc))
            ok = False
        if not ok:
            try:
                manager.reload()
            except Exception:
                pass
        return ok

    def _pack_dir(self) -> Path:
        base = Path(getattr(self.plugin, "plugin_data_dir", ".")) / PACK_DIR_NAME
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _plugin_version(self) -> str:
        return _as_text(getattr(self.plugin, "PLUGIN_VERSION", "")) or ""

    # ------------------------------------------------------------ 总览

    async def _handle_overview(self):
        try:
            rows, warnings = self._emotion_rows()
            characters = self._characters()
            engine = getattr(self.plugin, "tts_engine", None)
            stats = dict(getattr(engine, "stats", {}) or {})
            config = self.plugin.config

            payload = {
                "plugin": {
                    "name": PLUGIN_NAME,
                    "display_name": "Genie TTS LLM",
                    "version": self._plugin_version(),
                    "repo": "https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm",
                },
                "themes": [dict(theme) for theme in THEMES],
                "densities": list(DENSITIES),
                "counts": {
                    "characters": len(characters),
                    "emotions": len(rows),
                    "warnings": warnings,
                    "commands": len(COMMAND_TABLE),
                    "themes": len(THEMES),
                    "endpoints": self._registered,
                },
                "stats": {
                    "requests": _as_int(stats.get("requests"), 0),
                    "succeeded": _as_int(stats.get("succeeded"), 0),
                    "failed": _as_int(stats.get("failed"), 0),
                    "skipped_no_speech": _as_int(stats.get("skipped_no_speech"), 0),
                    "leak_guard_hits": _as_int(stats.get("leak_guard_hits"), 0),
                    "truncation_guard_hits": _as_int(
                        stats.get("truncation_guard_hits"), 0
                    ),
                    "text_truncated": _as_int(stats.get("text_truncated"), 0),
                    "empty_result_retries": _as_int(
                        stats.get("empty_result_retries"), 0
                    ),
                    "queue_size": engine.queue_size() if engine else 0,
                },
                "limits": {
                    "max_text_length": _as_int(
                        config.get("tts_max_text_length"), DEFAULT_MAX_TEXT_LENGTH
                    ),
                    "synth_text_limit": MAX_SYNTH_TEXT_LENGTH,
                    "timeout_seconds": _as_int(config.get("tts_timeout_seconds"), 120),
                    "max_retries": _as_int(config.get("tts_max_retries"), 3),
                    "chunk_gap_ms": _as_int(config.get("chunk_gap_ms"), DEFAULT_CHUNK_GAP_MS),
                    "max_chunk_gap_ms": MAX_CHUNK_GAP_MS,
                    "max_custom_pause_ms": MAX_CUSTOM_PAUSE_MS,
                    "sentences_per_chunk": _as_int(config.get("sentences_per_chunk"), 2),
                    "sample_rate": SAMPLE_RATE,
                    "tail_padding_ms": _as_int(
                        config.get("tts_tail_padding_ms"), DEFAULT_TAIL_PADDING_MS
                    ),
                    "max_tail_padding_ms": MAX_TAIL_PADDING_MS,
                },
                "toggles": {
                    "sentence_splitting": _as_bool(config.get("enable_sentence_splitting")),
                    "custom_pause_marker": _as_bool(config.get("enable_custom_pause_marker")),
                    "translation": _as_bool(config.get("enable_translation"), True),
                    "text_cleaning": _as_bool(config.get("enable_tts_text_cleaning")),
                    "leak_guard": _as_bool(config.get("enable_tts_leak_guard"), True),
                    "truncation_guard": _as_bool(
                        config.get("enable_tts_truncation_guard"), True
                    ),
                    "state_persistence": _as_bool(config.get("enable_state_persistence"), True),
                    "failure_notice": _as_bool(config.get("enable_tts_failure_notice"), False),
                    "keepalive": _as_bool(config.get("enable_space_keepalive")),
                    "group_default": _as_bool(config.get("enable_group_tts_by_default")),
                },
                "defaults": {
                    "character": _as_text(config.get("default_character")),
                    "emotion": _as_text(config.get("default_emotion_name")),
                    "language": _as_text(config.get("tts_default_language")) or "jp",
                    "trigger_mode": _as_text(config.get("tts_trigger_mode")) or "always",
                    "auto_output_mode": _as_text(config.get("auto_tts_output_mode")),
                    "tool_output_mode": _as_text(config.get("llm_tool_tts_output_mode")),
                },
                "session": {
                    "active_sessions": len(getattr(self.plugin, "active_sessions", ()) or ()),
                    "w_active_sessions": len(getattr(self.plugin, "w_active_sessions", ()) or ()),
                    "active_groups": len(getattr(self.plugin, "active_groups", ()) or ()),
                    "inactive_groups": len(getattr(self.plugin, "inactive_groups", ()) or ()),
                    "session_emotions": len(getattr(self.plugin, "session_emotions", ()) or ()),
                },
                "servers": len(engine._server_urls()) if engine else 0,
                "packs": len(self._list_packs()),
                "run_log": self._run_log_summary(),
                "history": list(reversed(self._history[-MAX_HISTORY_ROWS:])),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            return self._ok(payload)
        except Exception as exc:
            logger.error("Genie TTS WebUI overview 失败: " + str(exc))
            return self._err("读取总览失败: " + str(exc))

    def _run_log_summary(self) -> Dict[str, Any]:
        """总览卡片用的日志摘要；没有日志模块时给一份全 0 的占位。"""
        log = self._run_log()
        if log is None:
            return {"available": False, "enabled": False, "attached": False}
        try:
            snapshot = log.snapshot()
        except Exception as exc:
            logger.debug("Genie TTS WebUI: 读取日志摘要失败: " + str(exc))
            return {"available": False, "enabled": False, "attached": False}
        logs = snapshot.get("logs") or {}
        synths = snapshot.get("synths") or {}
        return {
            "available": True,
            "enabled": bool(snapshot.get("enabled")),
            "attached": bool(snapshot.get("attached")),
            "log_size": _as_int(logs.get("size"), 0),
            "log_total": _as_int(logs.get("total"), 0),
            "issues": _as_int(logs.get("issues"), 0),
            "synth_size": _as_int(synths.get("size"), 0),
            "synth_total": _as_int(synths.get("total"), 0),
            "failed": _as_int(synths.get("failed"), 0),
            "skipped": _as_int(synths.get("skipped"), 0),
            "success_rate": synths.get("success_rate") or 0.0,
            "avg_elapsed_ms": _as_int(synths.get("avg_elapsed_ms"), 0),
        }

    async def _handle_commands(self):
        groups: List[Dict[str, Any]] = []
        seen: Dict[str, Dict[str, Any]] = {}
        for name in COMMAND_GROUP_ORDER:
            bucket = {"group": name, "items": []}
            seen[name] = bucket
            groups.append(bucket)
        for item in COMMAND_TABLE:
            bucket = seen.get(item["group"])
            if bucket is None:
                bucket = {"group": item["group"], "items": []}
                seen[item["group"]] = bucket
                groups.append(bucket)
            bucket["items"].append(dict(item))
        return self._ok({"groups": groups, "total": len(COMMAND_TABLE)})

    # ------------------------------------------------------------ 感情

    async def _handle_emotions(self):
        try:
            rows, warnings = self._emotion_rows()
            characters: List[Dict[str, Any]] = []
            for name, emotions in sorted(self._characters().items(), key=lambda kv: str(kv[0])):
                size = len(emotions) if isinstance(emotions, dict) else 0
                characters.append({"name": str(name), "count": size})
            return self._ok(
                {
                    "characters": characters,
                    "rows": rows,
                    "warnings": warnings,
                    "languages": list(emotion_pack.KNOWN_LANGUAGES),
                    "import_modes": list(emotion_pack.IMPORT_MODES),
                    "default_import_mode": emotion_pack.DEFAULT_IMPORT_MODE,
                    "file": str(getattr(self._manager, "file_path", "")),
                }
            )
        except Exception as exc:
            logger.error("Genie TTS WebUI emotions 失败: " + str(exc))
            return self._err("读取感情库失败: " + str(exc))

    async def _handle_emotion_upsert(self):
        body = await self._read_body()
        character = _as_text(body.get("character"), emotion_pack.MAX_NAME_LENGTH)
        emotion = _as_text(body.get("emotion"), emotion_pack.MAX_NAME_LENGTH)
        if not character or not emotion:
            return self._err("角色名与感情名都不能为空")

        entry_raw = {
            "ref_audio_path": _as_text(body.get("ref_audio_path")),
            "ref_audio_text": _as_text(body.get("ref_audio_text")),
            "language": _as_text(body.get("language")),
        }
        entry, reason = emotion_pack.normalize_entry(entry_raw)
        if entry is None:
            return self._err(reason or "参考音频配置不合法")

        original_character = _as_text(body.get("original_character"))
        original_emotion = _as_text(body.get("original_emotion"))
        overwrite = _as_bool(body.get("overwrite"), True)

        async with self._write_lock:
            characters = {
                name: dict(emotions)
                for name, emotions in self._characters().items()
                if isinstance(emotions, dict)
            }
            renaming = bool(original_character) and (
                original_character != character or original_emotion != emotion
            )
            if (
                not overwrite
                and emotion in characters.get(character, {})
                and not (original_character == character and original_emotion == emotion)
            ):
                return self._err(character + " 的 " + emotion + " 已存在")

            if renaming and original_emotion:
                bucket = characters.get(original_character)
                if isinstance(bucket, dict):
                    bucket.pop(original_emotion, None)
                    if not bucket:
                        characters.pop(original_character, None)

            characters.setdefault(character, {})[emotion] = entry
            if len(characters) > emotion_pack.MAX_CHARACTERS:
                return self._err("角色数量超过上限 " + str(emotion_pack.MAX_CHARACTERS))
            if len(characters[character]) > emotion_pack.MAX_EMOTIONS_PER_CHARACTER:
                return self._err(
                    "单个角色的感情数量超过上限 "
                    + str(emotion_pack.MAX_EMOTIONS_PER_CHARACTER)
                )
            if not await self._commit(characters):
                return self._err("写入 emotions.json 失败，已回滚")

        rows, warnings = self._emotion_rows()
        return self._ok(
            {
                "saved": {"character": character, "emotion": emotion},
                "renamed": renaming,
                "rows": rows,
                "warnings": warnings,
                "summary": emotion_pack.summarize(self._characters()),
            }
        )

    async def _handle_emotion_delete(self):
        body = await self._read_body()
        items = body.get("items")
        character = _as_text(body.get("character"))
        targets: List[Tuple[str, str]] = []
        if isinstance(items, list) and items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = _as_text(item.get("character"))
                emo = _as_text(item.get("emotion"))
                if name:
                    targets.append((name, emo))
        elif character:
            targets.append((character, _as_text(body.get("emotion"))))
        if not targets:
            return self._err("没有指定要删除的条目")

        removed: List[Dict[str, str]] = []
        missing: List[Dict[str, str]] = []
        async with self._write_lock:
            characters = {
                name: dict(emotions)
                for name, emotions in self._characters().items()
                if isinstance(emotions, dict)
            }
            for name, emo in targets:
                bucket = characters.get(name)
                if bucket is None:
                    missing.append({"character": name, "emotion": emo})
                    continue
                if not emo:
                    for existing in sorted(bucket.keys()):
                        removed.append({"character": name, "emotion": existing})
                    characters.pop(name, None)
                    continue
                if emo in bucket:
                    bucket.pop(emo, None)
                    removed.append({"character": name, "emotion": emo})
                    if not bucket:
                        characters.pop(name, None)
                else:
                    missing.append({"character": name, "emotion": emo})
            if not removed:
                return self._err("要删除的条目都不存在")
            if not await self._commit(characters):
                return self._err("写入 emotions.json 失败，已回滚")

        rows, warnings = self._emotion_rows()
        return self._ok(
            {
                "removed": removed,
                "missing": missing,
                "rows": rows,
                "warnings": warnings,
                "summary": emotion_pack.summarize(self._characters()),
            }
        )

    async def _handle_emotion_copy(self):
        body = await self._read_body()
        target = _as_text(body.get("target_character"), emotion_pack.MAX_NAME_LENGTH)
        if not target:
            return self._err("目标角色名不能为空")
        move = _as_bool(body.get("move"))
        overwrite = _as_bool(body.get("overwrite"))

        raw_items = body.get("items")
        pairs: List[Tuple[str, str]] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                name = _as_text(item.get("character"))
                emo = _as_text(item.get("emotion"))
                if name:
                    pairs.append((name, emo))
        if not pairs:
            return self._err("没有选择要复制的条目")

        copied: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []
        async with self._write_lock:
            characters = {
                name: dict(emotions)
                for name, emotions in self._characters().items()
                if isinstance(emotions, dict)
            }
            for name, emo in pairs:
                bucket = characters.get(name) or {}
                sources = sorted(bucket.keys()) if not emo else [emo]
                for source_emotion in sources:
                    entry = bucket.get(source_emotion)
                    if not isinstance(entry, dict):
                        skipped.append(
                            {
                                "character": name,
                                "emotion": source_emotion,
                                "reason": "源条目不存在",
                            }
                        )
                        continue
                    if name == target:
                        skipped.append(
                            {
                                "character": name,
                                "emotion": source_emotion,
                                "reason": "目标角色与源角色相同",
                            }
                        )
                        continue
                    existing = characters.get(target, {}).get(source_emotion)
                    if existing is not None and not overwrite:
                        skipped.append(
                            {
                                "character": target,
                                "emotion": source_emotion,
                                "reason": "目标已存在同名感情",
                            }
                        )
                        continue
                    characters.setdefault(target, {})[source_emotion] = dict(entry)
                    copied.append({"character": target, "emotion": source_emotion})
                    if move:
                        bucket.pop(source_emotion, None)
                if move and not bucket:
                    characters.pop(name, None)

            if not copied:
                reason = skipped[0]["reason"] if skipped else "没有可复制的条目"
                return self._err(reason)
            if len(characters) > emotion_pack.MAX_CHARACTERS:
                return self._err("角色数量超过上限 " + str(emotion_pack.MAX_CHARACTERS))
            if not await self._commit(characters):
                return self._err("写入 emotions.json 失败，已回滚")

        rows, warnings = self._emotion_rows()
        return self._ok(
            {
                "copied": copied,
                "skipped": skipped,
                "moved": move,
                "rows": rows,
                "warnings": warnings,
                "summary": emotion_pack.summarize(self._characters()),
            }
        )

    async def _handle_character_rename(self):
        body = await self._read_body()
        character = _as_text(body.get("character"))
        new_name = _as_text(body.get("new_name"), emotion_pack.MAX_NAME_LENGTH)
        merge = _as_bool(body.get("merge"))
        if not character or not new_name:
            return self._err("原角色名与新角色名都不能为空")
        if character == new_name:
            return self._err("新角色名与原角色名相同")

        async with self._write_lock:
            characters = {
                name: dict(emotions)
                for name, emotions in self._characters().items()
                if isinstance(emotions, dict)
            }
            if character not in characters:
                return self._err("角色 " + character + " 不存在")
            if new_name in characters and not merge:
                return self._err("角色 " + new_name + " 已存在，勾选合并后再试")
            source = characters.pop(character)
            merged = characters.get(new_name) or {}
            merged.update(source)
            characters[new_name] = merged
            if not await self._commit(characters):
                return self._err("写入 emotions.json 失败，已回滚")

        rows, warnings = self._emotion_rows()
        return self._ok(
            {
                "renamed": {"from": character, "to": new_name},
                "rows": rows,
                "warnings": warnings,
                "summary": emotion_pack.summarize(self._characters()),
            }
        )


    # ------------------------------------------------------ 导入 / 导出

    async def _handle_emotion_export(self):
        """直接返回裸 JSON 文本，供前端 blob 下载。这是唯一不套 envelope 的接口。"""
        try:
            only_characters = _split_csv(self._query("characters"))
            note = _as_text(self._query("note"), emotion_pack.MAX_TEXT_LENGTH)
            raw_items = self._query("items")
            only_items: List[Dict[str, str]] = []
            if raw_items:
                try:
                    parsed = json.loads(raw_items)
                    if isinstance(parsed, list):
                        only_items = [item for item in parsed if isinstance(item, dict)]
                except Exception:
                    only_items = []

            characters = emotion_pack.select_characters(
                self._clean_characters(), only_characters, only_items
            )
            if not characters:
                return self._err("没有可导出的感情条目")

            pack = emotion_pack.build_pack(
                characters,
                plugin_version=self._plugin_version(),
                note=note,
                source="webui",
            )
            text = emotion_pack.dumps_pack(pack)
            filename = _as_text(self._query("filename"))
            filename = (
                emotion_pack.safe_pack_filename(filename)
                if filename
                else emotion_pack.default_pack_filename(characters)
            )
            return QuartResponse(
                text,
                status=200,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Disposition": "attachment; filename=" + filename,
                    "Cache-Control": "no-store",
                },
            )
        except EmotionPackError as exc:
            return self._err(str(exc))
        except Exception as exc:
            logger.error("Genie TTS WebUI 导出失败: " + str(exc))
            return self._err("导出失败: " + str(exc))

    async def _handle_emotion_export_preview(self):
        """返回导出文本本身，给"复制到剪贴板"和下载受限时的兜底路径用。"""
        body = await self._read_body()
        try:
            only_characters = _split_csv(body.get("characters"))
            raw_items = body.get("items")
            only_items = (
                [item for item in raw_items if isinstance(item, dict)]
                if isinstance(raw_items, list)
                else []
            )
            characters = emotion_pack.select_characters(
                self._clean_characters(), only_characters, only_items
            )
            if not characters:
                return self._err("没有可导出的感情条目")
            pack = emotion_pack.build_pack(
                characters,
                plugin_version=self._plugin_version(),
                note=_as_text(body.get("note"), emotion_pack.MAX_TEXT_LENGTH),
                source="webui",
            )
            text = emotion_pack.dumps_pack(pack)
            filename = _as_text(body.get("filename"))
            filename = (
                emotion_pack.safe_pack_filename(filename)
                if filename
                else emotion_pack.default_pack_filename(characters)
            )
            return self._ok(
                {
                    "filename": filename,
                    "text": text,
                    "bytes": len(text.encode("utf-8")),
                    "summary": emotion_pack.summarize(characters),
                }
            )
        except EmotionPackError as exc:
            return self._err(str(exc))
        except Exception as exc:
            return self._err("生成导出文本失败: " + str(exc))

    async def _handle_emotion_import(self):
        body = await self._read_body()
        payload = body.get("payload")
        if payload is None:
            payload = body.get("text")
        if payload is None or (isinstance(payload, str) and not payload.strip()):
            return self._err("没有收到感情包内容")

        dry_run = _as_bool(body.get("dry_run"))
        try:
            mode = emotion_pack.normalize_import_mode(body.get("mode"))
            incoming, meta, invalid = emotion_pack.loads_pack(payload)
        except EmotionPackError as exc:
            return self._err(str(exc))
        except Exception as exc:
            return self._err("解析感情包失败: " + str(exc))

        if not incoming and not invalid:
            return self._err("感情包里没有任何条目")

        async with self._write_lock:
            current = self._clean_characters()
            merged, report = emotion_pack.compute_import(current, incoming, mode, invalid)
            report["meta"] = {
                key: meta.get(key)
                for key in ("format", "version", "exported_at", "plugin_version", "note", "source")
                if meta.get(key) is not None
            }
            report["summary_text"] = emotion_pack.describe_report(report)
            report["dry_run"] = dry_run

            if dry_run or not report.get("changed"):
                rows, warnings = self._emotion_rows()
                return self._ok({"report": report, "rows": rows, "warnings": warnings})

            if not await self._commit(merged):
                return self._err("写入 emotions.json 失败，已回滚")

        rows, warnings = self._emotion_rows()
        return self._ok({"report": report, "rows": rows, "warnings": warnings})

    # ------------------------------------------------------------ 快照

    def _list_packs(self) -> List[Dict[str, Any]]:
        try:
            base = self._pack_dir()
        except Exception:
            return []
        packs: List[Dict[str, Any]] = []
        try:
            entries = sorted(
                (p for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return []
        for item in entries[:MAX_PACK_FILES]:
            info: Dict[str, Any] = {
                "filename": item.name,
                "bytes": 0,
                "modified": "",
                "characters": 0,
                "emotions": 0,
                "note": "",
                "error": "",
            }
            try:
                stat = item.stat()
                info["bytes"] = stat.st_size
                info["modified"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                )
                characters, meta, _invalid = emotion_pack.loads_pack(
                    item.read_text(encoding="utf-8-sig")
                )
                counts = emotion_pack.summarize(characters)
                info["characters"] = counts["characters"]
                info["emotions"] = counts["emotions"]
                info["note"] = _as_text(meta.get("note"), 200)
                info["exported_at"] = _as_text(meta.get("exported_at"))
            except Exception as exc:
                info["error"] = str(exc)
            packs.append(info)
        return packs

    async def _handle_packs(self):
        try:
            return self._ok(
                {"packs": self._list_packs(), "directory": str(self._pack_dir())}
            )
        except Exception as exc:
            return self._err("读取快照目录失败: " + str(exc))

    async def _handle_pack_save(self):
        body = await self._read_body()
        try:
            only_characters = _split_csv(body.get("characters"))
            raw_items = body.get("items")
            only_items = (
                [item for item in raw_items if isinstance(item, dict)]
                if isinstance(raw_items, list)
                else []
            )
            characters = emotion_pack.select_characters(
                self._clean_characters(), only_characters, only_items
            )
            if not characters:
                return self._err("没有可保存的感情条目")
            filename = _as_text(body.get("filename"))
            filename = (
                emotion_pack.safe_pack_filename(filename)
                if filename
                else emotion_pack.default_pack_filename(characters)
            )
            pack = emotion_pack.build_pack(
                characters,
                plugin_version=self._plugin_version(),
                note=_as_text(body.get("note"), emotion_pack.MAX_TEXT_LENGTH),
                source="webui",
            )
            target = self._pack_dir() / filename
            if target.exists() and not _as_bool(body.get("overwrite")):
                return self._err("快照 " + filename + " 已存在，勾选覆盖后再试")
            target.write_text(emotion_pack.dumps_pack(pack), encoding="utf-8")
            return self._ok(
                {
                    "filename": filename,
                    "summary": emotion_pack.summarize(characters),
                    "packs": self._list_packs(),
                }
            )
        except EmotionPackError as exc:
            return self._err(str(exc))
        except Exception as exc:
            logger.error("Genie TTS WebUI 保存快照失败: " + str(exc))
            return self._err("保存快照失败: " + str(exc))

    async def _handle_pack_delete(self):
        body = await self._read_body()
        filename = emotion_pack.safe_pack_filename(_as_text(body.get("filename")), "")
        if not filename:
            return self._err("文件名不能为空")
        try:
            target = self._pack_dir() / filename
            if not target.is_file():
                return self._err("快照 " + filename + " 不存在")
            target.unlink()
            return self._ok({"deleted": filename, "packs": self._list_packs()})
        except Exception as exc:
            return self._err("删除快照失败: " + str(exc))

    async def _handle_pack_restore(self):
        body = await self._read_body()
        filename = emotion_pack.safe_pack_filename(_as_text(body.get("filename")), "")
        if not filename:
            return self._err("文件名不能为空")
        dry_run = _as_bool(body.get("dry_run"))
        try:
            mode = emotion_pack.normalize_import_mode(body.get("mode"))
            target = self._pack_dir() / filename
            if not target.is_file():
                return self._err("快照 " + filename + " 不存在")
            incoming, meta, invalid = emotion_pack.loads_pack(
                target.read_text(encoding="utf-8-sig")
            )
        except EmotionPackError as exc:
            return self._err(str(exc))
        except Exception as exc:
            return self._err("读取快照失败: " + str(exc))

        async with self._write_lock:
            merged, report = emotion_pack.compute_import(
                self._clean_characters(), incoming, mode, invalid
            )
            report["summary_text"] = emotion_pack.describe_report(report)
            report["dry_run"] = dry_run
            report["filename"] = filename
            if dry_run or not report.get("changed"):
                rows, warnings = self._emotion_rows()
                return self._ok({"report": report, "rows": rows, "warnings": warnings})
            if not await self._commit(merged):
                return self._err("写入 emotions.json 失败，已回滚")

        rows, warnings = self._emotion_rows()
        return self._ok({"report": report, "rows": rows, "warnings": warnings})

    async def _handle_pack_download(self):
        filename = emotion_pack.safe_pack_filename(self._query("filename"), "")
        if not filename:
            return self._err("文件名不能为空")
        try:
            target = self._pack_dir() / filename
            if not target.is_file():
                return self._err("快照 " + filename + " 不存在")
            return QuartResponse(
                target.read_text(encoding="utf-8-sig"),
                status=200,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Disposition": "attachment; filename=" + filename,
                    "Cache-Control": "no-store",
                },
            )
        except Exception as exc:
            return self._err("下载快照失败: " + str(exc))

    # ------------------------------------------------------------ 服务器

    async def _handle_servers(self):
        engine = getattr(self.plugin, "tts_engine", None)
        if engine is None:
            return self._err("TTS 引擎尚未初始化")
        try:
            probes = await engine.probe_servers(timeout=8.0)
        except Exception as exc:
            return self._err("探测服务器失败: " + str(exc))

        local_characters = set(str(name) for name in self._characters().keys())
        remote_characters: set = set()
        servers: List[Dict[str, Any]] = []
        for probe in probes:
            characters = [str(name) for name in (probe.get("characters") or [])]
            remote_characters.update(characters)
            latency = probe.get("latency")
            servers.append(
                {
                    "url": _as_text(probe.get("url")),
                    "ok": bool(probe.get("ok")),
                    "busy": bool(probe.get("busy")),
                    "latency_ms": round(float(latency) * 1000, 1) if latency else None,
                    "characters": characters,
                    "character_count": len(characters),
                    "error": _as_text(probe.get("error")),
                }
            )

        config = self.plugin.config
        missing_remote = sorted(local_characters - remote_characters) if remote_characters else []
        unused_remote = sorted(remote_characters - local_characters) if remote_characters else []
        return self._ok(
            {
                "servers": servers,
                "online": sum(1 for item in servers if item["ok"]),
                "total": len(servers),
                "queue_size": engine.queue_size(),
                "mismatch": {
                    "missing_on_server": missing_remote,
                    "not_registered_locally": unused_remote,
                },
                "keepalive": {
                    "enabled": _as_bool(config.get("enable_space_keepalive")),
                    "interval_minutes": _as_int(
                        config.get("space_keepalive_interval_minutes"), 25
                    ),
                    "urls": list(self.plugin._get_keepalive_urls() or []),
                    "running": bool(getattr(self.plugin, "_keepalive_task", None)),
                },
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    # ------------------------------------------------ 分段与停顿可视化

    async def _handle_preview(self):
        """把文本走一遍与真实合成一致的预处理链，逐块展示会补多少静音。

        这是工作台最有用的一块：停顿到底加没加上、加在哪，肉眼可见。
        """
        body = await self._read_body()
        text = _as_text(body.get("text"), MAX_PREVIEW_TEXT_LENGTH)
        if not text:
            return self._err("预览文本不能为空")

        engine = getattr(self.plugin, "tts_engine", None)
        if engine is None:
            return self._err("TTS 引擎尚未初始化")
        config = self.plugin.config

        steps: List[Dict[str, str]] = []
        current = text

        pause_enabled = _as_bool(
            body.get("enable_custom_pause_marker"),
            _as_bool(config.get("enable_custom_pause_marker")),
        )
        declared_pause_ms = int(round(pause_budget_seconds(current) * 1000))
        marker_count = len(PAUSE_MARKER_PATTERN.findall(current))
        if not pause_enabled and marker_count:
            stripped = PAUSE_MARKER_PATTERN.sub(" ", current)
            stripped = " ".join(stripped.split())
            if stripped != current:
                steps.append(
                    {
                        "name": "剥除停顿标记",
                        "detail": "自定义停顿标记未启用，"
                        + str(marker_count)
                        + " 个 [pause] 会被删掉而不是生效",
                    }
                )
                current = stripped
            declared_pause_ms = 0

        if _as_bool(config.get("enable_tts_text_cleaning")):
            try:
                cleaned = engine._clean_text_for_tts(current)
            except Exception as exc:
                return self._err("文本清洗正则报错: " + str(exc))
            if cleaned != current:
                steps.append({"name": "文本清洗", "detail": "按清洗正则移除了部分内容"})
            current = cleaned
            if not current:
                return self._ok(
                    {
                        "steps": steps,
                        "chunks": [],
                        "blocked": "清洗后文本为空，不会合成",
                    }
                )

        max_text_length = _as_int(
            config.get("tts_max_text_length"), DEFAULT_MAX_TEXT_LENGTH
        )
        truncated = False
        if max_text_length > 0 and len(current) > max_text_length:
            shortened = engine._truncate_text_for_tts(current, max_text_length)
            if shortened and shortened != current:
                steps.append(
                    {
                        "name": "长度截断",
                        "detail": str(len(current))
                        + " 字 超过上限 "
                        + str(max_text_length)
                        + " 字，截到 "
                        + str(len(shortened))
                        + " 字；后面 "
                        + str(len(current) - len(shortened))
                        + " 字不会被朗读",
                    }
                )
                current = shortened
                truncated = True

        language = _as_text(body.get("language")) or _as_text(
            config.get("tts_default_language")
        )
        punctuated = engine._ensure_terminal_punctuation(current, language or None)
        if punctuated != current:
            steps.append({"name": "补句末标点", "detail": "降低尾音被截断的概率"})
            current = punctuated

        if not has_pronounceable(current):
            return self._ok(
                {
                    "steps": steps,
                    "chunks": [],
                    "text": current,
                    "blocked": "没有可发音内容，会直接跳过合成（这正是防止整条参考音频被返回的保护）",
                }
            )

        splitting = _as_bool(
            body.get("enable_sentence_splitting"),
            _as_bool(config.get("enable_sentence_splitting")),
        )
        sentences_per_chunk = _as_int(
            body.get("sentences_per_chunk"), _as_int(config.get("sentences_per_chunk"), 2)
        )
        gap_ms = _as_int(body.get("chunk_gap_ms"), engine._chunk_gap_ms())
        gap_ms = max(0, min(gap_ms, MAX_CHUNK_GAP_MS))

        if splitting:
            raw_chunks = engine._split_text_into_chunks(current, sentences_per_chunk)
            if not raw_chunks:
                return self._ok(
                    {
                        "steps": steps,
                        "chunks": [],
                        "text": current,
                        "blocked": "切分后没有可合成的块",
                    }
                )
            steps.append(
                {
                    "name": "分段合成",
                    "detail": "每 "
                    + str(sentences_per_chunk)
                    + " 句一块，共 "
                    + str(len(raw_chunks))
                    + " 块，基准间隔 "
                    + str(gap_ms)
                    + "ms",
                }
            )
        else:
            raw_chunks = [current]
            steps.append({"name": "整段合成", "detail": "未开启分段，整条文本一次合成"})

        chunks: List[Dict[str, Any]] = []
        total_gap_ms = 0
        previous: Optional[str] = None
        for index, chunk in enumerate(raw_chunks):
            boundary = 0
            if index > 0:
                boundary = engine._boundary_gap_ms(previous, gap_ms)
                total_gap_ms += boundary
            chunk_pause_ms = (
                int(round(pause_budget_seconds(chunk) * 1000)) if pause_enabled else 0
            )
            auto_pause_ms = int(round(auto_pause_budget_seconds(chunk) * 1000))
            tail = chunk.rstrip(" \t\r\n")
            chunks.append(
                {
                    "index": index + 1,
                    "text": chunk,
                    "chars": len(chunk),
                    "pronounceable": count_pronounceable(chunk),
                    "gap_before_ms": boundary,
                    "custom_pause_ms": chunk_pause_ms,
                    "auto_pause_ms": auto_pause_ms,
                    "tail": tail[-1] if tail else "",
                    "voiceable": has_pronounceable(chunk),
                }
            )
            previous = chunk

        pronounceable_total = count_pronounceable(current)
        auto_pause_total = int(round(auto_pause_budget_seconds(current) * 1000))
        custom_pause_total = declared_pause_ms if pause_enabled else 0
        expected_seconds = engine._expected_audio_seconds(current)

        return self._ok(
            {
                "steps": steps,
                "chunks": chunks,
                "text": current,
                "truncated": truncated,
                "blocked": "",
                "totals": {
                    "chunks": len(chunks),
                    "chars": len(current),
                    "pronounceable": pronounceable_total,
                    "chunk_gap_ms": total_gap_ms,
                    "custom_pause_ms": custom_pause_total,
                    "auto_pause_ms": auto_pause_total,
                    "pause_total_ms": total_gap_ms + custom_pause_total + auto_pause_total,
                    "expected_seconds": round(expected_seconds, 2),
                },
                "settings": {
                    "enable_sentence_splitting": splitting,
                    "sentences_per_chunk": sentences_per_chunk,
                    "chunk_gap_ms": gap_ms,
                    "enable_custom_pause_marker": pause_enabled,
                    "language": language,
                    "max_text_length": max_text_length,
                },
            }
        )

    # ------------------------------------------------------------ 合成

    async def _handle_synthesize(self):
        body = await self._read_body()
        text = _as_text(body.get("text"), MAX_SYNTH_TEXT_LENGTH + 1)
        if not text:
            return self._err("要合成的文本不能为空")
        if len(text) > MAX_SYNTH_TEXT_LENGTH:
            return self._err(
                "试听文本请控制在 " + str(MAX_SYNTH_TEXT_LENGTH) + " 字以内"
            )

        engine = getattr(self.plugin, "tts_engine", None)
        if engine is None:
            return self._err("TTS 引擎尚未初始化")
        if not engine._server_urls():
            return self._err("还没有配置 TTS 服务器地址")

        character = _as_text(body.get("character"))
        emotion = _as_text(body.get("emotion"))
        ref_path = _as_text(body.get("ref_audio_path"))
        ref_text = _as_text(body.get("ref_audio_text"))

        if ref_path and ref_text:
            # 直接试听一份还没登记的参考音频（感情库里点"试听候选"）
            if not emotion_pack.is_safe_ref_audio_path(ref_path):
                return self._err("参考音频路径必须是相对路径且不能包含 ..")
            if not character:
                character = _as_text(self.plugin.config.get("default_character"))
        else:
            character, emotion, entry = self.plugin._resolve_tts_profile(
                "webui:studio", character or None, emotion or None
            )
            if not entry:
                return self._err(
                    "找不到可用的参考音频："
                    + (character or "未指定角色")
                    + " / "
                    + (emotion or "未指定感情")
                )
            ref_path = _as_text(entry.get("ref_audio_path"))
            ref_text = _as_text(entry.get("ref_audio_text"))
            if not body.get("language"):
                body["language"] = _as_text(entry.get("language"))

        language = _as_text(body.get("language")) or None
        started = time.perf_counter()
        try:
            audio_path = await engine.synthesize(
                character, ref_path, ref_text, text, "webui:studio", language
            )
        except Exception as exc:
            logger.error("Genie TTS WebUI 合成异常: " + str(exc))
            return self._err("合成异常: " + str(exc))
        elapsed = time.perf_counter() - started

        if not audio_path or not os.path.exists(audio_path):
            return self._err(
                "合成失败：文本可能没有可发音内容，或服务器未返回音频。"
                "先用分段预览确认文本，再到服务器页看探测结果。"
            )

        try:
            size = os.path.getsize(audio_path)
        except OSError:
            size = 0
        payload: Dict[str, Any] = {
            "character": character,
            "emotion": emotion,
            "language": language or "",
            "text": text,
            "path": audio_path,
            "filename": os.path.basename(audio_path),
            "bytes": size,
            "mime": "audio/wav",
            "duration_seconds": _wav_duration_seconds(size),
            "expected_seconds": round(engine._expected_audio_seconds(text), 2),
            "elapsed_seconds": round(elapsed, 2),
            "queue_size": engine.queue_size(),
            "created_at": time.strftime("%H:%M:%S"),
            "audio_base64": "",
            "too_large": False,
        }
        if size and size <= MAX_INLINE_AUDIO_BYTES:
            try:
                with open(audio_path, "rb") as handle:
                    payload["audio_base64"] = base64.b64encode(handle.read()).decode(
                        "ascii"
                    )
            except OSError as exc:
                return self._err("读取音频文件失败: " + str(exc))
        else:
            payload["too_large"] = True

        self._history.append(
            {
                key: payload[key]
                for key in (
                    "character",
                    "emotion",
                    "text",
                    "filename",
                    "bytes",
                    "duration_seconds",
                    "elapsed_seconds",
                    "created_at",
                )
            }
        )
        if len(self._history) > MAX_HISTORY_ROWS:
            del self._history[:-MAX_HISTORY_ROWS]
        return self._ok(payload)


    # ------------------------------------------------------------ 配置

    def _schema(self) -> Dict[str, Any]:
        schema = getattr(self.plugin.config, "schema", None)
        if isinstance(schema, dict) and schema:
            return schema
        try:
            raw = (Path(__file__).parent / "_conf_schema.json").read_text(
                encoding="utf-8-sig"
            )
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            logger.warning("Genie TTS WebUI: 读取配置模板失败: " + str(exc))
            return {}

    def _describe_field(self, key: str, meta: Any, value: Any) -> Dict[str, Any]:
        meta = meta if isinstance(meta, dict) else {}
        field_type = _as_text(meta.get("type")) or "string"
        field: Dict[str, Any] = {
            "key": key,
            "title": _as_text(meta.get("title")) or key,
            "description": _as_text(meta.get("description")),
            "hint": _as_text(meta.get("hint")),
            "type": field_type,
            "options": [str(item) for item in (meta.get("options") or [])],
            "secret": _is_secret_key(key),
        }
        default = meta.get("default")

        if field_type == "object":
            children_meta = meta.get("items")
            children_meta = children_meta if isinstance(children_meta, dict) else {}
            source = value if isinstance(value, dict) else {}
            fallback = default if isinstance(default, dict) else {}
            children: List[Dict[str, Any]] = []
            for child_key, child_meta in children_meta.items():
                if _as_bool((child_meta or {}).get("invisible")):
                    continue
                child_value = source.get(child_key, fallback.get(child_key))
                children.append(
                    self._describe_field(str(child_key), child_meta, child_value)
                )
            field["children"] = children
            return field

        if field_type == "list":
            items = value if isinstance(value, list) else (default if isinstance(default, list) else [])
            field["value"] = [_as_text(item) for item in items]
            field["default"] = [_as_text(item) for item in (default or []) if _as_text(item)]
            return field

        resolved = default if value is None else value
        if field_type == "bool":
            field["value"] = _as_bool(resolved, _as_bool(default))
            field["default"] = _as_bool(default)
        elif field_type == "int":
            field["value"] = _as_int(resolved, _as_int(default, 0))
            field["default"] = _as_int(default, 0)
        elif field_type == "float":
            field["value"] = _as_float(resolved, _as_float(default, 0.0))
            field["default"] = _as_float(default, 0.0)
        else:
            text = "" if resolved is None else str(resolved)
            field["value"] = MASKED_SECRET if (field["secret"] and text) else text
            field["default"] = "" if default is None else str(default)
        return field

    async def _handle_config(self):
        try:
            schema = self._schema()
            if not schema:
                return self._err("读不到配置模板 _conf_schema.json")
            config = self.plugin.config
            grouped: List[Dict[str, Any]] = []
            claimed: set = set()

            for group_id, title, description, keys in CONFIG_GROUPS:
                fields: List[Dict[str, Any]] = []
                for key in keys:
                    meta = schema.get(key)
                    if meta is None:
                        continue
                    claimed.add(key)
                    if _as_bool(meta.get("invisible")):
                        continue
                    fields.append(self._describe_field(key, meta, config.get(key)))
                if fields:
                    grouped.append(
                        {
                            "id": group_id,
                            "title": title,
                            "description": description,
                            "fields": fields,
                        }
                    )

            leftovers = [
                self._describe_field(key, meta, config.get(key))
                for key, meta in schema.items()
                if key not in claimed and not _as_bool((meta or {}).get("invisible"))
            ]
            if leftovers:
                grouped.append(
                    {
                        "id": "misc",
                        "title": "其它",
                        "description": "尚未归组的配置项（新增配置会自动出现在这里）。",
                        "fields": leftovers,
                    }
                )

            return self._ok(
                {
                    "groups": grouped,
                    "restart_required_keys": sorted(RESTART_REQUIRED_KEYS),
                    "masked": MASKED_SECRET,
                    "total": sum(len(group["fields"]) for group in grouped),
                }
            )
        except Exception as exc:
            logger.error("Genie TTS WebUI config 失败: " + str(exc))
            return self._err("读取配置失败: " + str(exc))

    def _coerce(self, meta: Any, raw: Any, original: Any) -> Any:
        """按 schema 类型把前端传来的值转成配置该有的类型。"""
        meta = meta if isinstance(meta, dict) else {}
        field_type = _as_text(meta.get("type")) or "string"
        default = meta.get("default")

        if field_type == "object":
            children_meta = meta.get("items")
            children_meta = children_meta if isinstance(children_meta, dict) else {}
            base = dict(original) if isinstance(original, dict) else {}
            incoming = raw if isinstance(raw, dict) else {}
            for child_key, child_meta in children_meta.items():
                if child_key not in incoming:
                    continue
                base[child_key] = self._coerce(
                    child_meta, incoming[child_key], base.get(child_key)
                )
            return base

        if field_type == "list":
            if isinstance(raw, list):
                items = raw
            else:
                text = _as_text(raw)
                items = [line.strip() for line in text.replace(",", "\n").split("\n")]
            return [str(item).strip() for item in items if str(item).strip()]

        if field_type == "bool":
            return _as_bool(raw, _as_bool(default))
        if field_type == "int":
            return _as_int(raw, _as_int(original, _as_int(default, 0)))
        if field_type == "float":
            return _as_float(raw, _as_float(original, _as_float(default, 0.0)))

        text = "" if raw is None else str(raw)
        if text == MASKED_SECRET:
            # 掩码占位符一律表示"保持原值"，避免把密钥回写成占位符字符串。
            return original
        return text

    async def _handle_config_save(self):
        body = await self._read_body()
        values = body.get("values")
        if not isinstance(values, dict) or not values:
            return self._err("没有需要保存的配置项")

        schema = self._schema()
        if not schema:
            return self._err("读不到配置模板 _conf_schema.json")

        config = self.plugin.config
        saved: List[str] = []
        rejected: List[Dict[str, str]] = []
        for key, raw in values.items():
            meta = schema.get(key)
            if meta is None:
                rejected.append({"key": str(key), "reason": "配置模板里没有这个键"})
                continue
            try:
                coerced = self._coerce(meta, raw, config.get(key))
            except Exception as exc:
                rejected.append({"key": str(key), "reason": str(exc)})
                continue
            options = [str(item) for item in (meta.get("options") or [])]
            if options and str(coerced) not in options:
                rejected.append(
                    {"key": str(key), "reason": "只能是 " + " / ".join(options)}
                )
                continue
            config[key] = coerced
            saved.append(str(key))

        if not saved:
            reason = rejected[0]["reason"] if rejected else "没有有效的配置项"
            return self._err(reason)

        try:
            config.save_config()
        except Exception as exc:
            logger.error("Genie TTS WebUI 保存配置失败: " + str(exc))
            return self._err("保存配置失败: " + str(exc))

        needs_reload = any(key in RESTART_REQUIRED_KEYS for key in saved)
        return self._ok(
            {
                "saved": saved,
                "rejected": rejected,
                "needs_reload": needs_reload,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    # ------------------------------------------------------------ 会话

    async def _handle_sessions(self):
        plugin = self.plugin
        rows: List[Dict[str, Any]] = []
        session_ids = set()
        session_ids.update(getattr(plugin, "active_sessions", set()) or set())
        session_ids.update(getattr(plugin, "w_active_sessions", set()) or set())
        session_ids.update((getattr(plugin, "session_emotions", {}) or {}).keys())
        session_ids.update((getattr(plugin, "session_w_settings", {}) or {}).keys())

        for session_id in sorted(str(item) for item in session_ids):
            profile = (getattr(plugin, "session_emotions", {}) or {}).get(session_id) or {}
            w_profile = (getattr(plugin, "session_w_settings", {}) or {}).get(session_id) or {}
            rows.append(
                {
                    "session_id": session_id,
                    "tts_active": session_id in (getattr(plugin, "active_sessions", set()) or set()),
                    "w_active": session_id in (getattr(plugin, "w_active_sessions", set()) or set()),
                    "character": _as_text(profile.get("character")),
                    "emotion": _as_text(profile.get("emotion")),
                    "w_character": _as_text(w_profile.get("character")),
                    "w_emotion": _as_text(w_profile.get("emotion")),
                    "has_last_audio": session_id
                    in (getattr(plugin, "last_audio_paths", {}) or {}),
                }
            )

        return self._ok(
            {
                "sessions": rows,
                "groups": {
                    "active": sorted(str(item) for item in (getattr(plugin, "active_groups", set()) or set())),
                    "inactive": sorted(str(item) for item in (getattr(plugin, "inactive_groups", set()) or set())),
                },
                "persistence": _as_bool(plugin.config.get("enable_state_persistence"), True),
                "group_default": _as_bool(plugin.config.get("enable_group_tts_by_default")),
            }
        )

    async def _handle_session_toggle(self):
        body = await self._read_body()
        target = _as_text(body.get("target"))
        scope = _as_text(body.get("scope")) or "session"
        enabled = _as_bool(body.get("enabled"))
        kind = _as_text(body.get("kind")) or "tts"
        if not target:
            return self._err("会话 ID / 群号不能为空")

        plugin = self.plugin
        if scope == "group":
            group_id = plugin._normalize_group_id(target)
            if not group_id:
                return self._err("群号不合法")
            if enabled:
                plugin.active_groups.add(group_id)
                plugin.inactive_groups.discard(group_id)
            else:
                plugin.active_groups.discard(group_id)
                plugin.inactive_groups.add(group_id)
        elif kind == "w":
            if enabled:
                plugin.w_active_sessions.add(target)
            else:
                plugin.w_active_sessions.discard(target)
        else:
            if enabled:
                plugin.active_sessions.add(target)
            else:
                plugin.active_sessions.discard(target)

        try:
            await plugin._persist_state()
        except Exception as exc:
            logger.warning("Genie TTS WebUI: 持久化会话状态失败: " + str(exc))

        return await self._handle_sessions()

    # ------------------------------------------------------------ 偏好

    async def _handle_prefs(self):
        theme = DEFAULT_THEME
        density = DEFAULT_DENSITY
        tab = ""
        log_paint = True
        try:
            # default 在部分 AstrBot 版本里是必填位置参数（不是关键字缺省），
            # 少传就会 TypeError，界面偏好读不回来 —— 主题/密度/分区每次都退回默认值。
            stored = await self.plugin.get_kv_data(PREFS_KV_KEY, None)
            if isinstance(stored, dict):
                candidate = _as_text(stored.get("theme"))
                if candidate in THEME_IDS:
                    theme = candidate
                candidate = _as_text(stored.get("density"))
                if candidate in DENSITIES:
                    density = candidate
                tab = _as_text(stored.get("tab"), 32)
                # 老快照里没有这个键，只有显式写了 false 才算关掉。
                if "log_paint" in stored:
                    log_paint = bool(stored.get("log_paint"))
        except Exception as exc:
            # 读不回来只影响主题/密度/分区的记忆，不影响功能，所以不往上抛；
            # 但要留一条 WARNING，不然「主题每次都变回月夜」会查不到原因。
            logger.warning("Genie TTS WebUI: 读取界面偏好失败: " + str(exc))
        return self._ok(
            {
                "theme": theme,
                "density": density,
                "tab": tab,
                "log_paint": log_paint,
                "themes": [dict(item) for item in THEMES],
                "densities": list(DENSITIES),
            }
        )

    async def _handle_prefs_save(self):
        body = await self._read_body()
        theme = _as_text(body.get("theme"))
        density = _as_text(body.get("density"))
        tab = _as_text(body.get("tab"), 32)
        if theme and theme not in THEME_IDS:
            return self._err("未知主题: " + theme)
        if density and density not in DENSITIES:
            return self._err("未知密度: " + density)
        payload = {
            "theme": theme or DEFAULT_THEME,
            "density": density or DEFAULT_DENSITY,
            "tab": tab,
            # 运行日志着色开关：前端传什么就存什么，缺省视为开。
            "log_paint": bool(body.get("log_paint", True)),
        }
        try:
            await self.plugin.put_kv_data(PREFS_KV_KEY, payload)
        except Exception as exc:
            return self._err("保存界面偏好失败: " + str(exc))
        return self._ok(payload)

    # ------------------------------------------------------------ 运行日志

    def _run_log(self):
        """拿插件持有的 RunLog；老版本插件对象上可能没有这个属性。"""
        return getattr(self.plugin, "run_log", None)

    @staticmethod
    def _log_dictionaries() -> Dict[str, Any]:
        """前端渲染 badge / 下拉框要用的全量标签表。"""
        return {
            "levels": list(run_log_mod.LEVEL_NAMES),
            "issue_level": run_log_mod.ISSUE_LEVEL_KEY,
            "tags": [
                {"key": key, "label": label}
                for key, label in run_log_mod.TAG_LABELS.items()
            ],
            "sources": [
                {"key": key, "label": label}
                for key, label in run_log_mod.SYNTH_SOURCES.items()
            ],
            "statuses": [
                {"key": key, "label": label}
                for key, label in run_log_mod.SYNTH_STATUSES.items()
            ],
        }

    def _log_filters(self, for_synth: bool) -> Dict[str, str]:
        if for_synth:
            return {
                "status": _as_text(self._query("status"), 32),
                "source": _as_text(self._query("source"), 32),
                "character": _as_text(self._query("character"), 120),
                "emotion": _as_text(self._query("emotion"), 120),
                "session": _as_text(self._query("session"), 200),
                "search": _as_text(self._query("search"), 200),
            }
        return {
            "level": _as_text(self._query("level"), 32),
            "tag": _as_text(self._query("tag"), 64),
            "session": _as_text(self._query("session"), 200),
            "search": _as_text(self._query("search"), 200),
        }

    async def _handle_logs(self):
        log = self._run_log()
        if log is None:
            return self._err("当前插件实例没有运行日志模块，请重载插件")
        try:
            limit = _clamp_int(
                _as_int(self._query("limit"), 120), 1, run_log_mod.MAX_CAPACITY
            )
            offset = max(0, _as_int(self._query("offset"), 0))
            filters = self._log_filters(False)
            items, total = log.buffer.query(limit=limit, offset=offset, **filters)
            return self._ok(
                {
                    "items": items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "facets": log.buffer.facets(),
                    "dictionaries": self._log_dictionaries(),
                    "enabled": bool(log.enabled),
                    "attached": bool(log.attached),
                    "filters": filters,
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except Exception as exc:
            logger.error("Genie TTS WebUI 读取运行日志失败: " + str(exc))
            return self._err("读取运行日志失败: " + str(exc))

    async def _handle_synth_logs(self):
        log = self._run_log()
        if log is None:
            return self._err("当前插件实例没有运行日志模块，请重载插件")
        try:
            limit = _clamp_int(
                _as_int(self._query("limit"), 40), 1, run_log_mod.MAX_SYNTH_CAPACITY
            )
            offset = max(0, _as_int(self._query("offset"), 0))
            filters = self._log_filters(True)
            items, total = log.synth.query(limit=limit, offset=offset, **filters)
            stat_limit = _clamp_int(_as_int(self._query("stats"), 60), 1, 200)
            return self._ok(
                {
                    "items": items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "facets": log.synth.facets(),
                    "emotions": log.synth.emotion_stats(stat_limit),
                    "dictionaries": self._log_dictionaries(),
                    "enabled": bool(log.enabled),
                    "filters": filters,
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except Exception as exc:
            logger.error("Genie TTS WebUI 读取合成记录失败: " + str(exc))
            return self._err("读取合成记录失败: " + str(exc))

    async def _handle_logs_clear(self):
        log = self._run_log()
        if log is None:
            return self._err("当前插件实例没有运行日志模块，请重载插件")
        body = await self._read_body()
        scope = _as_text(body.get("scope"), 16) or "all"
        if scope not in ("all", "logs", "synths"):
            return self._err("未知清理范围: " + scope)
        try:
            dropped = log.clear(scope)
        except Exception as exc:
            return self._err("清空失败: " + str(exc))
        total = _as_int(dropped.get("logs"), 0) + _as_int(dropped.get("synths"), 0)
        return self._ok({"scope": scope, "dropped": dropped, "total": total})

    async def _handle_logs_export(self):
        """裸文本下载。与感情包导出一样不套 envelope。"""
        log = self._run_log()
        if log is None:
            return self._err("当前插件实例没有运行日志模块，请重载插件")
        kind = _as_text(self._query("kind"), 16) or "logs"
        if kind not in ("logs", "synths"):
            return self._err("未知导出类型: " + kind)
        try:
            if kind == "synths":
                limit = _clamp_int(
                    _as_int(self._query("limit"), 0), 0, run_log_mod.MAX_SYNTH_CAPACITY
                )
                text = log.synth.export_text(limit, **self._log_filters(True))
            else:
                limit = _clamp_int(
                    _as_int(self._query("limit"), 0), 0, run_log_mod.MAX_CAPACITY
                )
                text = log.buffer.export_text(limit, **self._log_filters(False))
        except Exception as exc:
            logger.error("Genie TTS WebUI 导出日志失败: " + str(exc))
            return self._err("导出日志失败: " + str(exc))

        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = "genie-tts-" + kind + "-" + stamp + ".txt"
        return QuartResponse(
            text,
            status=200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": "attachment; filename=" + filename,
                "Cache-Control": "no-store",
            },
        )

