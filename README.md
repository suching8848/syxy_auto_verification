# Campus Network Auto-Login

> **当前版本：v1.7.0** | 免费开源 | 仅供学习研究使用

校园网断线自动认证工具。定时检测网络状态，检测到 captive portal 后通过后台 HTTP 请求静默完成认证，**完全无感**——不断网、不弹窗、不影响使用。

## 两种典型用法

这个工具解决的是同一件事——**一直盯着网络，断了立刻自动重连**——只是有两种启动方式，按你的需要挑：

### 场景 A：临时用一次，想看着它干活

打开 `CampusNet.exe`，切到 **「手动守护」** 页，点 **「开始守护」**，然后就一直挂着：

- 页面顶部大字号显示当前状态（待命中 / 守护中 / 正在重连），配彩色圆点，一眼就知道通不通
- 断网瞬间自动重连，日志区用人话写清楚："检测到断网，开始自动重连…" → "自动重连成功，网络已恢复（断网时长 1m2s）"
- 点右上角 **✕** 会**缩到右下角托盘**继续守护，不会退出；托盘右键可"打开主窗口"或"退出"
- 想让它停，点 **「停止守护」**（或托盘右键退出）

> 场景 A 需要窗口，所以要用 `CampusNet.exe`（图形版）。
> 如果你更喜欢终端，`python auto_login.py` 的菜单模式是同一套逻辑。

### 场景 B：装一次，之后永远不用管

每天固定时刻校园网会要求重新认证，你把这个时刻填一次就行——**大多数人用这个**：

1. 打开 `CampusNet.exe`，切到 **「每天自动守护」** 页
2. 填好「每天 23:00 启动，守护 30 分钟后自动停止」
3. 点 **「开启每天自动守护」**，在弹出的 UAC 窗口点「是」

之后每天到点系统会自己拉起它，**全程无窗口、无托盘图标、不弹任何东西**——你甚至不知道它在跑。它持续探测网络，断网那一刻立即自动重连，跑满时长后自己退出，当天只在 `logs\` 里留一份日志。

同一页还有「测试一次静默运行」（跑 1 分钟，你亲眼确认它真的什么都不显示）和「取消每天自动守护」。

命令行等价写法：

```powershell
.\setup_task.ps1 -Silent -SilentAt 22:50 -RunMinutes 30
Unregister-ScheduledTask -TaskName CampusNetAutoLogin_Silent -Confirm:$false
```

> **为什么能做到"完全无提示"？** 因为 `CampusNet.exe` 是以无控制台子系统（`--windowed`）打包的，进程从出生就没有任何窗口可显示；而旧的 `auto_login.exe` 是 `--console` 版本，被计划任务拉起时 Windows 一定会先弹一个黑框再靠代码藏起来——那一瞬间是看得见的。这是两个 exe 的**本质区别**，静默场景必须用 `CampusNet.exe`。

### 界面长什么样

窗口分三个选项卡，**「每天自动守护」是默认页**，因为大部分人只用这个：

| 选项卡 | 内容 |
|---|---|
| 手动守护 | 状态圆点 + 开始/停止守护 + 运行日志（默认展开） |
| 每天自动守护 | 每天几点启动、守护多少分钟、「开启每天自动守护」按钮 |
| 设置 | 学号、密码、校园网认证地址、检测地址、默认打开哪个选项卡 |

配置和日志都在程序旁边自动生成：第一次运行 `CampusNet.exe` 会在同目录创建 `auto_login_config.json` 和 `logs\`，不需要手动准备任何文件。

### 两种 exe 怎么选

| 文件 | 界面 | 用途 |
|---|---|---|
| `CampusNet.exe` | 图形窗口 + 托盘 | 手动使用（场景 A）、静默守护（场景 B） |
| `auto_login.exe` | 终端 | 排障、`--auth` 单次测试、老部署方式 |

## 工作原理

```
启动 → 定时检测网络状态（GET baidu.com + 校验响应内容）
  ├── 响应含"baidu" → 网络正常 → 等待后继续检测
  └── 响应不含"baidu" → portal 劫持 → 连续 2 次确认 →
      提取 JS 跳转参数 → GET index.jsp 拿 Cookie → POST 学号密码 → 认证完成
