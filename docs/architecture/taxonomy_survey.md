# 3D 数据分类体系调研 (Taxonomy Survey)

> 本文档整理现有 3D 数据集分类体系的调研资料，供参考设计自有分类体系。

---

## 1. Sketchfab 分类体系

**核心逻辑：视觉导向 + 创作者社区 (Visual-First & Community-Centric)**

### 1.1 顶层分类 (Top-level Categories) - 共 19 类

1.  **Animals & Pets (动物与宠物)**: 涵盖真实动物、昆虫、史前生物
2.  **Architecture (建筑)**: 房子、桥梁、纪念碑、室内设计
3.  **Art & Abstract (艺术与抽象)**: 雕塑、概念艺术 *(适合做"风格化编辑"数据)*
4.  **Cars & Vehicles (汽车与载具)**: 汽车、摩托、火车、船、飞机
5.  **Characters & Creatures (角色与生物)**: 人物、怪物、机器人、二次元角色 *(编辑任务的重点区域)*
6.  **Cultural Heritage & History (文化遗产与历史)**: 博物馆扫描件、考古文物
7.  **Electronics & Gadgets (电子产品)**: 手机、相机、电脑、复古电器
8.  **Fashion & Style (时尚与风格)**: 鞋子、衣服、包袋、珠宝 *(非常适合"换材质/换纹理"编辑任务)*
9.  **Food & Drink (食物与饮料)**: 水果、蛋糕、瓶装水
10. **Furniture & Home (家具与家居)**: 桌椅、灯具、装饰品
11. **Music (音乐)**: 乐器、音响设备
12. **Nature & Plants (自然与植物)**: 树木、花草、岩石、地形
13. ~~News & Politics (新闻与政治)~~: *对数据集构建意义不大*
14. ~~People (人物)~~: 真实人物扫描、解剖学模型
15. ~~Places & Travel (地点与旅行)~~: 场景级模型、地标
16. **Science & Technology (科学与技术)**: 显微镜、太空探索、医疗器械
17. **Sports & Fitness (运动与健身)**: 运动器材、球类
18. **Weapons & Military (武器与军事)**: 刀剑、枪械、坦克
19. ~~Gaming (游戏)~~: *跨类别标签，通常指 Low-poly 或 Asset pack*

### 1.2 特点评价

| 优点 | 局限 |
|:---|:---|
| 分类"生活化"，覆盖日常方方面面 | 类别粒度较粗 |
| 社区驱动，持续更新 | 不适合学术研究场景 |
| 适合面向大众的编辑指令 | 层级结构较浅 |

---

## 2. TurboSquid 分类体系

**核心逻辑：功能导向 + 工业标准 (Function-First & Industry Standard)**

主要服务于游戏开发、电影特效和建筑可视化。分类非常**层级化**，区分到"零件"级别。

### 2.1 顶层分类与关键子类

1.  **Cars (汽车)** *（独立大类）*
    - 细分：按品牌（BMW, Audi）、按类型（Sedan, Truck, Race Car）
    
2.  **Vehicles (载具)** *（非汽车类）*
    - Aircraft (飞机), Watercraft (船), Space (航天), Military (军事载具)
    
3.  **Characters (角色)**
    - 细分：Man, Woman, Child, Robot, Monster, Clothing (角色服装)
    
4.  **Architecture (建筑)**
    - 细分：Building (整楼), House (住宅), Stadium (体育场), Interior (室内), Street Elements (街头设施)
    
5.  **Furniture (家具)**
    - 细分：Table, Chair, Sofa, Cabinet, Lighting, Office Furniture
    
6.  **Anatomy (解剖)** *（特色分类）*
    - 细分：Skeleton (骨骼), Internal Organs (内脏), Skin (皮肤) *(适合医学或科研类数据)*
    
7.  **Landscape (景观)**
    - 细分：Plants (植物), Trees (树), Rocks (石头), Terrain (地形)
    
8.  **Technology (科技)**
    - 细分：Computer, Phone, Electronics, Industrial (工业机器)
    
9.  **Food (食物)**
    - 细分：Fruit, Vegetable, Drink, Dish (菜肴)

### 2.2 特点评价

| 优点 | 局限 |
|:---|:---|
| 对**刚性物体 (Hard Surface)** 分类极其详尽 | 版权受限，不能直接用于开源数据集 |
| 适合"部件编辑"任务 | 自然物/有机体分类有限 |
| 工业标准，易于对接商业工作流 | 需付费获取高质量资产 |

---

## 3. ShapeNet 分类体系

**核心逻辑：基于 WordNet 语义本体**

### 3.1 层级结构示例

```
entity
 ├── artifact
 │    ├── vehicle
 │    │    ├── car
 │    │    └── bicycle
 │    ├── furniture
 │    │    ├── chair
 │    │    └── table
 │    └── container
 │         └── suitcase
 └── living_thing
      ├── animal
      └── plant
```

### 3.2 核心类别 (ShapeNetCore v2 - 55 类)

