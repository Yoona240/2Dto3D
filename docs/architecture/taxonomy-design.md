# 数据分类体系设计 (Taxonomy System Design)

> 本文档记录我们自有数据分类体系的设计与实现方案。

---

## 1. 设计原则

### 1.1 数据驱动方法论

**采用自下而上的数据驱动方法，而非预定义分类**：

```
Step 1: 下载 ShapeNet 官方分类体系 (taxonomy.json) 作为骨架
Step 2: 对 LVIS 1156 类别进行语义聚类 (embedding-based)
Step 3: 用 LLM 为聚类结果命名
Step 4: 将 LVIS 物体挂载到 ShapeNet 分类树
Step 5: 人工校验边界情况
```

### 1.2 核心思想

- **ShapeNet 作为骨架**：保留完整的 WordNet 语义层级结构
- **LVIS 作为补充**：挂载到 ShapeNet 节点，或创建新根节点
- **嵌入聚类驱动**：使用语义嵌入确保类别划分的合理性

---

## 2. 实现方案

### 2.1 数据来源

| 数据 | 来源 | 输出文件 |
|:---|:---|:---|
| ShapeNet 分类树 | GitHub 镜像 (marian42/shapegan) | `data/captions/shapenet_taxonomy_tree.json` |
| LVIS 类别列表 | Objaverse-LVIS | `data/captions/objects.json` (1156 类) |
| LVIS 语义增强 | LLM 生成描述 | `data/captions/lvis_enriched.json` |
| LVIS 聚类结果 | 嵌入 + 层次聚类 | `data/captions/lvis_clusters.json` |
| 统一分类体系 | ShapeNet + LVIS 合并 | `data/captions/unified_taxonomy.json` |

### 2.2 实现脚本

| 脚本 | 功能 |
|:---|:---|
| `scripts/download_shapenet_taxonomy.py` | 下载并解析 ShapeNet 分类树 |
| `scripts/enrich_lvis.py` | 用 LLM 为每个 LVIS 类别生成语义描述 |
| `scripts/cluster_lvis.py` | 对 enriched LVIS 进行嵌入聚类 |
| `scripts/merge_taxonomy.py` | 将 LVIS 挂载到 ShapeNet 树 |

---

## 3. ShapeNet 分类树结构

### 3.1 统计信息

- **根类别数**: 55
- **总节点数**: 349
- **层级深度**: 基于 WordNet synset

### 3.2 结构示例

```json
{
  "synsetId": "02691156",
  "name": "airplane",
  "numInstances": 4045,
  "subcategories": [
    {"synsetId": "02690373", "name": "airliner", "subcategories": [...]},
    {"synsetId": "02842573", "name": "biplane", "subcategories": []},
    {"synsetId": "03595860", "name": "jet", "subcategories": [...]}
  ]
}
```

---

## 4. LVIS 语义增强

### 4.1 增强目的

原始 LVIS 类别名仅为单词 (如 `banana`, `barge`)，无法区分语义。通过 LLM 生成描述：

```json
{
  "banana": {
    "name": "banana",
    "type": "food",
    "description": "Tropical fruit with curved elongated shape, soft flesh, and yellow peel when ripe"
  },
  "barge": {
    "name": "barge",
    "type": "vehicle",
    "description": "Flat-bottomed boat for cargo transport on rivers and canals"
  }
}
```

### 4.2 用于聚类的文本

将 type + description 组合为聚类用文本：
```
"A 3D object of type 'food': Tropical fruit with curved elongated shape..."
"A 3D object of type 'vehicle': Flat-bottomed boat for cargo transport..."
```

---

## 5. LVIS 聚类方案

### 5.1 聚类方法

1. **文本嵌入**: 使用 `text-embedding-v3` 模型
2. **层次聚类**: 使用 `scipy.cluster.hierarchy`
3. **聚类数**: 20 类 (通过 Elbow Method 确定)

### 5.2 聚类结果示例

| 聚类 | 数量 | 示例物体 |
|:---|:--:|:---|
| Animals | 104 | alligator, bear, bird, cat... |
| Produce | 67 | apple, banana, artichoke... |
| Vehicles | 73 | airplane, car, bicycle... |
| Furniture | 48 | armchair, bed, bench... |
| Clothing | 85 | shirt, shoe, jacket... |
| Containers | 53 | bottle, barrel, bucket... |
| Tools & Devices | 137 | ax, hammer, brush... |
| Electronics | 26 | CD player, amplifier, camera... |
| Sports Equipment | 35 | baseball, basketball, ski... |
| Musical Instruments | 16 | banjo, guitar, piano... |
| Baked Goods | 78 | bagel, cake, bread... |
| Beverages | 23 | alcohol, coffee, juice... |
| Home Appliances | 26 | washer, blender, dishwasher... |
| Tableware & Cookware | 50 | bowl, plate, chopsticks... |
| Household Textiles | 44 | towel, blanket, curtain... |
| ... | ... | ... |