```

**Captive portal 检测**：脚本不只检查网络是否可达，还会校验响应内容是否真的来自 baidu。如果校园网 portal 透明代理了 HTTP 流量（返回登录页冒充 baidu），脚本能从内容识别出来。

**认证方式**：支持三种模式：

| 模式 | 适用场景 | 原理 |
|---|---|---|
| `portal_post`（默认） | 需要 POST 用户名密码的校园网 | 提取 JS 跳转参数 → 后台 POST 登录 |
| `http` | portal 只需访问特定 URL 续期 | 后台 GET 请求 portal URL |
| `browser` | portal 必须交互才能登录 | 打开浏览器 + 模拟 Enter |

## 运行模式

| # | 模式 | 入口 | 说明 |
|---|------|------|------|
| 1 | **图形界面** | 双击 `CampusNet.exe` | 窗口 + 托盘，点「开始守护」后持续守护、断网自动重连（场景 A） |
| 2 | **静默守护** | `CampusNet.exe --silent` 或 `setup_task.ps1 -Silent` | 无窗口无托盘，到点自己探测、断网即重连、跑满时长自退（场景 B） |
| 3 | **交互终端** | `python auto_login.py` → 菜单 [1] | 持续检测 + 断网自动重连，终端可见 |
| 4 | **认证测试** | `--auth` 或菜单 [2] | 单次认证并验证联网，退出码 0 成功、1 失败 |
| 5 | **无感部署** | 菜单 [4] → `setup_task.ps1` | 每天定时触发，通过 Windows 计划任务后台静默运行 |
| 6 | **系统托盘** | `--tray` 或菜单 [5] | 隐藏终端窗口，通知区域显示图标；鼠标悬停查看状态，右键退出 |
| 7 | **开机自启** | `setup_task.ps1 -Boot` | 系统启动时自动运行，持续监控网络，断网即重连。Session 0 运行，无需用户登录 |

> **模式 2 说明**：静默守护由计划任务到点拉起，不再需要点任何按钮。`--silent --now` 可跳过等待、立刻开始探测（手动测试用）。


> **模式 2 说明**：认证测试和自动重连都不会主动登出。`portal_post` 必须收到明确的成功 JSON，并通过后续联网检测才算成功；已在线时可能缺少认证参数，建议在自然断网时测试。

## 文件说明

```
auto_login.py               # 主程序 / 引擎（CLI + 检测 + 认证）
gui_app.py                  # 图形界面入口（窗口 + 托盘，场景 A/B）
auto_login_config.json      # 配置文件（含密码，不提交 git）
auto_login_config.example.json  # 配置模板（可提交）
setup_task.ps1              # 一键部署到 Windows 计划任务（含 -Silent 静默模式）
test_auto_login.py          # 离线回归测试（python -m unittest test_auto_login）
build/CampusNet.spec        # 图形版打包配方（--windowed，无控制台）
build/auto_login.spec       # 终端版打包配方（--console）
assets/campusnet.ico        # 程序图标
logs/                       # 运行日志（按日期，自动清理 7 天前）
RELEASE_v*.md               # 各版本 Release 说明稿
LICENSE                     # MIT 协议
```

## 快速开始

### 1. 获取程序

**方式 A：下载 exe（推荐，无需安装 Python）**

从 [Releases](https://github.com/suching8848/syxy_auto_verification/releases) 下载 `auto_login_v1.7.0.zip`，解压到任意文件夹（**注意整个文件夹一起解压，`assets` 子目录是图标，不能少**）。

**方式 B：运行 Python 脚本**

```bash
git clone https://github.com/suching8848/syxy_auto_verification.git
cd syxy_auto_verification
```

### 2. 配置

**exe 用户**：双击 `CampusNet.exe`。第一次运行会在它旁边自动生成 `auto_login_config.json` 和 `logs\`，然后在界面「设置」页填好学号和密码、点保存即可。

**Python 用户**：将 `auto_login_config.example.json` 重命名为 `auto_login_config.json`，用记事本打开编辑：

```json
{
    "check_url": "http://www.baidu.com",
    "check_interval_ok": 5,
    "check_interval_fail": 2,
    "fail_threshold": 2,
    "request_timeout": 5,
    "auth_method": "portal_post",
    "run_duration_minutes": 60,
    "auth_cooldown_seconds": 30,
    "check_expected_body": "baidu",
    "portal_url": "http://10.10.200.102",
    "username": "你的学号",
    "password": "你的密码"
}
```

> `portal_url` 默认已填三亚学院认证地址，一般不需要改。如果换学校了才需要改。

### 3. 测试

```powershell
# exe 用户：双击后菜单选 [2]
# Python 用户：
python auto_login.py --auth
```

看到 `Auth test PASSED` 说明配置正确。

### 4. 实现无感（关弹窗 + 计划任务）

要实现**完全无感**（断网 → 自动认证 → 恢复，全程不弹任何窗口），需要做两件事：

**① 关闭 Windows 自动弹窗（必须）**

以管理员身份打开 PowerShell，运行：

```powershell
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet" -Name "EnableActiveProbing" -Value 0 -Type DWord
```

重启电脑生效。**恢复方法**：把 `-Value 0` 改成 `-Value 1` 再运行一次。

**② 部署计划任务**

**打开管理员 PowerShell**（三种方式任选一种）：

- 右键开始菜单 → **终端(管理员)** 或 **Windows PowerShell(管理员)**
- `Win + R` → 输入 `powershell` → `Ctrl + Shift + Enter`（以管理员运行）
- 在程序目录空白处 `Shift + 右键` → **在此处打开 PowerShell 窗口**，然后执行 `Start-Process powershell -Verb RunAs` 提升到管理员

**进入程序所在目录**（如果不是从目录打开的）：

```powershell
cd "你的程序目录"    # 例如 cd "C:\Users\xxx\Desktop\校园网认证"
```

然后运行部署脚本：

```powershell
.\setup_task.ps1
```

脚本做了以下事情：
- **自动选择运行方式**：优先使用 exe（通过 `powershell.exe Start-Process -WindowStyle Hidden` 启动），没有 exe 则用 Python（优先 `pythonw.exe` 无窗口）
- **注册计划任务**：任务名 `CampusNetAutoLogin`，按 `schedule_time` 触发（默认 18:50），运行 60 分钟后自动退出
- **任务配置**：`LogonType Interactive`（支持 browser 模式模拟按键）、`Hidden=$true`（不弹窗口）、2 小时执行时限、已有实例运行时忽略新实例

部署脚本读取配置中的 `schedule_time`（缺省 18:50）；修改配置后需要重新部署。也可显式覆盖：

```powershell
.\setup_task.ps1 -ScheduleTime "18:50"
```

修改运行时长编辑 `auto_login_config.json`：

```json
"run_duration_minutes": 60   // 0 = 无限运行
```

> **注意**：如果以 exe 方式运行，计划任务**完全无窗口**，不会弹出任何终端或浏览器。

### 5. 开机自启动

除了每天定时触发，你还可以让脚本在 Windows 开机时自动启动，持续监控网络，断网即自动重连。

**① 部署开机自启任务**

以管理员身份打开 PowerShell，进入程序目录，运行：

```powershell
.\setup_task.ps1 -Boot
```

脚本会创建 `CampusNetAutoLogin_Boot` 计划任务（与 `CampusNetAutoLogin` 独立）：

| 配置项 | 值 |
|---|---|
| 触发条件 | 系统启动时 |
| 运行身份 | SYSTEM (Session 0) |
| 运行时长 | 无限（`run_duration_minutes` 强制为 0）|
| 崩溃恢复 | 最多重试 3 次，间隔 1 分钟 |
| 窗口 | 完全隐藏 |

**② 验证**

不重启也能手动启动测试：

```powershell
Start-ScheduledTask -TaskName CampusNetAutoLogin_Boot
```

查看 `logs/` 目录确认脚本正常运行。

**③ 认证模式兼容性**

| 模式 | 开机自启兼容？| 说明 |
|---|---|---|
| portal_post | 兼容 | 纯 HTTP，Session 0 正常工作 |
| http | 兼容 | 纯 HTTP，Session 0 正常工作 |
| browser | 不兼容 | 需要用户会话打开浏览器，请改用 portal_post 或 http |

**④ 注意事项**

- 建议将程序放在系统可访问的位置（如 `C:\Program Files\校园网认证\`）
- 开机自启任务和每日定时任务是独立的，建议二选一
- 如需停止：`Stop-ScheduledTask -TaskName CampusNetAutoLogin_Boot`
- 如需删除：`Unregister-ScheduledTask -TaskName CampusNetAutoLogin_Boot -Confirm:$false`

### 6. 取消计划任务

```powershell
Unregister-ScheduledTask -TaskName CampusNetAutoLogin -Confirm:$false
```

## 终端菜单说明（v1.7.0 起不再随包发布）

> **`auto_login.exe`（终端版）从 v1.7.0 起不再打包发布**，普通用户请用上面的 `CampusNet.exe`。
> 下面这个菜单仍然存在，但只在你从源码运行时才看得到：
>
> ```bash
> python auto_login.py
> ```
>
> 需要终端版 exe 的话，可以用 `build/auto_login.spec` 自己构建，或用旧版 Release（≤ v1.6.1）。

从源码运行时显示交互菜单：

```
══════════════════════════════════════════════════════
  校园网自动认证工具 v1.5
  项目: https://github.com/suching8848/syxy_auto_verification
