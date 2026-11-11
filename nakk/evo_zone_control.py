# -*- coding: utf-8 -*-
"""
EvoZone（觉醒副本 / 麒麟组队）专属控制 —— `oasc evo <子命令>` 的实现。

组队结构（本项目实际配置，三个实例缺一不可）：
    御魂1 = leader，邀请 2 位好友（invite_number=two）
    御魂2 = member，接受邀请（invite_number=one）
    御魂3 = member，接受邀请（invite_number=one）
因此本脚本所有命令默认覆盖这 3 个实例，且按组队顺序操作：
    start : member 先进入等待 -> leader 最后创建队伍发邀请
    stop  : leader 先停（不再发邀请）-> member 后停

与探索/突破不同：那些历来只跑两个号（oas_control.DEFAULT_CONFIGS），
不要把它们的默认值改成 3 个。EvoZone 的每实例配置与组队协同见 module/team_flow/。

命令：
    status                       只读总览：状态、角色、麒麟、层数、限制、日志进度
    set <键=值>... [--apply]     批量修改三个实例的 EvoZone 参数（默认 dry-run）
    start [--dry-run]            按 member -> leader 顺序启动三个实例
    stop  [--dry-run]            按 leader -> member 顺序停止三个实例
    help                         显示帮助
    （solo 子命令由 oasc.ps1 转交 oas_control.py 的 solo-evo）

复用 oas_control.py 的 HTTP/WS 工具函数；运行时由 .venv 解释器执行。
"""
import argparse
import asyncio
import json
import re
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

# 必须在 import oas_control 之前完成解释器检查：oas_control 顶层也有回退，
# 但它的回退会重跑自己而不是本脚本，导致命令参数丢失。
try:
    import websockets  # noqa: F401
except ImportError:
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists() and Path(sys.executable).resolve() != venv_py.resolve():
        import subprocess
        sys.exit(subprocess.call([str(venv_py), str(Path(__file__).resolve())] + sys.argv[1:]))
    print("[错误] 缺少 websockets 库，请用项目自带的 .venv\\Scripts\\python.exe 运行本脚本")
    sys.exit(1)

from oas_control import (  # noqa: E402
    ROOT, EVO_CONFIGS, STATE_NAMES, health_check, load_config, set_arg,
    ws_get_state, start_one, stop_one, pad, _request,
)

TASK = "evo_zone"
LOG_DIR = ROOT / "log"

LAYERS = ["壹层", "贰层", "叁层", "肆层", "伍层", "陆层", "柒层", "捌层", "玖层", "拾层"]
KIRINS = ["火麒麟", "风麒麟", "水麒麟", "雷麒麟"]
ROLES = ["leader", "member", "alone", "wild"]
INVITES = ["one", "two"]

# 键 => (group, argument, types, 允许的取值或 None)
_FIELDS = {
    "layer":        ("evo_zone_config", "layer", "string", LAYERS),
    "kirin":        ("evo_zone_config", "kirin_type", "string", KIRINS),
    "role":         ("evo_zone_config", "user_status", "string", ROLES),
    "count":        ("evo_zone_config", "limit_count", "integer", None),
    "time":         ("evo_zone_config", "limit_time", "time", None),
    "buff":         ("evo_zone_config", "soul_buff_enable", "boolean", None),
    "invite":       ("invite_config", "invite_number", "string", INVITES),
    "friend1":      ("invite_config", "friend_1", "string", None),
    "friend2":      ("invite_config", "friend_2", "string", None),
}
# 同时接受配置里的真实字段名
FIELDS = dict(_FIELDS)
for _alias, _real in {
    "kirin_type": "kirin", "user_status": "role", "limit_count": "count",
    "limit_time": "time", "soul_buff_enable": "buff",
    "invite_number": "invite", "friend_1": "friend1", "friend_2": "friend2",
}.items():
    FIELDS[_alias] = _FIELDS[_real]

