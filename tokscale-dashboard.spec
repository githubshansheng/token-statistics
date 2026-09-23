# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

root = Path(SPECPATH)
tokscale_candidates = sorted(
    (root / ".npm-cache" / "_npx").glob(
        "*/node_modules/@tokscale/cli-win32-x64-msvc/bin/tokscale.exe"
    ),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
if not tokscale_candidates:
    raise FileNotFoundError("未找到 tokscale Windows 原生可执行文件，请先运行一次 npx tokscale")
tokscale = tokscale_candidates[0]

# ---- 通用发行包不预构建/携带任何面板快照或用户统计数据 ----
# 目标电脑首次启动只扫描当前 Windows 用户的本地会话日志。
# 图表与数据文件均由运行时写入 %LOCALAPPDATA%\TokscaleDashboard。
import os

snapshot = root / "dashboard.html"
resources = [
    (root / "serve.py", "."),
    (root / "build_dashboard.py", "."),
    (root / "runtime_support.py", "."),
    (tokscale, "."),
]
# dashboard.html、graph.json、pricing.json、model-cost-config.json 等均不可进包。
# 即使源码目录残留开发机数据，也始终生成干净的通用发行版。

a = Analysis(
    [str(root / "portable_launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(src), dst) for src, dst in resources]
          + [(str(src), dst) for src, dst in collect_data_files("clr_loader")],
    hiddenimports=(
        collect_submodules("concurrent")
        + ["webview.platforms.edgechromium", "clr",
           "pystray", "pystray._win32", "winreg",
           "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"]
        + collect_submodules("clr_loader")
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Tokscale用量面板-桌面版-v1.7.7-Windows-x64",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