──────────────────────────────────────────────────────
  程序目录: C:\Users\xxx\Desktop\校园网认证
──────────────────────────────────────────────────────
  [1] 启动自动认证  — 后台检测 + 断网自动重连
  [2] 测试认证      — 发一次请求验证配置是否正确
  [3] 修改配置      — 学号 / 密码 / 认证地址
  [4] 无感部署指南  — 关弹窗 + 计划任务（实现完全无感）
  [5] 系统托盘模式  — 隐藏窗口 + 通知区域图标，后台运行
  [6] 使用帮助      — 完整说明和常见问题
  [q] 退出
──────────────────────────────────────────────────────
```

- **[1] 启动自动认证**：开始循环检测，窗口关闭即停止。每 5 秒检测一次，发现断网 2 秒内确认
- **[4] 无感部署指南**：手把手教你怎么关弹窗、设计划任务，菜单直接显示可复制的目录路径
- **[5] 系统托盘模式**：隐藏终端窗口，通知区域显示蓝色信息图标。鼠标悬停查看运行状态，右键菜单 Exit 退出。或直接 `python auto_login.py --tray`
- **[6] 使用帮助**：FAQ，涵盖配置文件、认证失败、闪退等常见问题

## 全部配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `auth_method` | `"portal_post"` | 认证模式：`"portal_post"` / `"http"` / `"browser"` |
| `check_url` | `http://www.baidu.com` | 检测网络用的地址（HTTP，HTTPS 无法被 portal 劫持） |
| `check_expected_body` | `"baidu"` | 响应内容必须包含的关键词，否则判定为 portal 劫持 |
| `check_interval_ok` | `5` | 网络正常时检测间隔（秒） |
| `check_interval_fail` | `2` | 断网/portal 模式下检测间隔（秒） |
| `fail_threshold` | `2` | 连续失败多少次后触发认证 |
| `request_timeout` | `5` | HTTP 请求超时（秒） |
| `run_duration_minutes` | `60` | 运行多久自动退出（分钟），`0` 为无限 |
| `auth_cooldown_seconds` | `30` | 认证重试基础间隔；连续失败按 30、60、120…秒退避，默认上限 300 秒 |

