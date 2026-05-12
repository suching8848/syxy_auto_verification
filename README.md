# Campus Network Auto-Login

> **当前版本：v1.2** | 免费开源 | 仅供学习研究使用

校园网断线自动认证工具。定时检测网络状态，检测到 captive portal 后通过后台 HTTP 请求静默完成认证，无需打开浏览器。

## 工作原理

```
启动 → 每 N 秒检测网络状态（GET + 响应内容校验）
  ├── 网络正常 → 静默等待
  └── 检测到 portal 劫持 → 提取登录参数 → POST 认证 → 恢复联网
```

**Captive portal 检测**：脚本不只检查网络是否可达，还会校验响应内容是否真的来自目标网站。如果 portal 透明代理了 HTTP 流量（返回登录页冒充目标网站内容），脚本能识别出来。

**认证方式**：支持三种模式，按 portal 类型选择：

| 模式 | 适用场景 | 原理 |
|---|---|---|
| `portal_post` | 需要 POST 用户名密码的校园网 portal | GET 触发 JS 跳转 → 提取登录参数 → POST 认证 |
| `http` | portal 只需访问特定 URL 即可续期 | 后台 GET 请求 portal URL |
| `browser` | portal 必须交互才能登录 | 打开浏览器 + 模拟 Enter 按键 |

## 文件说明

```
auto_login.py          # 主程序
auto_login_config.json # 配置文件
setup_task.ps1         # 一键部署到 Windows 计划任务
logs/                  # 运行日志（按日期，自动清理 7 天前）
```

## 快速开始

### 1. 编辑配置

编辑 `auto_login_config.json`，填入你的校园网账号信息：

```json
{
    "check_url": "http://www.baidu.com",
    "check_interval_ok": 15,
    "check_interval_fail": 10,
    "fail_threshold": 2,
    "request_timeout": 5,
    "auth_method": "portal_post",
    "run_duration_minutes": 60,
    "auth_cooldown_seconds": 30,
    "check_expected_body": "baidu",
    "portal_host": "http://10.10.200.102",
    "username": "你的学号",
    "password": "你的密码"
}
```

> `portal_host` 通常在浏览器手动登录时地址栏里就能看到，一般是 `http://10.10.xxx.xxx` 这样的 IP。

### 2. 手动测试

```powershell
# 测试认证是否成功（跳过网络检测，直接发一次认证）
python auto_login.py --auth

# 完整运行（网络检测 + 断网自动认证）
python auto_login.py
```

按 `Ctrl+C` 停止。

### 3. 部署到计划任务

以管理员身份运行 PowerShell：

```powershell
.\setup_task.ps1
```

创建 `CampusNetAutoLogin` 任务，每天 17:55 自动启动，运行 60 分钟。

**调整触发时间：** 如果校园网在固定时间断网（比如每天 18:00），编辑 `setup_task.ps1` 第 55 行把时间改到断网前一两分钟：

```powershell
$trigger = New-ScheduledTaskTrigger -Daily -At "17:58"
```

重新运行 `.\setup_task.ps1` 即可。

### 4. 关闭 Windows 自动弹窗

Windows 会自己检测 captive portal 然后弹浏览器窗口。部署计划任务前需要关掉这个行为，否则每次断网都会弹窗：

以管理员身份运行 PowerShell：

```powershell
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet" -Name "EnableActiveProbing" -Value 0 -Type DWord
```

重启电脑或重新连接网络后生效。从此认证由脚本在后台静默完成，不会弹出任何窗口。

### 5. 移除

```powershell
Unregister-ScheduledTask -TaskName CampusNetAutoLogin -Confirm:$false
```

## 全部配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `auth_method` | `"http"` | 认证模式：`"http"` / `"browser"` / `"portal_post"` |
| `check_url` | `http://www.baidu.com` | 用于检测网络的地址（建议用 HTTP，HTTPS 无法被 portal 劫持检测） |
| `check_expected_body` | `"baidu"` | 响应内容必须包含此关键词，否则判定为 portal 劫持 |
| `check_interval_ok` | `30` | 网络正常时的检测间隔（秒） |
| `check_interval_fail` | `10` | 断网/portal 模式下的检测间隔（秒） |
| `fail_threshold` | `2` | 连续失败多少次后触发认证 |
| `request_timeout` | `5` | HTTP 请求超时（秒） |
| `run_duration_minutes` | `60` | 运行多久后自动退出，`0` 为无限 |
| `auth_cooldown_seconds` | `30` | 两次认证的最小间隔，防止频繁认证 |

