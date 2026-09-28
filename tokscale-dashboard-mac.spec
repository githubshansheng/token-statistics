# -*- mode: python ; coding: utf-8 -*-
# Tokscale Dashboard macOS 免安装 .app 打包配置。
#
# 用法：venv 内执行 pyinstaller tokscale-dashboard-mac.spec --noconfirm
# 产物：dist/TokscaleDashboard.app（免安装，双击即用，可拷贝到任意同架构 Mac）
#
# 与 Windows 版一致的纪律：通用发行包不预构建/携带任何面板快照或用户统计数据
# （dashboard.html、graph.json、pricing.json、model-cost-config.json 均不进包）。
# 目标 Mac 首次启动只扫描当前用户的本地会话日志，数据文件均由运行时写入
# ~/Library/Application Support/TokscaleDashboard。

from pathlib import Path

root = Path(SPECPATH)
# npx 拉取的 darwin 原生二进制（cli-darwin-arm64 / cli-darwin-x64），
# 取 mtime 最新的一个；打包机的架构决定了发行版的目标架构。
tokscale_candidates = sorted(
    (root / ".npm-cache" / "_npx").glob(
        "*/node_modules/@tokscale/cli-darwin-*/bin/tokscale"
    ),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
if not tokscale_candidates:
    raise FileNotFoundError(
        "未找到 tokscale macOS 原生可执行文件，请先运行一次 npx tokscale"
    )
tokscale = tokscale_candidates[0]

resources = [
    (root / "serve.py", "."),
    (root / "build_dashboard.py", "."),
    (root / "runtime_support.py", "."),
]

a = Analysis(
    [str(root / "mac_launcher.py")],
    pathex=[str(root)],
    # tokscale 放 binaries：PyInstaller 对 binaries 保留可执行位，
    # 确保运行时从 .app 复制出来后可直接执行。
    binaries=[(str(tokscale), ".")],
    datas=[(str(src), dst) for src, dst in resources],
    hiddenimports=["webview.platforms.cocoa"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pystray", "PIL", "PIL.Image", "winreg", "clr", "clr_loader",
              "tkinter"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir 模式
    name="TokscaleDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：双击 .app 不弹终端
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,       # 按打包机本机架构（arm64）
    codesign_identity=None, # ad-hoc 签名（PyInstaller 自动完成）
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TokscaleDashboard",
)

app = BUNDLE(
    coll,
    name="TokscaleDashboard.app",
    info_plist={
        "CFBundleDisplayName": "Tokscale 用量面板",
        "CFBundleName": "TokscaleDashboard",
        "CFBundleIdentifier": "com.tokscale.dashboard",
        "CFBundleShortVersionString": "1.7.7",
        "CFBundleVersion": "1.7.7",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": "本地面板，不上传任何数据。",
    },
)