**portal_post 模式字段：**

| 字段 | 说明 |
|---|---|
| `portal_url` | 校园网认证服务器地址（默认三亚学院 `http://10.10.200.102`） |
| `username` | 校园网用户名 / 学号 |
| `password` | 校园网密码 |
| `schedule_time` | 计划任务触发时间（HH:mm），修改后重新运行部署脚本 |

**http / browser 模式字段：**

| 字段 | 默认值 | 说明 |
|---|---|---|
| `portal_url` | (必填) | http 模式填完整续期 URL；browser 模式填认证页面 |
| `browser_wait_seconds` | `3` | 仅 browser 模式：打开浏览器后等多久模拟 Enter |

## 认证流程详解

### portal_post（默认，推荐）

适用于需要提交用户名密码的校园网 portal（如三亚学院系统）。

1. GET `check_url` → portal 返回含 JS 跳转的页面（`location.href='index.jsp?...'`）
2. 正则提取 index.jsp 完整 URL（含 `wlanuserip`、`nasip`、`mac` 等连接参数）
3. GET index.jsp → 拿到 `JSESSIONID` cookie
4. POST `InterFace.do?method=login`，携带用户名、密码、queryString、cookie
5. 解析 JSON，确认 `result` 为 `success`，再检测联网（最多 3 次）确认恢复

