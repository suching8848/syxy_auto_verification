# 项目目录与本机部署

## 唯一日常使用入口

`runtime/CampusNet/CampusNet.exe`：本机部署版本（当前 v2.0.3）。双击打开界面。换新版只需用 `dist/CampusNet.exe` 覆盖它，账号配置和日志目录都保留。
`runtime/CampusNet/auto_login_config.json`：实际账号及参数。
`runtime/CampusNet/logs/`：当前运行日志。

计划任务 CampusNetAutoLogin_Silent 启动此 exe，参数 `--silent --now --run-minutes 30`。实际触发时间以 Windows 计划任务中保存的配置为准。
本目录被 Git 忽略，不应对外分享或用于组装发布包。

## 源码和构建

- auto_login.py：检测、认证、重试、CLI。
- gui_app.py：GUI、托盘及计划任务配置。
- auto_login_config.example.json：公开配置模板。
- 根目录 auto_login_config.json：仅从源码启动时使用，不是计划任务配置。
- test_auto_login.py：离线回归测试。
- test_guard_instance.py、test_guard_gui.py：守护互斥及重复启动界面反馈的离线回归测试。
- setup_task.ps1：计划任务部署脚本。
- build/*.spec：exe 构建配方，其余 build 内容为可重新生成的构建缓存。
- packaging/：白名单发布打包脚本及使用说明。
- assets/：图标素材。
- dist/CampusNet.exe：最新构建产物，不作为日常运行入口。
- dist/archive/：旧使用目录、旧压缩包及迁移前任务 XML 备份，仅作回退。
- RELEASE_v*.md：各版本更新说明。

## 发布状态

已公开发布版本为 v2.0；v2.0.1（守护互斥）、v2.0.2（探测/登录绕过系统代理）、v2.0.3（不提交自凑登录参数、文档对齐）是本机修复，发布附件与本机部署分别管理。
旧版目录不再被本机计划任务引用。后续更新只替换 runtime/CampusNet 内的程序，保留账号配置。
