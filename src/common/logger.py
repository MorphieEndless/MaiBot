from datetime import datetime, timedelta
from structlog.types import EventDict
from structlog.stdlib import BoundLogger
from typing import Dict, Optional, Any, List, cast
from pathlib import Path

import logging
import json
import structlog
import threading
import time
import tomlkit

from .logger_color_and_mapping import MODULE_ALIASES, RESET_COLOR, CONVERTED_MODULE_COLORS as MODULE_COLORS

LOG_DIR = Path(__file__).parent.parent.parent.absolute().resolve() / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger_file = Path(__file__).resolve()
PROJECT_ROOT = logger_file.parent.parent.parent.resolve()
_file_handler: Optional["TimestampedFileHandler"] = None
_console_handler: Optional[logging.StreamHandler] = None
_cleanup_task_started = False


def _load_log_config() -> Dict[str, Any]:  # sourcery skip: use-contextlib-suppress
    """从配置文件加载日志设置"""
    config_path = PROJECT_ROOT / "config" / "config.toml"
    default_config = {
        "date_style": "%m-%d %H:%M:%S",
        "log_level_style": "lite",
        "color_text": "full",
        "log_level": "INFO",  # 全局日志级别（向下兼容）
        "console_log_level": "INFO",  # 控制台日志级别
        "file_log_level": "DEBUG",  # 文件日志级别
        "log_file_max_bytes": 5 * 1024 * 1024,  # 单个日志文件最大大小
        "max_log_files": 30,  # 最多保留的日志文件数量
        "log_cleanup_days": 30,  # 日志保留天数
        "suppress_libraries": [
            "faiss",
            "httpx",
            "urllib3",
            "asyncio",
            "websockets",
            "httpcore",
            "requests",
            "sqlalchemy",
            "openai",
            "uvicorn",
            "jieba",
        ],
        "library_log_levels": {
            "aiohttp": "WARNING",
            "PIL": "WARNING",
        },
    }

    try:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = tomlkit.load(f)
                if log_config := config.get("log", {}):
                    default_lib_log_lvs: Dict[str, str] = default_config["library_log_levels"]
                    new_lib_log_lvs: Dict[str, str] = log_config.get("library_log_levels", {})
                    # 合并默认和新的日志级别
                    merged_lib_log_lvs = default_lib_log_lvs | new_lib_log_lvs
                    log_config["library_log_levels"] = merged_lib_log_lvs
                    return log_config
    except Exception as e:
        print(f"[日志系统] 加载日志配置失败: {e}")
    return default_config


LOG_CONFIG = _load_log_config()


def _convert_pathname_to_module(logger, method_name, event_dict):
    # sourcery skip: extract-method, use-string-remove-affix
    """将 pathname 转换为模块风格的路径"""
    if "pathname" in event_dict:
        pathname = event_dict["pathname"]
        try:
            # 使用绝对路径确保准确性
            pathname_path = Path(pathname).resolve()
            rel_path = pathname_path.relative_to(PROJECT_ROOT)

            # 转换为模块风格：移除 .py 扩展名，将路径分隔符替换为点
            module_path = str(rel_path).replace("\\", ".").replace("/", ".")
            if module_path.endswith(".py"):
                module_path = module_path[:-3]

            # 使用转换后的模块路径替换 module 字段
            event_dict["module"] = module_path
            # 移除原始的 pathname 字段
            del event_dict["pathname"]
        except Exception:
            # 如果转换失败，删除 pathname 但保留原始的 module（如果有的话）
            del event_dict["pathname"]
            # 如果没有 module 字段，使用文件名作为备选
            if "module" not in event_dict:
                event_dict["module"] = Path(pathname).stem
    return event_dict


