"""
View Stitcher Module

提供可复用的多视角拼接能力：
1. 将多个视角图拼接为 3x2 网格
2. 添加边框与标签
3. 可选填充为正方形以适配 Gemini 的 1:1 输出
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont


# 标准视角顺序 (3x2 网格)
# Row 1: front, back, right
# Row 2: left, top, bottom
VIEW_ORDER = ["front", "back", "right", "left", "top", "bottom"]
GRID_COLS = 3
GRID_ROWS = 2


class ViewStitcher:
    """
    可复用的视角拼接器
    """

    def __init__(self):
        # 样式配置 - 白色边框 + 浅灰标签
        self.border_width = 8                    # 白色边框宽度
        self.border_color = (255, 255, 255)      # 白色边框
        self.canvas_bg_color = (200, 200, 200)   # 画布背景色（浅灰）

        # 标签样式
        self.label_height = 40                   # 标签区域高度
        self.label_bg_color = (240, 240, 240)    # 浅灰色背景
        self.label_text_color = (80, 80, 80)     # 深灰色文字
        self.label_font_size = 36                # 字体大小

        # 间距
        self.cell_gap = 10                       # 单元格之间的间距

    def stitch_views(
        self,
        views_dir: Path,
        output_path: Path,
        view_names: Optional[List[str]] = None,
        pad_to_square: bool = True,
    ) -> Tuple[Path, dict]:
        """
        将多个视角图拼接为 3x2 网格图，并可选填充为正方形

        Args:
            views_dir: 包含视角图的目录
            output_path: 输出拼接图的路径
            view_names: 要拼接的视角名称列表（默认使用 VIEW_ORDER）
            pad_to_square: 是否填充为正方形

        Returns:
            Tuple of (output_path, metadata)
        """
        views_dir = Path(views_dir)
        view_paths = {
            view_name: views_dir / f"{view_name}.png"
            for view_name in (view_names or VIEW_ORDER)
        }
        return self.stitch_view_paths(
            view_paths=view_paths,
            output_path=output_path,
            view_names=view_names,
            pad_to_square=pad_to_square,
        )

    def stitch_view_paths(
        self,
        view_paths: Dict[str, Path],
        output_path: Path,
        view_names: Optional[List[str]] = None,
        pad_to_square: bool = True,
    ) -> Tuple[Path, dict]:
        """
        将显式给定路径的多视角图拼接为 3x2 网格图，并可选填充为正方形。

        Args:
            view_paths: 视角名 -> 图像路径
            output_path: 输出拼接图路径
            view_names: 拼接顺序（默认 VIEW_ORDER）
            pad_to_square: 是否填充为正方形

        Returns:
            Tuple of (output_path, metadata)
        """
        if view_names is None:
            view_names = VIEW_ORDER

        # 加载所有视角图并获取尺寸
        view_images = {}
        max_width = 0
        max_height = 0

        for view_name in view_names:
            raw_path = view_paths.get(view_name)
            if raw_path is None:
                continue
            view_path = Path(raw_path)
            if view_path.exists():
                img = Image.open(view_path).convert("RGBA")
                view_images[view_name] = img
                max_width = max(max_width, img.width)
                max_height = max(max_height, img.height)

        if not view_images:
            raise ValueError("No view images found in provided view_paths")

        # 统一图片区域尺寸
        image_width = max_width
        image_height = max_height

        # 单元格尺寸 = 边框 + 图片 + 边框 + 标签
        cell_width = self.border_width * 2 + image_width
        cell_height = self.border_width * 2 + image_height + self.label_height

        # 创建拼接画布（包含间距）
        canvas_width = cell_width * GRID_COLS + self.cell_gap * (GRID_COLS - 1)
        canvas_height = cell_height * GRID_ROWS + self.cell_gap * (GRID_ROWS - 1)
        canvas = Image.new("RGB", (canvas_width, canvas_height), color=self.canvas_bg_color)

        # 记录每个视角的位置信息
        view_positions = {}  # 整个单元格位置（包含标签）
        image_positions = {}  # 仅图片区域位置（用于裁剪）

        # 拼接各视角
        for idx, view_name in enumerate(view_names[:6]):  # 最多 6 个
            row = idx // GRID_COLS
            col = idx % GRID_COLS

            # 单元格左上角（考虑间距）
            cell_x = col * (cell_width + self.cell_gap)
            cell_y = row * (cell_height + self.cell_gap)

            # 图片区域（在边框内）
            img_x = cell_x + self.border_width
            img_y = cell_y + self.border_width

            # 绘制白色边框背景
            self._draw_cell_background(canvas, cell_x, cell_y, cell_width, cell_height)

            if view_name in view_images:
                img = view_images[view_name]

                # 居中放置（如果尺寸不一致）
                offset_x = (image_width - img.width) // 2
                offset_y = (image_height - img.height) // 2

                # 粘贴图像
                paste_x = img_x + offset_x
                paste_y = img_y + offset_y
                if img.mode == "RGBA":
                    canvas.paste(img, (paste_x, paste_y), img)
                else:
                    canvas.paste(img, (paste_x, paste_y))

                # 添加标签（在图片下方的外部区域）
                label_y = img_y + image_height + self.border_width
                self._draw_label(canvas, view_name, cell_x, label_y, cell_width, self.label_height)

            # 记录位置信息
            view_positions[view_name] = {
                "x": cell_x,
                "y": cell_y,
                "width": cell_width,
                "height": cell_height,
            }
            image_positions[view_name] = {
                "x": img_x,
                "y": img_y,
                "width": image_width,
                "height": image_height,
            }

        # 填充为正方形
        content_width = canvas_width
        content_height = canvas_height

        if pad_to_square and canvas_width != canvas_height:
            square_size = max(canvas_width, canvas_height)
            square_canvas = Image.new("RGB", (square_size, square_size), color=self.canvas_bg_color)

            # 居中放置原始内容
            offset_x = (square_size - canvas_width) // 2
            offset_y = (square_size - canvas_height) // 2
            square_canvas.paste(canvas, (offset_x, offset_y))

            canvas = square_canvas

            # 更新所有位置信息（加上偏移量）
            for view_name in view_positions:
                view_positions[view_name]["x"] += offset_x
                view_positions[view_name]["y"] += offset_y
                image_positions[view_name]["x"] += offset_x
                image_positions[view_name]["y"] += offset_y
        else:
            offset_x = 0
            offset_y = 0
            square_size = canvas_width

        # 保存拼接图
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, "PNG")

        metadata = {
            "image_width": image_width,
            "image_height": image_height,
            "cell_width": cell_width,
            "cell_height": cell_height,
            "border_width": self.border_width,
            "label_height": self.label_height,
            "cell_gap": self.cell_gap,
            "content_width": content_width,
            "content_height": content_height,
            "content_offset_x": offset_x,
            "content_offset_y": offset_y,
            "grid_width": square_size,
            "grid_height": square_size,
            "view_positions": view_positions,
            "image_positions": image_positions,
            "view_names": list(view_images.keys()),
        }

        return output_path, metadata

    def _draw_cell_background(self, canvas: Image.Image, x: int, y: int, width: int, height: int):
        """绘制单元格背景（白色边框效果）"""
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([x, y, x + width, y + height], fill=self.border_color)

    def _draw_label(
        self,
        canvas: Image.Image,
        label_text: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ):
        """在指定区域添加标签（图片外部区域）"""
        draw = ImageDraw.Draw(canvas)

        draw.rectangle([x, y, x + width, y + height], fill=self.label_bg_color)

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", self.label_font_size)
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", self.label_font_size)
            except Exception:
                font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), label_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        text_x = x + (width - text_width) // 2
        text_y = y + (height - text_height) // 2

        draw.text((text_x, text_y), label_text, fill=self.label_text_color, font=font)