FIELD_HELP = [
    ("layer / kirin / role", "层数 / 麒麟类型 / 身份",
     f"{'/'.join(LAYERS[:3])}…{LAYERS[-1]} | {'/'.join(KIRINS)} | {'/'.join(ROLES)}"),
    ("count / time", "次数上限 / 时限", "整数(1-9999) | HH:MM:SS"),
    ("buff / invite", "觉醒加成 / 邀请人数", "true/false | one/two"),
    ("friend1 / friend2", "好友 1 / 好友 2 名字", "任意字符串（空串用 friend1= ）"),
]


# ------------------------------- 配置 / 日志读取 -------------------------------

def resolve_configs(only) -> list:
    """默认三个组队实例；--only 可重复指定子集。只做存在性校验。"""
    configs = list(only) if only else list(EVO_CONFIGS)
    missing = [n for n in configs if not (ROOT / "config" / f"{n}.json").exists()]
    if missing:
        print(f"[错误] 配置不存在：{'、'.join(missing)}")
        sys.exit(1)
    # 去重但保持顺序
    return list(dict.fromkeys(configs))


def summary(name: str) -> dict:
    data = load_config(name)
    ez = data.get(TASK, {})
    cfg = ez.get("evo_zone_config", {})
    inv = ez.get("invite_config", {})
    sch = ez.get("scheduler", {})
    return {
        "role": cfg.get("user_status", "?"),
        "kirin": cfg.get("kirin_type", "?"),
        "layer": cfg.get("layer", "?"),
        "count": cfg.get("limit_count", "?"),
        "time": cfg.get("limit_time", "?"),
        "buff": cfg.get("soul_buff_enable", "?"),
        "invite": inv.get("invite_number", "?"),
        "enable": sch.get("enable", "?"),
        "next_run": sch.get("next_run", "?"),
    }


