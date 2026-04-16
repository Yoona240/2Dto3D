"""
Logging Configuration

提供统一的日志系统，支持：
- 控制台输出
- 文件记录（按模块分离）
- 结构化日志（JSON 格式，便于分析）

使用示例:
    from utils.logger import get_logger
    
    logger = get_logger("image_api")
    logger.info("Generating image", extra={"prompt": prompt, "model": model})
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# 日志根目录（懒加载，首次使用时从 config.yaml workspace.logs_dir 读取）
_LOG_DIR: Optional[Path] = None


def _get_log_dir() -> Path:
    global _LOG_DIR
    if _LOG_DIR is not None:
        return _LOG_DIR
    try:
        from config.config import load_config
        cfg = load_config()
        _LOG_DIR = Path(cfg.workspace.logs_dir)
    except Exception:
        _LOG_DIR = Path(__file__).parent.parent / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


class JsonFormatter(logging.Formatter):
    """JSON 格式化器，用于结构化日志"""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # 添加 extra 字段
        for key in ["prompt", "image_path", "output_path", "model", "elapsed_time", 
                    "response_id", "error", "status", "subject", "style_hint"]:
            if hasattr(record, key):
                log_data[key] = getattr(record, key)
        
        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """控制台格式化器，带颜色"""
    
    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        msg = f"{color}[{timestamp}] [{record.levelname}] [{record.name}]{self.RESET} {record.getMessage()}"
        
        # 添加关键 extra 字段
        extras = []
        if hasattr(record, "prompt"):
            prompt = getattr(record, "prompt", "")
            if len(prompt) > 80:
                prompt = prompt[:80] + "..."
            extras.append(f"prompt={prompt}")
        if hasattr(record, "output_path"):
            extras.append(f"output={getattr(record, 'output_path', '')}")
        if hasattr(record, "model"):
            extras.append(f"model={getattr(record, 'model', '')}")
        if hasattr(record, "elapsed_time"):
            extras.append(f"elapsed={getattr(record, 'elapsed_time', 0):.1f}s")
        
        if extras:
            msg += f" | {' | '.join(extras)}"
        
        return msg


def get_logger(
    name: str,
    level: int = logging.INFO,
    log_to_file: bool = True,
    log_to_console: bool = True,
) -> logging.Logger:
    """
    获取或创建 logger
    
    Args:
        name: logger 名称（如 "image_api", "prompt_optimizer"）
        level: 日志级别
        log_to_file: 是否输出到文件
        log_to_console: 是否输出到控制台
    
    Returns:
        配置好的 logger 实例
    """
    logger = logging.getLogger(name)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    logger.setLevel(level)
    logger.propagate = False  # 避免传递到 root logger
    
    # 控制台 Handler
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(ConsoleFormatter())
        logger.addHandler(console_handler)
    
    # 文件 Handler (JSON 格式)
    if log_to_file:
        # 按日期 + 模块名分文件
        today = datetime.now().strftime("%Y%m%d")
        log_file = _get_log_dir() / f"{name}_{today}.jsonl"
        
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)
    
    return logger


# 预定义常用 logger
def get_image_api_logger() -> logging.Logger:
    """获取 Image API 专用 logger"""
    return get_logger("image_api")


def get_prompt_logger() -> logging.Logger:
    """获取 Prompt 生成专用 logger"""
    return get_logger("prompt")


def get_pipeline_logger() -> logging.Logger:
    """获取 Pipeline 专用 logger"""
    return get_logger("pipeline")
