# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 exe。

用法：
    python -m PyInstaller benecan.spec --noconfirm
产物：
    dist/BeneCAN.exe（约 150~250 MB，含 PySide6/pyarrow/pyqtgraph/cantools）
说明：
    * dbc/ 目录随包分发（示例 DBC）；运行时会在 exe 同级目录读写
      dbc/channel_map.json，用户可自行往里放 DBC。
    * 日志写在 exe 同级的 logs/ 目录（见 utils/log_setup.py）。
    * 排除用不到的 Qt 模块以控制体积（WebEngine/Quick/3D/Charts 等）。
"""

hidden = ["pyarrow", "pyarrow.parquet", "pyarrow.lib", "cantools", "bitstruct",
          "pyqtgraph"]

excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.QtQmlModels",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtTextToSpeech", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtRemoteObjects", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtDesigner",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "pandas", "matplotlib", "scipy", "tkinter", "IPython", "jupyter",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("dbc", "dbc")],
    hiddenimports=hidden,
    hookspath=["hooks"],          # 覆盖 pyqtgraph 官方钩子（见 hooks/hook-pyqtgraph.py）
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BeneCAN",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
