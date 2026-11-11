# -*- coding: utf-8 -*-
"""
一键调度脚本（御魂1 / 御魂2）

执行流程：
  1. 检查各配置运行状态：已在运行的实例跳过停止/重启，未运行的先停止保证干净起点
  2. 启用 Exploration（探索），next_run 设为当前时间前 5 分钟 -> 立即执行
  3. 启用 RealmRaid（个人突破），next_run 设为当前时间后 1 分钟 -> 紧随其后
  4. 仅启动未运行的实例；运行中的实例保持不动，新调度靠配置热加载在任务间隙生效

为什么启停走 WebSocket 而不是 REST：
  module/server/script_router.py 的 REST 版 /{name}/start、/{name}/stop
  调用 async 方法时漏了 await，实际不会执行（OASX 走的是 WebSocket，故官方界面正常）。
  WebSocket /ws/{配置名} 收到 "start"/"stop" 文本后会正确 await 执行。

前置条件：
  OAS 后端已在运行（用 `oas start` 启动，默认 http://127.0.0.1:22288）
  依赖 websockets 库；项目自带的 .venv\\Scripts\\python.exe 已包含，
  用其他解释器且缺少该库时，本脚本会自动用 .venv 解释器重新执行自己。

用法：
  python quick_start_exploration.py                # 启动完即退出（默认）
  python quick_start_exploration.py --watch 60     # 需要时观察 60 秒日志
  python quick_start_exploration.py --port 22288 御魂1 御魂2
注意：脚本退出（含 Ctrl+C）只会断开观察连接，不会停止已经启动的脚本任务。
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

# ----------------------- 可按需修改的默认参数 -----------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 22288
DEFAULT_CONFIGS = ["御魂1", "御魂2"]   # config/ 下的 json 文件名（不带后缀）

EXPLORATION_TASK = "Exploration"     # 探索
REALM_RAID_TASK = "RealmRaid"        # 个人突破（个人结界突破）
EXPLORATION_LEAD = timedelta(minutes=5)  # 探索：当前时间提前量（设为过去 -> 立即执行）
RAID_AFTER = timedelta(minutes=1)        # 个人突破：当前时间延后量
TIMEOUT = 10
# 与 module/server/script_process.py 的 ScriptState 对应：1 = RUNNING
STATE_RUNNING = 1
# --------------------------------------------------------------------


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


def set_arg(base: str, config_name: str, task: str, group: str,
            argument: str, types: str, value) -> None:
    """通用设值接口，经 pydantic 校验后写入 config/{config_name}.json。"""
    query = urlencode({"types": types, "value": str(value)})
    url = (f"{base}/{quote(config_name)}/{quote(task)}/{quote(group)}/"
           f"{quote(argument)}/value?{query}")
    try:
        result = _request("PUT", url)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    if result.lower() != "true":
        raise RuntimeError(f"接口返回非 true：{result}")
    print(f"  - {config_name} {task}.{group}.{argument} = {value}")


def configure(base: str, config_name: str, run_now: datetime,
              run_raid: datetime) -> None:
    # 必须先 enable，否则 next_run 再早也不会被调度（禁用任务直接被排除）
    set_arg(base, config_name, EXPLORATION_TASK, "scheduler",
            "enable", "boolean", "true")
    set_arg(base, config_name, EXPLORATION_TASK, "scheduler",
            "next_run", "date_time", run_now.strftime("%Y-%m-%d %H:%M:%S"))
    set_arg(base, config_name, REALM_RAID_TASK, "scheduler",
            "enable", "boolean", "true")
    set_arg(base, config_name, REALM_RAID_TASK, "scheduler",
            "next_run", "date_time", run_raid.strftime("%Y-%m-%d %H:%M:%S"))


async def _open_ws(host: str, port: int, config_name: str):
    uri = f"ws://{host}:{port}/ws/{quote(config_name, safe='')}"
    # 子进程日志是持续推送的，关闭帧可能只有 close，给较长超时即可
    ws = await websockets.connect(uri, max_size=None, open_timeout=10)
    return ws


async def _drain(ws, seconds: float, tag: str, echo: bool = False) -> None:
    """在 seconds 时间内尽量读取服务端推送，避免缓冲堆积。"""
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.05, deadline - asyncio.get_event_loop().time()))
        except asyncio.TimeoutError:
            break
        except websockets.ConnectionClosed:
            print(f"[{tag}] 连接已关闭")
            return
        if echo:
            _print_frame(tag, msg)


def _print_frame(tag: str, msg) -> None:
    text = msg if isinstance(msg, str) else str(msg)
    for line in text.splitlines():
        print(f"[{tag}] {line}")


async def connect_and_state(host: str, port: int, name: str):
    """连接 WebSocket 并读取服务端首帧推送的运行状态。

    服务端在连接建立后会先推送 {"state": <int>}（见 script_router.py），
    之后才是 schedule JSON 和纯文本日志。

    Returns:
        (ws, state): state 为 1 表示运行中；读取失败时为 None（按未运行处理）。
    """
    ws = await _open_ws(host, port, name)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 3
    state = None
    while state is None and loop.time() < deadline:
        try:
            msg = await asyncio.wait_for(
                ws.recv(), timeout=max(0.1, deadline - loop.time()))
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            break
        try:
            data = json.loads(msg)
        except (ValueError, TypeError):
            continue  # 纯文本日志帧，跳过
        if isinstance(data, dict) and "state" in data:
            state = data["state"]
    return ws, state


async def start_and_watch(ws, tag: str, watch: float) -> None:
    await ws.send("start")
    print(f"  - 已发送启动指令 {tag}")
    if watch <= 0:
        # 等待服务端确认接收并开始拉起子进程，随后即可断开
        await asyncio.sleep(1)
        return
    deadline = asyncio.get_event_loop().time() + watch
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(
                ws.recv(), timeout=min(5, deadline - asyncio.get_event_loop().time()))
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            print(f"[{tag}] 观察连接已关闭（任务本身不受影响）")
            return
        _print_frame(tag, msg)


async def amain(args, run_now: datetime, run_raid: datetime) -> None:
    base = f"http://{args.host}:{args.port}"

    print("[1/4] 检查 OAS 服务...")
    health_check(base)

    print("[2/4] 检查各配置运行状态（运行中的实例跳过重启）...")
    sockets: dict = {}
    running: dict = {}
    for name in args.configs:
        ws, state = await connect_and_state(args.host, args.port, name)
        sockets[name] = ws
        if state == STATE_RUNNING:
            running[name] = True
            print(f"  - {name} 已在运行 (state={state})，跳过停止/重启")
        else:
            # state 为 0(未运行)/2(异常退出)/None(状态未知)：统一先发 stop 保证干净起点
            running[name] = False
            await ws.send("stop")
            print(f"  - {name} 未运行 (state={state})，发送停止以保证干净起点")
            await _drain(ws, 2.0, name)

    print("[3/4] 启用任务并设置时间（写盘）...")
    failed = []
    for name in args.configs:
        print(f"  配置 {name}:")
        try:
            configure(base, name, run_now, run_raid)
        except Exception as e:
            print(f"  [失败] {name}: {e}")
            failed.append(name)
    if failed:
        for ws in sockets.values():
            await ws.close()
        print(f"\n[中止] 以下配置设置失败，未执行启动：{', '.join(failed)}")
        sys.exit(1)

    to_start = [n for n in args.configs if not running[n]]
    if args.watch > 0 and to_start:
        print(f"[4/4] 启动 {len(to_start)} 个未运行实例并观察 {int(args.watch)} 秒"
              f"（Ctrl+C 可提前退出，不影响任务）...")
    else:
        print("[4/4] 启动未运行的脚本进程（运行中的实例保持不动，启动完即退出）...")
    try:
        if to_start:
            await asyncio.gather(*(start_and_watch(sockets[n], n, args.watch)
                                   for n in to_start))
        for n in args.configs:
            if running[n]:
                print(f"  - {n} 保持运行；新调度将在任务间隙热加载（通常 5 秒内）")
    finally:
        for ws in sockets.values():
            try:
                await ws.close()
            except Exception:
                pass
    print("\n完成。任务已在 OAS 服务端运行，可用 OASX / Web / 日志查看状态。")


def main() -> None:
    parser = argparse.ArgumentParser(description="一键安排探索+个人突破并启动 oas（WebSocket 启停）")
    parser.add_argument("configs", nargs="*", default=DEFAULT_CONFIGS,
                        help="配置名，默认 御魂1 御魂2")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--watch", type=float, default=0,
                        help="启动后观察日志的秒数，默认 0（启动完即退出）；需要观察时例如 --watch 60")
    args = parser.parse_args()

    now = datetime.now().replace(microsecond=0)
    run_now = now - EXPLORATION_LEAD    # 探索：5 分钟前（立即执行）
    run_raid = now + RAID_AFTER         # 个人突破：1 分钟后

    print(f"OAS 服务：http://{args.host}:{args.port}  (WebSocket 同端口)")
    print(f"目标配置：{', '.join(args.configs)}")
    print(f"探索(Exploration) 执行时间：{run_now}（当前时间前 5 分钟，立即）")
    print(f"个人突破(RealmRaid) 执行时间：{run_raid}（当前时间后 1 分钟）")
    print()

    try:
        asyncio.run(amain(args, run_now, run_raid))
    except KeyboardInterrupt:
        print("\n已退出观察（任务不会被停止）。")


if __name__ == "__main__":
    main()
