import asyncio
import os
import re
import uuid
import wave
import time
from pathlib import Path
from typing import Optional, Dict

import httpx
from astrbot.api import logger, AstrBotConfig

# --- 音频参数 (必须与Genie TTS服务输出匹配) ---
BYTES_PER_SAMPLE = 2
CHANNELS = 1
SAMPLE_RATE = 32000

# --- 参考音频泄漏防护 ---
# Genie 的 t2s 解码在“整段文本没有可发音字符”时会退化成输出整条参考音频的
# semantic tokens，最终合成出“参考音频 + 新语音”拼在一起的音频。
# 服务端已修复该缺陷，插件侧同样做两层兜底：
#   1) 切分阶段就把纯标点/纯符号片段并入相邻片段，不让它单独进入合成；
#   2) 合成后按“可发音字符数”估算时长上限，明显超标时重试并取较短结果。
PRONOUNCEABLE_PATTERN = re.compile(
    "["
    "0-9A-Za-z"
    "\u00c0-\u024f"  # 拉丁字母补充 + 拉丁扩展 A
    "\u0370-\u04ff"  # 希腊字母 + 西里尔字母
    "\u1100-\u11ff"  # 谚文字母
    "\u3005-\u3007"  # 々 〆 〇
    "\u3041-\u3096"  # 平假名
    "\u3105-\u312f"  # 注音符号
    "\u3131-\u318e"  # 谚文兼容字母
    "\u30a1-\u30fa"  # 片假名（不含长音符 ー 与中点 ・）
    "\u31f0-\u31ff"  # 片假名语音扩展
    "\u3400-\u4dbf"  # CJK 扩展 A
    "\u4e00-\u9fff"  # CJK 基本区
    "\uac00-\ud7a3"  # 谚文音节
    "\uf900-\ufaff"  # CJK 兼容表意文字
    "\uff10-\uff19"  # 全角数字
    "\uff21-\uff3a"  # 全角大写拉丁字母
    "\uff41-\uff5a"  # 全角小写拉丁字母
    "\uff66-\uff9d"  # 半角片假名
    "]"
)
PAUSE_MARKER_PATTERN = re.compile(r"\[pause\s*=\s*(\d+)\s*(?:ms)?\]", re.IGNORECASE)
MAX_CUSTOM_PAUSE_MS = 3000
DEFAULT_LEAK_GUARD_SECONDS_PER_CHAR = 0.9
DEFAULT_LEAK_GUARD_MIN_SECONDS = 3.0


def has_pronounceable(text: str) -> bool:
    """判断文本里是否存在至少一个可发音字符。"""
    return bool(PRONOUNCEABLE_PATTERN.search(text or ""))


def count_pronounceable(text: str) -> int:
    """统计文本里的可发音字符数量，用于估算合理音频时长。"""
    return len(PRONOUNCEABLE_PATTERN.findall(text or ""))


def pause_budget_seconds(text: str) -> float:
    """统计 [pause=ms] 标记声明的静音总时长（秒）。"""
    total_ms = 0
    for raw in PAUSE_MARKER_PATTERN.findall(text or ""):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        total_ms += max(0, min(value, MAX_CUSTOM_PAUSE_MS))
    return total_ms / 1000.0