全程后台 HTTP 请求，不弹浏览器。

### http — 后台 GET 续期

适用于访问 `success.jsp` 即可续期的 portal。后台 GET 请求 `portal_url`，带浏览器 UA。

### browser — 浏览器交互

适用于必须手动交互的 portal。打开浏览器访问 `portal_url`，等待后模拟 Enter 按键。计划任务需要 `LogonType Interactive`。

## 运行效果

### 手动运行（`python auto_login.py`）

交互模式每轮检测输出 STATUS 行：

```
[22:01:06] [START] Service started [interactive]
[22:01:06] [WARN] Check #1 failed: response missing 'baidu' (portal injected?)
[22:01:06] [STATUS] [WARN] check failed 1/2 | next in 1s | uptime 0s | checks #1
[22:01:07] [DOWN] Network DOWN (reason: response missing 'baidu'), starting auth
[22:01:07] [AUTH] Portal JS redirect: http://10.10.200.102/eportal/index.jsp?wlanuserip=...
[22:01:08] [AUTH] Auth OK [HTTP 200, 28ms] body: {"userIndex":"...","result":"success",...}
[22:01:13] [RECOVER] Network restored — outage: 6s, checks: 3, auth_attempts: 1
[22:01:13] [STATUS] [OK]  reachable | next in 1s | uptime 7s | checks #4
```

### 测试认证（`--auth`）

```powershell
python auto_login.py --auth
```

直接发一次认证请求并退出，不启动检测循环。用于验证配置。

### 后台运行（计划任务）

计划任务直接运行 `pythonw.exe`（无窗口） + `--background` 标志，完全静默。除关键事件（DOWN/AUTH/RECOVER/STOP）外，每 2 轮检测（约 10 秒）输出 STATUS 状态行，方便通过 `logs/` 目录日志文件监控运行状态。

## 分发给别人

打包成两个 exe，对方不需要装 Python：

```powershell
pip install pyinstaller

# 图形版（推荐给普通用户）：--windowed 无控制台，双击出窗口，也能用于静默守护
pyinstaller --clean --noconfirm build/CampusNet.spec      # → dist\CampusNet.exe

# 终端版（排障用）：--console，保留完整终端菜单
pyinstaller --onefile --console --name auto_login --specpath build auto_login.py
```

> **为什么必须用 spec 文件而不是一行命令？** `CampusNet.spec` 里写死了
> `console=False` 和图标路径。官方文档见 `docs/GUI_PLAN.md`。

分发给别人需要的文件（已打包在 `auto_login_v1.7.0.zip`）：

```
CampusNet.exe                   # 图形版主程序（双击即用，也支持 --silent）
assets\campusnet.ico            # 托盘/程序图标，必须和 exe 一起
使用说明.txt                     # 三句话说清怎么用
setup_task.ps1                  # 计划任务部署脚本（含 -Silent 静默模式）
auto_login_config.example.json  # 配置模板（程序也会自己生成）
RELEASE_v1.7.0.md               # 本版 Release 说明
```