**portal_post 模式额外字段：**

| 字段 | 说明 |
|---|---|
| `portal_host` | Portal 服务器地址，如 `http://10.10.200.102` |
| `username` | 校园网用户名 / 学号 |
| `password` | 校园网密码 |

**http 模式额外字段：**

| 字段 | 说明 |
|---|---|
| `portal_url` | 认证续期 URL（如 `success.jsp?userIndex=...`） |

**browser 模式额外字段：**

| 字段 | 默认值 | 说明 |
|---|---|---|
| `portal_url` | (必填) | 在浏览器中打开的认证页面 |
| `browser_wait_seconds` | `3` | 打开浏览器后等待多久再模拟 Enter |

## 三种认证模式详解

### portal_post — 后台 POST 登录（推荐）

适用于需要提交用户名密码的大多数校园网 portal。

**认证流程：**

1. GET `check_url`（如 baidu.com）→ portal 返回含 JS 跳转的页面
2. 正则提取 index.jsp 完整 URL（含 `wlanuserip`、`nasip`、`mac` 等连接参数）
3. GET index.jsp → 拿到 `JSESSIONID` cookie
4. POST `InterFace.do?method=login`，携带用户名、密码、连接参数、cookie
5. 认证完成，网络恢复

全程在后台通过 HTTP 请求完成，无浏览器窗口。

### http — 后台 GET 续期

适用于访问特定 URL 即可完成续期的 portal 系统（如 `success.jsp`）。

- 后台 GET 请求 `portal_url`
- 带浏览器 User-Agent，模拟真实浏览器
- 不需要提交用户名密码

**注意**：如果 portal 采用透明代理（不改变 URL）而非 HTTP 重定向，需要配置 `check_expected_body` 来检测 portal 劫持。

### browser — 浏览器交互

适用于 portal 必须手动交互（验证码、选择运营商等）的情况。

- 打开默认浏览器访问 `portal_url`
- 等待指定秒数后模拟 Enter 按键
- 计划任务必须使用 `LogonType Interactive`（`setup_task.ps1` 已默认配置）

## 运行效果

### 手动运行（交互模式）

`python auto_login.py` 自动识别为交互模式，每轮检测输出 STATUS 行：

```
[22:01:06] [START] Service started [interactive]
[22:01:06] [INFO] Config: auth=portal_post check=http://www.baidu.com interval=15s threshold=2 duration=60min
[22:01:06] [WARN] Check #1 failed: response missing 'baidu' (portal injected?)
[22:01:06] [STATUS] [WARN] check failed 1/2 | next in 15s | uptime 0s | checks #1
[22:01:21] [DOWN] Network DOWN (reason: response missing 'baidu' (portal injected?)), starting auth
[22:01:21] [AUTH] Portal JS redirect: http://10.10.200.102/eportal/index.jsp?wlanuserip=...
[22:01:22] [AUTH] Auth OK [HTTP 200, 28ms] body: {"userIndex":"...","result":"success",...}
[22:01:22] [STATUS] [DOWN] outage 1s | auth #2 | next in 10s | uptime 16s | checks #2
[22:01:32] [RECOVER] Network restored — outage: 16s, checks: 3, auth_attempts: 1
[22:01:32] [STATUS] [OK]  reachable | next in 15s | uptime 26s | checks #3
```

### 测试认证（--auth 模式）

```powershell
python auto_login.py --auth
```

直接发一次认证请求并退出，不启动网络检测循环。用于验证配置是否正确。

### 后台运行（计划任务）

通过 `pythonw.exe` + 计划任务运行时，自动识别为后台模式，只记录关键事件，不输出 STATUS 行。

