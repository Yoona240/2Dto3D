# Infinigen UV 环境使用指南

## 📍 环境信息

| 项目 | 值 |
|------|-----|
| **环境位置** | `/home/xiaoliang/local_envs/2d3d` |
| **Python 版本** | 3.11.14 |
| **infinigen 版本** | 1.19.0 (可编辑模式) |
| **包管理器** | uv (位于 `~/.local/bin/uv`) |

---

## 🚀 快速开始

### 方式一：激活环境（推荐用于交互式开发）

```bash
# 激活环境
source /home/xiaoliang/local_envs/2d3d/bin/activate

# 现在可以直接使用 python
python your_script.py

# 退出环境
deactivate
```

### 方式二：直接指定 Python 路径

```bash
# 不需要激活，直接运行
/home/xiaoliang/local_envs/2d3d/bin/python your_script.py
```

### 方式三：使用 uv run

```bash
# uv 会自动使用正确的环境
cd /seaweedfs/xiaoliang/code/infinigen
uv run python your_script.py
```

---

## 📦 包管理常用命令

### 安装包

```bash
# 安装单个包
uv pip install package_name --python /home/xiaoliang/local_envs/2d3d/bin/python

# 安装指定版本
uv pip install "package_name==1.2.3" --python /home/xiaoliang/local_envs/2d3d/bin/python

# 从 requirements.txt 安装
uv pip install -r requirements.txt --python /home/xiaoliang/local_envs/2d3d/bin/python

# 安装带可选依赖的包
uv pip install "package[extra1,extra2]" --python /home/xiaoliang/local_envs/2d3d/bin/python
```

### 卸载包

```bash
uv pip uninstall package_name --python /home/xiaoliang/local_envs/2d3d/bin/python
```

### 查看已安装的包

```bash
# 列出所有包
uv pip list --python /home/xiaoliang/local_envs/2d3d/bin/python

# 搜索特定包
uv pip list --python /home/xiaoliang/local_envs/2d3d/bin/python | grep package_name

# 查看包详情
uv pip show package_name --python /home/xiaoliang/local_envs/2d3d/bin/python
```

### 导出依赖

```bash
# 导出为 requirements.txt
uv pip freeze --python /home/xiaoliang/local_envs/2d3d/bin/python > requirements.txt
```

---

## 🔧 常用别名（可选）

在 `~/.bashrc` 中添加以下别名，简化命令：

```bash
# Infinigen 环境快捷方式
alias infinigen-activate='source /home/xiaoliang/local_envs/2d3d/bin/activate'
alias infinigen-python='/home/xiaoliang/local_envs/2d3d/bin/python'
alias infinigen-pip='uv pip --python /home/xiaoliang/local_envs/2d3d/bin/python'

# 使用示例:
# infinigen-activate     # 激活环境
# infinigen-python script.py  # 运行脚本
# infinigen-pip install xxx   # 安装包
```

添加后执行 `source ~/.bashrc` 生效。

---

## 🔄 uv vs pip vs conda 对比

| 特性 | uv | pip | conda |
|------|-----|-----|-------|
| 安装速度 | ⚡ 极快 (10-100x) | 慢 | 中等 |
| 依赖解析 | 快速准确 | 较慢 | 准确但慢 |
| 环境隔离 | venv | venv | conda env |
| 锁文件 | uv.lock | 无 | environment.yml |
| 二进制包 | 支持 | 支持 | conda 专用格式 |

---

## 📝 项目开发工作流

### 1. 日常开发

```bash
# 进入项目目录
cd /seaweedfs/xiaoliang/code/infinigen

# 激活环境
source /home/xiaoliang/local_envs/2d3d/bin/activate

# 运行测试
pytest tests/

# 运行脚本
python infinigen_examples/generate_nature.py
```

### 2. 添加新依赖

```bash
# 安装新包
uv pip install new_package --python /home/xiaoliang/local_envs/2d3d/bin/python

# 如果需要更新 pyproject.toml，手动添加到 dependencies 列表
```

### 3. 同步依赖（从 uv.lock）

```bash
cd /seaweedfs/xiaoliang/code/infinigen
uv sync --python /home/xiaoliang/local_envs/2d3d/bin/python
```

---

## ⚠️ 注意事项

### 1. 代理设置
如果需要从 PyPI 下载包，记得设置代理：
```bash
export http_proxy="http://127.0.0.1:38372"
export https_proxy="http://127.0.0.1:38372"
# 或使用别名
proxy_on
```

### 2. terrain 编译
当前使用最小安装模式，未编译 terrain。如需使用：
```bash
cd /seaweedfs/xiaoliang/code/infinigen
make terrain
```

### 3. 环境位置
- 环境在本地磁盘 `/home/xiaoliang/local_envs/2d3d`（快速）
- 代码在挂载盘 `/seaweedfs/xiaoliang/code/infinigen`
- 这种分离确保了环境加载速度快

### 4. 与 conda 共存
- 此 uv 环境与 conda 完全独立
- conda 已配置延迟加载，不会影响终端启动速度
- 需要用 conda 环境时，输入 `conda activate <env>` 即可

---

## 🐛 常见问题

### Q: 提示找不到包？
```bash
# 确认使用正确的 Python
which python  # 应该显示 /home/xiaoliang/local_envs/2d3d/bin/python

# 如果不对，重新激活环境
source /home/xiaoliang/local_envs/2d3d/bin/activate
```

### Q: 安装包时超时？
```bash
# 设置代理
export http_proxy="http://127.0.0.1:38372"
export https_proxy="http://127.0.0.1:38372"
```

### Q: 如何重建环境？
```bash
# 删除旧环境
rm -rf /home/xiaoliang/local_envs/2d3d

# 创建新环境
uv venv /home/xiaoliang/local_envs/2d3d --python 3.11

# 安装依赖
cd /seaweedfs/xiaoliang/code/infinigen
uv pip install -r pyproject.toml --python /home/xiaoliang/local_envs/2d3d/bin/python
```

### Q: 如何升级 uv？
```bash
uv self update
# 或
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 📚 更多资源

- [uv 官方文档](https://docs.astral.sh/uv/)
- [Infinigen 项目文档](./docs/)
- [环境使用总览](/home/xiaoliang/ENV_USAGE.md)

---

*创建时间: 2026-01-25*
*环境配置: uv 0.9.26 + Python 3.11.14*