def configure_structlog():
    """配置structlog"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.CallsiteParameterAdder(
                parameters=[
                    structlog.processors.CallsiteParameter.PATHNAME,
                    structlog.processors.CallsiteParameter.LINENO,
                ]
            ),
            _convert_pathname_to_module,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt=LOG_CONFIG.get("date_style", "%m-%d %H:%M:%S"), utc=False),
            # 根据输出类型选择不同的渲染器
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# 配置structlog
configure_structlog()


class TimestampedFileHandler(logging.Handler):
    """基于时间戳的文件处理器，简单的轮转份数限制"""

    def __init__(self, log_dir, max_bytes=5 * 1024 * 1024, backup_count=30, encoding="utf-8"):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.encoding = encoding
        self._lock = threading.Lock()

        # 当前活跃的日志文件
        self.current_file = None
        self.current_stream = None
        self._init_current_file()

    def _init_current_file(self):
        """初始化当前日志文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_file = self.log_dir / f"app_{timestamp}.log.jsonl"
        self.current_stream = open(self.current_file, "a", encoding=self.encoding)

    def _should_rollover(self):
        """检查是否需要轮转"""
        if self.current_file and self.current_file.exists():
            return self.current_file.stat().st_size >= self.max_bytes
        return False

    def _do_rollover(self):
        """执行轮转：关闭当前文件，创建新文件"""
        if self.current_stream:
            self.current_stream.close()

        # 清理旧文件
        self._cleanup_old_files()

        # 创建新文件
        self._init_current_file()

    def _cleanup_old_files(self):
        """清理旧的日志文件，保留指定数量"""
        try:
            # 获取所有日志文件
            log_files = list(self.log_dir.glob("app_*.log.jsonl"))

            # 按修改时间排序
            log_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

            # 删除超出数量限制的文件
            for old_file in log_files[self.backup_count :]:
                try:
                    old_file.unlink()
                    print(f"[日志清理] 删除旧文件: {old_file.name}")
                except Exception as e:
                    print(f"[日志清理] 删除失败 {old_file}: {e}")

        except Exception as e:
            print(f"[日志清理] 清理过程出错: {e}")

    def emit(self, record):
        """发出日志记录"""
        try:
            with self._lock:
                # 检查是否需要轮转
                if self._should_rollover():
                    self._do_rollover()

                # 写入日志
                if self.current_stream:
                    msg = self.format(record)
                    self.current_stream.write(msg + "\n")
                    self.current_stream.flush()

        except Exception:
            self.handleError(record)

    def close(self):
        """关闭处理器"""
        with self._lock:
            if self.current_stream:
                self.current_stream.close()
                self.current_stream = None
        super().close()


