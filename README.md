# OKX Trader GUI

基于 Python、PySide6 和 [python-okx](https://github.com/okxapi/python-okx) 的分阶段桌面交易软件。

## 当前功能

- 白色卡片合约监控界面，默认 BTC、ETH、SOL 的 USDT 永续合约。
- 输入完整合约代码添加（例如 `DOGE-USDT-SWAP`），支持大小写归一化、格式校验、重复提示、删除和单选。
- 点击「启动行情」订阅 OKX 公共 WebSocket `tickers`；右侧显示最新成交价和滚动 24 小时涨跌幅。
- 运行期间添加或删除合约会更新订阅；断线后自动重连，报价清空以避免显示过期价格。订阅失败会显示提示。
- 停止行情或关闭窗口会释放后台连接；没有合约时自动停止行情。
- 预留当前选中合约和阶梯式语音播报设置区域，尚未实现语音功能。

启动时不自动连接网络，不需要 API 密钥。当前仅接入公共行情，不包含下单功能。合约格式在本地校验，合约是否存在由 OKX 订阅结果确认。

## 环境准备

推荐 Python 3.12；虚拟环境位于 `.venv`。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

当前机器可用的 Python 路径：`/home/pei/miniconda3/envs/netron-dev/bin/python`。

复现验收依赖版本：

```bash
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
```

## 启动

```bash
bash run_gui.sh
```

Linux Qt 桌面需要 `libxcb-cursor0`，可用 `sudo apt-get install libxcb-cursor0` 安装。本机因 sudo 需要密码，将 Ubuntu 软件包解包到了 `.local/qt`，启动脚本会加载该路径，不修改系统。重建本机本地库可执行：

```bash
mkdir -p .local/qt .local/packages
cd .local/packages
apt-get download libxcb-cursor0
cd ../..
dpkg-deb -x .local/packages/libxcb-cursor0_*.deb .local/qt
```

系统库完整时也可运行 `.venv/bin/python -m okx_gui`，或激活环境后运行 `okx-gui`。

## 测试

```bash
.venv/bin/python -m pytest
```

测试默认使用 Qt offscreen 平台，不依赖桌面或 OKX 网络。覆盖添加/校验/删除/单选、动态订阅、报价更新、停止及关闭、行情消息解析和 SDK 导入。

## 结构

```text
src/okx_gui/app.py       GUI 与合约交互
src/okx_gui/market.py    python-okx 公共 WebSocket 后台线程、重连与行情解析
tests/                  离线测试
run_gui.sh              本机桌面启动脚本
pyproject.toml          工程依赖和 pytest 配置
requirements-lock.txt   验收依赖快照
```

行情地址为 `wss://ws.okx.com/ws/v5/public`（默认 443 端口），频道及字段参照 [OKX API 文档](https://www.okx.com/docs-v5/en/)。24h 涨跌幅为 `(last / open24h - 1) × 100%`。若所在网络无法连接该公共服务，界面会显示重试状态。
