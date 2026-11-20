# Caliper · VCU 测量数据分析工具

读 INCA 录的 MDF（`.dat` / `.mf4`）或 CAN/LIN 的 `.blf`，用自己写的工况规则在长时程数据里筛出工况点，并把区间带和标记画在曲线上。纯本地、单文件、零网络调用的个人诊断工具。

不复刻 ETAS MDA 的广度——广度让给 MDA，深度押在**工况语义**上：规则、三态、可复现记录。

## 快速开始

双击 `start.bat` 即启动。命令行自检（不弹界面）：

```bat
start.bat --check
```

- 固定用 `D:\Tools\Python\python.exe`（3.9.6），**不靠 PATH**——PATH 里那个 3.13 没装依赖。
- 无需安装、无需联网、无 venv、无构建步骤。已装齐 asammdf（MDF）+ python-can / canmatrix（BLF）+ PySide6 / pyqtgraph / numpy。
- 启动失败会写 `out\startup_error.log` 并留窗，成功则删掉它。
- 测量文件默认在 `E:\Project\T1V_Test`，但 GUI 能打开任意路径的 `.dat` / `.blf`。

## 能做什么

- **两类数据源**：MDF3 `.dat` 经 asammdf；`.blf` 经 python-can 流式扫描 + canmatrix 解码，首次打开手选 DBC 集合，之后按报文 ID 自适应排每通道主 DBC，可在映射对话框人工覆盖。会话缓存住原始报文，勾选信号零重扫。
- **工况规则**：一条规则 = 信号 / 比较符 / 阈值；也支持跳变条件（信号从 A 变成 B 的那一采样点）、窗口条件（回看 N ms 内波动幅度 max-min 或变化次数超阈值）、复杂表达式、配置型规则。规则在界面里编辑，程序写盘（校验 → `.bak` 备份 → 原子替换 → 版本号 +1），不用手改 JSON。规则集按数据来源分在 `rules/DAT/`、`rules/BLF/`。
- **三态评估**：命中 / 未命中 / **未评估**。规则引用的信号在这份文件里没采属正常（这次没关注），不报错、不折叠成未命中。
- **图上标注**：每条子规则用自己的颜色画区间带与标记点，右栏三态分区、点条目跳转、可勾选显隐。两条标尺定位时刻，右栏给 C1/C2/Δ 读数；BLF 时基按首帧显示相对秒，避免 1.8e9 的 epoch 秒。
- **可复现记录**：导出记录 JSON 含数据哈希、规则集哈希、时点、原始值与三态，连跑两次字节一致。

## 架构红线

依赖单向，禁止反向 import：

```
GUI(caliper_gui) → 规则(caliper_rules) → 内核(caliper_core) → asammdf / python-can+canmatrix
```

三条不可破：

1. `not_evaluated` 不许折叠成 `no_hit`（"没采这个量" ≠ "工况没发生"）。
2. GUI 不直接读 MDF、rules 不直接调 asammdf——数据一律经 `Measurement` 契约。
3. 内核不硬编码信号名或阈值——阈值属于规则 `when`，信号名属于规则文件。

BLF 分支只换最底层解析器，`load_blf` 产出同一个 `Measurement`，规则层和 GUI 层零感知。

## 验收

全部程序化断言，不截屏、不录屏、不抓大屏。命令用 `D:\Tools\Python\python.exe`，注意设 `PYTHONIOENCODING=utf-8`：

```bat
python start.py --check              :: 环境自检
python caliper_core.py --self-test       :: 内核自检
python caliper_core.py --self-test-blf   :: BLF 内核自检
python caliper_rules.py --self-test      :: 规则引擎自检
python caliper_rules.py --self-test-window :: 窗口条件自检
python caliper_gui.py --verify-window   :: 窗口条件编辑器往返
python caliper_gui.py --verify-workflow  :: GUI 单项验收（另有 20+ 个 --verify-* 开关）
python caliper_gui.py --bad-transition noignore   :: 变异测试：证明断言能抓到坏实现
```

每个 `--verify-*` 大多配一个 `--bad-*` 变异开关，用来证明断言不是永远绿的废断言。完整命令表、实测项数与变异清单见 `HANDOFF.md` 第 5、6 节。

## 文档索引

| 文档 | 内容 |
|---|---|
| `HANDOFF.md` | 接手手册：环境、文件地图、三条红线、界面用法、验收命令表、变异清单、踩坑记录、BLF 数据硬事实 |
| `plan.md` | 规划与历史：对标、范围、架构契约、各阶段实测结论与教训（含 v29–v59 的变更记录） |

## 目录速览

- `start.bat` / `start.py`：启动与自检入口
- `inventory.py` / `blf_inventory.py`：**离线**通道清单生成器（运行时不 import）
- `caliper_core.py`：数据内核（MDF / BLF 解析、对齐、质量检查、时区）
- `caliper_rules.py`：规则引擎 + 规则文件读写校验
- `caliper_gui.py`：界面与主窗口（约 4000 行）
- `caliper_verify.py`：全部程序化验收（约 3900 行），`caliper_gui.py --verify*` 转发到此处
- `caliper_report.py`：规则集导出 / 可复现记录
- `caliper_templates.py`：领域模板验收工具（只验证，不是引擎）
- `verify_fixtures.py`：验收专用规则集夹具（不依赖 `rules/` 示例文件名）
- `description/`：`变量描述.xlsx` 与生成的 `变量描述.json`（界面只读 JSON）
- `realdata/`：真实测量数据（Creep02.dat / 行车中驾驶模式切换.dat / 能耗_L003.blf 等），验收与标注验证的数据源
- `xlsx_to_desc.py`：`description/变量描述.xlsx` → `description/变量描述.json`
- `rules/`：运行时规则集（`DAT/`、`BLF/` 子目录 + `templates/` 模板）
- `out/`：运行产物与验收证据，**不是源码资产**。注意 `channels.json` / `channels.csv` 是主组定位与模板校验的运行时依赖，勿删
