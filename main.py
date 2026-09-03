import asyncio
import httpx
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.api.provider import LLMResponse, ProviderRequest

# 从新模块导入功能
from . import emotion_pack
from .emotion_pack import EmotionPackError
from .emotion_manager import EmotionManager
from .tts_engine import DEFAULT_MAX_TEXT_LENGTH, TTSEngine, has_pronounceable
from .external_apis import translate_text


@register(
    "astrbot_plugin_genie_tts_llm",
    "Whereis-Alice",
    "一个通过 LLM、翻译和 Genie TTS 实现语音合成的插件，支持主动语音工具",
    "1.9.2",
    "https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm",
)
class GenieTtsLlmPlugin(Star):
    # 会话开关/音色选择的持久化键。AstrBot 的插件 KV 存储按 plugin_id 隔离。
    STATE_KV_KEY = "session_state_v1"
    # 插件版本号：WebUI 总览与感情包元数据都会读它。
    PLUGIN_VERSION = "1.9.2"
    # 感情包快照目录名（位于插件数据目录下）。
    PACK_DIR_NAME = "emotion_packs"
    # 能被识别为「导入模式」的 token，真正的语义交给 emotion_pack 归一化。
    PACK_MODE_TOKENS = frozenset(
        {
            "merge",
            "overwrite",
            "replace",
            "skip",
            "update",
            "force",
            "合并",
            "覆盖",
            "替换",
            "清空",
        }
    )
    # 只预演不落盘的关键字。
    PACK_DRY_TOKENS = frozenset(
        {"试运行", "预演", "预览", "dry", "dryrun", "dry-run", "-n", "--dry-run"}
    )
    # 最多记住多少个会话的"最近一条语音"，避免长期运行后字典无限增长。
    MAX_REMEMBERED_AUDIO = 200
    # 清理 LLM 复读的历史 TTS 失败提示。整条匹配，不留残渣。
    TTS_FAILURE_NOTICE_PATTERN = re.compile(
        r"\n?\(TTS(?:失败[：:][^)]*|合成失败|音频发送失败)\)"
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.active_sessions: Set[str] = set()
        self.w_active_sessions: Set[str] = set()
        self.active_groups: Set[str] = set()  # 新增：群组级TTS开关
        self.inactive_groups: Set[str] = set()
        self.session_emotions: Dict[str, Dict[str, str]] = {}
        self.session_w_settings: Dict[str, Dict[str, str]] = {}
        self.last_tts_trigger_at: Dict[str, float] = {}
        self.skip_next_auto_tts_sessions: Set[str] = set()
        self.pending_auto_tts_sessions: Set[str] = set()
        self.checked_auto_tts_sessions: Set[str] = set()
        self._keepalive_stop_event = asyncio.Event()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._llm_translation_conflict_logged = False
        # 会话 -> 最近一次成功合成的临时音频路径，供 /tts-again 复用。
        self.last_audio_paths: Dict[str, str] = {}
        self._state_lock = asyncio.Lock()
        self._state_restored = asyncio.Event()
        self._state_restore_task: Optional[asyncio.Task] = None

        # 初始化辅助模块
        plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_genie_tts_llm")
        # WebUI 工作台与感情包指令都要用到数据目录，存成属性方便复用。
        self.plugin_data_dir = plugin_data_dir
        emotions_file_path = plugin_data_dir / "emotions.json"
        self.emotion_manager = EmotionManager(emotions_file_path)
        # 感情库整体写盘要串行：WebUI 与聊天指令可能并发提交。
        self._emotion_write_lock = asyncio.Lock()

        self.http_client = httpx.AsyncClient(timeout=300.0)
        self.tts_engine = TTSEngine(self.config, self.http_client, plugin_data_dir)

        if self.config.get("enable_space_keepalive"):
            self._keepalive_task = asyncio.create_task(self._keep_alive_loop())

        # 初始化白名单群组（自动开启 TTS）
        whitelist = self.config.get("group_whitelist", [])
        for group_id in whitelist:
            normalized_group_id = self._normalize_group_id(group_id)
            if normalized_group_id:
                self.active_groups.add(normalized_group_id)
                logger.info(f"白名单群组 [{normalized_group_id}] 已自动开启语音合成。")

        if self.config.get("enable_group_tts_by_default", False):
            logger.info("已开启全部群默认语音合成；群组黑名单仍优先。")

        # KV 读取是协程，__init__ 里只能挂后台任务；恢复完成后置位 _state_restored。
        self._state_restore_task = asyncio.create_task(self._restore_state())

        # 注册 WebUI 语音合成工作台。注册失败只记日志，不影响插件主流程。
        self.web_api = None
        try:
            from .web_api import GenieWebApi

            self.web_api = GenieWebApi(self)
            # register() 内部已经打过日志，这里只兜住异常。
            self.web_api.register(context)
        except Exception as exc:
            self.web_api = None
            logger.error(f"Genie TTS: WebUI 工作台注册失败: {exc}")

        logger.info("LLM TTS 插件已加载。")

    # ------------------------------------------------------------ 状态持久化

    @staticmethod
    def _as_str_set(value: object) -> Set[str]:
        """把持久化数据里的任意结构安全转成字符串集合。"""
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(item) for item in value if item}

    @staticmethod
    def _as_profile_map(
        value: object, keys: Tuple[str, ...]
    ) -> Dict[str, Dict[str, str]]:
        """转成 {会话: {字段: 值}}；字段不齐的脏数据直接丢弃，避免后续 KeyError。"""
        if not isinstance(value, dict):
            return {}
        restored: Dict[str, Dict[str, str]] = {}
        for session_id, payload in value.items():
            if not session_id or not isinstance(payload, dict):
                continue
            if any(not payload.get(key) for key in keys):
                continue
            restored[str(session_id)] = {key: str(payload[key]) for key in keys}
        return restored

    def _state_persistence_enabled(self) -> bool:
        return bool(self.config.get("enable_state_persistence", True))

    async def _restore_state(self) -> None:
        """从插件 KV 存储恢复上次的会话/群组开关与音色选择。"""
        try:
            if not self._state_persistence_enabled():
                return

            saved = await self.get_kv_data(self.STATE_KV_KEY, None)
            if not isinstance(saved, dict):
                return

            self.active_sessions.update(self._as_str_set(saved.get("active_sessions")))
            self.w_active_sessions.update(
                self._as_str_set(saved.get("w_active_sessions"))
            )
            self.active_groups.update(self._as_str_set(saved.get("active_groups")))
            self.inactive_groups.update(self._as_str_set(saved.get("inactive_groups")))
            self.session_emotions.update(
                self._as_profile_map(
                    saved.get("session_emotions"), ("character", "emotion")
                )
            )
            self.session_w_settings.update(
                self._as_profile_map(saved.get("session_w_settings"), ("character",))
            )

            # 同一会话不能同时是固定情感模式和自动情感识别模式，自动模式优先。
            self.active_sessions -= self.w_active_sessions
            # 运行时手动关掉的群，优先级高于 __init__ 里的白名单自动开启。
            self.active_groups -= self.inactive_groups

            logger.info(
                "已恢复 TTS 会话状态 | 固定情感会话: %d | 自动情感会话: %d | "
                "已开启群: %d | 已关闭群: %d"
                % (
                    len(self.active_sessions),
                    len(self.w_active_sessions),
                    len(self.active_groups),
                    len(self.inactive_groups),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"恢复 TTS 会话状态失败，本次以空状态启动: {exc}")
        finally:
            self._state_restored.set()

    async def _persist_state(self) -> None:
        """把会话/群组开关与音色选择写回插件 KV 存储。失败只告警，不影响本次指令。"""
        if not self._state_persistence_enabled():
            return

        payload = {
            "active_sessions": sorted(self.active_sessions),
            "w_active_sessions": sorted(self.w_active_sessions),
            "active_groups": sorted(self.active_groups),
            "inactive_groups": sorted(self.inactive_groups),
            "session_emotions": {
                key: dict(value) for key, value in self.session_emotions.items()
            },
            "session_w_settings": {
                key: dict(value) for key, value in self.session_w_settings.items()
            },
        }
        try:
            async with self._state_lock:
                await self.put_kv_data(self.STATE_KV_KEY, payload)
        except Exception as exc:
            logger.warning(f"保存 TTS 会话状态失败（不影响本次操作）: {exc}")

    def _remember_last_audio(
        self, session_id: str, audio_path: Optional[str]
    ) -> Optional[str]:
        """记下会话最近一条语音供 /tts-again 复用，并按 LRU 限制字典长度。"""
        if not session_id or not audio_path:
            return audio_path
        # 先 pop 再插入，让 dict 的插入顺序等价于访问顺序。
        self.last_audio_paths.pop(session_id, None)
        self.last_audio_paths[session_id] = audio_path
        while len(self.last_audio_paths) > self.MAX_REMEMBERED_AUDIO:
            self.last_audio_paths.pop(next(iter(self.last_audio_paths)), None)
        return audio_path

    def _append_tts_failure_notice(self, resp: LLMResponse, reason: str) -> None:
        """在回复末尾追加 TTS 失败提示；用户可在配置里关掉这些提示。"""
        if not self.config.get("enable_tts_failure_notice", True):
            logger.info(f"TTS 失败提示已按配置隐藏: {reason}")
            return
        resp.result_chain.chain.append(Comp.Plain(f"\n({reason})"))

    # ------------------------------------------------------------ 群组与触发

    def _normalize_group_id(self, group_id: Optional[object]) -> str:
        """统一群号格式，避免配置里的字符串和事件里的数字不匹配。"""
        return str(group_id) if group_id else ""

    def _is_group_blacklisted(self, group_id: Optional[object]) -> bool:
        """检查群组是否在黑名单中"""
        group_id = self._normalize_group_id(group_id)
        if not group_id:
            return False
        blacklist = self.config.get("group_blacklist", [])
        return str(group_id) in [str(g) for g in blacklist]

    def _is_group_tts_active(self, group_id: Optional[object]) -> bool:
        """检查群组级 TTS 是否开启。黑名单 > 运行时关闭 > 默认全开/白名单/手动开启。"""
        group_id = self._normalize_group_id(group_id)
        if not group_id or self._is_group_blacklisted(group_id):
            return False
        if group_id in self.inactive_groups:
            return False
        return bool(self.config.get("enable_group_tts_by_default", False)) or (
            group_id in self.active_groups
        )

    def _should_generate_tts_now(self, session_id: str) -> bool:
        """按配置判断本次 LLM 回复是否需要生成语音。"""
        mode = str(self.config.get("tts_trigger_mode", "always")).strip().lower()

        if mode in {"always", "一直触发"}:
            return True

        if mode in {"interval", "time", "按间隔"}:
            try:
                interval_seconds = max(
                    int(self.config.get("tts_trigger_interval_seconds", 300) or 0), 0
                )
            except (TypeError, ValueError):
                interval_seconds = 300
            if interval_seconds <= 0:
                return True

            now = time.monotonic()
            last_trigger_at = self.last_tts_trigger_at.get(session_id)
            if last_trigger_at is None or now - last_trigger_at >= interval_seconds:
                self.last_tts_trigger_at[session_id] = now
                return True

            remaining = interval_seconds - (now - last_trigger_at)
            logger.info(
                f"[{session_id}] 已按时间间隔跳过本次 TTS，约 {remaining:.1f} 秒后可再次触发。"
            )
            return False

        if mode in {"random", "probability", "随机概率"}:
            try:
                probability = float(self.config.get("tts_trigger_probability", 30) or 0)
            except (TypeError, ValueError):
                probability = 30.0
            probability = min(max(probability, 0.0), 100.0)
            triggered = random.random() * 100 < probability
            if not triggered:
                logger.info(
                    f"[{session_id}] 已按随机概率跳过本次 TTS（当前概率: {probability:g}%）。"
                )
            return triggered

        logger.warning(f"未知 TTS 触发模式: {mode}，已按 always 处理。")
        return True

    def _preview_log_text(self, text: Optional[str], max_length: int = 180) -> str:
        if not text:
            return ""
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= max_length:
            return compact
        return f"{compact[:max_length]}..."

    def _log_translation_result(
        self, session_id: str, source_text: str, target_text: Optional[str]
    ) -> None:
        if not self.config.get("enable_translation_debug_log", False):
            return
        logger.info(
            f"[{session_id}] TTS翻译结果 | 原文: {self._preview_log_text(source_text)} | "
            f"合成文本: {self._preview_log_text(target_text)}"
        )

    def _normalize_translation_workflow(self, workflow: Optional[object]) -> str:
        normalized = str(workflow or "").strip().lower()
        workflow_aliases = {
            "llm_injection": "llm_injection",
            "llm": "llm_injection",
            "inject": "llm_injection",
            "prompt_injection": "llm_injection",
            "provider_translation": "provider_translation",
            "provider": "provider_translation",
            "astrbot_provider": "provider_translation",
            "backend": "provider_translation",
        }
        return workflow_aliases.get(normalized, "")

    def _get_translation_workflow(self) -> str:
        settings = self.config.get("llm_injection_settings", {})
        configured_workflow = self._normalize_translation_workflow(
            settings.get("translation_workflow")
        )
        if configured_workflow:
            return configured_workflow

        if settings.get("enable_llm_translation", False):
            return "llm_injection"

        if settings.get("use_astrbot_provider", False) or self._has_external_translation_api_config():
            return "provider_translation"

        return "llm_injection"

    def _should_inject_llm_translation_tags(self) -> bool:
        if not self.config.get("enable_translation", True):
            return False
        return self._get_translation_workflow() == "llm_injection"

    def _should_inject_llm_emotion_tags(self) -> bool:
        """情感标签与翻译链路解耦，只受“生成情感标签”开关控制。

        [emotion=xxx] 与 $翻译$ 是两件独立的事：provider_translation 链路下，
        情感标签会在文本送去翻译之前先被剥离，既不会污染翻译输入，也不会漏进聊天。
        """
        settings = self.config.get("llm_injection_settings", {})
        return bool(settings.get("enable_llm_emotion", False))

    def _get_tts_target_language_name(self) -> str:
        language_code = str(self.config.get("tts_default_language", "jp") or "jp").strip().lower()
        language_names = {
            "jp": "日语",
            "ja": "日语",
            "zh": "中文",
            "en": "英语",
        }
        return language_names.get(language_code, language_code or "目标语言")

    def _extract_tool_text_directives(
        self, text: str
    ) -> Tuple[str, Optional[str], Optional[str]]:
        working_text = (text or "").strip()
        emotion_matches = re.findall(r"\[emotion=(.*?)\]", working_text)
        tagged_emotion = emotion_matches[-1].strip() if emotion_matches else None
        working_text = re.sub(r"\s*\[emotion=.*?\]\s*", " ", working_text).strip()

        tagged_translation = None
        translation_match = re.search(
            r"(?:\$(.+?)\$|\uFF04(.+?)\uFF04)\s*$", working_text, re.DOTALL
        )
        if translation_match:
            tagged_translation = (
                translation_match.group(1) or translation_match.group(2) or ""
            ).strip()
            working_text = working_text[: translation_match.start()].strip()

        display_text = working_text or tagged_translation or ""
        display_text = re.sub(r"\s+", " ", display_text).strip()
        return display_text, tagged_emotion, tagged_translation

    def _strip_pause_markers(self, text: str) -> str:
        """移除自定义停顿标记 [pause=ms]，避免它出现在用户可见的聊天文本里。"""
        if not text:
            return text
        stripped = re.sub(
            r"\[pause\s*=\s*\d+\s*(?:ms)?\]", " ", text, flags=re.IGNORECASE
        )
        stripped = re.sub(r"[ \t]{2,}", " ", stripped)
        return stripped.strip()

    def _strip_llm_tts_directives(
        self,
        text: str,
        strip_translation: bool = True,
        strip_emotion: bool = True,
        strip_pause: bool = False,
    ) -> Tuple[str, Optional[str], Optional[str], bool]:
        """Remove hidden TTS directives from text before it reaches chat."""
        working_text = (text or "").strip()
        if not working_text:
            return "", None, None, False

        changed = False
        tagged_emotion = None
        tagged_translation = None

        if strip_emotion:
            emotion_matches = re.findall(r"\[emotion=(.*?)\]", working_text)
            if emotion_matches:
                tagged_emotion = emotion_matches[-1].strip()
                working_text = re.sub(
                    r"\s*\[emotion=.*?\]\s*", " ", working_text
                ).strip()
                changed = True

        if strip_translation:
            while True:
                translation_match = re.search(
                    r"\s*(?:\$(.+?)\$|\uFF04(.+?)\uFF04)\s*$",
                    working_text,
                    re.DOTALL,
                )
                if not translation_match:
                    break
                tagged_translation = (
                    translation_match.group(1)
                    or translation_match.group(2)
                    or ""
                ).strip()
                working_text = working_text[: translation_match.start()].strip()
                changed = True

        if strip_pause:
            pause_stripped = self._strip_pause_markers(working_text)
            if pause_stripped != working_text:
                working_text = pause_stripped
                changed = True

        cleaned_text = re.sub(r"[ \t]+", " ", working_text)
        cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()
        return cleaned_text, tagged_emotion, tagged_translation, changed

    def _sanitize_plain_components(
        self, chain: Optional[list], strip_translation: Optional[bool] = None
    ) -> bool:
        if not chain:
            return False

        if strip_translation is None:
            strip_translation = self._should_inject_llm_translation_tags()

        changed = False
        for component in chain:
            if not isinstance(component, Comp.Plain):
                continue
            cleaned_text, _, _, component_changed = self._strip_llm_tts_directives(
                getattr(component, "text", ""),
                strip_translation=strip_translation,
                strip_emotion=True,
                strip_pause=True,
            )
            if component_changed:
                component.text = cleaned_text
                changed = True
        return changed

    def _should_use_astrbot_provider_translation(
        self, disable_when_llm_translation_enabled: bool = False
    ) -> bool:
        settings = self.config.get("llm_injection_settings", {})
        provider_id = settings.get("astrbot_provider_id")

        if disable_when_llm_translation_enabled and self._should_inject_llm_translation_tags():
            if provider_id and not self._llm_translation_conflict_logged:
                logger.info(
                    "当前“外语TTS准备方式”为主 LLM 注入标签，AstrBot Provider 翻译将自动忽略。"
                )
                self._llm_translation_conflict_logged = True
            return False

        if self._get_translation_workflow() != "provider_translation":
            return False

        return bool(provider_id)

    def _has_external_translation_api_config(self) -> bool:
        api_config = self.config.get("translation_api", {})
        return bool(api_config.get("base_url") and api_config.get("api_key"))

    def _normalize_tts_output_mode(
        self, mode: Optional[object], default: str
    ) -> str:
        normalized = str(mode or "").strip().lower()
        mode_aliases = {
            "audio_only": "audio_only",
            "voice_only": "audio_only",
            "only_audio": "audio_only",
            "only_voice": "audio_only",
            "纯语音": "audio_only",
            "只发语音": "audio_only",
            "只发语音不发文字": "audio_only",
            "audio_and_text": "audio_and_text",
            "voice_and_text": "audio_and_text",
            "text_with_audio": "audio_and_text",
            "both": "audio_and_text",
            "full_text": "audio_and_text",
            "语音加文字": "audio_and_text",
            "语音跟原文都有": "audio_and_text",
            "原文和语音": "audio_and_text",
            "split_audio_text": "split_audio_text",
            "mixed": "split_audio_text",
            "hybrid": "split_audio_text",
            "partial_text": "split_audio_text",
            "一半文字一半语音": "split_audio_text",
            "半文字半语音": "split_audio_text",
        }
        return mode_aliases.get(normalized, default)

    def _get_auto_tts_output_mode(self) -> str:
        configured_mode = self.config.get("auto_tts_output_mode")
        if configured_mode in (None, ""):
            legacy_value = self.config.get("send_text_with_audio")
            if legacy_value is not None:
                return "audio_and_text" if legacy_value else "audio_only"
        return self._normalize_tts_output_mode(
            configured_mode, default="audio_and_text"
        )

    def _get_llm_tool_tts_output_mode(self) -> str:
        configured_mode = self.config.get("llm_tool_tts_output_mode", "audio_only")
        return self._normalize_tts_output_mode(
            configured_mode, default="audio_only"
        )

    def _split_text_for_mixed_output(self, text: str) -> Tuple[str, str]:
        compact_text = re.sub(r"\s+", " ", (text or "")).strip()
        if not compact_text:
            return "", ""

        regex_pattern = self.config.get(
            "sentence_split_regex", r"([。、，！？,.!?])"
        )
        try:
            parts = re.split(regex_pattern, compact_text)
        except re.error:
            parts = re.split(r"([。！？!?；;，,、])", compact_text)

        sentences = []
        for index in range(0, len(parts) - 1, 2):
            sentence = parts[index]
            delimiter = parts[index + 1] if index + 1 < len(parts) else ""
            merged = f"{sentence}{delimiter}".strip()
            if merged:
                sentences.append(merged)
        if len(parts) % 2 == 1 and parts[-1].strip():
            sentences.append(parts[-1].strip())

        if len(sentences) >= 2:
            split_index = max(1, len(sentences) // 2)
            audio_text = "".join(sentences[:split_index]).strip()
            plain_text = "".join(sentences[split_index:]).strip()
            return audio_text, plain_text

        pivot = max(1, len(compact_text) // 2)
        split_index = -1
        for match in re.finditer(r"[。！？!?；;，,、]", compact_text):
            candidate = match.end()
            if candidate >= pivot:
                split_index = candidate
                break

        if split_index <= 0:
            split_index = compact_text.rfind(" ", 0, pivot)
        if split_index <= 0 or split_index >= len(compact_text):
            return compact_text, ""

        audio_text = compact_text[:split_index].strip()
        plain_text = compact_text[split_index:].strip()
        return audio_text, plain_text

    async def _send_audio_message(self, session_id: str, audio_path: str) -> bool:
        return await self.context.send_message(
            session_id, MessageChain(chain=[Comp.Record(file=audio_path)])
        )

    async def _send_text_message(self, session_id: str, text: str) -> bool:
        text = self._strip_pause_markers(text)
        return await self.context.send_message(
            session_id, MessageChain(chain=[Comp.Plain(text)])
        )

    def _prepare_tts_output_segments(
        self, display_text: str, output_mode: str
    ) -> Tuple[str, str, str]:
        resolved_mode = self._normalize_tts_output_mode(
            output_mode, default="audio_and_text"
        )

        if resolved_mode == "audio_only":
            return display_text, "", resolved_mode

        if resolved_mode == "split_audio_text":
            audio_text, plain_text = self._split_text_for_mixed_output(display_text)
            if audio_text and plain_text:
                return audio_text, plain_text, resolved_mode
            logger.info("Mixed TTS output could not split text cleanly, fallback to audio_and_text.")
            resolved_mode = "audio_and_text"

        return display_text, display_text, resolved_mode

    async def _apply_auto_tts_output_mode(
        self,
        session_id: str,
        resp: LLMResponse,
        audio_path: str,
        full_display_text: str,
        plain_display_text: str,
        output_mode: str,
    ) -> None:
        if output_mode == "audio_only":
            resp.result_chain.chain = [Comp.Record(file=audio_path)]
            return

        audio_sent = await self._send_audio_message(session_id, audio_path)
        if audio_sent:
            resp.completion_text = plain_display_text
            resp.result_chain.chain = [Comp.Plain(plain_display_text)]
            return

        resp.completion_text = full_display_text
        resp.result_chain.chain = [
            Comp.Plain(full_display_text),
            Comp.Plain("\n(TTS音频发送失败)"),
        ]

    async def _dispatch_llm_tool_tts_output(
        self,
        session_id: str,
        audio_path: str,
        full_display_text: str,
        plain_display_text: str,
        output_mode: str,
    ) -> Tuple[bool, Optional[str]]:
        if output_mode == "audio_only":
            ok = await self._send_audio_message(session_id, audio_path)
            if ok:
                return True, None
            return False, "语音已经合成成功，但 AstrBot 主动发送语音失败了。"

        ok = await self._send_audio_message(session_id, audio_path)
        if not ok:
            return False, "语音已经合成成功，但 AstrBot 主动发送语音失败了。"

        if plain_display_text:
            text_ok = await self._send_text_message(session_id, plain_display_text)
            if not text_ok:
                return False, "语音已经发出，但补发文字失败了。"

        return True, None

    def _get_keepalive_urls(self) -> list[str]:
        """获取所有需要保活的目标地址。包括配置的TTS服务器和额外的保活地址。"""
        urls = set()

        # 添加所有配置的TTS服务器
        servers = self.config.get("tts_servers", [])
        if servers:
            for server in servers:
                if isinstance(server, str) and server:
                    urls.add(server.rstrip("/"))

        # 添加额外配置的保活地址
        custom_url = self.config.get("space_keepalive_url")
        if custom_url:
            urls.add(custom_url.rstrip("/"))

        return list(urls)

    async def _keep_alive_loop(self):
        """定时ping所有目标地址以避免休眠。"""
        interval_minutes = max(
            int(self.config.get("space_keepalive_interval_minutes", 25)), 1
        )

        async def ping(url):
            try:
                response = await self.http_client.get(url, timeout=30)
                logger.info(f"保活请求已发送到 {url}，状态码: {response.status_code}")
            except Exception as exc:
                logger.warning(f"向 {url} 发送保活请求失败: {exc}")

        while not self._keepalive_stop_event.is_set():
            try:
                target_urls = self._get_keepalive_urls()
                if not target_urls:
                    logger.warning("未找到任何可用于保活的地址，已跳过本次保活任务。")
                else:
                    await asyncio.gather(*(ping(url) for url in target_urls))
            except Exception as e:
                logger.error(f"保活任务发生意外错误: {e}")

            try:
                await asyncio.wait_for(
                    self._keepalive_stop_event.wait(), timeout=interval_minutes * 60
                )
            except asyncio.TimeoutError:
                continue

    @filter.command("注册感情")
    async def register_emotion_command(
        self,
        event: AstrMessageEvent,
        character_name: str,
        emotion_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        language: str = None,
    ):
        """注册一个新的感情并保存到文件"""
        if ".." in ref_audio_path or os.path.isabs(ref_audio_path):
            yield event.plain_result(
                "❌ 错误：参考音频路径无效。它必须是一个相对路径，且不能包含 '..'。"
            )
            return

        if self.emotion_manager.register_emotion(
            character_name, emotion_name, ref_audio_path, ref_audio_text, language
        ):
            yield event.plain_result(
                f"✅ 感情 '{emotion_name}' 已成功注册到角色 '{character_name}' 下。"
            )
        else:
            self.emotion_manager.reload()  # 如果保存失败，从文件重新加载以恢复状态
            yield event.plain_result("❌ 保存感情时发生错误，注册失败。")

    @filter.command("删除感情")
    async def delete_emotion_command(
        self, event: AstrMessageEvent, character_name: str, emotion_name: str
    ):
        """删除一个已注册的感情"""
        if not self.emotion_manager.character_exists(character_name):
            yield event.plain_result(f"❌ 错误：未找到角色 '{character_name}'。")
            return

        if not self.emotion_manager.get_emotion_data(character_name, emotion_name):
            yield event.plain_result(
                f"❌ 错误：角色 '{character_name}' 下未找到名为 '{emotion_name}' 的感情。"
            )
            return

        if self.emotion_manager.delete_emotion(character_name, emotion_name):
            yield event.plain_result(
                f"✅ 已成功删除角色 '{character_name}' 的感情 '{emotion_name}'。"
            )
        else:
            self.emotion_manager.reload()  # 如果保存失败，从文件重新加载以恢复状态
            yield event.plain_result("❌ 保存文件时发生错误，删除失败。")

    @filter.command("查看感情")
    async def view_emotions_command(
        self, event: AstrMessageEvent, character_name: str = ""
    ):
        """查看已注册的感情，可选只看某一个角色"""
        emotions_data = self.emotion_manager.emotions_data
        if not emotions_data:
            yield event.plain_result("当前未注册任何感情。")
            return

        wanted = (character_name or "").strip()
        if wanted:
            if wanted not in emotions_data:
                available = "、".join(str(name) for name in list(emotions_data)[:20])
                yield event.plain_result(
                    f"❌ 未找到角色 {wanted}。\n已注册角色: {available}"
                )
                return
            items = [(wanted, emotions_data.get(wanted) or {})]
            formatted_lines = [f"角色 {wanted} 的感情列表："]
        else:
            items = list(emotions_data.items())
            formatted_lines = ["所有已注册的感情列表："]

        total = 0
        for character, emotions in items:
            formatted_lines.append(f"\n角色: {character}")
            if isinstance(emotions, dict) and emotions:
                for emotion_name in emotions.keys():
                    formatted_lines.append(f"  - {emotion_name}")
                    total += 1
            else:
                formatted_lines.append("  (暂无感情)")

        formatted_lines.append(f"\n共 {len(items)} 个角色 / {total} 条感情。")
        formatted_lines.append("导出用 /感情导出，导入用 /感情导入，快照用 /感情包。")
        yield event.plain_result("\n".join(formatted_lines))

    # --------------------------------------------------- 感情包（导入导出）

    def _pack_dir(self) -> Path:
        """感情包快照目录，惰性创建。WebUI 与指令共用同一个目录。"""
        base = Path(getattr(self, "plugin_data_dir", ".")) / self.PACK_DIR_NAME
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _clean_emotion_characters(self) -> Tuple[Dict, list]:
        """返回通过校验的感情数据，以及被判定不合法的记录说明。"""
        try:
            return emotion_pack.normalize_characters(self.emotion_manager.emotions_data)
        except EmotionPackError as exc:
            logger.error(f"Genie TTS: 感情库整体不可解析: {exc}")
            return {}, []

    async def _commit_emotion_characters(self, characters: Dict) -> bool:
        """整体写回 emotions.json；失败时回滚到磁盘上的旧值。"""
        async with self._emotion_write_lock:
            manager = self.emotion_manager
            manager.emotions_data = characters
            ok = False
            try:
                ok = bool(manager._save_emotions_to_file())
            except Exception as exc:
                logger.error(f"Genie TTS: 写入 emotions.json 异常: {exc}")
                ok = False
            if not ok:
                try:
                    manager.reload()
                except Exception:
                    pass
            return ok

    @staticmethod
    def _strip_command_prefix(message_str: str, names: Tuple[str, ...]) -> str:
        """去掉指令名，把剩下的整段原文交还调用方当 JSON 载荷。

        AstrBot 的指令过滤器只会把消息切成 token 塞进声明的形参，多余的会被
        丢掉；感情包 JSON 里全是空格和花括号，只能自己从原文里切。

        注意：这里**不能**把空白压成一行。感情包常常裹在 Markdown 代码块里，
        压掉换行后 "\u0060\u0060\u0060json" 会和 JSON 正文粘成一行，围栏就剥不掉了。
        """
        text = (message_str or "").strip().lstrip("\ufeff").strip()
        if not text:
            return ""
        for name in names:
            for prefix in (name, "/" + name, "!" + name, "！" + name, "#" + name):
                if not text.startswith(prefix):
                    continue
                rest = text[len(prefix):]
                if not rest:
                    return ""
                if rest[0].isspace():
                    return rest.strip()
        return text

    def _split_pack_args(self, raw: str) -> Tuple[str, bool, str]:
        """从指令尾巴里解析出 (模式, 是否试运行, 剩余载荷)。

        只从头部最多吃两个 token，避免把 JSON 正文误当成参数；一个不认识但
        明显是「想写模式却写错了」的 token 也会被吃掉，好让上层报出清晰的
        模式错误，而不是让它混进 JSON 里变成难懂的解析失败。
        """
        text = (raw or "").strip()
        mode = ""
        dry = False
        for _round in range(2):
            if not text:
                break
            # 载荷可能从下一行才开始，所以按任意空白切，而不是只切空格。
            head_match = re.match(r"(\S+)\s*", text)
            if not head_match:
                break
            token = head_match.group(1)
            rest = text[head_match.end():].strip()
            lowered = token.lower()
            if not mode and (
                lowered in self.PACK_MODE_TOKENS or token in self.PACK_MODE_TOKENS
            ):
                mode = token
                text = rest
                continue
            if not dry and lowered in self.PACK_DRY_TOKENS:
                dry = True
                text = rest
                continue
            if (
                not mode
                and len(token) <= 24
                and not self._looks_like_pack_text(token)
                and (not rest or self._looks_like_pack_text(rest))
            ):
                # 写错的模式词，交给 normalize_import_mode 报「模式只能是…」
                mode = token
                text = rest
                continue
            break
        return mode, dry, text

    @staticmethod
    def _looks_like_pack_text(text: str) -> bool:
        """粗判一段文本是不是感情包 JSON（裸对象或 Markdown 代码块）。"""
        stripped = (text or "").strip().lstrip("\ufeff").strip()
        if not stripped:
            return False
        return stripped.startswith("{") or stripped.startswith("\u0060\u0060\u0060")

    async def _pack_text_from_event(
        self, event: AstrMessageEvent, inline_text: str
    ) -> Tuple[str, str]:
        """按 消息正文 > 引用消息 > 上传附件 的优先级找出感情包内容。"""
        inline = (inline_text or "").strip()
        if self._looks_like_pack_text(inline):
            return inline, "消息正文"

        try:
            components = list(event.get_messages() or [])
        except Exception:
            components = []

        for comp in components:
            if not isinstance(comp, Comp.Reply):
                continue
            quoted = (getattr(comp, "message_str", "") or "").strip()
            if not quoted:
                pieces = []
                for sub in getattr(comp, "chain", None) or []:
                    if isinstance(sub, Comp.Plain):
                        pieces.append(getattr(sub, "text", "") or "")
                quoted = "".join(pieces).strip()
            if quoted:
                return quoted, "引用消息"

        for comp in components:
            if not isinstance(comp, Comp.File):
                continue
            try:
                local_path = await comp.get_file()
            except Exception as exc:
                logger.warning(f"Genie TTS: 获取感情包附件失败: {exc}")
                continue
            if not local_path or not os.path.isfile(local_path):
                continue
            try:
                with open(local_path, "r", encoding="utf-8-sig") as handle:
                    content = handle.read(emotion_pack.MAX_PACK_BYTES + 1)
            except Exception as exc:
                logger.warning(f"Genie TTS: 读取感情包附件失败: {exc}")
                continue
            if content.strip():
                name = getattr(comp, "name", "") or os.path.basename(local_path)
                return content, f"附件 {name}"

        return inline, "消息正文"

    async def _apply_pack_import(
        self, incoming: Dict, mode: str, dry: bool, invalid: Optional[list] = None
    ) -> Tuple[Dict, str]:
        """算出合并结果并按需落盘，返回 (变更报告, 可直接回显的摘要)。"""
        current, _current_invalid = self._clean_emotion_characters()
        merged, report = emotion_pack.compute_import(current, incoming, mode, invalid)
        summary = emotion_pack.describe_report(report)
        if dry:
            return report, summary + "\n\n🧪 试运行结束，未写入。去掉「试运行」即真正导入。"
        if not report.get("changed"):
            return report, summary + "\n\n没有需要写入的变化，emotions.json 保持原样。"
        if not await self._commit_emotion_characters(merged):
            return report, summary + "\n\n❌ 写入 emotions.json 失败，已回滚到旧数据。"
        return report, summary + "\n\n✅ 已写入 emotions.json，立即生效，无需重启。"

    @filter.command("感情导出", alias={"导出感情"})
    async def export_emotions_command(
        self, event: AstrMessageEvent, character_name: str = ""
    ):
        """导出感情包 JSON；省略角色名则导出全部角色"""
        characters, invalid = self._clean_emotion_characters()
        if not characters:
            yield event.plain_result(
                "当前没有可导出的感情。先用 /注册感情 或 WebUI 工作台添加几条。"
            )
            return

        wanted = (character_name or "").strip()
        if wanted:
            picked = emotion_pack.select_characters(characters, [wanted], None)
            if not picked:
                available = "、".join(list(characters)[:20])
                yield event.plain_result(f"❌ 未找到角色 {wanted}。\n可导出: {available}")
                return
        else:
            picked = characters

        pack = emotion_pack.build_pack(
            picked, plugin_version=self.PLUGIN_VERSION, source="command"
        )
        text = emotion_pack.dumps_pack(pack)
        filename = emotion_pack.default_pack_filename(picked)

        target = None
        try:
            target = self._pack_dir() / filename
            target.write_text(text, encoding="utf-8")
            saved_hint = f"服务端快照: {filename}"
        except Exception as exc:
            logger.error(f"Genie TTS: 保存感情包快照失败: {exc}")
            target = None
            saved_hint = f"⚠ 服务端快照保存失败: {exc}"

        counts = emotion_pack.summarize(picked)
        char_count = counts.get("characters", 0)
        emotion_count = counts.get("emotions", 0)
        size_kb = max(1, len(text.encode("utf-8")) // 1024)
        scope_label = wanted or "全部角色"
        summary = [
            "📦 感情包已导出",
            f"范围: {scope_label}",
            f"内容: {char_count} 个角色 / {emotion_count} 条感情（约 {size_kb}KB）",
            saved_hint,
        ]
        if invalid:
            summary.append(f"⚠ 跳过 {len(invalid)} 条不合法记录，可在 WebUI 工作台里修。")
        summary.append(f"恢复: /感情包恢复 {filename} [模式] [试运行]")
        summary.append("也可以在 WebUI 工作台「感情包」页直接下载 .json。")
        yield event.plain_result("\n".join(summary))

        # QQ 侧才有文件消息段；发失败不影响上面的摘要与快照。
        if target is not None and event.get_platform_name() == "aiocqhttp":
            try:
                await event.send(
                    MessageChain(chain=[Comp.File(name=filename, file=str(target))])
                )
            except Exception as exc:
                logger.warning(f"Genie TTS: 感情包文件发送失败: {exc}")

        # 短包直接贴原文，方便复制到别处；/感情导入 能自动剥掉代码围栏。
        fence = "\u0060\u0060\u0060"
        if len(text) <= 1800:
            yield event.plain_result(fence + "json\n" + text + "\n" + fence)
        else:
            yield event.plain_result(
                "JSON 超过 1800 字就不贴原文了，请用上面的快照文件或 WebUI 下载。"
            )

    @filter.command("感情导入", alias={"导入感情"})
    async def import_emotions_command(self, event: AstrMessageEvent, mode: str = ""):
        """导入感情包：正文附 JSON、引用含 JSON 的消息，或上传 .json 附件"""
        raw_tail = self._strip_command_prefix(
            event.get_message_str(), ("感情导入", "导入感情")
        )
        parsed_mode, dry, payload = self._split_pack_args(raw_tail)
        chosen_mode = parsed_mode
        if not chosen_mode:
            # 过滤器会把正文的第一个 token 塞进声明的形参，那可能是 "{" 或者
            # 代码围栏，只有确实是模式词/试运行词时才采信。
            hint = (mode or "").strip()
            lowered = hint.lower()
            if lowered in self.PACK_MODE_TOKENS or hint in self.PACK_MODE_TOKENS:
                chosen_mode = hint
            elif lowered in self.PACK_DRY_TOKENS:
                dry = True

        text, origin = await self._pack_text_from_event(event, payload)
        if not (text or "").strip():
            yield event.plain_result(self._pack_import_usage())
            return

        try:
            incoming, meta, invalid_incoming = emotion_pack.loads_pack(text)
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ 感情包解析失败（来源: {origin}）：{exc}")
            return
        except Exception as exc:
            yield event.plain_result(f"❌ 感情包解析异常（来源: {origin}）：{exc}")
            return

        if not incoming:
            reason = f"，其中 {len(invalid_incoming)} 条不合法" if invalid_incoming else ""
            yield event.plain_result(
                f"❌ 感情包里没有任何可用记录（来源: {origin}）{reason}。"
            )
            return

        try:
            _report, tail = await self._apply_pack_import(
                incoming, chosen_mode, dry, invalid_incoming
            )
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        title = "📥 感情包导入（试运行）" if dry else "📥 感情包导入"
        head = [title, f"来源: {origin}"]
        note = str((meta or {}).get("note") or "").strip()
        if note:
            head.append(f"备注: {note[:120]}")
        source_tag = str((meta or {}).get("source") or "").strip()
        if source_tag:
            head.append(f"来源标记: {source_tag[:60]}")
        yield event.plain_result("\n".join(head) + "\n" + tail)

    @staticmethod
    def _pack_import_usage() -> str:
        fence = "\u0060\u0060\u0060"
        return (
            "❌ 没找到感情包内容。三种用法：\n"
            "1) 直接贴 JSON：/感情导入 merge {...}\n"
            "2) 引用一条含 JSON 的消息，再发 /感情导入\n"
            "3) 上传 .json 文件，在同一条消息里写 /感情导入\n"
            "模式：merge 只补新（默认）/ overwrite 冲突覆盖 / replace 先清空\n"
            "加「试运行」只预演不写盘，例如 /感情导入 overwrite 试运行\n"
            "JSON 可以是 /感情导出 的完整包，也可以是裸的 emotions.json；"
            "包在 " + fence + " 代码块里也能识别。"
        )

    @filter.command("感情包", alias={"感情包列表"})
    async def list_emotion_packs_command(self, event: AstrMessageEvent):
        """列出服务端保存的感情包快照"""
        try:
            base = self._pack_dir()
            entries = sorted(
                (
                    item
                    for item in base.iterdir()
                    if item.is_file() and item.suffix.lower() == ".json"
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 读取快照目录失败：{exc}")
            return

        if not entries:
            yield event.plain_result(
                "还没有任何感情包快照。\n"
                "用 /感情包保存 [文件名] 存一份，/感情导出 也会自动入库。"
            )
            return

        lines = [f"📦 感情包快照（共 {len(entries)} 份，按时间倒序）"]
        for item in entries[:20]:
            try:
                stat = item.stat()
                stamp = time.strftime("%m-%d %H:%M", time.localtime(stat.st_mtime))
                size_kb = max(1, stat.st_size // 1024)
            except Exception:
                stamp = "??"
                size_kb = 0
            try:
                pack_chars, _meta, _invalid = emotion_pack.loads_pack(
                    item.read_text(encoding="utf-8-sig")
                )
                counts = emotion_pack.summarize(pack_chars)
                char_count = counts.get("characters", 0)
                emotion_count = counts.get("emotions", 0)
                detail = f"{char_count} 角色 / {emotion_count} 感情"
            except Exception:
                detail = "⚠ 解析失败"
            lines.append(f"• {item.name}\n  {detail} · {size_kb}KB · {stamp}")

        if len(entries) > 20:
            lines.append(f"…另有 {len(entries) - 20} 份未列出。")
        lines.append("恢复: /感情包恢复 文件名 [模式] [试运行]")
        yield event.plain_result("\n".join(lines))

    @filter.command("感情包保存")
    async def save_emotion_pack_command(
        self, event: AstrMessageEvent, filename: str = ""
    ):
        """把当前感情库存成一份服务端快照"""
        characters, invalid = self._clean_emotion_characters()
        if not characters:
            yield event.plain_result("当前没有可保存的感情。")
            return

        name = (filename or "").strip()
        target_name = (
            emotion_pack.safe_pack_filename(name)
            if name
            else emotion_pack.default_pack_filename(characters)
        )
        try:
            target = self._pack_dir() / target_name
        except Exception as exc:
            yield event.plain_result(f"❌ 无法访问快照目录：{exc}")
            return

        if target.exists():
            yield event.plain_result(
                f"❌ 快照 {target_name} 已存在。换个文件名，或在 WebUI 里先删掉旧的。"
            )
            return

        pack = emotion_pack.build_pack(
            characters, plugin_version=self.PLUGIN_VERSION, source="command"
        )
        try:
            target.write_text(emotion_pack.dumps_pack(pack), encoding="utf-8")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入快照失败：{exc}")
            return

        counts = emotion_pack.summarize(characters)
        char_count = counts.get("characters", 0)
        emotion_count = counts.get("emotions", 0)
        lines = [
            f"✅ 已保存快照 {target_name}",
            f"{char_count} 个角色 / {emotion_count} 条感情",
        ]
        if invalid:
            lines.append(f"⚠ 跳过 {len(invalid)} 条不合法记录。")
        lines.append(f"恢复: /感情包恢复 {target_name}")
        yield event.plain_result("\n".join(lines))

    @filter.command("感情包恢复")
    async def restore_emotion_pack_command(
        self,
        event: AstrMessageEvent,
        filename: str,
        mode: str = "merge",
        option: str = "",
    ):
        """从服务端快照恢复感情库"""
        target_name = emotion_pack.safe_pack_filename(filename)
        try:
            target = self._pack_dir() / target_name
        except Exception as exc:
            yield event.plain_result(f"❌ 无法访问快照目录：{exc}")
            return

        if not target.is_file():
            yield event.plain_result(
                f"❌ 找不到快照 {target_name}。用 /感情包 看看有哪些。"
            )
            return

        tokens = [str(mode or "").strip(), str(option or "").strip()]
        dry = any(token.lower() in self.PACK_DRY_TOKENS for token in tokens if token)
        chosen_mode = ""
        for token in tokens:
            if token and token.lower() not in self.PACK_DRY_TOKENS:
                chosen_mode = token
                break

        try:
            incoming, meta, invalid_incoming = emotion_pack.loads_pack(
                target.read_text(encoding="utf-8-sig")
            )
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ 快照 {target_name} 解析失败：{exc}")
            return
        except Exception as exc:
            yield event.plain_result(f"❌ 读取快照 {target_name} 失败：{exc}")
            return

        if not incoming:
            yield event.plain_result(f"❌ 快照 {target_name} 里没有可用记录。")
            return

        try:
            _report, tail = await self._apply_pack_import(
                incoming, chosen_mode, dry, invalid_incoming
            )
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        title = "♻️ 感情包恢复（试运行）" if dry else "♻️ 感情包恢复"
        head = [title, f"快照: {target_name}"]
        note = str((meta or {}).get("note") or "").strip()
        if note:
            head.append(f"备注: {note[:120]}")
        yield event.plain_result("\n".join(head) + "\n" + tail)

    @filter.command("合成")
    async def direct_tts_command(
        self,
        event: AstrMessageEvent,
        character_name: str,
        emotion_name: str,
        text_to_synthesize: str,
    ):
        """根据角色和感情名直接合成语音"""
        group_id = event.message_obj.group_id
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        emotion_data = self.emotion_manager.get_emotion_data(
            character_name, emotion_name
        )
        if not emotion_data:
            yield event.plain_result(
                f"❌ 未找到角色 '{character_name}' 的感情 '{emotion_name}'。请先使用 /注册感情 指令添加。"
            )
            return

        yield event.plain_result("收到合成请求，正在处理...")
        audio_path = await self.tts_engine.synthesize(
            character_name=character_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=text_to_synthesize,
            session_id_for_log=event.unified_msg_origin,
            language=emotion_data.get("language"),
        )

        if audio_path:
            self._remember_last_audio(event.unified_msg_origin, audio_path)
            yield event.chain_result([Comp.Record(file=audio_path)])
        else:
            yield event.plain_result(
                "语音合成失败。\n"
                "• 若文本里只有标点或表情符号，是不会生成语音的；\n"
                "• 其它情况可用 /tts-status 检查服务器与队列。"
            )
        event.stop_event()

    @filter.command("tts-llm", alias={"开启语音合成"})
    async def start_tts(self, event: AstrMessageEvent):
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        session_id = event.unified_msg_origin
        self.active_sessions.add(session_id)
        self.w_active_sessions.discard(session_id)
        await self._persist_state()
        default_char = self.config.get("default_character")
        default_emotion = self.config.get("default_emotion_name")
        logger.info(f"会话 [{session_id}] 的 LLM TTS 功能已开启。")
        yield event.plain_result(
            f"▶️ 本对话的LLM语音合成已开启。\n将使用默认感情: {default_char} - {default_emotion}"
        )

    @filter.command("tts-q", alias={"关闭语音合成"})
    async def stop_tts(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        self.active_sessions.discard(session_id)
        self.w_active_sessions.discard(session_id)
        await self._persist_state()
        logger.info(f"会话 [{session_id}] 的所有 LLM TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本对话的所有LLM语音合成功能已关闭。")

    @filter.command("ttg", alias={"开启群语音"})
    async def start_group_tts(self, event: AstrMessageEvent):
        """开启当前群组的语音合成 (对所有人生效)"""
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id:
            yield event.plain_result("❌ 此指令仅限群聊使用。")
            return

        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        self.inactive_groups.discard(group_id)
        self.active_groups.add(group_id)
        await self._persist_state()
        default_char = self.config.get("default_character")
        default_emotion = self.config.get("default_emotion_name")

        settings = self.config.get("llm_injection_settings", {})
        enable_emotion = settings.get("enable_llm_emotion", False)

        logger.info(f"群组 [{group_id}] 的 LLM TTS 功能已开启。")

        if enable_emotion:
            yield event.plain_result(
                f"▶️ 本群组的LLM语音合成已开启 (全员生效)。\n当前已启用LLM情感注入，情感将由AI自动决定。\n(默认保底情感: {default_char} - {default_emotion})"
            )
        else:
            yield event.plain_result(
                f"▶️ 本群组的LLM语音合成已开启 (全员生效)。\n当前为固定情感模式: {default_char} - {default_emotion}"
            )

    @filter.command("ttg-q", alias={"关闭群语音"})
    async def stop_group_tts(self, event: AstrMessageEvent):
        """关闭当前群组的语音合成"""
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id:
            yield event.plain_result("❌ 此指令仅限群聊使用。")
            return

        # 必须同时记录"已手动关闭"并移出"已手动开启"：
        # 否则白名单群 /ttg-q 之后一重启就会被白名单初始化重新打开。
        self.inactive_groups.add(group_id)
        self.active_groups.discard(group_id)
        await self._persist_state()
        logger.info(f"群组 [{group_id}] 的 LLM TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本群组的LLM语音合成已关闭。")

    @filter.command("tts-w", alias={"开启自动情感识别"})
    async def start_tts_w(self, event: AstrMessageEvent):
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        session_id = event.unified_msg_origin
        self.w_active_sessions.add(session_id)
        self.active_sessions.discard(session_id)
        await self._persist_state()
        default_char = self.config.get("default_character")
        logger.info(f"会话 [{session_id}] 的 LLM 自动情感识别 TTS 功能已开启。")
        yield event.plain_result(
            f"▶️ 本对话的自动情感识别语音合成已开启。\n将使用默认角色: {default_char}"
        )

    @filter.command("tts-w-q", alias={"关闭自动情感识别"})
    async def stop_tts_w(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        self.w_active_sessions.discard(session_id)
        await self._persist_state()
        logger.info(f"会话 [{session_id}] 的 LLM 自动情感识别 TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本对话的自动情感识别语音合成已关闭。")

    @filter.command("sw", alias={"切换感情"})
    async def switch_emotion(
        self, event: AstrMessageEvent, character_name: str, emotion_name: str
    ):
        if self.emotion_manager.get_emotion_data(character_name, emotion_name):
            self.session_emotions[event.unified_msg_origin] = {
                "character": character_name,
                "emotion": emotion_name,
            }
            await self._persist_state()
            logger.info(
                f"会话 [{event.unified_msg_origin}] 切换感情至: {character_name} - {emotion_name}"
            )
            yield event.plain_result(
                f"本会话感情已切换为: {character_name} - {emotion_name}"
            )
        else:
            yield event.plain_result(
                f"❌ 未找到角色 '{character_name}' 的感情 '{emotion_name}'。"
            )

    @filter.command("sw-w", alias={"切换w角色"})
    async def switch_w_character(self, event: AstrMessageEvent, character_name: str):
        if self.emotion_manager.character_exists(character_name):
            self.session_w_settings[event.unified_msg_origin] = {
                "character": character_name
            }
            await self._persist_state()
            logger.info(
                f"会话 [{event.unified_msg_origin}] 切换自动情感识别角色至: {character_name}"
            )
            yield event.plain_result(
                f"本会话自动情感识别角色已切换为: {character_name}"
            )
        else:
            yield event.plain_result(f"❌ 未找到角色 '{character_name}'。")

    @filter.command("tts-status", alias={"语音状态"})
    async def tts_status(self, event: AstrMessageEvent):
        """查看本会话/本群的语音开关、当前音色、队列与 TTS 服务器状态"""
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)

        if session_id in self.w_active_sessions:
            session_state = "✅ 自动情感识别模式（/tts-w）"
        elif session_id in self.active_sessions:
            session_state = "✅ 固定情感模式（/tts-llm）"
        else:
            session_state = "⏹️ 未开启"

        lines = ["🎙️ Genie TTS 状态", f"• 本会话: {session_state}"]

        if group_id:
            if self._is_group_blacklisted(group_id):
                group_state = "⛔ 已被群黑名单禁用"
            elif self._is_group_tts_active(group_id):
                group_state = "✅ 已开启（全员生效）"
            else:
                group_state = "⏹️ 未开启"
            lines.append(f"• 本群 [{group_id}]: {group_state}")

        char_name, emotion_name, emotion_data = self._resolve_tts_profile(session_id)
        if emotion_data:
            lines.append(f"• 当前音色: {char_name} - {emotion_name}")
        else:
            lines.append(
                "• 当前音色: ❌ 不可用（角色 "
                + repr(char_name or "未设置")
                + " / 情感 "
                + repr(emotion_name or "未设置")
                + "），请用 /查看感情 检查注册情况"
            )

        trigger_mode = str(self.config.get("tts_trigger_mode", "always"))
        lines.append(f"• 触发模式: {trigger_mode}")
        lines.append(f"• 自动回复输出: {self._get_auto_tts_output_mode()}")
        lines.append(f"• 主动语音输出: {self._get_llm_tool_tts_output_mode()}")

        stats = self.tts_engine.stats
        lines.append(
            "• 合成统计: 成功 %d / 失败 %d / 无朗读内容跳过 %d / "
            "泄漏拦截 %d / 截断拦截 %d"
            % (
                stats.get("succeeded", 0),
                stats.get("failed", 0),
                stats.get("skipped_no_speech", 0),
                stats.get("leak_guard_hits", 0),
                stats.get("truncation_guard_hits", 0),
            )
        )
        text_truncated = stats.get("text_truncated", 0)
        max_text_length = self.config.get(
            "tts_max_text_length", DEFAULT_MAX_TEXT_LENGTH
        )
        truncated_note = (
            "（超长部分未被朗读，调大「语音文本长度上限」可避免）"
            if text_truncated
            else ""
        )
        lines.append(
            "• 文本超长截断: %s 次 / 上限 %s 字%s"
            % (text_truncated, max_text_length, truncated_note)
        )
        lines.append(f"• 排队中的合成请求: {self.tts_engine.queue_size()}")

        yield event.plain_result("\n".join(lines) + "\n\n🛰️ 正在探测 TTS 服务器…")

        probes = await self.tts_engine.probe_servers()
        if not probes:
            yield event.plain_result(
                "❌ 没有配置任何 TTS 服务器地址，请在插件配置的 tts_servers 里填写。"
            )
            return

        probe_lines = ["🛰️ TTS 服务器"]
        has_failure = False
        for index, probe in enumerate(probes, start=1):
            url = probe.get("url", "")
            latency = probe.get("latency") or 0.0
            if probe.get("ok"):
                characters = list(probe.get("characters") or [])
                busy_flag = "（正在合成中）" if probe.get("busy") else ""
                preview = "、".join(characters[:6]) or "无"
                if len(characters) > 6:
                    preview += " …"
                probe_lines.append(
                    f"{index}. ✅ {url}{busy_flag}\n"
                    f"   延迟 {latency:.2f}s | 可用角色 {len(characters)} 个: {preview}"
                )
            else:
                has_failure = True
                probe_lines.append(
                    f"{index}. ❌ {url}\n   {probe.get('error') or '连接失败'}"
                )

        if has_failure:
            probe_lines.append(
                "提示: HuggingFace Space 休眠后首次唤醒需要 1~3 分钟，"
                "可稍后重试，或在配置里开启 enable_space_keepalive 保活。"
            )

        yield event.plain_result("\n".join(probe_lines))

    @filter.command("tts-again", alias={"重发语音"})
    async def tts_again(self, event: AstrMessageEvent):
        """重新发送本会话最近一条合成成功的语音，不重复消耗 TTS 算力"""
        session_id = event.unified_msg_origin
        if self._is_group_blacklisted(event.message_obj.group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        audio_path = self.last_audio_paths.get(session_id)
        if not audio_path:
            yield event.plain_result(
                "本会话还没有生成过语音，先用 /合成 或开启 /tts-llm 让我说一句吧。"
            )
            return

        if not os.path.exists(audio_path):
            self.last_audio_paths.pop(session_id, None)
            yield event.plain_result(
                "最近一条语音的临时文件已被清理（临时音频默认只保留一段时间），请重新合成。"
            )
            return

        yield event.chain_result([Comp.Record(file=audio_path)])
        event.stop_event()

    @filter.command("tts-help", alias={"语音帮助"})
    async def tts_help(self, event: AstrMessageEvent):
        """列出本插件的全部指令"""
        yield event.plain_result(
            "🎙️ Genie TTS 指令一览\n"
            "【开关】\n"
            "• /tts-llm（开启语音合成）本会话开启固定情感模式\n"
            "• /tts-q（关闭语音合成）本会话关闭全部语音\n"
            "• /tts-w（开启自动情感识别）由 AI 决定情感\n"
            "• /tts-w-q（关闭自动情感识别）\n"
            "• /ttg（开启群语音）本群全员生效\n"
            "• /ttg-q（关闭群语音）\n"
            "【音色】\n"
            "• /sw 角色 感情（切换感情）\n"
            "• /sw-w 角色（切换w角色）\n"
            "• /查看感情 [角色] 列出已注册角色与感情\n"
            "• /注册感情 角色 感情 参考音频相对路径 参考文本 [语言]\n"
            "• /删除感情 角色 感情\n"
            "【合成】\n"
            "• /合成 角色 感情 文本 手动合成一条语音\n"
            "• /tts-again（重发语音）重发本会话最近一条语音\n"
            "【感情包】\n"
            "• /感情导出 [角色] 导出感情包 JSON（省略角色=全部）\n"
            "• /感情导入 [模式] [试运行] 附 JSON / 引用消息 / 上传文件\n"
            "• /感情包 列出服务端快照\n"
            "• /感情包保存 [文件名] 存一份当前感情库\n"
            "• /感情包恢复 文件名 [模式] [试运行]\n"
            "  模式：merge 只补新（默认）/ overwrite 冲突覆盖 / replace 先清空\n"
            "【诊断】\n"
            "• /tts-status（语音状态）开关、音色、队列与服务器状态\n"
            "• /tts-help（语音帮助）显示本帮助\n"
            "开关与音色选择会自动持久化，重启 AstrBot 后不会丢。\n"
            "更省事的做法：在 AstrBot WebUI 的插件页打开「语音合成工作台」，"
            "可视化管理感情、试听分段、导入导出感情包。"
        )

    @filter.llm_tool(name="genie_tts_speak")
    async def llm_tool_genie_tts_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        character_name: Optional[str] = None,
        emotion_name: Optional[str] = None,
    ) -> str:
        """在当前会话中直接发送一条 TTS 语音。

        仅当用户明确要求“说一句”“发语音”“让我听听声音”“念给我听”时使用。
        普通闲聊不要调用这个工具；日常语音仍由插件自己的自动触发模式控制。

        Args:
            text(string): 要合成为语音并直接发给用户的文本。必须是完整句子，句末要有标点；如果使用主LLM注入翻译模式，请传入目标语言文本。
            character_name(string): 可选。要使用的角色名；仅在明确知道已注册角色时填写，否则留空沿用当前会话或默认角色。
            emotion_name(string): 可选但建议填写。要使用的情感名；请从当前角色已注册情感中选择，让语音匹配要朗读内容的语气。
        """
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        text = text.strip()

        if self._is_group_blacklisted(group_id):
            return "当前群组已禁用语音功能，不能直接发送 TTS 语音。"
        if not text:
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"

        display_text, tagged_emotion, tagged_translation = self._extract_tool_text_directives(text)
        if not display_text:
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"
        if not emotion_name and tagged_emotion:
            emotion_name = tagged_emotion

        char_name, resolved_emotion, emotion_data = self._resolve_tts_profile(
            session_id, character_name, emotion_name
        )
        if not char_name or not self.emotion_manager.character_exists(char_name):
            return "没有找到可用角色，请先检查默认角色或角色注册情况。"
        if not emotion_data or not resolved_emotion:
            if emotion_name:
                return (
                    f"角色 '{char_name}' 下未找到情感 '{emotion_name}'。"
                    "请改用已注册的情感名，或留空让插件自动选择。"
                )
            return f"角色 '{char_name}' 目前没有可用的情感配置。"

        translation_enabled = self.config.get("enable_translation", True)
        translation_workflow = self._get_translation_workflow()

        if translation_enabled and translation_workflow == "provider_translation":
            target_text = await self._translate_text_with_backends(display_text)
        elif tagged_translation:
            target_text = tagged_translation
        else:
            target_text = display_text
        self._log_translation_result(session_id, display_text, target_text)

        if not target_text:
            return "语音发送失败：用于 TTS 的文本准备失败了，请检查翻译配置或日志。"

        output_mode = self._get_llm_tool_tts_output_mode()
        tts_text, plain_text, output_mode = self._prepare_tts_output_segments(
            display_text, output_mode
        )
        if not tts_text:
            return "语音发送失败：没有可用于朗读的文本。"

        tts_target_text = target_text
        if output_mode == "split_audio_text":
            if translation_enabled and translation_workflow == "provider_translation":
                tts_target_text = await self._translate_text_with_backends(tts_text)
                self._log_translation_result(session_id, tts_text, tts_target_text)
            elif tagged_translation:
                translated_audio_text, _, _ = self._prepare_tts_output_segments(
                    tagged_translation, output_mode
                )
                tts_target_text = translated_audio_text or tagged_translation
            else:
                tts_target_text = tts_text

            if not tts_target_text:
                return "语音发送失败：混合模式下用于 TTS 的文本准备失败了，请检查翻译配置或日志。"

        if not has_pronounceable(tts_target_text):
            return (
                "语音发送失败：这段文本里没有任何可以朗读的字（只有标点或表情符号）。"
                "请给出包含实际文字的内容再调用一次。"
            )

        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=tts_target_text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )
        if not audio_path:
            return "语音发送失败：TTS 合成没有成功，请检查服务状态或日志。"
        self._remember_last_audio(session_id, audio_path)

        ok, error_message = await self._dispatch_llm_tool_tts_output(
            session_id=session_id,
            audio_path=audio_path,
            full_display_text=display_text,
            plain_display_text=plain_text,
            output_mode=output_mode,
        )
        if not ok:
            return (
                (error_message or "语音已经合成成功，但 AstrBot 主动发送失败了。")
                + "请确认当前会话对应的平台实例仍然在线。"
            )

        self.skip_next_auto_tts_sessions.add(session_id)
        logger.info(
            f"[{session_id}] LLM 工具已主动发送 TTS 语音: {char_name} - {resolved_emotion}"
        )
        return (
            "语音已发送到当前会话。请不要逐字重复刚才朗读的整段内容，"
            "只需简短确认已经发出，或继续正常对话。"
        )

    def _pick_available_emotion_name(
        self, character_name: str, preferred_emotion: Optional[str] = None
    ) -> Optional[str]:
        if preferred_emotion and self.emotion_manager.get_emotion_data(
            character_name, preferred_emotion
        ):
            return preferred_emotion

        default_emotion = self.config.get("default_emotion_name")
        if default_emotion and self.emotion_manager.get_emotion_data(
            character_name, default_emotion
        ):
            return default_emotion

        character_emotions = self.emotion_manager.emotions_data.get(character_name, {})
        return next(iter(character_emotions.keys()), None)

    def _resolve_tts_profile(
        self,
        session_id: str,
        character_name: Optional[str] = None,
        emotion_name: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]:
        session_setting = self.session_emotions.get(session_id)

        resolved_char = character_name
        if not resolved_char:
            if session_setting:
                resolved_char = session_setting.get("character")
                if not emotion_name:
                    emotion_name = session_setting.get("emotion")
            elif session_id in self.w_active_sessions:
                resolved_char = self.session_w_settings.get(session_id, {}).get(
                    "character"
                )

        if not resolved_char:
            resolved_char = self.config.get("default_character")

        if not resolved_char or not self.emotion_manager.character_exists(resolved_char):
            return resolved_char, emotion_name, None

        resolved_emotion = emotion_name
        if resolved_emotion:
            emotion_data = self.emotion_manager.get_emotion_data(
                resolved_char, resolved_emotion
            )
            return resolved_char, resolved_emotion, emotion_data

        preferred_emotion = None
        if session_setting and session_setting.get("character") == resolved_char:
            preferred_emotion = session_setting.get("emotion")

        resolved_emotion = self._pick_available_emotion_name(
            resolved_char, preferred_emotion
        )
        if not resolved_emotion:
            return resolved_char, None, None

        emotion_data = self.emotion_manager.get_emotion_data(
            resolved_char, resolved_emotion
        )
        return resolved_char, resolved_emotion, emotion_data

    async def _translate_text_with_backends(
        self,
        original_text: str,
        disable_provider_during_llm_translation: bool = False,
    ) -> Optional[str]:
        settings = self.config.get("llm_injection_settings", {})
        target_text = None
        translation_prompt = str(settings.get("translation_prompt", "") or "").strip()
        if not translation_prompt:
            target_language_name = self._get_tts_target_language_name()
            translation_prompt = (
                f"请把以下文本翻译成{target_language_name}，保留原文的语气和句末标点，"
                "只输出译文，不要输出解释、引号或原文。"
            )

        if self._should_use_astrbot_provider_translation(
            disable_when_llm_translation_enabled=disable_provider_during_llm_translation
        ):
            try:
                provider_id = settings.get("astrbot_provider_id")
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    llm_resp = await provider.text_chat(
                        prompt=original_text, system_prompt=translation_prompt
                    )
                    target_text = llm_resp.completion_text.strip()
                else:
                    logger.error(f"未找到 Provider ID: {provider_id}")
            except Exception as e:
                logger.error(f"AstrBot Provider 翻译失败: {e}")

        if not target_text:
            api_config = self.config.get("translation_api", {})
            if self._has_external_translation_api_config():
                target_text = await translate_text(
                    original_text,
                    self.http_client,
                    api_config,
                    translation_prompt,
                )

        return target_text

    async def _translate_text_and_pick_emotion_with_backends(
        self, original_text: str, emotion_names: list[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        api_config = self.config.get("translation_api", {})
        prompt_template = api_config.get(
            "w_mode_prompt",
            "请对以下中文内容进行翻译和情感分析。首先翻译成日语，然后从以下情感列表中选择最合适的一个：{emotion_list}。请按以下格式输出：\n[翻译后的日语文本][选择的情感名]\n\n原文：{text}",
        )
        emotion_list_str = ", ".join(emotion_names)
        try:
            request_prompt = prompt_template.format(
                emotion_list=emotion_list_str, text=original_text
            )
        except KeyError:
            request_prompt = prompt_template

        strict_system_prompt = (
            "你是翻译与情感分析助手。请严格按照用户要求的格式作答，"
            "只输出翻译后的文本和末尾方括号中的情感名，不要添加解释。"
        )

        backend_result = None
        if self._should_use_astrbot_provider_translation():
            settings = self.config.get("llm_injection_settings", {})
            provider_id = settings.get("astrbot_provider_id")
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    llm_resp = await provider.text_chat(
                        prompt=request_prompt, system_prompt=strict_system_prompt
                    )
                    backend_result = llm_resp.completion_text.strip()
                else:
                    logger.error(f"未找到 Provider ID: {provider_id}")
            except Exception as e:
                logger.error(f"AstrBot Provider 自动情感翻译失败: {e}")

        if not backend_result and self._has_external_translation_api_config():
            backend_result = await translate_text(
                request_prompt,
                self.http_client,
                api_config,
                strict_system_prompt,
            )

        if not backend_result:
            return None, None

        match = re.search(r"(.*)\[(.+?)\]\s*$", backend_result.strip(), re.DOTALL)
        if not match:
            return backend_result.strip(), None

        return match.group(1).strip(), match.group(2).strip()

    def _build_llm_tool_prompt(self, session_id: Optional[str] = None) -> Optional[str]:
        settings = self.config.get("llm_injection_settings", {})
        if not settings.get("enable_llm_tts_tool_prompt", False):
            return None

        prompt_template = settings.get("llm_tts_tool_prompt", "")
        char_name = None
        if session_id:
            char_name, _, _ = self._resolve_tts_profile(session_id)
        if not char_name:
            char_name = self.config.get("default_character")

        emotions = []
        if char_name and self.emotion_manager.character_exists(char_name):
            emotions = list(self.emotion_manager.emotions_data.get(char_name, {}).keys())

        prompt_template = str(prompt_template).strip()
        if not prompt_template:
            return None

        emotions_text = ", ".join(emotions)
        try:
            prompt = prompt_template.format(
                character=char_name or "",
                emotions=emotions_text,
            )
        except (KeyError, IndexError, ValueError):
            prompt = prompt_template

        prompt = prompt.strip()
        if not prompt:
            return None

        runtime_lines = []
        if emotions:
            runtime_lines.append(
                f"genie_tts_speak 当前可用情感：{emotions_text}。"
                "调用工具时必须优先根据朗读内容选择最贴切的 emotion_name；"
                "只有完全无法判断时才允许留空使用默认情感。"
            )

        if self.config.get("enable_translation", True):
            target_language_name = self._get_tts_target_language_name()
            if self._get_translation_workflow() == "llm_injection":
                runtime_lines.append(
                    f"当前语音合成目标语言是{target_language_name}。如果你调用 genie_tts_speak，"
                    f"请先把要朗读的内容翻成{target_language_name}，再把翻译后的完整句子直接填入 text 参数，"
                    "并保留句末标点。"
                )
            else:
                runtime_lines.append(
                    f"当前语音合成目标语言是{target_language_name}。如果你调用 genie_tts_speak，"
                    "text 参数直接填写完整原文即可，插件会在发送前自动翻译。"
                )
        else:
            runtime_lines.append(
                "当前语音合成不做翻译，text 参数直接填写最终要朗读的完整句子即可。"
            )

        return "\n".join([prompt, *runtime_lines]).strip()

    def _build_pause_prompt(self) -> Optional[str]:
        """自定义停顿标记开启时，返回要注入给 LLM 的提示词；关闭时返回 None。"""
        if not self.config.get("enable_custom_pause_marker", False):
            return None
        prompt = self.config.get("custom_pause_prompt", "")
        if isinstance(prompt, str):
            prompt = prompt.strip()
        return prompt or None

    async def _synthesize_speech_from_context(
        self, text: str, session_id: str
    ) -> Optional[str]:
        """根据当前会话设置合成语音（固定感情模式）"""
        char_name, emotion_name, emotion_data = self._resolve_tts_profile(session_id)
        if not char_name or not emotion_name:
            logger.error(f"[{session_id}] 未配置默认角色或感情。")
            return None
        if not emotion_data:
            logger.error(f"[{session_id}] 找不到感情配置: {char_name} - {emotion_name}")
            return None

        # 整段没有可发音字符时直接放弃：Genie 的 t2s 对纯标点段会返回空音频，
        # 旧版本甚至会把参考音频原样拼进结果里。
        if not has_pronounceable(text):
            logger.info(f"[{session_id}] 文本没有可朗读字符，已跳过合成: {text[:40]}")
            return None

        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )
        return self._remember_last_audio(session_id, audio_path)

    @filter.on_llm_request()
    async def inject_llm_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """在LLM请求前注入提示词"""
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)

        # 黑名单群组不进行任何处理
        if self._is_group_blacklisted(group_id):
            return

        # 只有在开启了TTS模式（自动或固定，或群组模式）时才注入
        is_group_tts_active = self._is_group_tts_active(group_id)
        is_active = (
            session_id in self.active_sessions
            or session_id in self.w_active_sessions
            or is_group_tts_active
        )

        if not is_active:
            tool_prompt = self._build_llm_tool_prompt(session_id)
            if tool_prompt:
                pause_prompt = self._build_pause_prompt()
                if pause_prompt:
                    tool_prompt = f"{tool_prompt}\n\n{pause_prompt}"
                req.system_prompt += f"\n\n{tool_prompt}"
                logger.info(f"[{session_id}] 已注入LLM语音工具提示。")
            return

        settings = self.config.get("llm_injection_settings", {})
        auto_tts_this_turn = self._should_generate_tts_now(session_id)
        self.checked_auto_tts_sessions.add(session_id)
        if auto_tts_this_turn:
            self.pending_auto_tts_sessions.add(session_id)
        else:
            self.pending_auto_tts_sessions.discard(session_id)

        enable_emotion = (
            auto_tts_this_turn and self._should_inject_llm_emotion_tags()
        )
        enable_translation = (
            auto_tts_this_turn and self._should_inject_llm_translation_tags()
        )
        tool_prompt = self._build_llm_tool_prompt(session_id)

        if not enable_emotion and not enable_translation and not tool_prompt:
            return

        prompts_to_inject = []

        if enable_emotion:
            # 确定当前角色以获取可用情感列表
            char_name = None
            if session_id in self.w_active_sessions:
                char_name = self.session_w_settings.get(session_id, {}).get(
                    "character"
                ) or self.config.get("default_character")
            elif session_id in self.active_sessions or is_group_tts_active:
                # 固定模式或群组模式下
                session_setting = self.session_emotions.get(session_id)
                char_name = (
                    session_setting["character"]
                    if session_setting
                    else self.config.get("default_character")
                )

            if char_name and self.emotion_manager.character_exists(char_name):
                emotions = list(self.emotion_manager.emotions_data[char_name].keys())
                emotions_str = ", ".join(emotions)

                prompt_template = settings.get("llm_emotion_prompt", "")
                try:
                    emotion_prompt = prompt_template.format(emotions=emotions_str)
                except KeyError:
                    emotion_prompt = prompt_template
                prompts_to_inject.append(emotion_prompt)
            else:
                logger.warning(
                    f"[{session_id}] 情感注入被跳过：角色 '{char_name}' 未注册任何情感，"
                    "本轮不会要求 LLM 输出 [emotion=xxx] 标签，自动TTS将回落默认情感。"
                    "可用 /注册感情 为该角色注册情感。"
                )

        if enable_translation:
            trans_prompt = settings.get("llm_translation_prompt", "")
            if trans_prompt:
                prompts_to_inject.append(trans_prompt)

        if tool_prompt:
            prompts_to_inject.append(tool_prompt)

        pause_prompt = self._build_pause_prompt()
        pause_injected = bool(pause_prompt) and (enable_translation or bool(tool_prompt))
        if pause_injected:
            prompts_to_inject.append(pause_prompt)

        if prompts_to_inject:
            final_prompt = "\n\n".join(prompts_to_inject)
            req.system_prompt += f"\n\n{final_prompt}"
            logger.info(
                f"[{session_id}] 已注入LLM提示词 "
                f"(AutoTTS: {auto_tts_this_turn}, Emotion: {enable_emotion}, "
                f"Trans: {enable_translation}, Tool: {bool(tool_prompt)}, "
                f"Pause: {pause_injected})"
            )

    @filter.on_llm_response()
    async def intercept_llm_response_for_tts(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        original_text = resp.completion_text.strip()
        if not original_text:
            return

        # 黑名单群组不进行任何处理
        if self._is_group_blacklisted(group_id):
            return

        # 0. 清理可能存在的幻觉报错 (防止LLM复读之前的错误提示)
        #    整条正则匹配。旧实现用 "(TTS失败: 角色" 做模糊匹配，
        #    会把 "'角色名'无效)" 这种残渣留在正文里发给用户。
        cleaned_text = self.TTS_FAILURE_NOTICE_PATTERN.sub("", original_text).strip()
        removed_failure_notice = cleaned_text != original_text
        original_text = cleaned_text
        if not original_text:
            return

        configured_llm_emotion = self._should_inject_llm_emotion_tags()
        configured_llm_translation = self._should_inject_llm_translation_tags()
        original_text, injected_emotion, injected_translation, stripped_directives = (
            self._strip_llm_tts_directives(
                original_text,
                strip_translation=configured_llm_translation,
                strip_emotion=configured_llm_emotion or "[emotion=" in original_text,
            )
        )
        # 旧版本只在剥离了内部标签时才写回，导致被清理掉的幻觉失败提示
        # 依然会原样发给用户；这里把清理结果一并写回可见回复。
        if stripped_directives or removed_failure_notice:
            resp.completion_text = original_text.strip()
            resp.result_chain.chain = [Comp.Plain(resp.completion_text)]

        # 检查是否开启了TTS (个人会话 或 群组)
        is_group_tts_active = self._is_group_tts_active(group_id)
        is_active = (
            session_id in self.active_sessions
            or session_id in self.w_active_sessions
            or is_group_tts_active
        )

        if not is_active:
            return

        settings = self.config.get("llm_injection_settings", {})
        enable_llm_emotion = configured_llm_emotion
        enable_llm_translation = configured_llm_translation
        translation_workflow = self._get_translation_workflow()

        # 更新 LLM 回复文本为净化后的文本 (去除标签和翻译部分)
        resp.completion_text = original_text.strip()
        # 同时更新 result_chain 中的 Plain 消息，否则用户还是会看到标签
        # 注意：这里假设 result_chain 第一个是 Plain。如果不是，可能需要遍历。
        # 简单起见，我们重建 chain
        resp.result_chain.chain = [Comp.Plain(resp.completion_text)]

        if session_id in self.skip_next_auto_tts_sessions:
            self.skip_next_auto_tts_sessions.discard(session_id)
            self.pending_auto_tts_sessions.discard(session_id)
            self.checked_auto_tts_sessions.discard(session_id)
            logger.info(f"[{session_id}] 已由 LLM 主动语音工具发送语音，跳过本次自动 TTS。")
            return

        if session_id in self.checked_auto_tts_sessions:
            should_generate_auto_tts = session_id in self.pending_auto_tts_sessions
        else:
            should_generate_auto_tts = self._should_generate_tts_now(session_id)
        self.pending_auto_tts_sessions.discard(session_id)
        self.checked_auto_tts_sessions.discard(session_id)

        if not should_generate_auto_tts:
            return

        # --- 开始 TTS 处理流程 ---

        audio_path: Optional[str] = None
        target_emotion = None
        emotion_source = ""
        target_text = None
        char_name = None

        # 确定角色
        if session_id in self.w_active_sessions:
            char_name = self.session_w_settings.get(session_id, {}).get(
                "character"
            ) or self.config.get("default_character")
        else:
            # 固定模式 或 群组模式
            session_setting = self.session_emotions.get(session_id)
            char_name = (
                session_setting["character"]
                if session_setting
                else self.config.get("default_character")
            )
            # 固定模式下，如果没有注入情感，使用默认情感
            if not injected_emotion:
                target_emotion = (
                    session_setting["emotion"]
                    if session_setting
                    else self.config.get("default_emotion_name")
                )
                emotion_source = "会话固定情感" if session_setting else "默认情感"

        if not char_name or not self.emotion_manager.character_exists(char_name):
            self._append_tts_failure_notice(resp, f"TTS失败: 角色'{char_name}'无效")
            return

        # 确定情感
        if enable_llm_emotion and injected_emotion:
            target_emotion = injected_emotion
            emotion_source = "LLM情感标签"
        elif enable_llm_emotion and not injected_emotion:
            logger.info(
                f"[{session_id}] 已开启LLM情感标签，但本轮回复未解析到 [emotion=xxx]，"
                "将使用会话固定/默认情感。请确认角色已注册情感、且情感提示词包含 {emotions} 占位符。"
            )

        # 确定翻译文本
        if enable_llm_translation and injected_translation:
            target_text = injected_translation
        elif not self.config.get("enable_translation", True):
            # 翻译功能已关闭，直接使用原文（适合中文模型）
            target_text = original_text
        else:
            if translation_workflow == "provider_translation":
                # provider 模式下，自动情感识别可由独立翻译 Provider 一并完成。
                if session_id in self.w_active_sessions and not target_emotion:
                    character_emotions = list(
                        self.emotion_manager.emotions_data[char_name].keys()
                    )
                    target_text, target_emotion = (
                        await self._translate_text_and_pick_emotion_with_backends(
                            original_text, character_emotions
                        )
                    )
                    if target_emotion:
                        emotion_source = "翻译Provider情感识别"

                if not target_text:
                    target_text = await self._translate_text_with_backends(
                        original_text,
                        disable_provider_during_llm_translation=True,
                    )

        self._log_translation_result(session_id, original_text, target_text)

        if not target_text:
            if translation_workflow == "llm_injection":
                logger.warning(
                    f"[{session_id}] 本轮自动TTS已触发，但主LLM没有返回 $...$ 翻译标签，"
                    "已跳过语音合成并保留原文本回复。"
                )
                return
            self._append_tts_failure_notice(resp, "TTS失败: 翻译无结果")
            return

        display_text = original_text
        output_mode = self._get_auto_tts_output_mode()
        tts_source_text, plain_display_text, output_mode = self._prepare_tts_output_segments(
            display_text, output_mode
        )
        if not tts_source_text:
            self._append_tts_failure_notice(resp, "TTS失败: 没有可用于朗读的文本")
            return

        if output_mode == "split_audio_text":
            if enable_llm_translation and injected_translation:
                translated_audio_text, _, _ = self._prepare_tts_output_segments(
                    injected_translation, output_mode
                )
                target_text = translated_audio_text or target_text
            elif self.config.get("enable_translation", True) and translation_workflow == "provider_translation":
                target_text = await self._translate_text_with_backends(
                    tts_source_text,
                    disable_provider_during_llm_translation=True,
                )
                self._log_translation_result(session_id, tts_source_text, target_text)
            else:
                target_text = tts_source_text

            if not target_text:
                self._append_tts_failure_notice(resp, "TTS失败: 混合模式翻译无结果")
                return

        # 最终合成
        # 如果此时还没有 target_emotion (比如固定模式没注入，或者自动模式失败)，使用默认
        if not target_emotion:
            target_emotion = self.config.get("default_emotion_name")
            emotion_source = "默认情感兜底"

        emotion_data = self.emotion_manager.get_emotion_data(char_name, target_emotion)
        if not emotion_data:
            # 尝试回落到默认情感
            invalid_emotion = target_emotion
            default_emotion = self.config.get("default_emotion_name")
            emotion_data = self.emotion_manager.get_emotion_data(
                char_name, default_emotion
            )
            if not emotion_data:
                self._append_tts_failure_notice(
                    resp, f"TTS失败: 情感'{target_emotion}'无效"
                )
                return
            target_emotion = default_emotion
            emotion_source = f"无效情感'{invalid_emotion}'回落默认"
            logger.warning(
                f"[{session_id}] 自动TTS情感无效，已回落默认情感: "
                f"{char_name} - {target_emotion}（原情感: {invalid_emotion}）"
            )

        logger.info(
            f"[{session_id}] 自动TTS情感选择 | 角色: {char_name} | "
            f"情感: {target_emotion} | 来源: {emotion_source or '未标记'} | "
            f"参考音频: {emotion_data.get('ref_audio_path')}"
        )

        # 整段都是标点/表情符号时不要送去合成：不仅浪费一次请求，
        # 还会在正文后面挂一条毫无意义的"合成失败"。
        if not has_pronounceable(target_text):
            logger.info(
                f"[{session_id}] 待合成文本没有可朗读字符，已跳过本轮自动TTS: "
                f"{target_text[:40]}"
            )
            return

        # 合成语音
        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=target_text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )

        if audio_path:
            self._remember_last_audio(session_id, audio_path)
            await self._apply_auto_tts_output_mode(
                session_id=session_id,
                resp=resp,
                audio_path=audio_path,
                full_display_text=display_text,
                plain_display_text=plain_display_text,
                output_mode=output_mode,
            )
        else:
            self._append_tts_failure_notice(resp, "TTS合成失败")

    @filter.on_decorating_result()
    async def sanitize_tts_directives_before_send(self, event: AstrMessageEvent):
        """Final guard to keep internal TTS directives out of visible chat."""
        result = event.get_result()
        chain = getattr(result, "chain", None) if result else None
        if not chain:
            return

        if self._sanitize_plain_components(chain):
            result.chain = chain
            event.set_result(result)
            logger.info(f"[{event.unified_msg_origin}] 已清理残留的TTS内部标签。")

    async def terminate(self):
        """插件卸载/停用时关闭http客户端"""
        self._keepalive_stop_event.set()
        if self._keepalive_task:
            await asyncio.gather(self._keepalive_task, return_exceptions=True)

        if self._state_restore_task and not self._state_restore_task.done():
            self._state_restore_task.cancel()
            await asyncio.gather(self._state_restore_task, return_exceptions=True)

        await self.tts_engine.terminate()
        await self.http_client.aclose()
        logger.info("LLM TTS 插件已卸载，HTTP客户端已关闭。")
