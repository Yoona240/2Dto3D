"""
LLM 客户端封装

提供统一的 chat() 接口，支持多种 LLM 提供商。
内置重试机制、日志记录、错误处理。

使用 requests 库（与其他项目保持一致）。

使用示例:
    from utils.llm_client import get_llm_client
    from utils.config import load_config
    
    config = load_config()
    client = get_llm_client(config.qh_mllm)
    
    response = client.chat(
        system_prompt="You are a helpful assistant.",
        user_prompt="Hello!"
    )
"""

from __future__ import annotations

import base64
import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from utils.validation import require_non_empty


def _redact_secrets(obj: Any) -> Any:
    """Best-effort redaction for secrets in logs."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in {"authorization", "x-api-key", "api_key", "apikey", "bearer"}:
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [_redact_secrets(v) for v in obj]
    return obj


def _write_llm_log(
    *,
    log_dir: Optional[Path],
    provider: str,
    request: dict[str, Any],
    response_text: str,
    response_json: Optional[dict[str, Any]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Write LLM IO logs to disk for debugging."""
    if not log_dir:
        return
    
    # Validate response_text before attempting to write (Fail Loudly principle)
    validated_response = require_non_empty(response_text, "LLM response text")
    
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Request (redacted)
        req = {"provider": provider, "request": _redact_secrets(request)}
        (log_dir / "llm_request.json").write_text(
            json.dumps(req, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        # Response text
        (log_dir / "llm_response.txt").write_text(
            validated_response, encoding="utf-8"
        )
        
        # Response JSON (simplified)
        if response_json is not None:
            simplified = {"provider": provider}
            choices = response_json.get("choices") or []
            messages = [c.get("message") for c in choices if isinstance(c, dict)]
            simplified["messages"] = messages
            if response_json.get("model"):
                simplified["model"] = response_json["model"]
            if response_json.get("usage"):
                simplified["usage"] = response_json["usage"]
            (log_dir / "llm_response.json").write_text(
                json.dumps(_redact_secrets(simplified), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass  # Logging must never break pipeline


class BaseLLMClient(ABC):
    """LLM 客户端基类"""
    
    @abstractmethod
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs
    ) -> str:
        """
        发送对话请求
        
        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示
            **kwargs: 额外参数（如 temperature, max_tokens, log_dir）
            
        Returns:
            LLM 响应文本
        """
        pass
    
    def chat_with_images(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[Union[str, Path]],
        **kwargs
    ) -> str:
        """
        发送带图片的对话请求（VLM）
        
        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示
            images: 图片路径列表
            **kwargs: 额外参数
            
        Returns:
            LLM 响应文本
        """
        raise NotImplementedError("This client does not support image inputs")
    
    def close(self):
        """Close client resources (if any)."""
        pass
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class OpenAICompatibleClient(BaseLLMClient):
    """
    OpenAI 兼容 API 客户端
    
    支持: OpenAI, Azure, 群核 OneAPI, NewAPI, OpenRouter 等
    使用 requests 库。
    """
    
    def __init__(self, config):
        self.api_key = _require_attr(config, "api_key")
        base_url = _require_attr(config, "base_url")
        self.base_url = base_url.rstrip("/")
        self.model = _require_attr(config, "default_model")
        self.timeout = float(_require_attr(config, "timeout"))
        self.max_retries = int(_require_attr(config, "max_retries"))
        self.default_temperature = float(_require_attr(config, "temperature"))
        self.default_max_tokens = int(_require_attr(config, "max_tokens"))
    
    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
    
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        log_dir: Optional[Union[str, Path]] = None,
        **kwargs,
    ) -> str:
        """发送文本对话请求"""
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.default_max_tokens,
            **kwargs,
        }
        
        # GPT-5 models don't support temperature parameter
        is_gpt5 = "gpt-5" in self.model.lower() or "gpt5" in self.model.lower()
        use_temperature = not is_gpt5
        
        if use_temperature:
            if temperature is not None:
                payload["temperature"] = temperature
            else:
                payload["temperature"] = self.default_temperature
        
        url = f"{self.base_url}/chat/completions"
        log_path = Path(log_dir) if log_dir else None
        
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    _write_llm_log(
                        log_dir=log_path,
                        provider="openai_compatible",
                        request={"url": url, "payload": payload},
                        response_text=content,
                        response_json=data,
                        meta={"model": self.model, "base_url": self.base_url},
                    )
                    return content
                    
                elif response.status_code == 400:
                    # Check if it's a temperature issue (fallback for unknown models)
                    if "temperature" in response.text and "temperature" in payload:
                        # Silently retry without temperature
                        del payload["temperature"]
                        continue
                    # Other 400 error
                    _write_llm_log(
                        log_dir=log_path,
                        provider="openai_compatible",
                        request={"url": url, "payload": payload},
                        response_text=response.text,
                        meta={"status_code": 400, "note": "bad_request"},
                    )
                    raise RuntimeError(f"Bad request: {response.text[:500]}")
                    
                elif response.status_code in (429, 500, 502, 503, 504):
                    # Rate limit or server error, retry with backoff
                    if attempt < self.max_retries:
                        wait_time = attempt * 2
                        print(f"Server error {response.status_code}, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    
                    _write_llm_log(
                        log_dir=log_path,
                        provider="openai_compatible",
                        request={"url": url, "payload": payload},
                        response_text=response.text,
                        meta={"status_code": response.status_code, "note": "server_error"},
                    )
                    raise RuntimeError(
                        f"Server error {response.status_code} after {self.max_retries} retries"
                    )
                else:
                    _write_llm_log(
                        log_dir=log_path,
                        provider="openai_compatible",
                        request={"url": url, "payload": payload},
                        response_text=response.text,
                        meta={"status_code": response.status_code, "note": "api_error"},
                    )
                    raise RuntimeError(
                        f"API error {response.status_code}: {response.text[:500]}"
                    )
                    
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries:
                    wait_time = attempt * 2
                    print(f"Request error: {e}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                raise RuntimeError(f"Request failed after {self.max_retries} retries: {e}")
        
        raise RuntimeError("All retries exhausted")
    
    def chat_with_images(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[Union[str, Path, Dict[str, str]]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        log_dir: Optional[Union[str, Path]] = None,
        **kwargs,
    ) -> str:
        """发送带图片的对话请求（VLM）"""

        # Build content with images
        content = []
        if user_prompt:
            content.append({"type": "text", "text": user_prompt})

        for img_item in images:
            # Handle pre-encoded image data
            if isinstance(img_item, dict):
                img_data = img_item.get("data")
                media_type = img_item.get("media_type", "image/png")

                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{img_data}"}
                })
                continue

            path = Path(img_item)
            if not path.exists():
                raise FileNotFoundError(f"Image not found: {img_item}")

            # Encode image
            with open(path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode("utf-8")

            # Determine media type
            suffix = path.suffix.lower()
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(suffix, "image/png")

            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{img_data}"}
            })

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})
        
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.default_max_tokens,
            **kwargs,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        
        url = f"{self.base_url}/chat/completions"
        log_path = Path(log_dir) if log_dir else None
        
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content_text = data["choices"][0]["message"]["content"]
                    
                    _write_llm_log(
                        log_dir=log_path,
                        provider="openai_compatible",
                        request={"url": url, "model": self.model, "images": len(images)},
                        response_text=content_text,
                        response_json=data,
                    )
                    return content_text
                
                elif response.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        wait_time = attempt * 2
                        time.sleep(wait_time)
                        continue
                    raise RuntimeError(f"Server error {response.status_code}")
                else:
                    raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
                    
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries:
                    time.sleep(attempt * 2)
                    continue
                raise RuntimeError(f"Request failed: {e}")
        
        raise RuntimeError("All retries exhausted")


def _require_attr(obj: Any, name: str):
    value = getattr(obj, name, None)
    if value is None:
        raise ValueError(f"Missing required config attribute: {name}")
    if isinstance(value, str) and value == "":
        raise ValueError(f"Config attribute cannot be empty: {name}")
    return value


def get_llm_client(config) -> OpenAICompatibleClient:
    """
    根据配置创建 LLM 客户端
    
    Args:
        config: QnMllmConfig 或 OpenAICompatibleConfig 对象（必填）
        
    Returns:
        LLM 客户端实例
        
    Example:
        from utils.llm_client import get_llm_client
        
        # 指定配置
        from utils.config import load_config
        config = load_config()
        client = get_llm_client(config.qh_mllm)
        
        response = client.chat(
            system_prompt="You are helpful.",
            user_prompt="Hello!"
        )
    """
    return OpenAICompatibleClient(config)
