# -*- coding: utf-8 -*-
"""全局常量与阈值。

通道映射依据上游转换器 Vehicle_log_tool_AI/features/zst2asc/file_convert.py：
    CHNum_int = int(CHNum, 16) + 1
    ListCHNum_CDCU = ['00','01','02','03','04','08','13']
    ListCHNum_LDCU = ['10','11']
    ListCHNum_RDCU = ['12']
"""

APP_NAME = "BeneCAN"
TOOL_VERSION = "V0.3"

# 通道号 -> (来源 ECU, 上游原始 CHNum)
CHANNEL_MAP = {
    1: ("CDCU", "00"),
    2: ("CDCU", "01"),
    3: ("CDCU", "02"),
    4: ("CDCU", "03"),
    5: ("CDCU", "04"),
    9: ("CDCU", "08"),
    17: ("LDCU", "10"),
    18: ("LDCU", "11"),
    19: ("RDCU", "12"),
    20: ("CDCU", "13"),
}

# CANFD DLC(hex) -> 实际字节数
DLC_TO_LEN = {9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}

# 时间轴体检阈值（秒）
TS_BACKWARD_THRESHOLD = 1.0   # 同通道回退超过该值 -> 记为一次疑似时间基准切换
TS_GAP_THRESHOLD = 5.0        # 同通道正向空档超过该值 -> 记为一次疑似丢包/暂停

# CANFD 行尾部固定常量列数量（实测 8 列，仅用于报告说明，解析不依赖该值）
CANFD_TAIL_COLUMNS = 8

# 索引缓存目录的覆盖开关。
# 默认写入系统临时目录（可写、不进 OneDrive 同步、便于清理）；
# 需要持久化时可设环境变量指向别处，例如：
#   set BENECAN_INDEX_DIR=%LOCALAPPDATA%\BeneCAN\index
INDEX_DIR_ENV = "BENECAN_INDEX_DIR"

# 日志：测试调试阶段默认全量 DEBUG（见 utils/log_setup.py）
LOG_DIR_NAME = "logs"
LOG_LEVEL_ENV = "BENECAN_LOG_LEVEL"

# 性能验收基线（待确认）
PERF_MAX_FILE_BYTES = 1_500_000_000
PERF_FIRST_INDEX_SECONDS = 30
PERF_CACHED_OPEN_SECONDS = 3
PERF_MAX_RSS_MB = 1500
PERF_MIN_FPS = 30