class ModuleColoredConsoleRenderer:
    """自定义控制台渲染器，为不同模块提供不同颜色"""

    def __init__(self, colors=True):
        # sourcery skip: merge-duplicate-blocks, remove-redundant-if
        self._colors = colors
        self._config = LOG_CONFIG
        self._enable_module_colors: bool = False
        self._enable_level_colors: bool = False
        self._enable_full_content_colors: bool = False

        # 日志级别颜色
        self._level_colors = {
            "debug": "\033[38;5;208m",  # 橙色
            "info": "\033[38;5;117m",  # 天蓝色
            "success": "\033[32m",  # 绿色
            "warning": "\033[33m",  # 黄色
            "error": "\033[31m",  # 红色
            "critical": "\033[35m",  # 紫色
        }

        # 根据配置决定是否启用颜色
        color_text = self._config.get("color_text", "title")
        if color_text == "none":
            self._colors = False
        elif color_text == "title":
            self._enable_module_colors = True
            self._enable_level_colors = False
            self._enable_full_content_colors = False
        elif color_text == "full":
            self._enable_module_colors = True
            self._enable_level_colors = True
            self._enable_full_content_colors = True
        else:
            self._enable_module_colors = True
            self._enable_level_colors = False
            self._enable_full_content_colors = False

    def __call__(self, logger, method_name, event_dict: EventDict) -> str:
        # sourcery skip: low-code-quality
        """渲染日志记录"""
        # 获取基本信息
        timestamp: str = event_dict.get("timestamp", "")
        level: str = event_dict.get("level", "info")
        logger_name: str = event_dict.get("logger_name") or event_dict.get("logger", "")
        event = event_dict.get("event", "")

        # 日志级别样式配置
        log_level_style = self._config.get("log_level_style", "lite")
        level_color = self._level_colors.get(level.lower(), "") if self._colors else ""

        # 构建输出
        parts = []
        # 时间戳（lite模式下按级别着色）
        if timestamp:
            if log_level_style == "lite" and level_color:
                timestamp_part = f"{level_color}{timestamp}{RESET_COLOR}"
            else:
                timestamp_part = timestamp
            parts.append(timestamp_part)

        # 日志级别显示（根据配置样式）
        if log_level_style == "compact":
            # 只显示首字母并着色
            level_text = level.upper()[0]
            level_part = f"{level_color}[{level_text:>8}]{RESET_COLOR}" if level_color else f"[{level_text:>8}]"
            parts.append(level_part)

        elif log_level_style == "full":
            # 显示完整级别名并着色
            level_text = level.upper()
            level_part = f"{level_color}[{level_text:>8}]{RESET_COLOR}" if level_color else f"[{level_text:>8}]"
            parts.append(level_part)

        # lite模式不显示级别，只给时间戳着色

        # 获取模块颜色，用于full模式下的整体着色
        module_color = ""
        if self._colors and self._enable_module_colors and logger_name:
            module_color = MODULE_COLORS.get(logger_name, "")

        # 模块名称（带颜色和别名支持）
        if logger_name:
            # 获取别名，如果没有别名则使用原名称
            display_name = MODULE_ALIASES.get(logger_name, logger_name)

            if self._colors and self._enable_module_colors and module_color:
                module_part = f"{module_color}[{display_name}]{RESET_COLOR}"
            else:
                module_part = f"[{display_name}]"
            parts.append(module_part)

        # 消息内容（确保转换为字符串）
        event_content = ""
        if isinstance(event, str):
            event_content = event
        elif isinstance(event, dict):
            # 如果是字典，格式化为可读字符串
            try:
                event_content = json.dumps(event, ensure_ascii=False, indent=None)
            except (TypeError, ValueError):
                event_content = str(event)
        else:
            # 其他类型直接转换为字符串
            event_content = str(event)

        # 在full模式下为消息内容着色
        if self._colors and self._enable_full_content_colors and module_color:
            event_content = f"{module_color}{event_content}{RESET_COLOR}"

        parts.append(event_content)

        # 处理其他字段
        extras = []
        for key, value in event_dict.items():
            if key not in (
                "timestamp",
                "level",
                "logger_name",
                "logger",
                "event",
                "module",
                "lineno",
                "pathname",
                "exception",
            ):
                # 确保值也转换为字符串
                if isinstance(value, (dict, list)):
                    try:
                        value_str = json.dumps(value, ensure_ascii=False, indent=None)
                    except (TypeError, ValueError):
                        value_str = str(value)
                else:
                    value_str = str(value)

                # 在full模式下为额外字段着色
                extra_field = f"{key}={value_str}"
                if self._colors and self._enable_full_content_colors and module_color:
                    extra_field = f"{module_color}{extra_field}{RESET_COLOR}"

                extras.append(extra_field)

        if extras:
            parts.append(" ".join(extras))

        rendered_message = " ".join(parts)
        exception_text = event_dict.get("exception")
        if exception_text:
            return f"{rendered_message}\n{exception_text}"

        return rendered_message


def start_log_cleanup_task():
    """启动日志清理任务"""
    global _cleanup_task_started

    # 防止重复启动清理任务
    if _cleanup_task_started:
        return

    _cleanup_task_started = True

    def cleanup_task():
        while True:
            try:
                cleanup_days = max(1, int(LOG_CONFIG.get("log_cleanup_days", 30) or 30))
                cutoff_date = datetime.now() - timedelta(days=cleanup_days)
                deleted_count = 0
                deleted_size = 0

                # 遍历日志目录
                for log_file in LOG_DIR.glob("*.log*"):
                    try:
                        file_time = datetime.fromtimestamp(log_file.stat().st_mtime)
                        if file_time < cutoff_date:
                            file_size = log_file.stat().st_size
                            log_file.unlink()
                            deleted_count += 1
                            deleted_size += file_size
                    except Exception as e:
                        logger = get_logger("logger")
                        logger.warning(f"清理日志文件 {log_file} 时出错: {e}")

                if deleted_count > 0:
                    logger = get_logger("logger")
                    logger.info(f"清理了 {deleted_count} 个过期日志文件，释放空间 {deleted_size / 1024 / 1024:.2f} MB")

            except Exception as e:
                logger = get_logger("logger")
                logger.error(f"清理旧日志文件时出错: {e}")
            time.sleep(24 * 60 * 60)  # 每24小时执行一次

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()


