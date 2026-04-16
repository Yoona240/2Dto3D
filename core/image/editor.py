"""
Image Editor Module

使用统一的 ImageApiClient 进行图像编辑。
支持: gemini-2.5-flash-image, imagen-4.0 等图像编辑模型。
"""

from typing import Union

from utils.config import GeminiResponseConfig, ImageApiConfig
from utils.image_api_client import ImageApiClient


class ImageEditor:
    """
    图像编辑器 - 使用统一 ImageApiClient
    """
    
    def __init__(self, config: Union[GeminiResponseConfig, ImageApiConfig]):
        """初始化图像编辑器"""
        self.config = config
        self.client = ImageApiClient(config)

    def edit_image(self, image_path: str, instruction: str, output_path: str) -> str:
        """
        基于指令编辑图像
        
        Args:
            image_path: 源图片路径
            instruction: 编辑指令
            output_path: 保存路径
            
        Returns:
            保存的图片路径
        """
        return self.client.edit_image(
            image_path,
            instruction,
            output_path,
            size=self.config.size,
            auto_size=False,
        )

    def close(self):
        self.client.close()
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
