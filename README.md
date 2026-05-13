# Campus Network Auto-Login

> **当前版本：v1.4** | 免费开源 | 仅供学习研究使用

校园网断线自动认证工具。定时检测网络状态，检测到 captive portal 后通过后台 HTTP 请求静默完成认证，**完全无感**——不断网、不弹窗、不影响使用。

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

## 四种运行模式

| # | 模式 | 入口 | 说明 |
|---|------|------|------|
| 1 | **交互终端** | `python auto_login.py` → 菜单 [1] | 持续检测 + 断网自动重连，终端可见 |
| 2 | **认证测试** | `--auth` 或菜单 [2] | 单次认证验证（已在线时可能不可靠 — portal logout API 不稳定） |
| 3 | **无感部署** | 菜单 [4] → `setup_task.ps1` | 每天定时触发，通过 Windows 计划任务后台静默运行 |
| 4 | **系统托盘** | `--tray` 或菜单 [5] | 隐藏终端窗口，通知区域显示图标；鼠标悬停查看状态，右键退出 |

> **模式 2 说明**：`--auth` 在已登录状态下会先尝试调 portal 登出 API 再重认证。但部分 portal 的登出 API 不可靠（返回成功但实际未下线），此时可能报 FAILED。**最准确的测试方式是在断网时运行。**

## 文件说明

```
auto_login.py               # 主程序
auto_login_config.json      # 配置文件（含密码，不提交 git）
auto_login_config.example.json  # 配置模板（可提交）
setup_task.ps1              # 一键部署到 Windows 计划任务
logs/                       # 运行日志（按日期，自动清理 7 天前）
```

## 快速开始

### 1. 获取程序

**方式 A：下载 exe（推荐，无需安装 Python）**

从 [Releases](https://github.com/suching8848/syxy_auto_verification/releases) 下载 `auto_login_v1.4.zip`，解压到任意文件夹。

**方式 B：运行 Python 脚本**

```bash
git clone https://github.com/suching8848/syxy_auto_verification.git
cd syxy_auto_verification
```

### 2. 配置

**exe 用户**：双击 `auto_login.exe`，程序会自动检测配置缺失并弹出配置向导，按提示输入学号和密码即可。

**Python 用户**：将 `auto_login_config.example.json` 重命名为 `auto_login_config.json`，用记事本打开编辑：

```json
{
    "check_url": "http://www.baidu.com",
    "check_interval_ok": 5,
    "check_interval_fail": 2,
    "fail_threshold": 2,
    "request_timeout": 5,
    "auth_method": "portal_post",
    "run_duration_minutes": 10,
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

以管理员身份打开 PowerShell，进入程序所在目录，运行：

```powershell
.\setup_task.ps1
```

每天 17:55 自动启动，运行 10 分钟后自动退出。修改时间编辑 `setup_task.ps1` 第 85 行：

```powershell
$trigger = New-ScheduledTaskTrigger -Daily -At "你的时间"
```

exe 用户通过 `powershell.exe Start-Process -WindowStyle Hidden` 启动，计划任务**完全无窗口**。

### 5. 取消计划任务

```powershell
Unregister-ScheduledTask -TaskName CampusNetAutoLogin -Confirm:$false
```

## exe 菜单说明

双击 `auto_login.exe` 后显示交互菜单：

```
══════════════════════════════════════════════════════
  校园网自动认证工具 v1.4
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
| `run_duration_minutes` | `10` | 运行多久自动退出，`0` 为无限 |
| `auth_cooldown_seconds` | `30` | 两次认证的最小间隔，防止频繁认证 |

**portal_post 模式字段：**

| 字段 | 说明 |
|---|---|
| `portal_url` | 校园网认证服务器地址（默认三亚学院 `http://10.10.200.102`） |
| `username` | 校园网用户名 / 学号 |
| `password` | 校园网密码 |
| `schedule_time` | 计划任务触发时间（24h 制，默认 `17:55`） |

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
5. Portal 返回 `result:success` → 认证完成

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

通过计划任务 + PowerShell `Start-Process -WindowStyle Hidden` 启动，无任何窗口。只记录关键事件（DOWN/AUTH/RECOVER/STOP），不输出 STATUS 行。日志在 `logs/` 目录。

## 分发给别人

打包成单个 exe，对方不需要装 Python：

```powershell
pip install pyinstaller
pyinstaller --onefile --console --name auto_login auto_login.py
```

分发给别人需要的文件（已打包在 `auto_login_v1.4.zip`）：

```
auto_login.exe              # 主程序
auto_login_config.example.json  # 配置模板
setup_task.ps1              # 计划任务部署脚本

对方解压后双击 exe 即可，配置向导会引导完成设置。

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
