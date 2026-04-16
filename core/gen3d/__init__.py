# 3D generation modules
from .base import Base3DGenerator
from .tripo import TripoGenerator
from .hunyuan import HunyuanGenerator
from .rodin import RodinGenerator

GENERATORS = {
    'tripo': TripoGenerator,
    'hunyuan': HunyuanGenerator,
    'rodin': RodinGenerator,
}

MODEL_ID_MAP = {
    'tripo': 'tp3',
    'hunyuan': 'hy3',
    'rodin': 'rd2',
}

def get_model_id(generator_name: str) -> str:
    """Get short model ID from generator name."""
    return MODEL_ID_MAP.get(generator_name.lower(), generator_name[:3])
