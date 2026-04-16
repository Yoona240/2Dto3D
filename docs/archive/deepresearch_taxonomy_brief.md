# DeepResearch 调研任务书：多模态通用分类体系（Text/Image/3D）

> 目的：请你输出“各类分类体系/本体/知识图谱”的调研报告（survey report），用于本项目后续选择与组合；不要输出具体设计方案或实现细节。

---

## 0. 一句话摘要
本项目在做 2D→3D 资产生成与编辑；流程最上游需要大规模生成 Caption/Prompt，因此需要一个**覆盖真实世界、可扩展、可跨 Text/Image/3D 对齐**的分类体系。ShapeNet taxonomy 过窄且偏 CAD 刚体，请你调研 WordNet/COCO/LVIS/OpenImages/Wikidata 等体系并产出对比报告。

---

## 1. 项目背景（当前系统在做什么）
### 1.1 管线目标
本仓库是一个 2D 图像驱动的 3D 生成/编辑 pipeline：
- 输入：单张图片 + 用户编辑指令
- 输出：编辑后的 3D 模型（GLB）以及过程资产（渲染多视图、编辑后的多视图等）
- 关键机制：Session 管理、阶段式 pipeline、可恢复、日志可追溯

### 1.2 pipeline 概览（与 taxonomy 需求强相关）
典型流程（概念上）：
0) 图像描述/生成文本构建（Caption/PROMPT）【流程起点】
1) Stage1：Image-to-3D 初始生成（Tripo/Hunyuan）
2) Stage2：Blender 渲染多视图
3) Stage3：多视图一致的图像编辑（Gemini Image API），含“指令优化”
4) Stage4：多视角重建得到编辑后 3D

**taxonomy 在这里的作用**：
- 上游（Step0）：作为“可采样的对象词表/层级”，支撑大规模 Caption/Prompt 生成能覆盖足够广的真实世界范围（避免只围绕少量 CAD/刚体类别打转）
- 中游（生成与评测）：组织不同类别/不同风格/不同属性的覆盖与配比，支持统计、采样、配额与长尾控制
- 下游（编辑任务对齐）：把几何编辑、风格迁移、部件编辑、材质替换等任务落到稳定的对象概念与元数据上

---

## 2. 数据生成意图：多样性来自哪里
### 2.1 你们已明确的“几何风格多样性”框架
你们定义了 5 类互斥的几何风格（强调形状构造逻辑，尽量剥离材质/摄影干扰）：
- Realistic / High-Fidelity
- Low-Poly / Faceted
- Voxel / Blocky
- Toon / Smooth-Inflated
- Chiseled / Planar

并提出生成公式：
- **物体类别 × 几何风格 × 结构化属性**
目标是最大化 mesh 拓扑的类间方差，服务 3D 风格迁移/几何编辑训练。

### 2.2 Caption/Prompt 的结构化约束（影响 taxonomy 设计）
- Caption 只描述“物体本体”：几何/结构/材质/表面纹理；避免摄影词与主观词
- Prompt 构建更偏“可控生成”：对象 + 风格 + 几何关键词

**对 taxonomy 的启示**：
- taxonomy 不仅是“名词列表”，还需要能承载：部件/结构属性、材质与表面词、风格标签、复杂度评分等可控维度

---

## 3. 当前 taxonomy 方案（已有工程与数据）
你们目前采用一个“ShapeNet 骨架 + LVIS 扩展 + 嵌入聚类”的数据驱动流程：
- Step 1：ShapeNet taxonomy tree 作为骨架（保留 WordNet synset 层级）
- Step 2：Objaverse-LVIS 1156 类别作为覆盖补充
- Step 3：LLM 为 LVIS 类别做语义增强（type + description）
- Step 4：embedding + 层次聚类得到 ~20 个 cluster roots
- Step 5：把可匹配项挂载到 ShapeNet 节点，其余作为新根节点

