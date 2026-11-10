# -*- coding: utf-8 -*-
"""MuMu 模拟器多开窗口的启动、停止与摆放
========================================
窗口标题匹配「MuMu安卓设备」「MuMu安卓设备-1」「MuMu安卓设备-2」，只处理正在
运行且可见的窗口：位置大小已符合目标则什么都不做，否则用 SetWindowPos 挪过去
并回读校验（Qt 窗口偶发抢回原位，故带重试）。

启动/停止走 MuMu 自带的命令行 MuMuManager.exe（与 mumu-cli.exe 是同一个文件），
不依赖图形界面的多开管理窗口，主程序没开时会自动拉起。

布局（layout）用于区分不同的窗口摆放方案，默认 base：
    base      两个窗口并排铺满主屏（各 1282x761 可见边框）
    hand      等同 hand 2
    hand 2    左右各占主屏一半宽度，两者同宽同高
    hand 3    在 hand 2 基础上，第三个窗口贴在左下角（755x464）

坐标一律按 DWM 可见边框（DWMWA_EXTENDED_FRAME_BOUNDS）计算，而不是
GetWindowRect：后者把窗口四周的透明投影也计入，直接按它切半屏会多出几像素
缝隙/重叠，肉眼看得见。MuMuManager 自带的 layout_window 也是按窗口矩形设的，
实测每边差 3px，所以这里不用它。

place（默认动作）会先补齐没在运行的实例、等窗口真正就绪后再摆；不想启动就加
--no-start，只想看差异就加 --dry-run。

start/stop/restart 只作用于布局需要的那几个实例：stop 不会顺手关掉别的布局
（例如 hand 3 的 3 号机）还在跑的实例。

用法（项目根目录）:
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows            # 等价 place base
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows hand 2
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows hand 3
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows base --dry-run
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows --list
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows hand 2 --no-start
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows start hand 3   # 启动并摆放
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows stop           # 只关 base 的实例
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows stop hand 3    # 只关 0/1/2
    .venv\\Scripts\\python.exe -m dev_tools.mumu_windows restart hand 2
"""
import argparse
import ctypes
import json
import os
import subprocess
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

# 不带动作时默认只摆窗；带这些词则是启动/停止
ACTIONS = ('place', 'start', 'stop', 'restart', 'list', 'help')

# 「hand」不带编号时按 hand 2 处理，与用户口头叫法一致
HAND_DEFAULT_VARIANT = '2'

# 三个多开窗口的标题，顺序即左、右、左下
WINDOW_TITLES = ('MuMu安卓设备', 'MuMu安卓设备-1', 'MuMu安卓设备-2')

# 每个布局需要的实例序号，用于 start/stop/restart
LAYOUT_INDEXES = {
    'base': (0, 1),
    'hand2': (0, 1),
    'hand3': (0, 1, 2),
}

# hand 布局的窗口高度（可见边框）：用户手动调成 960x578 后定下的基准，宽度运行时按
# 屏幕宽度取半，高度固定，保证两台机器在任何屏幕上都同高同宽、画面比例一致
HAND2_HEIGHT = 578

# hand 3 里第三个窗口的尺寸：沿用用户手调的 755x464，紧贴 hand 2 下方（y=578）
HAND3_THIRD_WIDTH = 755
HAND3_THIRD_HEIGHT = 464

# 规划中但还没实现的布局：显式列出，避免与「拼错的布局名」混为一谈
LAYOUTS_TODO = ('hand1',)

# MuMuManager.exe 的常见位置，注册表读不到时的兜底（只放默认安装目录，不写本机路径）
MANAGER_FALLBACKS = (
    r'C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe',
    r'C:\Program Files\Netease\MuMuPlayer-12.0\nx_main\MuMuManager.exe',
)

# 单次 MuMuManager 调用的最长等待时间：正常都是秒级返回，卡住说明它自己挂了
MANAGER_TIMEOUT = 120

# 等待窗口稳定：模拟器启动后窗口会先出现在默认位置，随后再被自己改一次大小，
# 过早 SetWindowPos 会被它覆盖。这里用轮询判断「连续一段时间矩形不再变化」，
# 比起 hook 窗口消息简单得多，也够用。
WINDOW_READY_INTERVAL = 0.25
WINDOW_STABLE_SECONDS = 1.5
WINDOW_READY_TIMEOUT = 30.0

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


