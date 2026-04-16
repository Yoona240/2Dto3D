# 数据风格多样性设计 (Style Diversity Design)

> 本文档记录数据生成的风格多样性设计，包括几何风格分类、Caption 设计原则和 Prompt 构建策略。

---

## 1. 核心目标

通过 **物体类别 × 几何风格 × 结构化属性** 的组合，生成多样化的 3D 资产，确保：
- Mesh 拓扑结构的**类间方差 (Inter-class Variance)** 最大化
- 适合训练 3D 风格迁移或几何编辑模型

---

## 2. 几何风格分类体系 (Geometric Style Taxonomy)

> **核心思想**：风格定义应专注于**形状构造逻辑**，剥离材质干扰。
> 
> Image-to-3D 技术路线下，Source Image 的风格直接决定生成 Mesh 的**几何拓扑特征**。

### 2.1 五大核心几何风格

#### 1. Realistic / High-Fidelity (写实/高保真)

**定义**：追求物理世界的真实比例和表面细节。

| 特征 | 描述 |
|:---|:---|
| **边缘** | 自然、平滑倒角 (Beveled)，无锐利的人工切角 |
| **表面** | 包含微小的几何置换 (Displacement) 细节 |
| **比例** | 严格遵循解剖学或工业设计比例 |

**Prompt 关键词**：`photorealistic`, `highly detailed`, `8k`, `anatomically correct`, `intricate details`

**对应编辑任务**：局部微调、表情修改

---

#### 2. Low-Poly / Faceted (低多边形/硬表面)

**定义**：故意限制多边形数量，强调"面"的可见性。

| 特征 | 描述 |
|:---|:---|
| **边缘** | 极度锐利 (Sharp Edges)，顶点法线不平滑 (Flat Shading) |
| **表面** | 由明显的平坦小面 (Facets) 组成，没有平滑过渡 |
| **比例** | 通常伴随一定程度的概括和抽象 |

**Prompt 关键词**：`low poly`, `faceted`, `low poly art`, `game asset`, `sharp edges`, `minimalist geometry`, `triangulated`

**对应编辑任务**：减面 (Decimation)、风格化抽象

---

#### 3. Voxel / Blocky (体素/积木)

**定义**：由正交的立方体堆叠而成，几何上最独特的风格。

| 特征 | 描述 |
|:---|:---|
| **边缘** | 90度直角 |
| **表面** | 阶梯状 (Stair-stepping)，没有斜面或曲面 |
| **比例** | 像素化 (Pixelated) 的体积感 |

**Prompt 关键词**：`voxel art`, `minecraft style`, `composed of cubes`, `lego style`, `8-bit 3d`, `blocky`

**对应编辑任务**：体素化 (Voxelization)、乐高化

---

#### 4. Toon / Smooth-Inflated (卡通/Q版/充气感)

**定义**：动漫手办风格，极度平滑的表面，去除高频噪点。

| 特征 | 描述 |
|:---|:---|
| **边缘** | 极度圆润，流动感强 |
| **表面** | 像气球一样饱满 (Inflated) 或陶泥一样光滑 (Clay-like) |
| **比例** | 夸张（大头、大眼、粗四肢） |

**Prompt 关键词**：`toon style`, `chibi`, `smooth clay`, `vinyl toy`, `rounded edges`, `minimalist shapes`, `inflated`, `blind box style`

**对应编辑任务**：平滑化 (Smoothing)、变形夸张 (Deformation)

---

#### 5. Chiseled / Planar (雕塑/切面/硬朗)

**定义**：强调块面感 (Planar)，像刀削或罗丹的雕塑。

| 特征 | 描述 |
|:---|:---|
| **边缘** | 硬朗、有力，像岩石或切削过的木头 |
| **表面** | 由大的平坦块面构成结构，忽略细微的起伏 |
| **区别** | 比 Low-poly 面数高，比 Realistic 更硬朗，比 Voxel 更自由（有斜面） |

**Prompt 关键词**：`chiseled`, `carved`, `planar style`, `statue`, `hard surface`, `sharp features`, `angular`

**对应编辑任务**：风格化雕刻、硬表面化

---

### 2.2 风格互斥性矩阵

验证 5 种风格的**全面性且无冗余性**：