class TTSEngine:
    """处理所有与TTS合成相关的核心逻辑，包括文本分块、并发合成和音频合并"""

    DEFAULT_TTS_CLEAN_REGEX = (
        r"\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|【[^【】]*】|\{[^{}]*\}|｛[^｛｝]*｝|<[^<>]*>|《[^《》]*》"
    )
    MAX_TTS_CLEAN_ROUNDS = 10

    def __init__(
        self,
        config: AstrBotConfig,
        http_client: httpx.AsyncClient,
        plugin_data_dir: Path,
    ):
        self.config = config
        self.http_client = http_client
        self.plugin_data_dir = plugin_data_dir
        self.tts_server_index = 0

        # 服务器锁：确保同一服务器的 set_reference_audio + /tts 是原子操作
        self._server_locks: Dict[str, asyncio.Lock] = {}

        # 全局合成锁：确保多个语音请求按"先到先得"顺序串行处理
        self._synthesis_lock = asyncio.Lock()

        # 设置临时音频目录
        self.temp_audio_dir = self.plugin_data_dir / "temp_audio"
        self.temp_audio_dir.mkdir(parents=True, exist_ok=True)

        # 运行统计，供 /tts-status 展示
        self.stats: Dict[str, int] = {
            "requests": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_no_speech": 0,
            "leak_guard_hits": 0,
        }

        # 启动清理任务
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        # 全局 FIFO 请求队列：严格先到先得
        self._request_queue = asyncio.Queue()
        self._request_worker_task = asyncio.create_task(self._request_worker_loop())

    # ------------------------------------------------------------------ 工具

    def _server_urls(self) -> list[str]:
        """读取并规范化已配置的 TTS 服务器列表（去空、去尾斜杠、去重）。"""
        raw_servers = self.config.get("tts_servers", []) or []
        urls: list[str] = []
        for item in raw_servers:
            url = str(item or "").strip().strip("/")
            if url and url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _discard_temp_file(path: Optional[str]) -> None:
        """安全删除临时音频文件，忽略文件不存在的情况。"""
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(f"删除临时文件 {path} 失败: {exc}")

    def _clean_text_for_tts(self, text: str) -> str:
        cleaned = text

        pattern = self.config.get("tts_text_clean_regex", self.DEFAULT_TTS_CLEAN_REGEX)
        if isinstance(pattern, str) and pattern:
            try:
                # Run multiple rounds so nested brackets can also be cleaned.
                for _ in range(self.MAX_TTS_CLEAN_ROUNDS):
                    updated = re.sub(pattern, "", cleaned)
                    if updated == cleaned:
                        break
                    cleaned = updated
            except re.error as exc:
                logger.warning(f"文本清洗正则无效，已跳过: {pattern} ({exc})")

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _ensure_terminal_punctuation(self, text: str, language: str = None) -> str:
        text = (text or "").strip()
        if not text:
            return text

        closing_marks = "\"'”’）)]】』」》"
        terminal_pattern = rf"[。！？!?\.．…]+[{re.escape(closing_marks)}]*$"
        if re.search(terminal_pattern, text):
            return text

        language_code = str(language or self.config.get("tts_default_language", "jp") or "jp").lower()
        punctuation = "." if language_code in {"en", "english"} else "。"
        closing_match = re.search(rf"([{re.escape(closing_marks)}]+)$", text)
        if closing_match:
            insert_at = closing_match.start()
            return f"{text[:insert_at]}{punctuation}{text[insert_at:]}"
        return f"{text}{punctuation}"

    def _truncate_text_for_tts(self, text: str, max_length: int) -> str:
        """将过长文本按句子边界截断，避免超长回复被切成过多块、长时间排队等待。

        仅影响送入 TTS 的音频文本；audio_and_text 模式下展示给用户的文字仍是完整原文。
        """
        text = (text or "").strip()
        if max_length <= 0 or len(text) <= max_length:
            return text

        window = text[:max_length]
        boundary_chars = "。！？!?…．.、，,；;\n"
        cut_at = -1
        for idx in range(len(window) - 1, -1, -1):
            if window[idx] in boundary_chars:
                cut_at = idx + 1
                break

        # 边界过于靠前会丢掉太多内容，此时退回硬截断，至少保留一半长度。
        if cut_at >= max(1, max_length // 2):
            truncated = window[:cut_at]
        else:
            truncated = window

        return truncated.strip()

    async def _cleanup_loop(self):
        """定期清理过期的临时音频文件"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时检查一次
                current_time = time.time()
                expiration_time = 1800  # 30分钟过期

                if not self.temp_audio_dir.exists():
                    continue

                count = 0
                for file_path in self.temp_audio_dir.glob("*.wav"):
                    try:
                        if current_time - file_path.stat().st_mtime > expiration_time:
                            file_path.unlink()
                            count += 1
                    except Exception as e:
                        logger.warning(f"清理文件 {file_path} 失败: {e}")

                if count > 0:
                    logger.info(f"已清理 {count} 个过期的临时音频文件。")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理任务发生错误: {e}")

    async def _request_worker_loop(self):
        """全局请求调度器：严格按 FIFO 处理整单合成请求。"""
        while True:
            try:
                request, result_future = await self._request_queue.get()
            except asyncio.CancelledError:
                break

            if result_future.cancelled():
                logger.info(
                    f"[{request.get('session_id_for_log', 'unknown')}] 请求已取消，跳过合成。"
                )
                self._request_queue.task_done()
                continue

            try:
                result = await self._synthesize_direct(**request)
                if not result_future.done():
                    result_future.set_result(result)
            except asyncio.CancelledError:
                if not result_future.done():
                    result_future.set_result(None)
                raise
            except Exception as e:
                logger.error(f"[{request.get('session_id_for_log', 'unknown')}] 请求处理异常: {e}")
                if not result_future.done():
                    result_future.set_result(None)
            finally:
                self._request_queue.task_done()

    async def terminate(self):
        """在插件卸载时停止后台清理任务。"""
        if self._request_worker_task:
            self._request_worker_task.cancel()
            await asyncio.gather(self._request_worker_task, return_exceptions=True)
            while not self._request_queue.empty():
                try:
                    _, result_future = self._request_queue.get_nowait()
                    if not result_future.done():
                        result_future.set_result(None)
                    self._request_queue.task_done()
                except asyncio.QueueEmpty:
                    break

        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)

    # -------------------------------------------------------------- 文本切分

    @staticmethod
    def _merge_unvoiceable_sentences(sentences: list[str]) -> list[str]:
        """把不含可发音字符的片段并入相邻片段。

        纯标点片段（例如被切出来的单独 "……"）单独送进 Genie 会触发参考音频泄漏，
        因此这里把它并到后一个可发音片段前面；若结尾仍有残留则追加到最后一段末尾。
        整段都没有可发音字符时返回空列表，表示这次不该合成。
        """
        merged: list[str] = []
        pending = ""
        for sentence in sentences:
            if not sentence:
                continue
            if has_pronounceable(sentence):
                merged.append(f"{pending}{sentence}" if pending else sentence)
                pending = ""
            else:
                pending += sentence

        if pending:
            if not merged:
                return []
            merged[-1] = f"{merged[-1]}{pending}"
        return merged

    def _split_text_into_chunks(self, text: str, sentences_per_chunk: int) -> list[str]:
        """根据标点将文本切分为句子，再按指定数量合并成块。

        返回空列表表示整段文本没有任何可发音内容，调用方应放弃本次合成。
        """
        if not has_pronounceable(text):
            return []

        if sentences_per_chunk <= 0:
            return [text]

        regex_pattern = self.config.get("sentence_split_regex", r"([。、，！？,.!?])")
        try:
            splitter = re.compile(regex_pattern)
        except re.error as exc:
            logger.warning(f"句子切分正则无效，已退回单块模式: {regex_pattern} ({exc})")
            return [text]

        sentences = splitter.split(text)
        if not sentences:
            return []

        if splitter.groups == 1:
            # 带一个捕获组时 re.split 交替返回 [正文, 分隔符, 正文, ...]
            full_sentences = []
            for i in range(0, len(sentences) - 1, 2):
                # 修复：分隔符不再因为正文为空而被整个丢弃（例如连续的 "！？"）
                merged = f"{sentences[i]}{sentences[i + 1]}"
                if merged:
                    full_sentences.append(merged)
            if len(sentences) % 2 == 1 and sentences[-1]:
                full_sentences.append(sentences[-1])
        else:
            # 无捕获组（分隔符已被丢弃）或多捕获组时，按原顺序保留非空片段
            full_sentences = [part for part in sentences if part]

        full_sentences = self._merge_unvoiceable_sentences(full_sentences)
        if not full_sentences:
            return []

        chunks = []
        for i in range(0, len(full_sentences), sentences_per_chunk):
            chunk = "".join(full_sentences[i : i + sentences_per_chunk])
            chunks.append(chunk)

        logger.info(f"文本已切分为 {len(chunks)} 个块。")
        return chunks

    async def _merge_wav_files(self, input_paths: list[str]) -> Optional[str]:
        """以无损的方式将多个WAV文件按顺序合并为一个，并清理分块文件。"""
        if not input_paths:
            return None

        output_path = self.temp_audio_dir / f"{uuid.uuid4()}_merged.wav"

        try:
            with wave.open(input_paths[0], "rb") as wf_in:
                params = wf_in.getparams()

            with wave.open(str(output_path), "wb") as wf_out:
                wf_out.setparams(params)
                for file_path in input_paths:
                    with wave.open(file_path, "rb") as wf_in:
                        wf_out.writeframes(wf_in.readframes(wf_in.getnframes()))

            logger.info(f"成功将 {len(input_paths)} 个音频文件合并到: {output_path}")
            return str(output_path)

        except Exception as e:
            logger.error(f"合并WAV文件时出错: {e}")
            self._discard_temp_file(str(output_path))
            return None
        finally:
            # 无论合并成功与否，都尝试清理输入的临时分块文件
            for file_path in input_paths:
                self._discard_temp_file(file_path)

    def _get_server_lock(self, server_url: str) -> asyncio.Lock:
        """获取或创建指定服务器的锁，用于保证 set_reference_audio + /tts 的原子性。"""
        if server_url not in self._server_locks:
            self._server_locks[server_url] = asyncio.Lock()
        return self._server_locks[server_url]

    # ---------------------------------------------------------------- 合成实现

    def _leak_guard_enabled(self) -> bool:
        return bool(self.config.get("enable_tts_leak_guard", True))

    def _expected_audio_seconds(self, text: str) -> float:
        """按可发音字符数 + 停顿标记估算这段文本合理的最大音频时长（秒）。

        阈值刻意放得很宽（默认 0.9s/字，约为正常语速的 6 倍），只用于识别
        “整条参考音频被拼进结果”这类量级异常，不会误伤正常的慢速朗读。
        """
        per_char = self.config.get(
            "tts_leak_guard_seconds_per_char", DEFAULT_LEAK_GUARD_SECONDS_PER_CHAR
        )
        floor_seconds = self.config.get(
            "tts_leak_guard_min_seconds", DEFAULT_LEAK_GUARD_MIN_SECONDS
        )
        try:
            per_char = float(per_char)
        except (TypeError, ValueError):
            per_char = DEFAULT_LEAK_GUARD_SECONDS_PER_CHAR
        try:
            floor_seconds = float(floor_seconds)
        except (TypeError, ValueError):
            floor_seconds = DEFAULT_LEAK_GUARD_MIN_SECONDS

        per_char = max(per_char, 0.2)
        floor_seconds = max(floor_seconds, 1.0)
        budget = count_pronounceable(text) * per_char + pause_budget_seconds(text)
        return max(floor_seconds, budget)

    async def _stream_tts_to_file(
        self,
        server_url: str,
        character_name: str,
        text: str,
        tts_timeout: float,
        session_id_for_log: str,
    ) -> tuple[Optional[str], float]:
        """调用一次 /tts 并把裸 PCM 流封装成 WAV 落盘，返回 (文件路径, 音频秒数)。

        /tts 返回 0 字节时（服务端判定整段文本无可发音内容）返回 (None, 0.0)，
        绝不能把 0 帧的空 WAV 当成合成成功，否则用户会收到一条空语音。
        """
        tts_payload = {
            "character_name": character_name,
            "text": text,
            "split_sentence": True,
        }
        output_path = self.temp_audio_dir / f"{uuid.uuid4()}.wav"
        tts_start = time.perf_counter()
        first_byte_elapsed = None
        total_audio_bytes = 0

        try:
            async with self.http_client.stream(
                "POST", f"{server_url}/tts", json=tts_payload, timeout=tts_timeout
            ) as response_tts:
                response_tts.raise_for_status()
                with wave.open(str(output_path), "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(BYTES_PER_SAMPLE)
                    wf.setframerate(SAMPLE_RATE)
                    async for chunk in response_tts.aiter_bytes():
                        if not chunk:
                            continue
                        if first_byte_elapsed is None:
                            first_byte_elapsed = time.perf_counter() - tts_start
                        total_audio_bytes += len(chunk)
                        wf.writeframes(chunk)
        except BaseException:
            # 任何异常（含取消）都不留下半截临时文件
            self._discard_temp_file(str(output_path))
            raise

        total_elapsed = time.perf_counter() - tts_start
        audio_seconds = total_audio_bytes / float(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)
        ttfb_text = (
            f"{first_byte_elapsed:.2f}s" if first_byte_elapsed is not None else "无音频"
        )
        logger.info(
            f"[{session_id_for_log}] TTS服务器 {server_url} 计时 | "
            f"首包响应 {ttfb_text} | 合成传输 {total_elapsed:.2f}s | "
            f"音频时长 {audio_seconds:.2f}s"
        )

        if total_audio_bytes <= 0:
            self._discard_temp_file(str(output_path))
            logger.warning(
                f"[{session_id_for_log}] TTS服务器 {server_url} 未返回任何音频数据，"
                f"已丢弃空音频（文本可能只有标点或表情符号）。"
            )
            return None, 0.0

        return str(output_path), audio_seconds

    async def _attempt_synthesis_on_server(
        self,
        server_url: str,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """使用单个指定的TTS服务器尝试合成语音，并返回保存好的文件路径。"""
        logger.info(f"[{session_id_for_log}] 尝试TTS服务器: {server_url}")

        # 获取此服务器的锁，确保 set_reference_audio 和 /tts 不会被其他请求打断
        server_lock = self._get_server_lock(server_url)

        best_path: Optional[str] = None
        async with server_lock:
            try:
                # Propagate the language parameter directly, fallback to config only if None provided
                if not language:
                    language = self.config.get("tts_default_language", "jp")

                ref_payload = {
                    "character_name": character_name,
                    "audio_path": ref_audio_path,
                    "audio_text": ref_audio_text,
                    "language": language,
                }
                tts_timeout = self.config.get("tts_timeout_seconds", 120)
                ref_start = time.perf_counter()
                response = await self.http_client.post(
                    f"{server_url}/set_reference_audio", json=ref_payload, timeout=60
                )
                response.raise_for_status()
                logger.info(
                    f"[{session_id_for_log}] 参考音频设定完成 | "
                    f"{time.perf_counter() - ref_start:.2f}s"
                )

                leak_guard = self._leak_guard_enabled()
                expected_seconds = self._expected_audio_seconds(text) if leak_guard else 0.0
                best_seconds = 0.0

                # 最多合成两次：第一次结果时长量级异常时重试并保留较短的一次。
                for attempt in range(2):
                    audio_path, audio_seconds = await self._stream_tts_to_file(
                        server_url,
                        character_name,
                        text,
                        tts_timeout,
                        session_id_for_log,
                    )
                    if not audio_path:
                        if (
                            leak_guard
                            and best_path is not None
                            and best_seconds > expected_seconds
                        ):
                            # 首次结果已被判定为疑似泄漏，重试又没拿回音频：
                            # 宁可这次合成失败，也不要把叠加音频发给用户。
                            logger.warning(
                                f"[{session_id_for_log}] 重试未返回音频，"
                                f"已丢弃疑似参考音频泄漏的首次结果。"
                            )
                            self._discard_temp_file(best_path)
                            return None
                        return best_path

                    if best_path is None or audio_seconds < best_seconds:
                        self._discard_temp_file(best_path)
                        best_path, best_seconds = audio_path, audio_seconds
                    else:
                        self._discard_temp_file(audio_path)

                    if not leak_guard or best_seconds <= expected_seconds:
                        return best_path

                    if attempt == 0:
                        # 同一次合成只计一次，避免重试把统计翻倍
                        self.stats["leak_guard_hits"] += 1
                    tail = (
                        "正在重试一次。"
                        if attempt == 0
                        else "两次结果均偏长，采用较短的一次。"
                    )
                    logger.warning(
                        f"[{session_id_for_log}] 合成结果异常偏长"
                        f"（{audio_seconds:.2f}s > 预期上限 {expected_seconds:.2f}s），"
                        f"疑似参考音频被拼入结果。{tail}"
                    )

                return best_path
            except Exception as e:
                logger.warning(
                    f"[{session_id_for_log}] TTS服务器 {server_url} 交互失败: {e}"
                )
                self._discard_temp_file(best_path)
                return None

    async def _synthesis_worker(
        self,
        worker_id: int,
        task_queue: asyncio.Queue,
        results_list: list,
        retry_counts: dict,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        session_id_for_log: str,
        language: str = None,
    ):
        """单个TTS服务器的工作进程，从队列中获取任务并处理，失败时会重试"""
        servers = self._server_urls()
        num_servers = len(servers)
        if num_servers == 0:
            return
        max_retries = self.config.get("tts_max_retries", 3)

        while True:
            try:
                # 使用非阻塞方式获取任务，如果队列为空则退出
                task_index, chunk_text = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                # 队列真正为空，退出循环
                break
            except asyncio.CancelledError:
                break

            current_retry = retry_counts.get(task_index, 0)
            start_server_idx = worker_id % num_servers
            log_id = f"{session_id_for_log}-chunk-{task_index + 1}"
            audio_path = None

            # 第一轮 prefer_idle=True：只挑当前空闲的服务器，立刻开工；
            # 第二轮 prefer_idle=False：所有服务器都忙，则按顺序排队等待。
            for prefer_idle in (True, False):
                for offset in range(num_servers):
                    server_url = servers[(start_server_idx + offset) % num_servers]
                    if prefer_idle and self._get_server_lock(server_url).locked():
                        continue

                    audio_path = await self._attempt_synthesis_on_server(
                        server_url,
                        character_name,
                        ref_audio_path,
                        ref_audio_text,
                        chunk_text,
                        log_id,
                        language=language,
                    )
                    if audio_path:
                        suffix = "" if prefer_idle else " (等待后)"
                        logger.info(
                            f"[Worker-{worker_id}] 成功合成块 {task_index + 1} "
                            f"于服务器 {server_url}{suffix}"
                        )
                        results_list[task_index] = audio_path
                        break
                if audio_path:
                    break

            if not audio_path:
                # 失败处理：检查是否还有重试机会
                if current_retry < max_retries:
                    retry_counts[task_index] = current_retry + 1
                    logger.warning(
                        f"[Worker-{worker_id}] 块 {task_index + 1} 合成失败，"
                        f"放回队列重试 ({current_retry + 1}/{max_retries})"
                    )
                    # 延迟一小段时间后重新放回队列，避免立即重试造成服务器过载
                    await asyncio.sleep(0.5)
                    await task_queue.put((task_index, chunk_text))
                else:
                    logger.error(
                        f"[Worker-{worker_id}] 块 {task_index + 1} 达到最大重试次数 "
                        f"({max_retries})，放弃。"
                    )
                    results_list[task_index] = None

            task_queue.task_done()

    async def _synthesize_direct(
        self,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """执行单个请求的合成逻辑。由全局队列调度器按 FIFO 调用。"""

        # 全局锁确保语音请求按顺序处理，先请求的先完成
        async with self._synthesis_lock:
            servers = self._server_urls()
            if not servers:
                logger.error(f"[{session_id_for_log}] 未配置TTS服务器。")
                return None

            text_for_tts = text

            # 自定义停顿标记 [pause=ms]：开启则保留，交由 Space 精确插入静音；
            # 关闭则在送入 TTS 前剥除，避免把标记本身朗读出来。
            if not self.config.get("enable_custom_pause_marker", False):
                stripped_pause = PAUSE_MARKER_PATTERN.sub(" ", text_for_tts)
                stripped_pause = re.sub(r"\s+", " ", stripped_pause).strip()
                if stripped_pause != text_for_tts:
                    text_for_tts = stripped_pause

            if self.config.get("enable_tts_text_cleaning", False):
                cleaned_text = self._clean_text_for_tts(text_for_tts)
                if cleaned_text != text_for_tts:
                    logger.info(
                        f"[{session_id_for_log}] TTS文本已清洗: '{text_for_tts[:30]}' -> '{cleaned_text[:30]}'"
                    )
                text_for_tts = cleaned_text
                if not text_for_tts:
                    logger.warning(
                        f"[{session_id_for_log}] TTS文本清洗后为空，跳过合成。"
                    )
                    self.stats["skipped_no_speech"] += 1
                    return None

            max_text_length = self.config.get("tts_max_text_length", 150)
            try:
                max_text_length = int(max_text_length)
            except (TypeError, ValueError):
                max_text_length = 150
            if max_text_length > 0 and len(text_for_tts) > max_text_length:
                truncated_text = self._truncate_text_for_tts(
                    text_for_tts, max_text_length
                )
                if truncated_text and truncated_text != text_for_tts:
                    logger.info(
                        f"[{session_id_for_log}] TTS文本超过长度上限({max_text_length})，"
                        f"已截断: {len(text_for_tts)} -> {len(truncated_text)} 字符。"
                    )
                    text_for_tts = truncated_text

            punctuated_text = self._ensure_terminal_punctuation(text_for_tts, language)
            if punctuated_text != text_for_tts:
                logger.info(
                    f"[{session_id_for_log}] 已为TTS文本补充句末标点，降低尾音截断概率。"
                )
                text_for_tts = punctuated_text

            # 纯标点 / 纯表情文本会让 Genie 输出整条参考音频，这里直接不合成。
            if not has_pronounceable(text_for_tts):
                logger.info(
                    f"[{session_id_for_log}] 文本没有可发音内容，跳过合成: "
                    f"'{text_for_tts[:30]}'"
                )
                self.stats["skipped_no_speech"] += 1
                return None

            if self.config.get("enable_sentence_splitting", False):
                sentences_per_chunk = self.config.get("sentences_per_chunk", 2)
                text_chunks = self._split_text_into_chunks(
                    text_for_tts, sentences_per_chunk
                )

                if not text_chunks:
                    logger.info(
                        f"[{session_id_for_log}] 切分后没有可合成的语音块，跳过合成。"
                    )
                    self.stats["skipped_no_speech"] += 1
                    return None

                if len(text_chunks) == 1:
                    # 采用归并后的文本：孤立标点已被并入，避免单独送入触发泄漏。
                    text_for_tts = text_chunks[0]
                else:
                    task_queue = asyncio.Queue()
                    for i, chunk in enumerate(text_chunks):
                        task_queue.put_nowait((i, chunk))

                    results_list = [None] * len(text_chunks)
                    retry_counts = {}  # 跟踪每个块的重试次数
                    # Worker 数量不超过服务器数量，避免长文本时创建过多空转协程
                    num_workers = min(len(text_chunks), len(servers))
                    workers = [
                        asyncio.create_task(
                            self._synthesis_worker(
                                worker_id=i,
                                task_queue=task_queue,
                                results_list=results_list,
                                retry_counts=retry_counts,
                                character_name=character_name,
                                ref_audio_path=ref_audio_path,
                                ref_audio_text=ref_audio_text,
                                session_id_for_log=session_id_for_log,
                                language=language,
                            )
                        )
                        for i in range(num_workers)
                    ]

                    logger.info(
                        f"[{session_id_for_log}] 创建了 {num_workers} 个worker来处理 {len(text_chunks)} 个语音块..."
                    )
                    try:
                        await task_queue.join()
                    finally:
                        for worker in workers:
                            worker.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)

                    successful_paths = [path for path in results_list if path]

                    # 如果有部分失败，或者全部失败，都需要清理已经生成的临时文件
                    if len(successful_paths) != len(text_chunks):
                        logger.error(
                            f"[{session_id_for_log}] 部分或全部语音块合成失败，正在清理临时文件。"
                        )
                        for path in successful_paths:
                            self._discard_temp_file(path)
                        return None

                    return (
                        successful_paths[0]
                        if len(successful_paths) == 1
                        else await self._merge_wav_files(successful_paths)
                    )

            # 如果不切分，则使用轮询逻辑
            logger.info(f"[{session_id_for_log}] 使用单块模式进行合成。")
            start_index = self.tts_server_index
            for i in range(len(servers)):
                current_index = (start_index + i) % len(servers)
                server_url = servers[current_index]

                if i == 0:
                    self.tts_server_index = (start_index + 1) % len(servers)

                audio_path = await self._attempt_synthesis_on_server(
                    server_url=server_url,
                    character_name=character_name,
                    ref_audio_path=ref_audio_path,
                    ref_audio_text=ref_audio_text,
                    text=text_for_tts,
                    session_id_for_log=session_id_for_log,
                    language=language,
                )
                if audio_path:
                    return audio_path

            logger.error(f"[{session_id_for_log}] 尝试所有TTS服务器后合成失败。")
            return None

    # ------------------------------------------------------------------ 对外

    def queue_size(self) -> int:
        """当前排队中的合成请求数量。"""
        return self._request_queue.qsize()

    async def probe_servers(self, timeout: float = 10.0) -> list[dict]:
        """并发探测所有已配置的 TTS 服务器，供 /tts-status 展示。"""
        servers = self._server_urls()
        if not servers:
            return []

        async def probe(server_url: str) -> dict:
            info: dict = {
                "url": server_url,
                "ok": False,
                "latency": None,
                "characters": [],
                "error": None,
                "busy": self._get_server_lock(server_url).locked(),
            }
            started = time.perf_counter()
            try:
                response = await self.http_client.get(
                    f"{server_url}/ui/options", timeout=timeout
                )
                response.raise_for_status()
                payload = response.json()
                info["ok"] = True
                info["characters"] = list(payload.get("characters") or [])
            except Exception as exc:
                info["error"] = f"{type(exc).__name__}: {exc}"
            info["latency"] = time.perf_counter() - started
            return info

        return list(await asyncio.gather(*(probe(url) for url in servers)))

    async def synthesize(
        self,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """将请求放入全局 FIFO 队列，严格先到先得。"""
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()
        request = {
            "character_name": character_name,
            "ref_audio_path": ref_audio_path,
            "ref_audio_text": ref_audio_text,
            "text": text,
            "session_id_for_log": session_id_for_log,
            "language": language,
        }
        self.stats["requests"] += 1
        await self._request_queue.put((request, result_future))
        logger.info(
            f"[{session_id_for_log}] 已进入TTS队列，等待中（当前队列长度: {self._request_queue.qsize()}）"
        )
        result = await result_future
        if result:
            self.stats["succeeded"] += 1
        else:
            self.stats["failed"] += 1
        return result
