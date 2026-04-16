# 物体分类与筛选说明文档

本文档记录了 `data/captions` 目录下物体数据集的筛选规则与分类逻辑。

## 1. 数据来源与处理
- **原始文件**: `objects.json`
- **处理后文件**: `categorized_objects.json`

## 2. 筛选规则 (Filtering)
在原始数据的基础上，我们进行了手动清洗，主要移除了以下类型的对象：
- **生物体**: 所有的动物（如猫、狗、鸟类等）。
- **人体相关**: 人物（person, man, woman）及人体组织/器官（如 heart, brain 等）。
- **过于单一或不适用的生物名词**。

目标是保留适合进行 **3D 静态物体生成** 的物品列表。

## 3. 分类体系 (Categorization)
我们将剩余的非生物物体整理为了 18 个主要类别。分类采用了由粗到细的策略，并针对易混淆项（如“灯具”与“家具”，“工具”与“硬件”）进行了细化拆分。

### 类别列表
1.  **Furniture (家具)**: 床、桌、椅、柜子、地毯等。
2.  **Lighting & Ceiling (照明与天花板)**: 灯具、吊扇、路灯等。
3.  **Kitchen & Dining (厨房与餐饮)**: 厨具、餐具、厨房电器。
4.  **Food & Beverage (食品与饮料)**: 各类食材、水果、零食、饮料。
5.  **Bathroom & Personal Care (卫浴与个护)**: 牙刷、毛巾、马桶、浴缸、化妆品。
6.  **Medical & Health (医疗健康)**: 药品、急救包、轮椅、拐杖。
7.  **Vehicles (交通工具)**: 汽车、飞机、船只、自行车及其部件。
8.  **Electronics & Appliances (电子与家电)**: 电脑、相机、电话、电视、家用电器。
9.  **Clothing & Accessories (服装与配饰)**: 衣服、鞋帽、背包、珠宝首饰。
10. **Sports, Hobbies & Recreation (运动与娱乐)**: 球类、棋牌、玩具、乐高、甚至吉祥物。
11. **Musical Instruments (乐器)**: 钢琴、吉他、鼓等。
12. **Tools, Construction & Garden (工具与园艺)**: 锤子、锯子、铲子、园艺工具。
13. **Hardware & Components (硬件与零件)**: 螺丝、开关、锁、齿轮、把手。
14. **Office & Stationery (办公与文具)**: 笔、纸、笔记本、订书机、钱币。
15. **Weapons & Military (武器与军事)**: 枪械、刀剑、坦克。
16. **Outdoor, Urban & Structures (户外与建筑)**: 亭子、路标、消防栓、建筑构件。
17. **Decor, Art & Religion (装饰、艺术与宗教)**: 画作、雕塑、花瓶、节日装饰。
18. **Containers & General Items (容器与通用物品)**: 箱子、瓶子、桶、袋子。

### 特殊类别
- **Misc (杂项)**: 包含无法归入上述主要类别的极少数特殊物品（极少量）。

## 4. 文件结构
输出的 `categorized_objects_v2.json` 格式如下：

```json
{
  "Category Name": [
    "object_name_1",
    "object_name_2",
    ...
  ],
  ...
}
```