对方解压后双击 `CampusNet.exe`：同目录会自动生成 `auto_login_config.json` 和 `logs\`，在「设置」页填好学号密码即可。

> 发布新版时，Release 说明稿统一放在仓库根目录（`RELEASE_v*.md`），
> 打包进 zip 后再上传到 GitHub Release，避免构建产物被清理后说明丢失。

## 附录：不兼容的 portal 怎么办

如果你的学校 portal 认证流程不同，需要从浏览器抓包确认 login API：

```powershell
# 用命令行启动 Chrome，强制所有窗口带 DevTools
Start-Process chrome -ArgumentList "--auto-open-devtools-for-tabs", "http://www.baidu.com"
```

1. DevTools → **Network** 标签，勾选 **Preserve log**
2. 在弹出的认证页输入账号密码登录
3. 找 Network 列表中 Method 为 **POST** 的请求
4. 点 **Payload** 标签，确认表单字段名和 API 路径
5. 如果字段名或路径与默认的不同，需要修改脚本 `do_auth_portal_post()` 中的 form_data

## 版本历史

### v1.7.0 (2026-09-13)

- **新增图形界面 `CampusNet.exe`**（`--windowed` 打包，无控制台）：三个选项卡 —— 「手动守护」「每天自动守护」「设置」，默认打开每天自动守护
- **「每天自动守护」可在界面里一键部署**：填好时刻和时长点按钮，弹出 UAC 后自动注册每日计划任务，全程不用碰命令行
- **日志翻译成人话**：底层 `[AUTH]/[RECOVER]/[DOWN]` 在界面上显示为「检测到断网，开始自动重连…」「自动重连成功，网络已恢复（断网时长 1m2s）」，原始记录收进「详细日志」
- **配置与日志就近生成**：程序旁边会自动创建 `auto_login_config.json` 和 `logs\`，配置存在程序旁边、双击可手改
- **新增 `--silent` 静默守护**：无窗口、无托盘、无任何提示，持续探测、断网即刻重连，跑满时长自退。`--silent --now --run-minutes 1` 可手动试跑
- **修复：exe 读不到配置**（重要）：冻结后的 exe 只在自身目录找配置，找不到就静默退回默认值（无账号、5s/60min），并把日志写进 `dist\logs\`。现改为固定在 exe 旁边查找与生成
- **修复：窗口显示后自动消失**：复用控制台版 `TrayApp` 时，其 `_hide_console()` 会用 `EnumWindows` 把进程内所有可见窗口一并隐藏，包括 Tk 主窗口
- **修复：退出时刷屏报错**：窗口销毁后 120ms 轮询定时器仍会操作已销毁控件（`TclError`）
- **修复：静默时刻算法**：手动启动时若当日窗口已过，会空等 24 小时，现立即开跑
- **修复：`setup_task.ps1` 参数**：新增 `-Silent` / `-SilentAt` / `-RunMinutes`
- 新增离线回归测试（29 个，全部 mock 网络），含静默调度算法、配置原子写、日志翻译层

### v1.6.1 (2026-09-13)

- **修复 portal 检测误判（重要）**：百度会把 `http://www.baidu.com` 301 到 `https://www.baidu.com/`，旧逻辑把这种正常的同站 HTTPS 升级当成 portal 劫持，导致网络正常时被判为断网；更严重的是认证后的 3 次联网复核也恒失败，**即使 portal 返回 success 也无法恢复到正常状态**
- **认证参数统一**：移除 `portal_host` 兼容回退，配置键统一为 `portal_url`，消除 `http` 模式下 `portal_url` 为空的隐患
- **断网诊断日志**：断网时额外记录对端是否 TCP 可达，用于区分"真离线"与"portal 透明代理"，便于排查
- **`setup_task.ps1` 错误处理**：`Register-ScheduledTask` 在权限不足时抛的是 CIM 非终止错误，旧脚本会打印**假的成功横幅**；现改为明确报错并校验注册结果
- **计划任务默认时间** 22:35 → 18:50
- 新增离线回归测试（15 个，全部 mock 网络）
- 已用真实断网演练验证：检测 → 认证 → 恢复闭环，单轮耗时 < 5 秒

### v1.6 (2026-05-16)

- 新增**开机自启动**模式：`setup_task.ps1 -Boot` 注册 `CampusNetAutoLogin_Boot` 计划任务
- 新增 `--boot` CLI 标志：强制无限运行 (`run_duration=0`)，Session 0 browser 模式兼容性警告
- 开机自启任务使用 `AtStartup` 触发器 + `SYSTEM/S4U` 身份，系统启动即运行，无需用户登录
- 崩溃自动重启：最多重试 3 次，间隔 1 分钟
- `show_seamless_guide()` 增加开机自启替代方案提示
- 文档：四种→五种运行模式，README 新增开机自启章节

### v1.5 (2026-05-14)