def normalize_layout(name: str, variant: str = '') -> str:
    """把布局名规整成 LAYOUT_BUILDERS 里的键，未知布局直接报错，不做静默回落。"""
    if name == 'hand':
        name = f'hand{variant or HAND_DEFAULT_VARIANT}'
    if name in LAYOUT_BUILDERS:
        return name
    available = ', '.join(LAYOUT_BUILDERS) + '（hand 等同于 hand 2）'
    if name in LAYOUTS_TODO:
        raise ValueError(f'布局 {name} 尚未实现（当前可用：{available}）')
    raise ValueError(f'未知布局 {name}（可用：{available}；规划中：{", ".join(LAYOUTS_TODO)}）')


def resolve_layout(name: str, variant: str = '') -> tuple:
    """把布局名解析成目标列表。"""
    return LAYOUT_BUILDERS[normalize_layout(name, variant)]()


def list_all_windows() -> None:
    """列出全部已知 MuMu 窗口的当前可见边框，不唤醒最小化的窗口。"""
    for title in WINDOW_TITLES:
        handles = find_windows(title)
        if not handles:
            logger.info(f'[{title}] 未运行')
            continue
        for hwnd in handles:
            logger.info(f'[{title}] hwnd={hwnd} 当前 {get_frame_rect(hwnd)}')


def wait_window_ready(title: str, timeout: float = WINDOW_READY_TIMEOUT) -> bool:
    """等窗口出现，且可见边框连续 WINDOW_STABLE_SECONDS 不再变化。

    模拟器启动后窗口先出现在默认位置，再被它自己改一次大小；此时 SetWindowPos
    的结果会被覆盖，所以必须等矩形稳定下来再摆。
    """
    deadline = time.time() + timeout
    last = None
    stable_since = 0.0
    while time.time() < deadline:
        handles = find_windows(title)
        if not handles:
            last, stable_since = None, 0.0
        else:
            ensure_restored(handles[0])
            current = get_frame_rect(handles[0])
            if current == last:
                if stable_since and time.time() - stable_since >= WINDOW_STABLE_SECONDS:
                    return True
            else:
                last, stable_since = current, time.time()
        time.sleep(WINDOW_READY_INTERVAL)
    logger.warning(f'[{title}] 等待窗口稳定超时（{timeout:g}s），仍继续尝试摆放')
    return False


def booting_indexes(manager_exe: str, indexes: tuple) -> tuple:
    """在 indexes 里找出还没进入 start_finished 的实例；查不到就当已就绪。"""
    if not manager_exe:
        return ()
    try:
        instances = manager_instances(manager_exe)
    except RuntimeError:
        return ()
    return tuple(i for i in indexes
                 if instances.get(i, {}).get('player_state') != 'start_finished')


def prepare_windows(layout: str, manager_exe: str = '', start: bool = True,
                    timeout: float = WINDOW_READY_TIMEOUT,
                    wait_indexes: tuple = ()) -> None:
    """把布局需要的实例补齐：没运行的启动，并等还没就绪的窗口稳定下来。

    只等新启动、仍在开机（wait_indexes 传入刚 restart 的实例）的窗口——已经
    跑稳的窗口再等只是白花时间。
    """
    indexes = LAYOUT_INDEXES[layout]
    missing = tuple(i for i in indexes if not find_windows(WINDOW_TITLES[i]))
    if missing and not start:
        logger.info(f'实例 {list(missing)} 未运行，按 --no-start 跳过启动与等待')
        indexes = tuple(i for i in indexes if i not in missing)
        missing = ()
    if missing:
        if not start_instances(find_manager(manager_exe), missing, timeout):
            raise RuntimeError(f'实例 {list(missing)} 未能全部启动')
    pending = dict.fromkeys(missing + wait_indexes
                            + booting_indexes(manager_exe, indexes))
    for index in pending:
        wait_window_ready(WINDOW_TITLES[index])


