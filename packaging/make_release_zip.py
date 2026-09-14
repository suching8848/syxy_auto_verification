"""打包发布 zip —— 只收白名单里的文件，杜绝把配置和日志带出去。

背景：运行目录（dist\auto_login_v1.7.2）同时也是计划任务的部署目录，
里面装着用户自己的 auto_login_config.json（含明文密码）和 logs\。
直接 Compress-Archive 整个目录会把这些一起打进 zip —— 上传到 GitHub
就等于公开泄漏密码。

用法：
    python packaging/make_release_zip.py [版本号]
"""

import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERSION = sys.argv[1] if len(sys.argv) > 1 else "1.7.2"
NAME = f"auto_login_v{VERSION}"

# 只有这些文件会进发布包，一个都不多
MANIFEST = [
    ("dist/CampusNet.exe", "CampusNet.exe"),
    ("assets/campusnet.ico", "assets/campusnet.ico"),
    ("packaging/使用说明.txt", "使用说明.txt"),
    ("setup_task.ps1", "setup_task.ps1"),
    ("auto_login_config.example.json", "auto_login_config.example.json"),
    (f"RELEASE_v{VERSION}.md", f"RELEASE_v{VERSION}.md"),
]

# 绝不允许出现在发布包里的东西
FORBIDDEN = ("auto_login_config.json", ".log", "_tray_icon.ico")


def main():
    missing = [src for src, _ in MANIFEST if not os.path.exists(os.path.join(ROOT, src))]
    if missing:
        raise SystemExit("缺少文件，先构建/补齐：\n  " + "\n  ".join(missing))

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"{NAME}.zip")

    staging = tempfile.mkdtemp(prefix="campusnet_release_")
    try:
        pkg = os.path.join(staging, NAME)
        for src, rel in MANIFEST:
            dst = os.path.join(pkg, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, src), dst)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for base, _dirs, files in os.walk(pkg):
                for f in files:
                    full = os.path.join(base, f)
                    z.write(full, os.path.relpath(full, staging))

        # 自检：宁可失败也不要把密码发出去
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            leaked = [n for n in names
                      if any(n.endswith(bad) for bad in FORBIDDEN)]
            if leaked:
                raise SystemExit(f"发布包里出现了不该有的文件：{leaked}")
            print(f"{zip_path}  ({os.path.getsize(zip_path)} bytes)")
            for n in sorted(names):
                print(f"   {n}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
