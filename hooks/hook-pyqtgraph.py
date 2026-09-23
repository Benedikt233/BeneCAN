# -*- mode: python ; coding: utf-8 -*-
"""自定义 pyqtgraph 打包钩子：覆盖 _pyinstaller_hooks_contrib 的官方版本。

原因：官方 hook-pyqtgraph.py 会调用 collect_submodules()，而 PyInstaller 会为此
启动一个隔离子进程；在本机环境下该子进程会以 0xC0000005（访问冲突）直接崩溃，
导致打包中断。pyqtgraph 的子模块通过 PyInstaller 的静态分析即可正常收集，
这里只需要补上运行时动态导入的 Qt 模块。
"""

hiddenimports = ["pyqtgraph.Qt"]

# 提前排除体积大且用不到的示例/测试资源
excludedimports = ["pyqtgraph.examples"]