## 附录：抓取 portal 的 login API（换学校 / portal 不兼容时）

如果你的 portal 地址不同，或者默认的 `InterFace.do?method=login` 流程不适用，需要从浏览器抓包确认 login API：

**用命令行启动 Chrome/Edge（强制所有窗口带 DevTools）：**

```powershell
Start-Process chrome -ArgumentList "--auto-open-devtools-for-tabs", "http://www.baidu.com"
# 或
Start-Process msedge -ArgumentList "--auto-open-devtools-for-tabs", "http://www.baidu.com"
```

1. DevTools → **Network** 标签，勾选 **Preserve log**
2. 在认证页面输入账号密码登录
3. 登录成功后，找 Network 列表里 Method 为 **POST** 的请求（通常是 `InterFace.do?method=login`）
4. 点 **Payload** 标签，确认表单字段名（`userId`、`password` 等），如果字段名不同需要改脚本 `do_auth_portal_post()` 中的 form_data
5. 从地址栏或请求 URL 中提取 `portal_host`

然后把抓到的信息填入配置即可。

## 分发给别人（无需安装 Python）

打包成单个 exe，对方不需要装 Python，双击即可使用：

```powershell
# 构建（需要先 pip install pyinstaller）
pyinstaller --onefile --console --name auto_login auto_login.py
```

构建产物在 `dist/auto_login.exe`。分发给别人时，把 `auto_login.exe` 和一份 `auto_login_config.example.json` 放在同一目录即可。

### 电脑小白使用流程

1. 把 `auto_login_config.example.json` 重命名为 `auto_login_config.json`
2. 双击 `auto_login.exe`
3. 程序会自动检测配置未完成，引导填写学号、密码
4. 菜单选 `[2]` 快速测试认证
5. 测试通过后，菜单选 `[1]` 启动自动认证
6. 按 `[4]` 查看使用指南，了解如何关闭 Windows 弹窗和部署计划任务

**exe 内置了完整的菜单系统**，不用记命令行参数：

```
  [1] 启动自动认证（后台检测 + 断网重连）
  [2] 快速测试认证（发一次请求验证配置）
  [3] 修改配置（学号 / 密码 / Portal地址）
  [4] 查看使用指南
  [q] 退出
```

## 版本历史

### v1.2 (2026-05-12)

- 新增交互式菜单系统：启动认证 / 测试认证 / 修改配置 / 使用指南，无需记命令行
- 新增首次配置向导：自动检测未完成的配置，引导填写学号、密码、Portal 地址
- 新增嵌入式使用指南：包含关闭 Windows 弹窗、定时建议、配置文件说明等保姆级教程
- 优化 exe 体验：配置未完成自动提示，菜单循环不闪退

### v1.1 (2026-05-12)

- 新增 `portal_post` 认证模式：自动提取 portal 的 JS 跳转参数，后台 POST 用户名密码完成认证
- 新增 captive portal 透明代理检测：校验响应内容关键词 + 正则提取 JS 跳转 URL
- 新增 `--auth` 测试命令：直接发一次认证请求，用于验证配置是否正确
- 新增 `--version` 版本信息
- 新增 `check_expected_body` 配置项
- 支持 PyInstaller 打包成独立 exe，无需安装 Python
- 修复 PyInstaller 打包后路径检测问题
- 双击 exe 运行完自动暂停，不会闪退

### v1.0 (初始版本)

- `http` 模式：后台 GET 请求 portal URL 续期
- `browser` 模式：打开浏览器 + 模拟 Enter
- 网络检测：HEAD 请求 check_url 判断连通性
- Windows 计划任务部署（setup_task.ps1）
- 交互 / 后台双模式自动识别
- 按日期日志 + 7 天自动清理

## 免责声明

- 本工具**仅供学习研究使用**，请勿用于非法用途
- **免费开源**，如通过付费渠道获得，请立即退款并举报
- 使用者自行承担因使用本工具产生的一切后果
- 项目地址：https://github.com/suching8848/syxy_auto_verification

## 依赖

- Python 3（仅标准库，无需 pip 安装）
- Windows 10/11
- （如打包成 exe，则无需任何依赖）