| 风格 | 表面平滑度 | 边缘锐利度 | 构成单元 | 典型视觉 |
|:---|:---|:---|:---|:---|
| **Realistic** | 中 (有细节) | 软硬结合 | 复杂拓扑 | 真实物体 |
| **Low-Poly** | 低 (平坦) | 极锐利 | 三角形/四边形 | 老游戏/折纸 |
| **Voxel** | 平坦 | 90度直角 | 立方体 | Minecraft/乐高 |
| **Toon** | 极高 (光滑) | 极圆润 | 连续曲面 | 手办/盲盒 |
| **Planar** | 平坦块面 | 硬朗 | 切削块体 | 雕像/岩石 |

---

## 3. Caption 设计原则

### 3.1 核心约束

| 原则 | 说明 | ✅ 正确示例 | ❌ 错误示例 |
|:---|:---|:---|:---|
| **描述物体本体** | 只描述对象的几何/结构/材质 | `a medieval sword with curved blade` | `studio lighting, white background` |
| **避免摄影术语** | 不要镜头、灯光、背景描述 | `polished steel texture` | `wide angle, depth of field` |
| **避免主观审美词** | 不要 beautiful, stunning 等 | `intricate engraved pattern` | `beautiful gorgeous design` |
| **结构化短语** | 主体 + 细节 + 属性 + 材质 | 见下方模板 | 长句自由描述 |

### 3.2 Caption 模板公式

```
{主体} + {结构细节} + {形状属性} + {材质/表面纹理} + {风格标识}
```

**各模块说明**：
- **主体**: 对象类别 + 关键部件 (`a DSLR camera with detachable lens`)
- **结构细节**: 构成方式 (`multi-component`, `articulated parts`, `hollow section`)
- **形状属性**: 几何描述 (`angular form`, `curved surface`, `tapered ends`)
- **材质/纹理**: 表面特征 (`brushed metal`, `matte plastic`, `engraved pattern`)
- **风格标识**: 艺术风格 (`low-poly style`, `cartoon proportions`)

---

## 4. 模块化词汇库 (Modular Vocabulary)

### 4.1 结构细节词库 (Structure)

| 类别 | 关键词 |
|:---|:---|
| 构成 | `multi-component`, `modular design`, `connected parts`, `articulated joints` |
| 空间 | `hollow section`, `open frame`, `solid core`, `layered construction` |
| 边缘 | `thin edges`, `thick body`, `reinforced structure`, `delicate framework` |

### 4.2 形状属性词库 (Shape)

| 类别 | 关键词 |
|:---|:---|
| 表面 | `smooth surface`, `faceted surface`, `geometric detail`, `organic curves` |
| 外形 | `elongated form`, `compact form`, `balanced proportions`, `asymmetric design` |
| 细节 | `tapered ends`, `rounded corners`, `sharp edges`, `beveled surfaces` |
| 几何 | `cylindrical`, `spherical`, `planar`, `conical`, `prismatic` |

### 4.3 材质/纹理词库 (Material)

| 类别 | 关键词 |
|:---|:---|
| 金属 | `brushed metal`, `polished steel`, `oxidized copper`, `chrome finish` |
| 塑料 | `matte plastic`, `translucent plastic`, `rubber grip`, `glossy surface` |
| 自然 | `wooden texture`, `leather wrapping`, `ceramic glaze`, `stone grain` |
| 装饰 | `engraved pattern`, `embossed details`, `woven texture`, `painted surface` |

### 4.4 风格词库 (Style)

| 风格 | 关键词 | 适用场景 |
|:---|:---|:---|
| 写实 | `realistic proportions`, `detailed features`, `natural appearance` | 产品渲染 |
| 低多边形 | `low-poly style`, `geometric faceted`, `minimal polygons`, `blocky form` | 游戏资产 |
| 卡通 | `cartoon style`, `rounded shapes`, `playful proportions`, `simplified features` | 休闲游戏 |
| 动漫 | `anime style`, `cel-shaded appearance`, `bold outlines` | JRPG |
| 极简 | `minimalist design`, `clean lines`, `abstract form`, `essential geometry` | 概念设计 |
| 雕塑 | `chiseled`, `carved`, `planar style`, `angular` | 艺术品 |

---

## 5. Prompt 构建策略

### 5.1 基本模板

