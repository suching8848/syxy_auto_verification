# v1.6.1

校园网断线自动认证工具。定时检测网络状态，检测到 captive portal 后通过后台 HTTP 请求静默完成认证，**完全无感**。

> ⚠️ **如果你在用 v1.6，请务必升级。** v1.6 存在一个会导致工具在正常网络下持续误判、且认证后无法恢复的检测 bug，详见下方第一条。

## 更新内容

### 修复：portal 检测误判（重要）

百度会将 `http://www.baidu.com` 301 重定向到 `https://www.baidu.com/`。v1.6 的检测逻辑把这种**正常的同站 HTTPS 升级**误判为 captive portal 劫持，造成两个后果：

1. **网络正常时被判为断网**，反复触发无意义的认证尝试，密集刷日志
2. **更严重**：认证成功后还有 3 次联网复核，复核用的是同一套判定逻辑，因此**恒为失败** —— 即使 portal 已经返回 `{"result":"success"}`，工具也永远无法进入恢复状态

修复方式：同 host、同 path 的 `http → https` 升级不再视为 portal 劫持；跨 host 跳转和同 host 改路径仍然照旧识别为 portal。新增 4 个回归测试锁住该判定行为。

> **已用真实断网演练验证**：检测 → 认证 → 恢复完整闭环，单轮耗时 < 5 秒。

### 修复：认证参数键名统一

v1.6 的代码同时接受 `portal_url` 和 `portal_host` 两个键，但 `http` 认证模式和配置完整性检查只读 `portal_url`。如果你的配置里写的是 `portal_host`，切换到 `http` 模式后 `portal_url` 会为空并直接失败。现已统一为 `portal_url`。

### 新增：断网诊断日志

断网时额外记录对端是否 TCP 可达，用于区分两种完全不同的故障：

- 对端可达但内容不对 → portal 透明代理，属正常劫持
- 对端不可达 → 真离线或 DNS 故障

排查换校区/换 portal 的问题时，这一行能直接省掉大量猜测。

### 修复：`setup_task.ps1` 会打印假的成功横幅

`Register-ScheduledTask` 在权限不足时抛出的是 CIM **非终止**错误，`$ErrorActionPreference = "Stop"` 拦不住它。因此旧脚本在**非管理员** PowerShell 里运行时会打印 `Task registered successfully!`，但任务其实根本没注册成功。

现在改为显式捕获失败、打印原因并提示需要管理员权限，同时注册后校验任务确实存在且触发器时间符合预期。

### 变更

- 计划任务默认时间 `22:35` → `18:50`
- 新增 15 个离线回归测试（全部 mock 网络，不做真实请求）

## 使用方式

```powershell
# 交互模式（菜单）
auto_login.exe
# 或
python auto_login.py

# 测试认证（返回码 0 成功 / 1 失败）
python auto_login.py --auth

# 系统托盘（隐藏窗口）
python auto_login.py --tray

# 计划任务部署 — 每日定时（管理员 PowerShell）
.\setup_task.ps1

# 计划任务部署 — 开机自启（管理员 PowerShell）
.\setup_task.ps1 -Boot
```

## 下载

- **`auto_login_v1.6.1.zip`**：含 exe + 配置模板 + 部署脚本，无需 Python
- 解压后双击 `auto_login.exe`，配置向导自动引导

## 开机自启 vs 每日定时

| | 开机自启 | 每日定时 |
|---|---|---|
| 任务名 | `CampusNetAutoLogin_Boot` | `CampusNetAutoLogin` |
| 触发 | 系统启动时 | 每天 `schedule_time`（默认 18:50） |
| 身份 | SYSTEM (Session 0) | 当前用户 (Interactive) |
| 运行时长 | 无限 | 60 分钟 |
| 删除命令 | `Unregister-ScheduledTask -TaskName CampusNetAutoLogin_Boot` | `Unregister-ScheduledTask -TaskName CampusNetAutoLogin` |

建议二选一。开机自启适用于断网时间不固定的场景。

## 注意事项

- 仅支持 Windows 10/11
- 默认配置为三亚学院校园网，换学校需改 `portal_url`
- 开机自启仅支持 `portal_post` 和 `http` 认证模式（纯 HTTP），不支持 `browser` 模式
- 工具**不会主动登出**校园网。因此网络在线时运行 `--auth` 可能缺少认证参数而报错（如"设备未注册"），这属正常现象，**不代表配置有问题** —— 最准确的测试方式是在真实断网时运行
- 使用代理软件（Clash 等 TUN 模式）时，认证请求可能被路由到虚拟网卡，导致 portal 收到错误的客户端 IP。认证期间建议关闭 TUN

**免费开源，仅供学习研究使用。**
