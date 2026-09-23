# -*- coding: utf-8 -*-
"""测试包初始化：先把 Qt 初始化好，再让其他依赖加载。

原因（实测结论，非猜测）：
    cantools 的 bitstruct C 扩展先加载、之后才初始化 Qt 时，进程会以
    0xC0000005（访问冲突）直接崩溃；而**先初始化 Qt 再加载 cantools 一切正常**
    （这正是 main.py 的导入顺序：Qt 在最前）。
    测试发现是按文件名字母序导入的（test_dbc 早于 test_graphic_smoke），
    所以在这里提前把 Qt 建起来，保持与产品一致的顺序。

    GUI 测试用离屏模式（QT_QPA_PLATFORM=offscreen），无需桌面即可运行。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _TEST_APP = QApplication.instance() or QApplication([])
except ImportError:                                   # 未装 PySide6 时纯逻辑测试照常跑
    _TEST_APP = None
