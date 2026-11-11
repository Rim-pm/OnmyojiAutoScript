# -*- coding: utf-8 -*-
"""
OAS 实例控制工具 —— oasc 实例控制命令（stopall/stop/start/disable/enable/solo-explore/solo-raid/solo-climb/runtask）的实现

与 quick_start_exploration.py 同一套路：
  - 启停必须走 WebSocket /ws/{配置名}（REST 版 /start、/stop 漏了 await，实际不生效）
  - 配置修改走 REST PUT /{配置名}/{任务}/scheduler/{参数}/value（与 WebUI 同接口，立即写盘并热加载）；
    任务名直接用 config json 里的下划线形式即可（script_set_arg 对下划线输入原样返回）
  - 任务启用状态以 config/{配置名}.json 的 scheduler.enable 为唯一事实来源，直接读盘

命令：
  stopall                        停止全部运行中的脚本实例（后端保留）
  stop <配置名>...               停止指定实例
  start <配置名>...              启动指定实例（未运行或异常状态可启动，按其现有调度表运行）
  disable <配置名>...            禁用实例全部任务（先存快照），运行中则一并停止
  enable <配置名>...             按快照恢复实例的启用任务（不自动启动，不恢复 next_run）
  run-task <任务名> [配置名...]  独占执行：禁用其余任务 -> 启用目标任务并立即执行 -> 重启实例
  solo-explore [配置名...]       = run-task Exploration（默认 御魂1 御魂2）
  solo-raid [配置名...]          = run-task RealmRaid（默认 御魂1 御魂2）
  solo-climb [配置名...]         = run-task ActivityShikigami（默认 御魂1 御魂2，当期式神爬塔）
  solo-evo [配置名...]           = run-task EvoZone（默认 御魂1 御魂2 御魂3，三实例组队）

注意：探索/突破/爬塔历来只跑两个号，DEFAULT_CONFIGS 保持两个；EvoZone 是
1 队长 + 2 队员的组队任务，必须显式覆盖三个实例，见 nakk/evo_zone_control.py。

快照：nakk/state/disable_snapshot.json（不入库）。多次 disable 按 union 合并，
不会丢最早的启用状态；enable 恢复全部成功后才移除对应条目。

用法示例：
  python nakk/oas_control.py stopall
  python nakk/oas_control.py --port 22288 disable 御魂1
  python nakk/oas_control.py solo-explore 御魂1 --dry-run
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# 缺少 websockets 时，改用项目自带的 .venv 解释器重新执行本脚本
try:
    import websockets  # noqa: F401
except ImportError:
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists() and Path(sys.executable).resolve() != venv_py.resolve():
        import subprocess
        sys.exit(subprocess.call([str(venv_py), str(Path(__file__).resolve())] + sys.argv[1:]))
    print("[错误] 缺少 websockets 库，请用项目自带的 .venv\\Scripts\\python.exe 运行本脚本")
    sys.exit(1)

import websockets

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_FILE = ROOT / "nakk" / "state" / "disable_snapshot.json"
DEFAULT_CONFIGS = ["御魂1", "御魂2"]
EVO_ZONE_TASK = "evo_zone"                      # 觉醒副本（麒麟组队）
EVO_CONFIGS = ["御魂1", "御魂2", "御魂3"]       # 1 队长 + 2 队员，见 evo_zone_control.py
EXPLORATION_TASK = "Exploration"   # 探索
REALM_RAID_TASK = "RealmRaid"      # 个人突破（个人结界突破）
ACTIVITY_SHIKIGAMI_TASK = "ActivityShikigami"  # 当期式神爬塔（活动爬塔）
IMMEDIATE_LEAD = timedelta(minutes=5)  # next_run 设为当前时间前 5 分钟 -> 立即执行
TIMEOUT = 10

# 与 module/server/script_process.py 的 ScriptState 对应
STATE_NAMES = {0: "未运行", 1: "运行中", 2: "异常", 3: "更新中"}


# ------------------------------- HTTP 基础 -------------------------------

def _request(method: str, url: str) -> str:
    req = Request(url=url, method=method)
    with urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8").strip()


def health_check(base: str) -> None:
    try:
        _request("GET", f"{base}/test")
    except (URLError, HTTPError) as e:
        print(f"[错误] 无法连接 OAS 服务 {base}：{e}")
        print("       请先运行 oas start 启动 OAS 后端")
        sys.exit(1)


def list_configs(base: str) -> list:
    return json.loads(_request("GET", f"{base}/config_list"))


def set_arg(base: str, config_name: str, task: str, group: str,
            argument: str, types: str, value) -> bool:
    """通用设值接口，经 pydantic 校验后写入 config/{config_name}.json。"""
    query = urlencode({"types": types, "value": str(value)})
    url = (f"{base}/{quote(config_name, safe='')}/{quote(task, safe='')}/"
           f"{quote(group, safe='')}/{quote(argument, safe='')}/value?{query}")
    try:
        result = _request("PUT", url)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:200]
        print(f"    [失败] {config_name} {task}.{group}.{argument}: HTTP {e.code} {detail}")
        return False
    except URLError as e:
        print(f"    [失败] {config_name} {task}.{group}.{argument}: {e}")
        return False
    if result.lower() != "true":
        print(f"    [失败] {config_name} {task}.{group}.{argument}: 接口返回非 true（{result}）")
        return False
    return True


def underscore(name: str) -> str:
    """大驼峰 -> 下划线，复刻 module/config/utils.convert_to_underscore（下划线输入原样返回）。"""
    if "_" in name:
        return name
    name = name.replace(" ", "")
    result = ""
    for i, char in enumerate(name):
        if char.isupper():
            if i > 0 and (name[i - 1].islower() or (i < len(name) - 1 and name[i + 1].islower())):
                result += "_"
            result += char.lower()
        elif char.isdigit():
            if i > 0 and (name[i - 1].isalpha() or (i < len(name) - 1 and name[i + 1].isalpha())):
                result += "_"
            result += char
        else:
            result += char
    return result


# ------------------------------- 配置读取 -------------------------------

def config_path(config_name: str) -> Path:
    return ROOT / "config" / f"{config_name}.json"


def load_config(config_name: str) -> dict:
    with open(config_path(config_name), encoding="utf-8") as f:
        return json.load(f)


def scheduler_tasks(config_name: str) -> list:
    """配置里带 scheduler 的任务键（下划线形式，按 json 顺序）。"""
    data = load_config(config_name)
    return [k for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("scheduler"), dict)]


def enabled_tasks(config_name: str) -> list:
    data = load_config(config_name)
    return [k for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("scheduler"), dict)
            and v["scheduler"].get("enable") is True]


# ------------------------------- 快照 -------------------------------

def load_snapshot() -> dict:
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_snapshot(snap: dict) -> None:
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    tmp.replace(SNAPSHOT_FILE)


def snapshot_add(config_name: str, tasks: list) -> None:
    """按 union 合并：多次 disable 不会丢最早的启用状态；空列表不动快照。"""
    if not tasks:
        return
    snap = load_snapshot()
    old = snap.get(config_name, [])
    merged = list(dict.fromkeys(old + tasks))
    if merged != old:
        snap[config_name] = merged
        save_snapshot(snap)


def snapshot_remove(config_name: str) -> None:
    snap = load_snapshot()
    if config_name in snap:
        del snap[config_name]
        save_snapshot(snap)


# ------------------------------- WebSocket 启停 -------------------------------

async def _ws_connect(host: str, port: int, config_name: str):
    uri = f"ws://{host}:{port}/ws/{quote(config_name, safe='')}"
    return await websockets.connect(uri, max_size=None, open_timeout=10)


async def _recv_state(ws, deadline):
    """持续收帧（跳过日志/调度文本）直到出现 state 帧或超时；返回 int 或 None。"""
    loop = asyncio.get_event_loop()
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            return None
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remain)
        except asyncio.TimeoutError:
            return None
        except websockets.ConnectionClosed:
            return None
        try:
            data = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and "state" in data:
            return int(data["state"])


async def _close_quietly(ws) -> None:
    try:
        await ws.close()
    except Exception:
        pass


async def ws_get_state(host: str, port: int, config_name: str):
    try:
        ws = await _ws_connect(host, port, config_name)
    except Exception as e:
        print(f"  [{config_name}] 连接失败：{e}")
        return None
    try:
        return await _recv_state(ws, asyncio.get_event_loop().time() + 5)
    finally:
        await _close_quietly(ws)


async def ws_send_command(host: str, port: int, config_name: str,
                          command: str, want: int, timeout: float,
                          guard: set = None):
    """连接 -> 读当前状态 -> 发送指令 -> 等待状态进入 want。返回 (前状态, 后状态)。

    guard 不为 None 时，仅当前状态在集合内才发送指令；
    不满足时指令不发出，直接返回 (before, before)。
    """
    try:
        ws = await _ws_connect(host, port, config_name)
    except Exception as e:
        print(f"  [{config_name}] 连接失败：{e}")
        return None, None
    try:
        loop = asyncio.get_event_loop()
        before = await _recv_state(ws, loop.time() + 5)
        if before is None:
            return None, None
        if guard is not None and before not in guard:
            return before, before
        await ws.send(command)
        after = before
        deadline = loop.time() + timeout
        while after != want and loop.time() < deadline:
            state = await _recv_state(ws, deadline)
            if state is None:
                break
            after = state
        return before, after
    finally:
        await _close_quietly(ws)


async def stop_one(host: str, port: int, config_name: str) -> bool:
    before, after = await ws_send_command(host, port, config_name, "stop", 0, 8)
    label = pad(config_name)
    if before is None:
        print(f"  {label} 状态未知，未确认停止")
        return False
    if before == 0:
        print(f"  {label} 未运行，跳过")
        return True
    if after == 0:
        print(f"  {label} 已停止（原状态：{STATE_NAMES.get(before, before)}）")
        return True
    print(f"  {label} 停止指令已发送，当前状态：{STATE_NAMES.get(after, after)}（请 oasc status 复查）")
    return False


async def start_one(host: str, port: int, config_name: str) -> bool:
    # RUNNING(1) 时后端 start() 对旧进程的清理有缺陷（未 await 的 stop），拒绝；
    # WARNING(2) 时脚本子进程已崩溃退出，后端直接拉起新进程是安全的，允许启动
    before, after = await ws_send_command(host, port, config_name, "start", 1, 20,
                                          guard={0, 2})
    label = pad(config_name)
    if before is None:
        print(f"  {label} 状态未知，未发送启动指令")
        return False
    if before == 1:
        print(f"  {label} 已在运行，跳过")
        return True
    if before not in (0, 2):
        print(f"  {label} 状态为 {STATE_NAMES.get(before, before)}，拒绝启动（请在 WebUI 确认后处理）")
        return False
    if after == 1:
        print(f"  {label} 已启动" + ("（原状态：异常）" if before == 2 else ""))
        return True
    print(f"  {label} 启动指令已发送，当前状态：{STATE_NAMES.get(after, after)}（可能仍在拉起，请 oasc status 复查）")
    return False


# ------------------------------- 输出辅助 -------------------------------

def pad(name: str, width: int = 12) -> str:
    length = sum(2 if ord(ch) > 0x2E7F else 1 for ch in name)
    return name + " " * max(0, width - length)


def state_label(state) -> str:
    if state is None:
        return "状态未知"
    return STATE_NAMES.get(state, f"state={state}")


# ------------------------------- 子命令 -------------------------------

async def cmd_stop(args, host: str, port: int) -> int:
    base = f"http://{host}:{port}"
    all_configs = list_configs(base)
    ok = True
    for name in args.configs:
        if name not in all_configs:
            print(f"  [失败] 配置不存在：{name}（现有：{'、'.join(all_configs)}）")
            ok = False
            continue
        if not await stop_one(host, port, name):
            ok = False
    return 0 if ok else 1


async def cmd_stopall(args, host: str, port: int) -> int:
    base = f"http://{host}:{port}"
    configs = list_configs(base)
    print(f"停止全部运行中的脚本实例（后端 {base} 保留）：")
    ok = True
    for name in configs:
        if not await stop_one(host, port, name):
            ok = False
    return 0 if ok else 1


async def cmd_start(args, host: str, port: int) -> int:
    base = f"http://{host}:{port}"
    all_configs = list_configs(base)
    ok = True
    for name in args.configs:
        if name not in all_configs:
            print(f"  [失败] 配置不存在：{name}（现有：{'、'.join(all_configs)}）")
            ok = False
            continue
        if not await start_one(host, port, name):
            ok = False
    return 0 if ok else 1


async def cmd_disable(args, host: str, port: int) -> int:
    base = f"http://{host}:{port}"
    all_configs = list_configs(base)
    ok = True
    for name in args.configs:
        if name not in all_configs:
            print(f"  [失败] 配置不存在：{name}（现有：{'、'.join(all_configs)}）")
            ok = False
            continue
        enabled = enabled_tasks(name)
        print(f"配置 {name}：启用中任务 {len(enabled)} 个"
              f"{'：' + '、'.join(enabled) if enabled else ''}")
        if args.dry_run:
            print("  [dry-run] 未做任何修改")
            continue
        # 快照先落盘（union 合并），再做禁用；停实例避免调度继续写盘
        snapshot_add(name, enabled)
        if not await stop_one(host, port, name):
            ok = False
        if not enabled:
            print(f"  没有需要禁用的任务；快照保持不变（可用 oasc enable {name} 恢复）")
            continue
        failed = [t for t in enabled
                  if not set_arg(base, name, t, "scheduler", "enable", "boolean", "false")]
        if failed:
            print(f"  [失败] {len(failed)} 个任务禁用失败：{'、'.join(failed)}")
            ok = False
        else:
            print(f"  已禁用 {len(enabled)} 个任务；快照已保存，可用 oasc enable {name} 恢复")
    return 0 if ok else 1


async def cmd_enable(args, host: str, port: int) -> int:
    base = f"http://{host}:{port}"
    ok = True
    for name in args.configs:
        tasks = load_snapshot().get(name)
        if tasks is None:
            print(f"配置 {name}：无快照可恢复")
            continue
        print(f"配置 {name}：快照中有 {len(tasks)} 个启用任务：{'、'.join(tasks)}")
        if args.dry_run:
            print("  [dry-run] 未做任何修改")
            continue
        state = await ws_get_state(host, port, name)
        if state == 1:
            print("  [注意] 实例正在运行，恢复的任务若 next_run 已过期会立即执行")
        failed = [t for t in tasks
                  if not set_arg(base, name, t, "scheduler", "enable", "boolean", "true")]
        if failed:
            snap = load_snapshot()
            snap[name] = failed
            save_snapshot(snap)
            print(f"  [失败] {len(failed)} 个任务恢复失败（保留在快照中）：{'、'.join(failed)}")
            ok = False
        else:
            snapshot_remove(name)
            print(f"  已恢复 {len(tasks)} 个任务并移除快照；实例未自动启动"
                  f"（oasc start {name} 或 WebUI 启动；next_run 未恢复，过期任务会立即执行）")
    return 0 if ok else 1


async def cmd_run_task(args, host: str, port: int, task_raw: str,
                       default_configs: list = None) -> int:
    base = f"http://{host}:{port}"
    task = underscore(task_raw.strip())
    configs = args.configs or (default_configs or DEFAULT_CONFIGS)
    ok = True
    for name in configs:
        avail = scheduler_tasks(name)
        if task not in avail:
            print(f"[失败] 配置 {name} 没有任务 {task}。可用任务：")
            for i in range(0, len(avail), 6):
                print("    " + "  ".join(pad(t, 22) for t in avail[i:i + 6]))
            ok = False
            continue
        enabled = enabled_tasks(name)
        others = [t for t in enabled if t != task]
        run_at = (datetime.now() - IMMEDIATE_LEAD).replace(microsecond=0)
        print(f"配置 {name}：独占执行 {task}")
        print(f"    将禁用 {len(others)} 个任务：{'、'.join(others) if others else '（无其他启用任务）'}")
        print(f"    {task} 置为启用，next_run = {run_at}（立即执行），随后重启实例")
        if args.dry_run:
            print("  [dry-run] 未做任何修改")
            continue
        # 快照先落盘（union 合并，记录本次禁用前的完整启用状态）
        snapshot_add(name, enabled)
        state = await ws_get_state(host, port, name)
        if state == 1 and not await stop_one(host, port, name):
            print("  [失败] 实例停止失败，放弃本轮修改")
            ok = False
            continue
        failed = []
        for t in others:
            if not set_arg(base, name, t, "scheduler", "enable", "boolean", "false"):
                failed.append(t)
        if not set_arg(base, name, task, "scheduler", "enable", "boolean", "true"):
            failed.append(task)
        if not set_arg(base, name, task, "scheduler", "next_run", "date_time",
                       run_at.strftime("%Y-%m-%d %H:%M:%S")):
            failed.append(task + ".next_run")
        if failed:
            print(f"  [失败] 以下设置未成功（快照未受影响，可 oasc enable {name} 恢复）：{'、'.join(failed)}")
            ok = False
            continue
        if not await start_one(host, port, name):
            ok = False
            continue
        print(f"    完成：仅 {task} 启用并已启动；其余 {len(others)} 个任务已禁用"
              f"（oasc enable {name} 可恢复）")
    return 0 if ok else 1


# ------------------------------- 入口 -------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OAS 实例控制：停止/启动实例、清空调度表、独占执行单个任务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=22288)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stopall", help="停止全部运行中的脚本实例")
    p = sub.add_parser("stop", help="停止指定实例")
    p.add_argument("configs", nargs="+", help="配置名（config/ 下的 json 文件名）")
    p = sub.add_parser("start", help="启动指定实例（仅未运行的实例）")
    p.add_argument("configs", nargs="+", help="配置名")
    p = sub.add_parser("disable", help="禁用实例全部任务并存快照")
    p.add_argument("configs", nargs="+", help="配置名")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")
    p = sub.add_parser("enable", help="按快照恢复实例的启用任务")
    p.add_argument("configs", nargs="+", help="配置名")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")
    p = sub.add_parser("run-task", help="独占执行指定任务")
    p.add_argument("task", help="任务名（如 Exploration、RealmRaid、orochi）")
    p.add_argument("configs", nargs="*", help="配置名，默认 " + " ".join(DEFAULT_CONFIGS))
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")
    p = sub.add_parser("solo-explore", help=f"= run-task {EXPLORATION_TASK}（独占执行探索）")
    p.add_argument("configs", nargs="*", help="配置名，默认 " + " ".join(DEFAULT_CONFIGS))
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")
    p = sub.add_parser("solo-raid", help=f"= run-task {REALM_RAID_TASK}（独占执行个人突破）")
    p.add_argument("configs", nargs="*", help="配置名，默认 " + " ".join(DEFAULT_CONFIGS))
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")
    p = sub.add_parser("solo-climb",
                       help=f"= run-task {ACTIVITY_SHIKIGAMI_TASK}（当期式神爬塔，默认 " + " ".join(DEFAULT_CONFIGS) + "）")
    p.add_argument("configs", nargs="*", help="配置名，默认 " + " ".join(DEFAULT_CONFIGS))
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")
    p = sub.add_parser("solo-evo",
                       help=f"= run-task {EVO_ZONE_TASK}（觉醒副本组队，默认 " + " ".join(EVO_CONFIGS) + "）")
    p.add_argument("configs", nargs="*", help="配置名，默认 " + " ".join(EVO_CONFIGS))
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做修改")

    args = parser.parse_args()
    base = f"http://{args.host}:{args.port}"
    health_check(base)

    async def dispatch() -> int:
        if args.cmd == "stopall":
            return await cmd_stopall(args, args.host, args.port)
        if args.cmd == "stop":
            return await cmd_stop(args, args.host, args.port)
        if args.cmd == "start":
            return await cmd_start(args, args.host, args.port)
        if args.cmd == "disable":
            return await cmd_disable(args, args.host, args.port)
        if args.cmd == "enable":
            return await cmd_enable(args, args.host, args.port)
        if args.cmd == "run-task":
            return await cmd_run_task(args, args.host, args.port, args.task)
        if args.cmd == "solo-explore":
            return await cmd_run_task(args, args.host, args.port, EXPLORATION_TASK)
        if args.cmd == "solo-raid":
            return await cmd_run_task(args, args.host, args.port, REALM_RAID_TASK)
        if args.cmd == "solo-climb":
            return await cmd_run_task(args, args.host, args.port, ACTIVITY_SHIKIGAMI_TASK)
        if args.cmd == "solo-evo":
            return await cmd_run_task(args, args.host, args.port,
                                      EVO_ZONE_TASK, default_configs=EVO_CONFIGS)
        print(f"未知命令：{args.cmd}")
        return 1

    try:
        sys.exit(asyncio.run(dispatch()))
    except KeyboardInterrupt:
        print("\n已中断（对实例的影响以 oasc status 为准）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