def place_layout(name: str, variant: str, dry_run: bool, manager_exe: str = '',
                 start: bool = True, timeout: float = WINDOW_READY_TIMEOUT,
                 wait_indexes: tuple = ()) -> bool:
    """按布局摆放全部窗口，返回是否全部到位。

    默认会先把该布局需要、但还没出现的窗口启动起来，等窗口稳定后再摆；
    刚启动时窗口会先出现在默认位置再自己改一次大小，摆早了会被覆盖。
    """
    layout = normalize_layout(name, variant)
    if not dry_run:
        prepare_windows(layout, manager_exe, start, timeout, wait_indexes)
    targets = resolve_layout(layout)
    all_ok = True
    for title, x, y, width, height in targets:
        if not process_target(title, (x, y, width, height), dry_run):
            all_ok = False
    return all_ok


def find_manager(explicit: str = '') -> str:
    """定位 MuMuManager.exe：显式指定 > 注册表安装目录 > 运行中的主程序 > 常见路径。"""
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError(f'指定的 MuMuManager.exe 不存在：{explicit}')
        return explicit

    import winreg
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMuPlayer'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MuMuPlayer'),
        (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMuPlayer'),
    )
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as reg:
                location = winreg.QueryValueEx(reg, 'InstallLocation')[0]
        except OSError:
            continue
        for sub in ('nx_main', 'shell'):
            exe = os.path.join(location, sub, 'MuMuManager.exe')
            if os.path.exists(exe):
                return exe

    try:
        import psutil
        for proc in psutil.process_iter(['name', 'exe']):
            if proc.info['name'] in ('MuMuNxMain.exe', 'MuMuPlayer.exe'):
                exe = os.path.join(os.path.dirname(proc.info['exe']), 'MuMuManager.exe')
                if os.path.exists(exe):
                    return exe
    except Exception:
        pass

    for exe in MANAGER_FALLBACKS:
        if os.path.exists(exe):
            return exe
    raise FileNotFoundError('找不到 MuMuManager.exe，请用 --manager 指定路径')


def manager(manager_exe: str, *args: str) -> dict:
    """调用 MuMuManager 并把 JSON 输出解析成 dict；失败抛 RuntimeError。"""
    try:
        result = subprocess.run([manager_exe, *args], capture_output=True,
                                timeout=MANAGER_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError(f'找不到 MuMuManager.exe：{manager_exe}')
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'MuMuManager {" ".join(args)} 超过 {MANAGER_TIMEOUT}s 无响应')
    stdout = result.stdout.decode('utf-8', errors='replace').strip()
    if result.returncode != 0 or not stdout:
        stderr = result.stderr.decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'MuMuManager {" ".join(args)} 失败：{stderr or stdout or result.returncode}')
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f'MuMuManager {" ".join(args)} 返回非 JSON：{stdout[:200]}')


def manager_instances(manager_exe: str) -> dict:
    """返回 {index: info}，index 为 int，按实例序号排序。"""
    data = manager(manager_exe, 'info', '-v', 'all')
    return {int(key): value for key, value in sorted(data.items(), key=lambda kv: int(kv[0]))}


def check_errors(result: dict) -> None:
    """把 MuMuManager 的 errcode 检查出来。

    单实例返回 {"errcode": 0, "errmsg": ""}，多实例返回
    {"0": {"errcode": 0, ...}, "1": {...}}，两种都要看。
    """
    if result.get('errcode'):
        raise RuntimeError(f'失败：{result.get("errmsg") or result.get("errcode")}')
    for key, value in result.items():
        if isinstance(value, dict) and value.get('errcode'):
            raise RuntimeError(f'实例 {key} 失败：{value.get("errmsg")}')


def wait_started(manager_exe: str, indexes: tuple, timeout: float) -> bool:
    """轮询 info 直到全部实例 player_state 为 start_finished。"""
    deadline = time.time() + timeout
    pending = set(indexes)
    while pending and time.time() < deadline:
        try:
            instances = manager_instances(manager_exe)
        except RuntimeError:
            time.sleep(1)
            continue
        for index in list(pending):
            info = instances.get(index, {})
            state = info.get('player_state', '')
            if state == 'start_finished':
                logger.info(f'实例 {index} 已启动（pid={info.get("pid")}, adb={info.get("adb_port")}）')
                pending.discard(index)
            elif info.get('launch_err_code'):
                raise RuntimeError(f'实例 {index} 启动失败：{info.get("launch_err_msg")}')
        if pending:
            time.sleep(1)
    if pending:
        logger.warning(f'等待超时，仍未启动：{sorted(pending)}')
        return False
    return True