- 新增 `--background` 参数：计划任务传此标志，不依赖 `isatty()` 判断运行模式，彻底解决 pythonw.exe 无 stdout 崩溃问题
- 修复 `sys.stdout.isatty()` 在计划任务环境下 `sys.stdout` 为 None 导致模块级崩溃的 bug
- 后台模式新增定期 STATUS 日志：每 2 轮检测（约 10 秒）输出完整状态行，方便通过日志文件监控运行状态
- `setup_task.ps1`：Python 路径统一传完整路径 + `--background`，优先 pythonw.exe（无窗口），任务 `Hidden=$true` 实现完全静默
- 交互模式与后台模式分离：背景模式跳过所有 `print()`，避免无 stdout 崩溃；交互模式启动后才输出 DISCLAIMER 和菜单

### v1.4 (2026-05-14)

- 新增**系统托盘模式**（`--tray` / 菜单 [5]）：隐藏窗口，通知区域图标常驻
- `--auth` 新增 logout 回退：已在线时先调 portal 登出 API，再触发 captive portal 重定向拿真实参数
- `run_detection_loop` 支持 `stop_event` 和 `status_callback`，线程安全退出
- 优化认证流程：index.jsp 响应体解析、portal API 参数探测、本地 IP 回退
- 菜单新增 [5] 系统托盘模式，[6] 使用帮助（原 [5]）
- 整理文档为四种运行模式
- 计划任务无窗口启动改为 PowerShell `Start-Process -WindowStyle Hidden`（移除 `run_hidden.vbs`）

### v1.3 (2026-05-13)

- 默认 `auth_method` 改为 `portal_post`，默认三亚学院认证地址
- `portal_host` 统一为 `portal_url`，消除两个字段的混淆
- 新增计划任务无窗口启动（最初用 VBS，v1.4 改为 PowerShell Start-Process -WindowStyle Hidden）
- `setup_task.ps1` 自动检测 exe，通过 PowerShell `Start-Process -WindowStyle Hidden` 实现零窗口
- 菜单显示程序目录路径，无感部署指南可一键复制
- portal_post 模式检查返回 JSON 中的 `result:fail`，避免误报
- http 模式增加响应内容 fail/error 检测
- 默认参数优化：检测间隔 1s、重试 5s、运行 10min
- 配置向导去掉"如"前缀，示例值全部通用化
- 菜单新增 [4] 无感部署指南（含恢复弹窗命令）、[5] FAQ 帮助
- 启动认证前提示"窗口关闭即停止"

### v1.2 (2026-05-12)

- 新增交互式菜单系统：启动 / 测试 / 配置 / 指南，无需命令行
- 新增首次配置向导：自动检测未完成，引导填写学号密码
- 新增嵌入式使用指南：关弹窗、定时建议、配置文件说明
- 优化 exe 体验：配置未完成自动提示，菜单循环不闪退

### v1.1 (2026-05-12)

- 新增 `portal_post` 认证模式：JS 跳转提取 + 后台 POST 登录
- 新增 captive portal 透明代理检测：内容校验 + 正则提取
- 新增 `--auth` / `--version` 命令、`check_expected_body` 配置
- 支持 PyInstaller 打包成独立 exe

### v1.0 (初始版本)

- `http` 和 `browser` 两种认证模式
- HEAD 请求网络检测、Windows 计划任务部署
- 交互 / 后台双模式、按日期日志 + 7 天清理

## 免责声明

- 本工具**仅供学习研究使用**，请勿用于非法用途
- **免费开源**，如通过付费渠道获得，请立即退款并举报
- 使用者自行承担因使用本工具产生的一切后果
- 项目地址：https://github.com/suching8848/syxy_auto_verification

## 依赖

- Python 3（仅标准库，无需 pip 安装）
- Windows 10/11
- （exe 版本无需任何依赖）

## 维护与验证

托盘常驻（`--tray` / 菜单 [5]）无限监控；其他托盘运行在检测结束时自动关闭图标。认证失败也受冷却限制，首次检测失败即切换到快速确认间隔。计划任务的 EXE 启动器会等待程序结束并传递退出码，Python 路径支持空格。

在 Windows 上运行离线回归测试（不连接校园网）：

```powershell
python -m unittest -v test_auto_login
```

修改源码后需重新打包 EXE，`dist/` 中原有发布文件不会自动更新。