def _get_file_handler(formatter: structlog.stdlib.ProcessorFormatter) -> "TimestampedFileHandler":
    global _file_handler
    if _file_handler is None:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if isinstance(handler, TimestampedFileHandler):
                _file_handler = handler
                return _file_handler
        _file_handler = TimestampedFileHandler(
            log_dir=LOG_DIR,
            max_bytes=max(1024, int(LOG_CONFIG.get("log_file_max_bytes", 5 * 1024 * 1024) or 5 * 1024 * 1024)),
            backup_count=max(1, int(LOG_CONFIG.get("max_log_files", 30) or 30)),
            encoding="utf-8",
        )
        # 设置文件handler的日志级别
        file_level: str = LOG_CONFIG.get("file_log_level", LOG_CONFIG.get("log_level", "INFO"))
        _file_handler.setLevel(getattr(logging, file_level.upper(), logging.INFO))
        _file_handler.setFormatter(formatter)
    return _file_handler


def _get_console_handler(formatter: structlog.stdlib.ProcessorFormatter) -> logging.StreamHandler:
    """获取控制台handler单例"""
    global _console_handler
    if _console_handler is None:
        _console_handler = logging.StreamHandler()
        # 设置控制台handler的日志级别
        console_level: str = LOG_CONFIG.get("console_log_level", LOG_CONFIG.get("log_level", "INFO"))
        _console_handler.setLevel(getattr(logging, console_level.upper(), logging.INFO))
        _console_handler.setFormatter(formatter)
    return _console_handler


def _close_handlers():
    """安全关闭所有handler"""
    global _file_handler, _console_handler

    if _file_handler:
        _file_handler.close()
        _file_handler = None

    if _console_handler:
        _console_handler.close()
        _console_handler = None


def _configure_third_party_loggers():
    """配置第三方库的日志级别"""
    # 设置根logger级别为所有handler中最低的级别，确保所有日志都能被捕获
    console_level: str = LOG_CONFIG.get("console_log_level", LOG_CONFIG.get("log_level", "INFO"))
    file_level: str = LOG_CONFIG.get("file_log_level", LOG_CONFIG.get("log_level", "INFO"))

    # 获取最低级别（DEBUG < INFO < WARNING < ERROR < CRITICAL）
    console_level_num = getattr(logging, console_level.upper(), logging.INFO)
    file_level_num = getattr(logging, file_level.upper(), logging.INFO)
    min_level = min(console_level_num, file_level_num)

    root_logger = logging.getLogger()
    root_logger.setLevel(min_level)

    # 完全屏蔽的库
    suppress_libraries: List[str] = LOG_CONFIG.get("suppress_libraries", [])
    for lib_name in suppress_libraries:
        lib_logger = logging.getLogger(lib_name)
        lib_logger.setLevel(99)  # 设置为比CRITICAL更高的级别，基本屏蔽所有日志
        lib_logger.propagate = False  # 阻止向上传播

    # 设置特定级别的库
    library_log_levels: Dict[str, str] = LOG_CONFIG.get("library_log_levels", {})
    for lib_name, level_name in library_log_levels.items():
        lib_logger = logging.getLogger(lib_name)
        level = getattr(logging, level_name.upper(), logging.WARNING)
        lib_logger.setLevel(level)


_configure_third_party_loggers()