def start_instances(manager_exe: str, indexes: tuple, timeout: float) -> bool:
    """启动指定实例（主程序没开会自动拉起），返回是否全部就绪。"""
    logger.info(f'启动实例 {list(indexes)}')
    result = manager(manager_exe, 'control', '-v', ','.join(str(i) for i in indexes), 'launch')
    check_errors(result)
    return wait_started(manager_exe, indexes, timeout)


def stop_instances(manager_exe: str, indexes: tuple) -> None:
    """关闭指定实例。"""
    logger.info(f'关闭实例 {list(indexes)}')
    check_errors(manager(manager_exe, 'control', '-v', ','.join(str(i) for i in indexes), 'shutdown'))


def restart_instances(manager_exe: str, indexes: tuple, timeout: float) -> bool:
    """重启指定实例并等待就绪。"""
    logger.info(f'重启实例 {list(indexes)}')
    check_errors(manager(manager_exe, 'control', '-v', ','.join(str(i) for i in indexes), 'restart'))
    return wait_started(manager_exe, indexes, timeout)


def running_indexes(manager_exe: str) -> tuple:
    """当前已启动的实例序号。"""
    instances = manager_instances(manager_exe)
    return tuple(index for index, info in instances.items() if info.get('is_process_started'))


def main() -> int:
    parser = argparse.ArgumentParser(
        description='MuMu 模拟器多开窗口的启动、停止与摆放',
        usage='%(prog)s [动作] [布局] [编号] [选项]',
        epilog='动作：place（默认）/ start / stop / restart / list / help')
    parser.add_argument('words', nargs='*', metavar='动作 布局 编号',
                        help='动作名可省略，省略时第一个参数按布局名处理')
    parser.add_argument('--dry-run', action='store_true', help='只报告差异，不真正调整')
    parser.add_argument('--list', action='store_true', help='只列出当前位置大小')
    parser.add_argument('--manager', default='', help='MuMuManager.exe 路径，默认自动查找')
    parser.add_argument('--timeout', type=float, default=90, help='等待实例启动的秒数，默认 90')
    parser.add_argument('--no-place', action='store_true', help='start/restart 后不自动摆放')
    parser.add_argument('--no-start', action='store_true', help='只摆放已在运行的实例，不自动启动')
    args = parser.parse_args()

    set_dpi_aware()

    words = list(args.words)
    action = 'place'
    if words and words[0] in ACTIONS:
        action = words.pop(0)
    if len(words) > 2:
        parser.error(f'多余的参数：{words[2:]}')
    layout_name = words[0] if words else DEFAULT_LAYOUT
    variant = words[1] if len(words) > 1 else ''

    if action == 'help':
        parser.print_help()
        return 0
    if action == 'list' or args.list:
        list_all_windows()
        return 0

    try:
        layout = normalize_layout(layout_name, variant)
    except ValueError as e:
        logger.error(str(e))
        return 2

    need_manager = action != 'place' or not args.no_start
    manager_exe = ''
    if need_manager:
        try:
            manager_exe = find_manager(args.manager)
        except FileNotFoundError as e:
            logger.error(str(e))
            return 2

    indexes = LAYOUT_INDEXES[layout]
    try:
        if action == 'stop':
            # 只关当前布局涉及、且确实在运行的实例，别把别的布局/账号一并关掉
            running = running_indexes(manager_exe)
            targets = tuple(i for i in indexes if i in running)
            if not targets:
                logger.info(f'布局 {layout} 的实例 {list(indexes)} 都没在运行，无需关闭')
                return 0
            stop_instances(manager_exe, targets)
            logger.info('实例已关闭')
            return 0

        if action in ('start', 'restart'):
            if action == 'start':
                started = start_instances(manager_exe, indexes, args.timeout)
            else:
                started = restart_instances(manager_exe, indexes, args.timeout)
            if not started:
                return 1
            if args.no_place:
                return 0

        all_ok = place_layout(layout, variant, args.dry_run, manager_exe,
                              start=not args.no_start, timeout=args.timeout,
                              wait_indexes=indexes if action in ('start', 'restart') else ())
    except RuntimeError as e:
        logger.error(str(e))
        return 1

    if args.dry_run:
        return 0
    logger.info('全部窗口已符合目标位置与大小' if all_ok else '部分窗口调整未生效，请重试')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