```
A 3D model of {Object}, {Geometry_Style} style, {Geometry_Keywords}, solid mesh, white background.
```

### 5.2 风格示例

| 物体 | 风格 | Prompt |
|:---|:---|:---|
| Wolf | Realistic | `A 3D model of a Wolf, realistic style, anatomically correct, highly detailed fur geometry...` |
| Wolf | Low-Poly | `A 3D model of a Wolf, low poly style, faceted geometry, sharp edges, minimal polygons...` |
| Wolf | Voxel | `A 3D model of a Wolf, voxel art style, composed of cubes, blocky, 8-bit...` |
| Wolf | Toon | `A 3D model of a Wolf, toon style, smooth clay finish, rounded shapes, chibi proportions...` |
| Wolf | Planar | `A 3D model of a Wolf, chiseled style, planar statue, angular cuts, carved wood geometry...` |

### 5.3 同一物体 × 多风格扩增

| 物体 | Realistic | Low-Poly | Cartoon |
|:---|:---|:---|:---|
| Camera | ✅ `multi-component body, angular form, matte black rubber grip` | ✅ `blocky geometric form, faceted angular surfaces` | ✅ `rounded playful proportions, smooth simplified body` |

---

## 6. Caption 质量评估指标

用于自动筛选高质量 Caption：

| 指标 | 要求 |
|:---|:---|
| **实体词覆盖度** | 必须包含核心类别 + ≥2 个特征描述 |
| **词汇结构完整度** | 至少包含：主体 + 形状 + 材质/细节 |
| **无摄影词** | 排除 `lighting`, `background`, `camera angle`, `lens` |
| **无主观词** | 排除 `beautiful`, `stunning`, `aesthetic` |
| **有风格标识** | 必须包含明确的风格关键词 |

---

## 7. 物体复杂度分级

### 7.1 评分标准 (1-5 分)

| 分数 | 描述 | 示例 | 策略 |
|:--:|:---|:---|:---|
| **1** | 极度简单/扁平 | `Band Aid`, `Card`, `Paper` | 跳过 |
| **2** | 基础几何体 | `Box`, `Ball`, `Brick` | 低权重 |
| **3** | 有组件结构 | `Chair`, `Lamp`, `Bottle` | 标准 |
| **4** | 复杂结构/镂空 | `Camera`, `Shoe`, `Toy` | 优先 |
| **5** | 精密/有机结构 | `Engine`, `Dragon`, `Tree` | 最高优先 |

### 7.2 数据扩增矩阵

**扩增公式**：`物体类别 × 风格 × 属性变体`

| 物体 (Score≥3) | Realistic | Low-Poly | Cartoon | Toon | Planar |
|:---|:---:|:---:|:---:|:---:|:---:|
| Camera | ✓ | ✓ | ✓ | ✓ | ✓ |
| Dragon | ✓ | ✓ | ✓ | ✓ | ✓ |
| Chair | ✓ | ✓ | ✓ | ✓ | ✓ |

**预计规模**：
- 高价值类别: ~500 类 (Score ≥ 3)
- 风格: 5 种
- 属性变体: 2-3 种/类别
- **总计: ~5000-7500 张多样化图像**

---

## 8. 类别 × 风格权重配置

不同类别适合不同的风格权重：

```json
{
  "category_style_weights": {
    "Animals": {"realistic": 3, "toon": 3, "lowpoly": 2, "voxel": 1, "planar": 1},
    "Vehicles": {"realistic": 4, "lowpoly": 3, "voxel": 2, "planar": 1},
    "Furniture": {"realistic": 4, "lowpoly": 2, "voxel": 2, "planar": 2},
    "Food": {"realistic": 5, "toon": 3, "lowpoly": 1},
    "default": {"realistic": 2, "lowpoly": 2, "toon": 2, "voxel": 1, "planar": 1}
  }
}
```

---

## 9. 常见错误避免

| 错误类型 | 说明 | 解决方案 |
|:---|:---|:---|
| 过度泛化 | 用 broad 词如 "device" | 使用具体类别名 |
| 语义重复 | `angular form + sharp edges` 冗余 | 保持描述互补 |
| 摄影性修饰 | 包含 lens / angle / lighting | 严格排除 |
| 材质与几何混淆 | `wood texture` vs `wood carving` | 区分材质与造型 |
