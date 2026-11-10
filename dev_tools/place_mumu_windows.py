# -*- coding: utf-8 -*-
"""把 MuMu 模拟器多开窗口摆到指定布局
========================================
窗口标题匹配「MuMu安卓设备」「MuMu安卓设备-1」「MuMu安卓设备-2」，只处理正在
运行且可见的窗口：未运行则跳过（不负责启动），位置大小已符合目标则什么都不做，
否则用 SetWindowPos 挪过去并回读校验（Qt 窗口偶发抢回原位，故带重试）。

布局（layout）用于区分不同的窗口摆放方案，默认 base：
    base      两个窗口并排铺满主屏（各 1282x761 可见边框）
    hand      等同 hand 2
    hand 2    左右各占主屏一半宽度，两者同宽同高
    hand 3    在 hand 2 基础上，第三个窗口贴在左下角（755x464）

坐标一律按 DWM 可见边框（DWMWA_EXTENDED_FRAME_BOUNDS）计算，而不是
GetWindowRect：后者把窗口四周的透明投影也计入，直接按它切半屏会多出几像素
缝隙/重叠，肉眼看得见。

用法（项目根目录）:
    .venv\\Scripts\\python.exe -m dev_tools.place_mumu_windows            # 等价 base
    .venv\\Scripts\\python.exe -m dev_tools.place_mumu_windows hand
    .venv\\Scripts\\python.exe -m dev_tools.place_mumu_windows hand 2
    .venv\\Scripts\\python.exe -m dev_tools.place_mumu_windows hand 3
    .venv\\Scripts\\python.exe -m dev_tools.place_mumu_windows base --dry-run
    .venv\\Scripts\\python.exe -m dev_tools.place_mumu_windows --list
"""
import argparse
import ctypes
import sys
import time
from ctypes import Structure, byref
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from win32api import GetSystemMetrics
from win32con import SM_CXSCREEN, SWP_NOACTIVATE, SWP_NOZORDER, SW_RESTORE
from win32gui import (EnumWindows, GetWindowRect, GetWindowText, IsIconic,
                      IsWindowVisible, SetWindowPos, ShowWindow)

from module.logger import logger


DEFAULT_LAYOUT = 'base'

# 「hand」不带编号时按 hand 2 处理，与用户口头叫法一致
HAND_DEFAULT_VARIANT = '2'

# 三个多开窗口的标题，顺序即左、右、左下
WINDOW_TITLES = ('MuMu安卓设备', 'MuMu安卓设备-1', 'MuMu安卓设备-2')

# hand 布局的窗口高度（可见边框）：用户手动调成 960x578 后定下的基准，宽度运行时按
# 屏幕宽度取半，高度固定，保证两台机器在任何屏幕上都同高同宽、画面比例一致
HAND2_HEIGHT = 578

# hand 3 里第三个窗口的尺寸：沿用用户手调的 755x464，紧贴 hand 2 下方（y=578）
HAND3_THIRD_WIDTH = 755
HAND3_THIRD_HEIGHT = 464

# 规划中但还没实现的布局：显式列出，避免与「拼错的布局名」混为一谈
LAYOUTS_TODO = ('hand1',)

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2，与 desktop_impl.dpi_awareness 一致：
# 不做感知时 GetWindowRect/SetWindowPos 会被系统按缩放比虚拟化，读数与设置值对不上
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# DWMWA_EXTENDED_FRAME_BOUNDS，返回不含投影的可见窗口边框
_DWMWA_EXTENDED_FRAME_BOUNDS = 9