```
airplane, bag, basket, bathtub, bed, bench, birdhouse, bookshelf, bottle, bowl
bus, cabinet, camera, can, cap, car, cellphone, chair, clock, dishwasher
display, earphone, faucet, file, guitar, helmet, jar, keyboard, knife, lamp
laptop, mailbox, microphone, microwave, motorcycle, mug, piano, pillow, pistol
pot, printer, remote, rifle, rocket, skateboard, sofa, speaker, table, telephone
tower, train, vessel, washer
```

### 3.3 偏重与缺失分析

#### ✅ 强覆盖类别 (ShapeNet 擅长)

| 类别 | 说明 |
|:---|:---|
| **家具** | chair / table / sofa / bed / cabinet / bookshelf，子类多、数量大 |
| **交通工具** | car / airplane / bus / train / motorcycle，多为刚体、拓扑清晰 |
| **规则化工业物体** | bottle / mug / bowl / lamp / trash can，拓扑干净、CAD 风格明显 |
| **单体刚体工具** | hammer / wrench / knife，简单机械结构 (但细分不够) |

#### ❌ 明显缺失或极弱的类别

| 类别 | 说明 |
|:---|:---|
| **自然物/非人造物** | 岩石、树木、植物、山体、云水火烟 (数据来源是 CAD/设计模型) |
| **非刚体/有机形变物体** | 布料、绳索、电线、食物、液体/柔体 |
| **场景级/组合级物体** | 房间、场景布置、室内/室外整体结构 (仅 object-level) |
| **现代日用品款式** | 手机型号、笔记本、耳机等消费电子的真实款式 (无品牌/款式语义) |
| **装饰性/非功能性资产** | 装饰摆件、艺术雕塑、饰品首饰、玩具公仔 |

### 3.4 判断规则

**ShapeNet 大概率没有：**
- 非刚体、自然形态、强风格/款式、强品牌/现实感、摆设/装饰/玩具/场景

**ShapeNet 大概率覆盖：**
- 刚体、工业设计风格、单体对象、可抽象成 CAD

### 3.5 结论

> **ShapeNet 不是"现实世界物体全集"，而是"规则化 CAD 对象子集"。**
> 
> 非常适合 **几何学习 / 分类 / 重建 benchmark**，
> 但不适合 **真实世界多样性 / 编辑 / 生成导向的 taxonomy**。
>
> **建议：不要把 ShapeNet 当"主 taxonomy"，只能当"几何稳定子集"。**
> 应该被 **Objaverse-LVIS / 商用库 taxonomy 覆盖或补充**。

---

## 4. Objaverse-LVIS 分类体系

**核心特点：基于 LVIS (Large Vocabulary Instance Segmentation) 词表**

### 4.1 概述

- **总类别数**: 1156 类
- **数据来源**: Objaverse 3D 模型集合
- **覆盖范围**: 日常物品、动物、交通工具、食物、工具等

### 4.2 优势

| 优点 | 说明 |
|:---|:---|
| 词汇量大 | 1156 类远超 ShapeNet 55 类 |
| 覆盖广 | 包含 ShapeNet 缺失的食物、动物、日用品 |
| 开源可用 | 可直接用于研究 |

### 4.3 局限

| 局限 | 说明 |
|:---|:---|
| 缺乏层级结构 | 扁平列表，无父子类别关系 |
| 质量参差 | 部分模型质量较低 |
| 需要聚类整理 | 需自行建立分类层级 |

---

## 5. 综合对比

| 分类体系 | 类别数 | 层级深度 | 刚体覆盖 | 自然物 | 场景级 | 数据可用性 |
|:---|:--:|:--:|:--:|:--:|:--:|:--:|
| Sketchfab | 19 大类 | 2-3 层 | ✅ | ✅ | ✅ | 需爬取 |
| TurboSquid | 50+ 大类 | 4-5 层 | ✅✅ | ⚠️ | ⚠️ | 付费 |
| ShapeNet | 55 类 | 基于 WordNet | ✅✅ | ❌ | ❌ | 开源 |
| Objaverse-LVIS | 1156 类 | 扁平 | ✅ | ✅ | ⚠️ | 开源 |

---

## 6. 推荐的超类划分 (12 类基准)

基于以上调研，建议采用以下 12 类作为超类划分：

| # | 超类名称 | 包含内容 | 适合的编辑任务 |
|:--:|:---|:---|:---|
| 1 | Characters & Creatures | 人、怪物、机器人 | Pose 编辑、换装 |
| 2 | Animals | 宠物、野生动物 | 毛发/纹理编辑 |
| 3 | Vehicles | 汽车、飞机、船 | 部件替换 |
| 4 | Architecture & Places | 房屋、桥梁 | 风格变换 |
| 5 | Furniture & Home | 家具、装饰 | 材质替换 |
| 6 | Electronics | 相机、电脑 | 硬表面细节 |
| 7 | Weapons & Military | 刀剑、坦克 | 旧化/战损效果 |
| 8 | Fashion & Clothing | 鞋、包、衣服 | 材质/款式变换 |
| 9 | Food | 食物、水果 | 纹理编辑 |
| 10 | Nature & Plants | 树、花、石头 | 自然形态 |
| 11 | Tools & Instruments | 乐器、工具 | 功能部件 |
| 12 | Arts & Cultural | 雕塑、文物 | 复杂几何/表面磨损 |
