# v2.0.3 验收状态（2026-10-28）

## 结论

构建、部署与离线验证通过；**故障修复待真实断网场景验证**。v2.0.2 复核发现的“缺参处理与文档不一致”已在 v2.0.3 修正；代理绕过不等于 TUN 绕过、请求头因果未受控，这两点作为明确限制记录。

## 已验证

| 项目 | 证据 |
|---|---|
| 构建 | `dist/CampusNet.exe` SHA256 `F678C59F014FD6837AEB4F5DA573EA8A06A053A9F74B217EF44A6CCDA1C513A5`，内嵌 `version=v2.0.3` |
| 部署 | `runtime/CampusNet/CampusNet.exe` 与构建产物同一哈希；v2.0.2 备份于 `runtime/CampusNet/backups/CampusNet-v2.0.2-20261028-232649.exe`（`9B1A139C…`）；计划任务未改，仍每日 20:55、30 分钟 |
| 离线测试 | 179 项全部通过（`test_auto_login` 158、`test_guard_instance` + `test_guard_gui` 21）；`py_compile`、`git diff --check` 通过 |
| HTTP 代理绕过 | 环境变量指向失效代理时，默认 `urlopen` 失败、`check_network` 返回 OK |
| 缺参不提交 | 本地地址为 `198.18.0.1` 与 `10.150.74.11` 两种情形都只做发现，不发出登录 POST |
| 隔离冒烟 | `build/smoke-v2.0.3`：约 61 秒、退出码 0、`version=v2.0.3` |
| 真实健康网络守护 | 23:26:53–23:27:53，13 次检查全部 `[OK]`、退出码 0；启动日志记录系统代理与门户源地址 |

本次真实运行观察到的两行新日志（TUN 当时为开启状态）：

```
[INFO] System proxy 127.0.0.1:7900 is configured; connectivity checks and portal login bypass it
[INFO] Route to portal 10.10.200.102 uses source address 198.18.0.1 (tunnel/reserved: portal detection may be affected)
```

## 未验证 / 已知限制

- **真实断网后的自动重新认证未验证**。健康网络下的运行只证明进程可用、探测路径可通。
- **不绕过 IP 层 TUN**。本次运行即处于 TUN 开启状态：到门户的源地址是 `198.18.0.1`，探测请求经隧道转发；门户拦截页是否可见、能否取得带参重定向，均未验证。
- **请求头与超时的因果关系未证实**。10-28 21:09–21:24 的差异（浏览器头得到响应、裸 urllib 超时）只是排查线索；代理状态、连接条件等变量未受控。
- **退避未改**。缺参或失败后仍按原策略退避（连续失败后最长 300 秒），短暂出现的门户跳转窗口仍可能错过。

## 复核意见处理

1. 缺参仍会提交普通内网 IP：v2.0.3 删除该路径，只做发现并记录原因；测试覆盖 TUN 地址与普通内网地址两种情形。
2. 代理绕过不等于 TUN 绕过：README、启动日志与本验收记录均明确标注，未声称绕过 TUN。
3. 请求头结论过强：README 与代码注释改为“可能原因，未受控验证”。
4. 退避窗口：保留原策略，作为已知限制记录。

## 下一步

- 下一次真实断网窗口（通常 20:55–21:25）只读核对日志：
  - 是否出现 `Portal JS redirect`（拿到参数）；
  - 是否出现 `Auth OK` 与 `[RECOVER]`；
  - 若出现 `Portal did not provide login parameters`，记录同次启动日志里的两行路径信息（系统代理、门户源地址）。
- 若 TUN 开启时确实无法发现门户，再评估按物理网卡绑定探测的方案（当前未实现）。
