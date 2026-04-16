# LVIS Clustering Comparison

## 方法对比

| 方法 | 步骤 | 数据文件 | 耗时 | API 成本 |
|:---|:---|:---|:---|:---|
| **原方法 (LLM增强)** | 1. LLM生成描述<br>2. 嵌入描述文本<br>3. 聚类 | `lvis_enriched.json`<br>`lvis_embeddings.npy`<br>`lvis_clusters.json` | ~15分钟 | 高 (1156个LLM调用) |
| **新方法 (直接词嵌入)** | 1. 嵌入类别名<br>2. 聚类 | `lvis_embeddings_simple.npy`<br>`lvis_clusters_simple.json` | ~5分钟 | 低 (仅嵌入API) |

## 聚类质量对比

### 原方法 (LLM Enriched) - 20 clusters
- **优点**: 语义丰富，考虑了物体功能和特征
- **示例**: "Animals" 聚类包含正确的动物物体
- **不足**: 依赖 LLM 质量，成本高

### 新方法 (Direct Embedding) - 20 clusters
- **优点**: 快速、低成本、可重复
- **示例**: 
  - Cluster 3 (78 items): alligator, bear, cat, dog, elephant... (动物聚类成功)
  - Cluster 4 (77 items): Tabasco sauce, almond, vegetables... (食物/调料)
  - Cluster 5 (60 items): bagel, bread, cake... (烘焙食品)
- **不足**: 仅基于名称，可能混淆同名异义词

### 聚类大小分布

| 方法 | Min | Max | Avg | 标准差 |
|:---|:--:|:--:|:--:|:--:|
| LLM Enriched | - | - | - | - |
| Direct Embedding | 16 | 132 | 57.8 | - |

## 建议

1. **快速原型**: 使用直接词嵌入（`cluster_lvis_simple.py`）
2. **生产环境**: 使用 LLM 增强（`cluster_lvis.py`）+ 人工校验
3. **混合方案**: 先用简单方法分组，再用 LLM 优化边界情况

## 使用方法

```bash
# 简单方法（推荐首次尝试）
python scripts/cluster_lvis_simple.py --n-clusters 20

# 使用缓存（第二次运行）
python scripts/cluster_lvis_simple.py --n-clusters 20 --use-cached

# LLM 增强方法
python scripts/enrich_lvis.py  # 先生成描述
python scripts/cluster_lvis.py  # 再聚类
```