---

## 6. 统一分类体系

### 6.1 合并策略

**以 ShapeNet 为骨架，LVIS 物体挂载到匹配节点**：

1. **直接匹配**: LVIS 物体名 = ShapeNet 节点名 → 添加到 `lvis_items`
2. **父类匹配**: LVIS 物体是 ShapeNet 子类 → 挂载到父节点
3. **新建节点**: ShapeNet 缺失 → 按 LVIS 聚类创建新根节点

### 6.2 合并结果

```
统一分类体系 (Unified Taxonomy)
├── ShapeNet 原有节点 (55 个根类别)
│   ├── airplane
│   │   ├── airliner
│   │   │   └── [lvis_items]: ["hot-air balloon"]
│   │   ├── biplane
│   │   └── jet
│   ├── chair
│   │   ├── armchair
│   │   │   └── [lvis_items]: ["rocking chair", "folding chair"]
│   │   └── ...
│   └── ...
│
└── LVIS 新建根节点 (20 个聚类)
    ├── 🆕 Animals (104 items)
    │   └── [lvis_items]: ["bear", "cat", "dog", ...]
    ├── 🆕 Produce (67 items)
    │   └── [lvis_items]: ["apple", "banana", ...]
    ├── 🆕 Baked Goods (78 items)
    │   └── [lvis_items]: ["bagel", "cake", ...]
    └── ...
```

### 6.3 统计信息

| 指标 | 数量 |
|:---|:--:|
| ShapeNet 根类别 | 55 |
| 新增 LVIS 根类别 | 20 |
| **总根类别** | **75** |
| LVIS 匹配到 ShapeNet | 182 |
| LVIS 未匹配 (新根) | 974 |
| LVIS 总物体 | 1156 |

---

## 7. 输出文件格式

### 7.1 unified_taxonomy.json 结构

```json
{
  "version": "2.0",
  "description": "Unified taxonomy: ShapeNet hierarchy + LVIS items mounted",
  "sources": {
    "shapenet": "ShapeNetCore taxonomy (preserved hierarchy)",
    "lvis": "Objaverse-LVIS categories (1156 items)"
  },
  "statistics": {
    "shapenet_root_categories": 55,
    "new_lvis_roots": 20,
    "total_root_categories": 75,
    "lvis_items_matched": 182,
    "lvis_items_unmatched": 974,
    "total_lvis_items": 1156
  },
  "taxonomy": [
    {
      "synsetId": "02691156",
      "name": "airplane",
      "numInstances": 4045,
      "subcategories": [...],
      "lvis_items": ["airplane", "jet plane", ...]
    },
    {
      "synsetId": "lvis_16",
      "name": "Animals",
      "name_zh": "动物",
      "source": "lvis_cluster",
      "description": "...",
      "lvis_items": ["bear", "cat", "dog", ...]
    }
  ]
}
```

---

## 8. 可视化工具

### 8.1 Taxonomy Viewer

**文件**: `data/captions/taxonomy_viewer.html`

**功能**:
- 三标签页: ShapeNet / LVIS Clusters / Unified Taxonomy
- 树状图视图: 可展开/折叠的层级结构
- 搜索过滤
- LVIS 物体高亮显示

**使用方法**:
```bash
cd data
python -m http.server 8080
# 访问 http://localhost:8080/captions/taxonomy_viewer.html
```

---

## 9. 后续任务

### 9.1 待完成

- [ ] **人工校验边界情况**: 检查 LVIS 聚类是否合理
- [ ] **复杂度评分**: 为每个 LVIS 物体添加 1-5 复杂度分数
- [ ] **风格适配配置**: 为每个类别配置合适的风格权重
- [ ] **更新生成脚本**: 集成新分类体系到 `generate_primitives.py`

### 9.2 可优化方向

- 改进匹配算法: 当前仅基于名称匹配，可加入语义相似度
- 细化 ShapeNet 子类挂载: 部分 LVIS 物体可挂载到更深层节点
- 补充中文名称: 为所有类别添加中文翻译

---

## 10. 相关文件索引

| 文件 | 用途 |
|:---|:---|
| `data/captions/shapenet_taxonomy_raw.json` | ShapeNet 原始下载数据 |
| `data/captions/shapenet_taxonomy_tree.json` | ShapeNet 解析后的树结构 |
| `data/captions/objects.json` | LVIS 原始类别列表 |
| `data/captions/lvis_enriched.json` | LVIS 语义增强数据 |
| `data/captions/lvis_embeddings.npy` | LVIS 类别嵌入向量 |
| `data/captions/lvis_clusters.json` | LVIS 聚类结果 |
| `data/captions/unified_taxonomy.json` | 统一分类体系 |
| `data/captions/taxonomy_viewer.html` | 可视化页面 |