def _normalize_embedded_event_dict(logger, method_name, event_dict: EventDict):
    """将嵌套在 event 字段中的结构化日志还原为可读文本。"""
    record = event_dict.get("_record")
    if record is not None:
        msg = getattr(record, "msg", None)
        if isinstance(msg, dict):
            embedded_event = msg
    else:
        embedded_event = event_dict.get("event")

    if not isinstance(embedded_event, dict):
        return event_dict

    event_text = embedded_event.get("event")
    event_dict["event"] = str(embedded_event) if event_text is None else event_text
    for field_name in ("logger_name", "module", "lineno", "pathname"):
        if field_name not in event_dict and field_name in embedded_event:
            event_dict[field_name] = embedded_event[field_name]

    for key, value in embedded_event.items():
        if key in {"event", "level", "timestamp", "logger_name", "module", "lineno", "pathname"}:
            continue
        if key not in event_dict:
            event_dict[key] = value

    return event_dict


# 为文件输出配置JSON格式
file_formatter = structlog.stdlib.ProcessorFormatter(
    processor=structlog.processors.JSONRenderer(ensure_ascii=False),
    foreign_pre_chain=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        _normalize_embedded_event_dict,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.CallsiteParameterAdder(
            parameters=[structlog.processors.CallsiteParameter.PATHNAME, structlog.processors.CallsiteParameter.LINENO]
        ),
        _convert_pathname_to_module,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ],
)

# 为控制台输出配置可读格式
console_formatter = structlog.stdlib.ProcessorFormatter(
    processor=ModuleColoredConsoleRenderer(colors=True),
    foreign_pre_chain=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        _normalize_embedded_event_dict,
        _convert_pathname_to_module,
        structlog.processors.TimeStamper(fmt=LOG_CONFIG.get("timestamp_format", "iso"), utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ],
)

file_handler = _get_file_handler(file_formatter)
console_handler = _get_console_handler(console_formatter)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[file_handler, console_handler],
)

raw_logger: BoundLogger = structlog.get_logger()

binds: dict[str, BoundLogger] = {}

start_log_cleanup_task()


def get_logger(module_name: str):
    """
    为指定模块获取一个结构化日志记录器。

    Args:
        module_name (str): 模块名称。
    Returns:
        BoundLogger: 绑定了模块名称的结构化日志记录器。
    """
    if module_name is None:
        return raw_logger
    logger: Optional[BoundLogger] = binds.get(module_name)
    if not logger:
        logger = cast(BoundLogger, structlog.get_logger(module_name)).bind(logger_name=module_name)
        binds[module_name] = logger
    return logger


logger = get_logger("logger")
logger = get_logger("logger")
console_level = LOG_CONFIG.get("console_log_level", LOG_CONFIG.get("log_level", "INFO"))
file_level = LOG_CONFIG.get("file_log_level", LOG_CONFIG.get("log_level", "INFO"))
max_log_files = max(1, int(LOG_CONFIG.get("max_log_files", 30) or 30))
log_cleanup_days = max(1, int(LOG_CONFIG.get("log_cleanup_days", 30) or 30))
logger.info(
    f"日志系统已初始化：控制台={console_level}，文件={file_level}，"
    f"轮转={max_log_files}个文件，清理={log_cleanup_days}天前"
)


def shutdown_logging():
    """优雅关闭日志系统，释放所有文件句柄"""
    # 先输出到控制台，避免日志系统关闭后无法输出
    print("[logger] 正在关闭日志系统...")

    # 关闭所有handler
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        if hasattr(handler, "close"):
            handler.close()
        root_logger.removeHandler(handler)

    # 关闭全局handler
    _close_handlers()

    # 关闭所有其他logger的handler
    logger_dict = logging.getLogger().manager.loggerDict
    for _name, logger_obj in logger_dict.items():
        if isinstance(logger_obj, logging.Logger):
            for handler in logger_obj.handlers[:]:
                if hasattr(handler, "close"):
                    handler.close()
                logger_obj.removeHandler(handler)

    # 使用 print 而不是 logger，因为 logger 已经关闭
    print("[logger] 日志系统已关闭")
