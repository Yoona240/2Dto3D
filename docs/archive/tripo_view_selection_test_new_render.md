# Tripo 视角选择测试报告


## 测试用例: `0255e658900d`

### 选择前（legacy）
![before](../test_output/tripo_view_selection/0255e658900d_views_only/selection_before_legacy.png)

### 选择后（entropy_edge + geometric remap）
![after](../test_output/tripo_view_selection/0255e658900d_views_only/selection_after_entropy_remap.png)

- 虚拟上方向 (virtual_up): `bottom`
- 对向成对校验: front-back=✅, left-right=✅

| Slot | New (geometric) | Rotation |
|---|---|---:|
| front | top | 0 |
| left | left | 270 |
| back | bottom | 180 |
| right | right | 90 |

### 信息密度评分

| View | Score |
|---|---:|
| top | 1.8327 |
| bottom | 1.5021 |
| right | 1.2612 |
| left | 1.2595 |
| back | 0.7115 |
| front | 0.6888 |

---

## 测试用例: `0216736eeccf`

### 选择前（legacy）
![before](../test_output/tripo_view_selection/0216736eeccf_views_only/selection_before_legacy.png)

### 选择后（entropy_edge + geometric remap）
![after](../test_output/tripo_view_selection/0216736eeccf_views_only/selection_after_entropy_remap.png)

- 虚拟上方向 (virtual_up): `left`

| Slot | New (geometric) | Rotation |
|---|---|---:|
| front | front | 270 |
| left | bottom | 270 |
| back | back | 90 |
| right | top | 270 |

### 信息密度评分

| View | Score |
|---|---:|
| front | 2.5134 |
| bottom | 2.4747 |
| back | 2.4130 |
| top | 1.8571 |
| right | 1.2071 |
| left | 1.1829 |

---

## 测试用例: `01ed987715e8`

### 选择前（legacy）
![before](../test_output/tripo_view_selection/01ed987715e8_views_only/selection_before_legacy.png)

### 选择后（entropy_edge + geometric remap）
![after](../test_output/tripo_view_selection/01ed987715e8_views_only/selection_after_entropy_remap.png)

- 虚拟上方向 (virtual_up): `top`

| Slot | New (geometric) | Rotation |
|---|---|---:|
| front | front | 0 |
| left | left | 0 |
| back | back | 0 |
| right | right | 0 |

### 信息密度评分

| View | Score |
|---|---:|
| front | 3.8137 |
| back | 3.4486 |
| left | 2.2714 |
| right | 2.2587 |
| bottom | 1.6867 |
| top | 1.5829 |

---

## 测试用例: `022bcc731701`

### 选择前（legacy）
![before](../test_output/tripo_view_selection/022bcc731701_views_only/selection_before_legacy.png)

### 选择后（entropy_edge + geometric remap）
![after](../test_output/tripo_view_selection/022bcc731701_views_only/selection_after_entropy_remap.png)

- 虚拟上方向 (virtual_up): `left`
- 对向成对校验: front-back=✅, left-right=✅

| Slot | New (geometric) | Rotation |
|---|---|---:|
| front | front | 270 |
| left | bottom | 270 |
| back | back | 90 |
| right | top | 270 |

### 信息密度评分

| View | Score |
|---|---:|
| back | 3.3110 |
| front | 3.2901 |
| top | 1.7602 |
| bottom | 1.3956 |
| right | 1.3164 |
| left | 1.2785 |

---


