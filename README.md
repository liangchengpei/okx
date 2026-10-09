# OKX Trader GUI

基于 Python、PySide6 和 [python-okx](https://github.com/okxapi/python-okx) 的分阶段桌面交易软件。

## 阶段一

本阶段提供空白主窗口、可安装的工程结构和 pytest 测试。已准备 REST SDK 和 WebSocket 依赖；启动与测试均不需要 API 密钥，也不会连接 OKX 或下单。

## 环境准备

推荐 Python 3.12。项目虚拟环境位于 `.venv`，不影响其他 Conda 环境。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

在当前机器重建环境时，可使用现有 Python：

```bash
/home/pei/miniconda3/envs/netron-dev/bin/python -m venv .venv
```

如需复现验收时的依赖版本：

```bash
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
```

## 启动空白 GUI

在项目目录运行：

```bash
bash run_gui.sh
```

启动脚本会加载项目内的 Qt 系统库（如有）。关闭窗口即可退出。

Linux 桌面需要 `libxcb-cursor0`，通常可用 `sudo apt-get install libxcb-cursor0` 安装。本机因 sudo 需要密码，改为将 Ubuntu 软件包解包到 `.local/qt`，由启动脚本设置库路径，不修改系统。若系统库已安装，也可直接运行 `.venv/bin/python -m okx_gui` 或激活环境后运行 `okx-gui`。

## 运行测试

```bash
.venv/bin/python -m pytest
```

测试默认使用 Qt offscreen 平台，不依赖桌面；也可显式运行：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
```

测试覆盖窗口显示与关闭、应用事件循环及 OKX REST/WebSocket 模块导入。

## 目录

```text
src/okx_gui/       应用入口和空白主窗口
tests/            GUI 与依赖冒烟测试
pyproject.toml    安装、依赖与 pytest 配置
requirements-lock.txt  验收环境的依赖版本快照
```

后续交易、行情、账户和异步网络功能按阶段添加。