def log_progress(name: str):
    """从最新一份该实例的日志尾部取 (当前场次, 最后时间戳, 文件名)。"""
    if not LOG_DIR.exists():
        return None, None, None
    files = sorted(LOG_DIR.glob(f"*_{name}.txt"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, None, None
    path = files[0]
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            tail = deque(f, maxlen=600)
    except OSError:
        return None, None, path.name
    count, last_ts = None, None
    for line in reversed(tail):
        if last_ts is None:
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if m:
                last_ts = m.group(1)
        if count is None:
            c = re.search(r"Current count:\s*(\d+)", line)
            if c:
                count = int(c.group(1))
        if count is not None and last_ts is not None:
            break
    return count, last_ts, path.name


def backend_alive(base: str) -> bool:
    try:
        _request("GET", f"{base}/test")
        return True
    except Exception:
        return False


# ------------------------------- 子命令 -------------------------------

async def cmd_status(args, host: str, port: int, configs: list) -> int:
    base = f"http://{host}:{port}"
    alive = backend_alive(base)
    states = {}
    if alive:
        for name in configs:
            states[name] = await ws_get_state(host, port, name)
    else:
        print(f"[提示] 后端 {base} 未运行，仅展示本地配置（实例状态请用 oas status）。")

    print(f"EvoZone 组队总览（{len(configs)} 个实例，1 队长 + 2 队员）")
    print(f"后端：{base if alive else '未运行'}   任务键：{TASK}")
    print("")
    header = (pad("实例", 12) + pad("状态", 12) + pad("角色", 10) +
              pad("麒麟", 10) + pad("层数", 8) + pad("进度", 12) +
              pad("次数", 8) + pad("时限", 12) + "日志更新")
    print("  " + header)
    counts, newest = [], None
    for name in configs:
        s = summary(name)
        count, last_ts, _ = log_progress(name)
        counts.append(count)
        if last_ts and (newest is None or last_ts > newest):
            newest = last_ts
        state = states.get(name)
        label = "后端未运行" if not alive else STATE_NAMES.get(state, "状态未知")
        progress = f"{count} / {s['count']}" if count is not None else f"— / {s['count']}"
        row = (pad(name, 12) + pad(label, 12) + pad(str(s["role"]), 10) +
               pad(str(s["kirin"]), 10) + pad(str(s["layer"]), 8) +
               pad(progress, 12) + pad(str(s["count"]), 8) +
               pad(str(s["time"]), 12) + (last_ts or "—"))
        print("  " + row)
    print("")
    known = [c for c in counts if c is not None]
    if known:
        if len(set(known)) == 1:
            print(f"  组队同步：三个实例场次一致（{known[0]}）" if len(known) == 3
                  else f"  进度一致（{known[0]}）")
        else:
            print(f"  [注意] 各实例场次不一致：{known}（可能有人掉队）")
    if newest:
        try:
            gap = int((datetime.now() - datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")).total_seconds())
            flag = "  [注意] 超过 3 分钟无新日志，可能卡住" if gap > 180 else ""
            print(f"  最近日志：{newest}（{gap} 秒前）{flag}")
        except ValueError:
            pass
    leaders = [n for n in configs if summary(n)["role"] == "leader"]
    members = [n for n in configs if summary(n)["role"] != "leader"]
    print(f"  角色分工：队长 {'、'.join(leaders) or '无'}；队员 {'、'.join(members) or '无'}")
    enable_str = "、".join(f"{n}={summary(n)['enable']}" for n in configs)
    print(f"  调度启用：{enable_str}")
    return 0


def parse_pairs(pairs: list) -> list:
    """解析 键=值，返回 [(key, group, argument, types, value)]。"""
    result = []
    for raw in pairs:
        if "=" not in raw:
            print(f"[错误] 参数格式应为 键=值，收到：{raw}")
            sys.exit(1)
        key, value = raw.split("=", 1)
        key = key.strip().lower()
        if key not in FIELDS:
            print(f"[错误] 未知参数 {key}。可用：{'、'.join(sorted(set(FIELDS)))}")
            sys.exit(1)
        group, argument, types, allowed = FIELDS[key]
        if allowed is not None and value not in allowed:
            print(f"[错误] {key} 的取值 {value!r} 不在允许范围：{'、'.join(allowed)}")
            sys.exit(1)
        if types == "integer":
            try:
                value = str(int(value))
            except ValueError:
                print(f"[错误] {key} 需要整数，收到：{value!r}")
                sys.exit(1)
            if not 1 <= int(value) <= 9999:
                print(f"[错误] {key} 需要在 1-9999 之间")
                sys.exit(1)
        elif types == "time":
            try:
                datetime.strptime(value, "%H:%M:%S")
            except ValueError:
                print(f"[错误] {key} 需要 HH:MM:SS 格式（如 00:30:00），收到：{value!r}")
                sys.exit(1)
        elif types == "boolean":
            if value.lower() not in ("true", "false", "1", "0"):
                print(f"[错误] {key} 需要 true/false，收到：{value!r}")
                sys.exit(1)
            value = "true" if value.lower() in ("true", "1") else "false"
        result.append((key, group, argument, types, value))
    return result


def cmd_set(args, host: str, port: int, configs: list) -> int:
    base = f"http://{host}:{port}"
    parsed = parse_pairs(args.pairs)
    print(f"EvoZone 批量设置（{len(configs)} 个实例：{'、'.join(configs)}）")
    changed = False
    for name in configs:
        s = summary(name)
        print(f"  配置 {name}（{s['role']}）")
        for key, group, argument, types, value in parsed:
            # 当前值：组名 + 字段名，从配置原文读，便于看出差异
            cfg = load_config(name).get(TASK, {}).get(group, {})
            old = cfg.get(argument, "?")
            same = (str(old).lower() == value) if types == "boolean" else (str(old) == str(value))
            if same:
                print(f"    {key:<14} {old}（已是目标值，跳过）")
                continue
            changed = True
            print(f"    {key:<14} {old} -> {value}   [{TASK}.{group}.{argument}, {types}]")
    if not args.apply:
        print("  [dry-run] 未做任何修改；确认无误后加 --apply 才会写入配置并热加载")
        return 0
    if not changed:
        print("  所有参数已是目标值，无需写入")
        return 0
    health_check(base)
    ok = True
    for name in configs:
        for key, group, argument, types, value in parsed:
            if not set_arg(base, name, TASK, group, argument, types, value):
                ok = False
    print("  已完成；运行中的实例会热加载新配置（下一轮生效）。" if ok else "  部分设置失败，见上方错误")
    return 0 if ok else 1


async def cmd_control(args, host: str, port: int, configs: list, action: str) -> int:
    roles = {n: summary(n)["role"] for n in configs}
    if action == "start":
        ordered = sorted(configs, key=lambda n: (roles[n] == "leader", n))
        why = "先让队员进入等待邀请状态，最后启动队长创建队伍"
    else:
        ordered = sorted(configs, key=lambda n: (roles[n] != "leader", n))
        why = "先停队长（停止继续发邀请），再停队员"
    verb = "启动" if action == "start" else "停止"
    print(f"EvoZone 组队{verb}（{len(configs)} 个实例）：{why}")
    for i, name in enumerate(ordered, 1):
        print(f"  {i}. {pad(name, 12)} 角色 {roles[name]}")
    if args.dry_run:
        print("  [dry-run] 未发送任何启停指令")
        return 0
    health_check(f"http://{host}:{port}")
    fn = start_one if action == "start" else stop_one
    ok = True
    for name in ordered:
        if not await fn(host, port, name):
            ok = False
    return 0 if ok else 1


def show_help() -> None:
    print("EvoZone（觉醒副本 / 麒麟组队）专属命令：oasc evo <子命令>")
    print("默认覆盖 3 个实例：御魂1(leader) 御魂2(member) 御魂3(member)")
    print("组队顺序：start 队员->队长，stop 队长->队员")
    print("")
    print("  oasc evo status                    只读总览（状态/角色/层数/日志进度）")
    print("  oasc evo set <键=值>... [--apply]  批量改配置，默认 dry-run")
    print("  oasc evo start [--dry-run]         按组队顺序启动三个实例")
    print("  oasc evo stop  [--dry-run]         按组队顺序停止三个实例")
    print("  oasc evo solo  [--dry-run]         独占执行 EvoZone（禁用其余任务并重启，副作用大）")
    print("  oasc evo help                      本帮助")
    print("")
    print("  通用选项：--only <配置名> 可重复，只作用于指定实例（status/set/start/stop 均支持）")
    print("")
    print("可设置的参数：")
    for keys, desc, values in FIELD_HELP:
        print(f"  {keys:<24} {desc:<16} {values}")
    print("")
    print("示例：")
    print("  oasc evo status")
    print("  oasc evo set layer=拾层 count=100 --only 御魂1    # 只预览，不写入")
    print("  oasc evo set layer=拾层 count=100 --apply          # 三个实例一起改")
    print("  oasc evo solo --dry-run                            # 预览独占执行计划")
    print("")
    print("注意：探索/突破/爬塔仍默认只跑 御魂1 御魂2，不受 evo 命令影响。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EvoZone 组队任务控制（默认 御魂1/御魂2/御魂3 三个实例）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=22288)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="只读总览")
    p.add_argument("--only", action="append", help="只看指定配置，可重复")

    p = sub.add_parser("set", help="批量修改 EvoZone 参数")
    p.add_argument("pairs", nargs="+", help="键=值，可多组")
    p.add_argument("--apply", action="store_true", help="真正写入配置（默认只预览）")
    p.add_argument("--only", action="append", help="只改指定配置，可重复")

    for name, help_text in (("start", "按组队顺序启动"), ("stop", "按组队顺序停止")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--dry-run", action="store_true", help="只打印顺序，不发送指令")
        p.add_argument("--only", action="append", help="只操作指定配置，可重复")

    sub.add_parser("help", help="显示帮助")

    args = parser.parse_args()
    if not args.cmd or args.cmd == "help":
        show_help()
        return
    if args.cmd == "solo":
        print("solo 由 oasc.ps1 转交 oas_control.py 的 solo-evo；请直接用 oasc evo solo --dry-run")
        return
    configs = resolve_configs(getattr(args, "only", None))
    if args.cmd == "status":
        sys.exit(asyncio.run(cmd_status(args, args.host, args.port, configs)))
    if args.cmd == "set":
        sys.exit(cmd_set(args, args.host, args.port, configs))
    if args.cmd in ("start", "stop"):
        sys.exit(asyncio.run(cmd_control(args, args.host, args.port, configs, args.cmd)))


if __name__ == "__main__":
    main()
