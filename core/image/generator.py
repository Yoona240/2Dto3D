"""
Text-to-Image Generator Module

使用统一的 ImageApiClient 进行图像生成。
支持: gemini-2.5-flash-image, imagen-4.0, Flux 等。
"""

from typing import Union

from utils.config import OpenRouterConfig, ImageApiConfig
from utils.image_api_client import ImageApiClient


class T2IGenerator:
    """
    T2I 生成器 - 使用统一 ImageApiClient
    """
    
    def __init__(self, config: Union[OpenRouterConfig, ImageApiConfig]):
        """初始化 T2I 生成器"""
        self.config = config
        self.client = ImageApiClient(config)
        
    def generate_image(self, prompt: str, output_path: str) -> str:
        """
        从文本生成图片
        
        Args:
            prompt: 文本描述
            output_path: 保存路径
            
        Returns:
            保存的图片路径
        """
        return self.client.generate_image(prompt, output_path)

    def close(self):
        self.client.close()
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
