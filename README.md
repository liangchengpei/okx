# OKX Trader GUI

基于 Python、PySide6 和 [python-okx](https://github.com/okxapi/python-okx) 的分阶段桌面交易软件。

## 当前功能

- 白色卡片合约监控界面，默认 BTC、ETH、SOL 的 USDT 永续合约。
- 输入完整合约代码添加（例如 `DOGE-USDT-SWAP`），支持大小写归一化、格式校验、重复提示和删除。
- 点击「启动行情」订阅 OKX 公共 WebSocket `tickers`；右侧显示最新成交价和滚动 24 小时涨跌幅。
- 运行期间添加或删除合约会更新订阅；断线后自动重连，报价清空以避免显示过期价格。订阅失败会显示提示。
- 停止行情或关闭窗口会释放后台连接；没有合约时自动停止行情。
- 每个合约独立设置静音价格范围和价格间隔，支持开始/停止阶梯式语音播报。开始播报会自动启动行情。

启动时不自动连接网络，不需要 API 密钥。当前仅接入公共行情，不包含下单功能。合约格式在本地校验，合约是否存在由 OKX 订阅结果确认。

## 范围外阶梯式语音播报

每个合约右侧依次为「低价格」「高价格」「价格间隔」和「开始播报」。先启动行情并等待实时价格，再填写设置并开始播报。

- 两个价格均必填，必须为大于 0 的有效数字，左侧低价格必须严格小于右侧高价格；价格间隔也必须大于 0。
- 开始时现价必须位于 `[低价格, 高价格]` 内；没有有效报价、现价不在范围内或价格设置不合法时，弹出对话框说明原因，不启动播报。
- 范围内及恰好处于边界均静音。首次跌破低价格或突破高价格时，播报合约名称和当前价格。
- 在范围外按价格间隔继续双向阶梯播报，阶梯以对应边界为基准。跳过多个档位时只播报一次最新价格。
- 回到范围内清理该合约尚未播放的语音，当前语音播完后恢复静音并重置阶梯；再次越界重新开始。直接从区间上方跳到下方也会按新的一侧播报。
- 各合约独立启停，运行时锁定输入；停止后可以修改设置。删除合约、停止行情或关闭窗口会清理对应播报。

例如低价格 `82400`、高价格 `82800`、间隔 `100`：现价 `82600` 时可以启动；`82400` 到 `82800`（含两端）内静音，涨到 `82801` 时首次播报，继续到 `82900` 时播报；回到 `82800` 静音。随后跌到 `82399` 时重新播报，继续跌到 `82300` 时播报。

### 情况紧急

每个合约行右侧独立提供「情况紧急」开关，默认关闭。打开后先保持安静，只有该合约发生价格报警才会启动它的紧急循环；其他合约报警不会触发它。

所有合约共用串行播放器，价格播报优先，在没有待播价格的空档播报「情况紧急」。多个合约同时启动紧急循环时轮流播放，声音不会重叠，紧急提示不占价格待播队列容量。

关闭某个合约的开关只结束该合约的紧急循环，不影响其他合约。删除合约会清理它的紧急配置；关闭窗口或语音出错会结束所有紧急循环。价格回到区间或停止行情后，已触发的紧急循环继续，直到关闭对应开关。重新打开后需等待该合约新的价格报警。「测试语音」不会启动紧急循环，紧急提示使用全局音量。

Linux 优先使用 eSpeak NG 离线生成 WAV，再使用 `paplay` 通过桌面音频系统播放，合成和播放均由异步 QProcess 执行，不阻塞 GUI。该路径已经通过用户实际试听确认，避免 speech-dispatcher 模块崩溃后仍显示播放状态却没有声音的问题。所有合约共用一个串行语音队列，当前播报完成才播放下一条，默认最多等待 5 条；队列满时丢弃最旧的待播内容，保留最新请求。回到范围内或行情重连只清理待播内容，当前语音继续播完；手动停止、删除合约或关闭窗口会立即停止。支持取消以及子进程错误提示，不发送行情到外部语音服务。其他平台或缺少本地播放工具时回退到 Qt TextToSpeech。

「测试语音」左侧提供 0–100% 音量滑块，0 为静音，调整对下一条播报生效，测试和行情播报共用此设置。点击「测试语音」可以不启动行情直接试听 BTC 合约价格 82600.05（明确读出“点零五”）。此设置只调节应用播报，实际声音还受系统音量和输出设备影响。

Linux 推荐安装 `espeak-ng` 和 `pulseaudio-utils`：

```bash
sudo apt-get install espeak-ng pulseaudio-utils
```

本机的 eSpeak 可执行文件已放在 `.local/espeak/usr/bin/espeak-ng`，程序会自动识别（依赖系统已安装的 libespeak-ng 和 espeak-ng-data）。如需重建这个本地可执行文件：

```bash
mkdir -p .local/packages .local/espeak
cd .local/packages
apt-get download espeak-ng
cd ../..
dpkg-deb -x .local/packages/espeak-ng_*.deb .local/espeak
```

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

测试默认使用 Qt offscreen 平台，不依赖桌面或 OKX 网络。覆盖添加/校验/删除、动态订阅、报价更新、停止及关闭、行情消息解析、范围校验/越界/回区间静音/阶梯/跳价/小数精度、语音排队/取消和 SDK 导入。

## 结构

```text
src/okx_gui/app.py       GUI 与合约交互
src/okx_gui/market.py    python-okx 公共 WebSocket 后台线程、重连与行情解析
src/okx_gui/voice.py     阶梯触发规则和语音队列
src/okx_gui/audio.py     Linux 离线音频合成和桌面播放
tests/                  离线测试
run_gui.sh              本机桌面启动脚本
pyproject.toml          工程依赖和 pytest 配置
requirements-lock.txt   验收依赖快照
```

行情地址为 `wss://ws.okx.com/ws/v5/public`（默认 443 端口），频道及字段参照 [OKX API 文档](https://www.okx.com/docs-v5/en/)。24h 涨跌幅为 `(last / open24h - 1) × 100%`。若所在网络无法连接该公共服务，界面会显示重试状态。