class _RECT(Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


def set_dpi_aware() -> None:
    """把本进程切到 Per-Monitor V2，保证读到的矩形是物理像素。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
    except AttributeError:
        pass


def find_windows(title: str) -> list:
    """按标题精确匹配可见的顶层窗口。"""
    handles = []

    def callback(hwnd, _):
        if IsWindowVisible(hwnd) and GetWindowText(hwnd) == title:
            handles.append(hwnd)
        return True

    EnumWindows(callback, None)
    return handles


def get_rect(hwnd) -> tuple:
    """GetWindowRect：返回 (x, y, width, height)，含透明投影。"""
    left, top, right, bottom = GetWindowRect(hwnd)
    return left, top, right - left, bottom - top


def get_frame_rect(hwnd) -> tuple:
    """DWM 可见边框：返回 (x, y, width, height)，不含投影；取不到时退回 GetWindowRect。"""
    rect = _RECT()
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, byref(rect), ctypes.sizeof(rect))
    if result != 0:
        return get_rect(hwnd)
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def frame_insets(hwnd) -> tuple:
    """窗口矩形相对可见边框的外扩量 (left, top, right, bottom)。

    SetWindowPos 设的是窗口矩形，想让可见边框正好落在屏幕半边，就得把这圈
    投影补回去。投影宽度随 DPI/主题变化，故每次运行时实测，不写死。
    """
    left, top, width, height = get_rect(hwnd)
    fleft, ftop, fwidth, fheight = get_frame_rect(hwnd)
    return (fleft - left, ftop - top,
            (left + width) - (fleft + fwidth), (top + height) - (ftop + fheight))


def ensure_restored(hwnd) -> None:
    """最小化的窗口先还原：最小化时 GetWindowRect 返回 -32000 的占位矩形，无法测量。"""
    if IsIconic(hwnd):
        ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)


def place_window(hwnd, target: tuple, attempts: int = 3) -> bool:
    """把窗口可见边框摆到 target，回读校验；成功返回 True。

    target 中任一分量为 None 表示沿用当前值（例如高度不关心，保持原样）。
    """
    ensure_restored(hwnd)
    for attempt in range(1, attempts + 1):
        current = get_frame_rect(hwnd)
        x = current[0] if target[0] is None else target[0]
        y = current[1] if target[1] is None else target[1]
        width = current[2] if target[2] is None else target[2]
        height = current[3] if target[3] is None else target[3]
        wanted = (x, y, width, height)
        if current == wanted:
            if attempt > 1:
                logger.info(f'第 {attempt} 次调整后到位')
            return True
        left, top, right, bottom = frame_insets(hwnd)
        SetWindowPos(hwnd, 0, x - left, y - top,
                     width + left + right, height + top + bottom,
                     SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.15)
    actual = get_frame_rect(hwnd)
    logger.warning(f'调整失败：目标 {target}，实际 {actual}')
    return False


def process_target(title: str, target: tuple, dry_run: bool) -> bool:
    """处理一个标题对应的全部窗口，返回是否全部符合目标。"""
    handles = find_windows(title)
    if not handles:
        logger.info(f'[{title}] 未运行，跳过')
        return True

    all_ok = True
    for hwnd in handles:
        ensure_restored(hwnd)
        current = get_frame_rect(hwnd)
        wanted = tuple(cur if want is None else want
                       for cur, want in zip(current, target))
        if current == wanted:
            logger.info(f'[{title}] 已在目标位置大小 {current}，无需调整')
            continue
        logger.info(f'[{title}] hwnd={hwnd} {current} -> {wanted}')
        if dry_run:
            continue
        if not place_window(hwnd, target):
            all_ok = False
    return all_ok


def _layout_base() -> tuple:
    """并排铺满主屏，两个窗口同尺寸（可见边框各 1282x761）。"""
    return (
        (WINDOW_TITLES[0], 0, 0, 1282, 761),
        (WINDOW_TITLES[1], 638, 0, 1282, 761),
    )


def _layout_hand2() -> tuple:
    """左右各占主屏一半宽，两者同宽同高（高度取 HAND2_HEIGHT）。"""
    half = GetSystemMetrics(SM_CXSCREEN) // 2
    return (
        (WINDOW_TITLES[0], 0, 0, half, HAND2_HEIGHT),
        (WINDOW_TITLES[1], half, 0, half, HAND2_HEIGHT),
    )


def _layout_hand3() -> tuple:
    """hand 2 的两个窗口保持不变，第三个窗口贴在左下角，尺寸沿用用户手调值。"""
    return _layout_hand2() + (
        (WINDOW_TITLES[2], 0, HAND2_HEIGHT, HAND3_THIRD_WIDTH, HAND3_THIRD_HEIGHT),
    )


LAYOUT_BUILDERS = {
    'base': _layout_base,
    'hand2': _layout_hand2,
    'hand3': _layout_hand3,
}


def resolve_layout(name: str, variant: str = '') -> tuple:
    """把布局名解析成目标列表；未知布局直接报错，不做静默回落。"""
    if name == 'hand':
        name = f'hand{variant or HAND_DEFAULT_VARIANT}'
    if name in LAYOUT_BUILDERS:
        return LAYOUT_BUILDERS[name]()
    available = ', '.join(LAYOUT_BUILDERS) + '（hand 等同于 hand 2）'
    if name in LAYOUTS_TODO:
        raise ValueError(f'布局 {name} 尚未实现（当前可用：{available}）')
    raise ValueError(f'未知布局 {name}（可用：{available}；规划中：{", ".join(LAYOUTS_TODO)}）')


def list_all_windows() -> None:
    """列出全部已知 MuMu 窗口的当前可见边框，不唤醒最小化的窗口。"""
    for title in WINDOW_TITLES:
        handles = find_windows(title)
        if not handles:
            logger.info(f'[{title}] 未运行')
            continue
        for hwnd in handles:
            logger.info(f'[{title}] hwnd={hwnd} 当前 {get_frame_rect(hwnd)}')


def main() -> int:
    parser = argparse.ArgumentParser(description='把 MuMu 模拟器多开窗口摆到指定布局')
    parser.add_argument('layout', nargs='?', default=DEFAULT_LAYOUT,
                        help=f'布局名，默认 {DEFAULT_LAYOUT}')
    parser.add_argument('variant', nargs='?', default='',
                        help=f'布局编号，仅 hand 使用，默认 {HAND_DEFAULT_VARIANT}')
    parser.add_argument('--dry-run', action='store_true', help='只报告差异，不真正调整')
    parser.add_argument('--list', action='store_true', help='只列出当前位置大小')
    args = parser.parse_args()

    set_dpi_aware()
    if args.list:
        list_all_windows()
        return 0

    try:
        targets = resolve_layout(args.layout, args.variant)
    except ValueError as e:
        logger.error(str(e))
        return 2

    all_ok = True
    for title, x, y, width, height in targets:
        if not process_target(title, (x, y, width, height), args.dry_run):
            all_ok = False

    if args.dry_run:
        return 0
    logger.info('全部窗口已符合目标位置与大小' if all_ok else '部分窗口调整未生效，请重试')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