输出数据（仓库中已有）：
- ShapeNet 解析树：`data/shapenet_taxonomy_tree.json`
- LVIS 原始：`data/objects.json`
- LVIS enriched：`data/lvis_enriched.json`
- LVIS clusters：`data/lvis_clusters.json`
- Unified taxonomy：`data/unified_taxonomy.json`

---

## 4. 关键问题：为什么 ShapeNet 不够（需要重新调研）
ShapeNetCore（55 类）更像“规则化 CAD 对象子集”，对现实世界多样性不足：
- 覆盖偏刚体、工业设计、拓扑干净的对象
- 明显缺失：自然物、非刚体/柔体、强风格化日用品、装饰玩具、场景级组合等
- 不适合“生成/编辑导向”的广覆盖 taxonomy

因此：
- ShapeNet 只能当“几何稳定子集/锚点”，不应该当主 taxonomy。

---

## 5. 调研范围（必须覆盖，尽量只写结论，不要推导设计）

请你分别调研并总结这些体系的“类别组织方式”，重点关注其是否能支撑本项目 Step0 的 Caption/Prompt 广覆盖生成，以及跨模态对齐：

- 文本/概念层：WordNet、Wikidata、BabelNet（可选：ConceptNet）
- 图像数据集标签：COCO、LVIS、Open Images（可选：Visual Genome、ImageNet）
- 3D 数据集标签/组织：ShapeNet（含其 WordNet 对齐方式）、Objaverse（含 Objaverse-LVIS）、PartNet（部件层级）

---

## 6. 你需要回答的关键问题（调研视角）

1) 这些体系各自解决了什么问题？它们的“类别定义/粒度/层级/多标签/同义词/多语言”分别怎么做？
2) 哪些体系更适合做“对象词表/概念对齐层”（用于生成 Caption/Prompt）？哪些更适合做“数据集标注层”？为什么？
3) COCO vs LVIS vs Open Images：长尾覆盖与类别组织差异是什么？它们是否有层级？如何处理同义词与歧义？
4) WordNet vs Wikidata vs BabelNet：
   - 是否适合作为“统一概念层”？
   - 在多语言（尤其中文）与同义词/多义词处理上的差异是什么？
5) 3D 侧的难点：3D 类别与现实概念对齐时常见歧义是什么？PartNet 这种“部件层级”在编辑任务里能提供什么额外价值？

---

## 7. 交付物（请你输出一份调研报告，而非方案）

请你输出结构化调研报告，格式尽量统一、便于直接阅读与二次引用：

### 7.1 一张总览对比表（必需）
对每个体系给出：
- 类型（concept ontology / image dataset labels / 3d dataset labels）
- 类别规模（数量级即可）与是否层级化
- 是否支持多标签、同义词/别名、多语言
- 典型使用场景与主要局限
- 对本项目 Step0（Caption/Prompt 广覆盖生成）的价值：高/中/低（并用 1-2 句话说明）

### 7.2 每个体系 6 条以内要点（必需）
每个体系用不超过 6 条 bullet 总结：
- 类别组织方式（层级/扁平、粒度）
- 与其他体系的对齐关系（例如 WordNet 对齐、Wikidata QID）
- 常见坑（歧义、同义词爆炸、类目不一致等）
- 对“覆盖真实世界长尾”的表现

### 7.3 结论（必需）
请你给出“调研结论”，但不要输出具体设计：
- 如果目标是“广覆盖对象词表 + 多模态对齐”，哪 2-3 个体系最适合作为主参考？理由是什么？
- 对本项目来说，ShapeNet 适合扮演什么角色、不适合扮演什么角色？

---

## 8. 简版评估维度（写报告时请沿用）
- 覆盖：真实世界长尾覆盖能力
- 组织：层级质量/粒度一致性/多标签能力
- 对齐：与其他体系的对齐方式与难度
- 多语言：是否原生支持中文/别名机制
- 3D 相关：是否提供部件/属性/关系等可编辑语义
