# -*- coding: utf-8 -*-
"""统计 Exploration(探索) 与 RealmRaid(个人突破) 的执行情况并持久化。

用法:
    ./toolkit/python.exe nakk/analyze_exp_raid_log.py
自动扫描 log/ 下所有 `YYYY-MM-DD_账号.txt` 日志(排除 server/-c),
解析每次探索/突破的起止、耗时、战斗数, 并:
  1. 按行写入 nakk/stats/runs.csv (按 日期+账号 幂等更新, 可反复重跑)
  2. 重新生成累计统计报告 nakk/stats/探索与突破时长统计.md

成功判定: 任务以调度日志 "Delay task `<task>` ... (success=True)" 正常收尾。
战斗计数: 日志中 "Win battle" / "Battle result is win" 出现次数。
"""
import csv
import math
import os
import re
import sys
import io
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE, "log")
STATS_DIR = os.path.join(BASE, "nakk", "stats")
CSV_PATH = os.path.join(STATS_DIR, "runs.csv")
REPORT_PATH = os.path.join(STATS_DIR, "探索与突破时长统计.md")

# 参与探索轮替、用于计算推荐等待时长的账号
PAIR_CONFIGS = {"御魂1", "御魂2"}
# 单次突破战斗数达到该值视为"长间隔/券接近满"场景, 不计入常规等待推荐
FULL_TICKET_BATTLES = 28

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \|")
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)\.txt$")
START_RE = re.compile(r"Scheduler: Start task `(\w+)`")
END_RE = re.compile(r"Scheduler: End task `(\w+)`")
DELAY_RE = re.compile(r"Delay task `(\w+)` to .*? \(success=(True|False)")
BATTLE_WIN_RE = re.compile(r"(Win battle|Battle result is win)")
BATTLE_START_RE = re.compile(r"Start battle process")
BATTLE_FALSE_RE = re.compile(r"(Battle result is false|False battle)")
FATAL_RE = re.compile(r"(GameStuckError|GameTooManyClickError|GameBugError"
                      r"|GamePageUnknownError|Request human takeover)")
TASKS = {"Exploration": "探索", "RealmRaid": "个人突破"}
DELAY_KEY = {"exploration": "Exploration", "realm_raid": "RealmRaid"}
SKIP_CONFIGS = {"server", "-c"}

CSV_FIELDS = ["date", "config", "task", "status", "start", "end",
              "duration_sec", "battles_win", "battle_lose", "battle_start",
              "fatal_errors"]


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")


def fmt_dur(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"
    return f"{sec // 60}m{sec % 60:02d}s"


def load_lines(path):
    """返回 1-based 的行列表, lines[i] 为第 i 行。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return [None] + lines


def parse_events(lines):
    events = []
    for i, line in enumerate(lines):
        if i == 0 or not line:
            continue
        m = TS_RE.match(line)
        if not m:
            continue
        ts = parse_ts(m.group(1))
        m2 = START_RE.search(line)
        if m2 and m2.group(1) in TASKS:
            events.append(("start", m2.group(1), ts, i))
            continue
        m2 = END_RE.search(line)
        if m2 and m2.group(1) in TASKS:
            events.append(("end", m2.group(1), ts, i))
    return events


def pair_events(events):
    runs, pending = [], {}
    for kind, task, ts, line in events:
        if kind == "start":
            if task in pending:
                runs.append(dict(task=task, start=pending[task][0], start_line=pending[task][1],
                                 end=None, end_line=None, status="中止(未End即重启)"))
            pending[task] = (ts, line)
        else:
            if task in pending:
                runs.append(dict(task=task, start=pending[task][0], start_line=pending[task][1],
                                 end=ts, end_line=line, status=None))
                del pending[task]
            else:
                runs.append(dict(task=task, start=None, start_line=None,
                                 end=ts, end_line=line, status="异常(无Start的End)"))
    for task, (ts, line) in pending.items():
        runs.append(dict(task=task, start=ts, start_line=line,
                         end=None, end_line=None, status="运行中(日志截止)"))
    runs.sort(key=lambda r: (r["start"] or r["end"]))
    return runs


def run_stats(lines, r):
    """统计运行区间内的战斗/异常, 依据 Delay success 标记判定成败。"""
    wins = starts = fails = fatals = 0
    delay = None
    lo, hi = r["start_line"], r["end_line"]
    for line in lines[lo:hi + 1]:
        if BATTLE_WIN_RE.search(line):
            wins += 1
        if BATTLE_START_RE.search(line):
            starts += 1
        if BATTLE_FALSE_RE.search(line):
            fails += 1
        if FATAL_RE.search(line):
            fatals += 1
        m = DELAY_RE.search(line)
        if m and DELAY_KEY.get(m.group(1)) == r["task"]:
            delay = m.group(2)
    if r["status"]:
        return wins, starts, fails, fatals, r["status"]
    if delay == "True":
        return wins, starts, fails, fatals, "成功"
    if delay == "False":
        return wins, starts, fails, fatals, "失败(success=False)"
    if fatals:
        return wins, starts, fails, fatals, "失败(异常退出)"
    return wins, starts, fails, fatals, "疑似成功(无标记,无异常)"


def parse_file(date, config, path):
    lines = load_lines(path)
    runs = pair_events(parse_events(lines))
    rows = []
    for r in runs:
        if r["start"] is None:
            dur = 0
            wins = lose = starts = fatals = 0
            status = r["status"]
        elif r["end"] is None:
            dur = wins = lose = starts = fatals = 0
            status = r["status"]
        else:
            dur = int((r["end"] - r["start"]).total_seconds())
            wins, starts, lose, fatals, status = run_stats(lines, r)
        rows.append(dict(
            date=date, config=config, task=r["task"], status=status,
            start=r["start"].strftime("%H:%M:%S") if r["start"] else "",
            end=r["end"].strftime("%H:%M:%S") if r["end"] else "",
            duration_sec=dur, battles_win=wins, battle_lose=lose,
            battle_start=starts, fatal_errors=fatals))
    return rows


def discover_files():
    files = []
    for name in sorted(os.listdir(LOG_DIR)):
        m = FILE_RE.match(name)
        if not m or m.group(2) in SKIP_CONFIGS:
            continue
        files.append((m.group(1), m.group(2), os.path.join(LOG_DIR, name)))
    return files


def load_csv():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(rows):
    os.makedirs(STATS_DIR, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["date"], r["config"],
                                       r["start"] or "99"))
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def linear_fit(points):
    """最小二乘 y = a + b*x, 返回 (a, b); 点太少返回 (0, 0)。"""
    n = len(points)
    if n < 3:
        return 0.0, 0.0
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    if sxx == 0:
        return my, 0.0
    b = sxy / sxx
    return my - b * mx, b


def ceil_minute(sec):
    return int(math.ceil(sec / 60.0) * 60)


def aggregate(rows):
    """按 (date, config, task) 汇总成功运行。返回 {(date,cfg,task): dict}"""
    agg = {}
    for r in rows:
        if r["status"] != "成功":
            continue
        k = (r["date"], r["config"], r["task"])
        g = agg.setdefault(k, dict(n=0, dur=0, wins=0, lose=0, max_dur=0))
        g["n"] += 1
        g["dur"] += int(r["duration_sec"])
        g["wins"] += int(r["battles_win"])
        g["lose"] += int(r["battle_lose"])
        g["max_dur"] = max(g["max_dur"], int(r["duration_sec"]))
    return agg


def build_report(rows):
    agg = aggregate(rows)
    dates = sorted({k[0] for k in agg})
    L = []
    L.append("# 探索与个人突破时长统计\n")
    L.append("> 由 `nakk/analyze_exp_raid_log.py` 自动生成, 勿手改。"
             "原始逐行数据见 [runs.csv](runs.csv)。重新运行脚本即可追加/刷新数据。\n")
    L.append("> 口径: 成功=任务以 `Delay task ... success=True` 收尾; "
             "耗时=调度器 Start→End; 战斗数=`Win battle` 计数(即胜利场次);\n"
             "> 单次耗时与当次突破券存量(距上次突破的间隔)强相关, 对比时注意战斗数。\n")

    # ---- 按日期汇总 ----
    L.append("\n## 按日期汇总(仅成功运行)\n")
    L.append("| 日期 | 账号 | 任务 | 次数 | 战斗 | 总耗时 | 平均 | 最大 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for d in dates:
        for (dd, cfg, task), g in sorted(agg.items()):
            if dd != d:
                continue
            L.append(f"| {d} | {cfg} | {TASKS[task]} | {g['n']} | {g['wins']} | "
                     f"{fmt_dur(g['dur'])} | {fmt_dur(g['dur'] / g['n'])} | "
                     f"{fmt_dur(g['max_dur'])} |")

    # ---- 总体统计 + 线性模型 ----
    L.append("\n## 总体统计(全部日期, 仅成功运行)\n")
    L.append("| 账号 | 任务 | 次数 | 总战斗 | 总耗时 | 平均 | 最大 | 秒/场 |")
    L.append("|---|---|---|---|---|---|---|---|")
    overall = {}
    for (d, cfg, task), g in agg.items():
        o = overall.setdefault((cfg, task),
                               dict(n=0, dur=0, wins=0, max_dur=0, pts=[]))
        o["n"] += g["n"]
        o["dur"] += g["dur"]
        o["wins"] += g["wins"]
        o["max_dur"] = max(o["max_dur"], g["max_dur"])
    for r in rows:
        if r["status"] == "成功" and (r["config"], r["task"]) in overall:
            overall[(r["config"], r["task"])]["pts"].append(
                (int(r["battles_win"]), int(r["duration_sec"])))
    for (cfg, task), o in sorted(overall.items()):
        spb = f"{o['dur'] / o['wins']:.1f}" if o["wins"] else "-"
        L.append(f"| {cfg} | {TASKS[task]} | {o['n']} | {o['wins']} | "
                 f"{fmt_dur(o['dur'])} | {fmt_dur(o['dur'] / o['n'])} | "
                 f"{fmt_dur(o['max_dur'])} | {spb} |")

    L.append("\n### 时长线性模型(最小二乘: 耗时 ≈ 开销 + 秒/场 × 战斗数)\n")
    for (cfg, task), o in sorted(overall.items()):
        if task != "RealmRaid" or cfg not in PAIR_CONFIGS:
            continue
        pts = [p for p in o["pts"] if p[0] > 0]
        a, b = linear_fit(pts)
        if b:
            L.append(f"- {cfg}: 耗时 ≈ {a:.0f}s + {b:.1f}s × 战斗数 "
                     f"(样本 {len(pts)} 次)")
        else:
            L.append(f"- {cfg}: 样本不足, 暂不拟合")

    # ---- 推荐等待时长 ----
    L.append("\n## 推荐轮替等待时长(探索结束后, 下一轮探索需等待双方突破完成)\n")
    raid_ok = [r for r in rows if r["task"] == "RealmRaid" and r["status"] == "成功"
               and r["config"] in PAIR_CONFIGS]
    if raid_ok:
        typical = [r for r in raid_ok if int(r["battles_win"]) < FULL_TICKET_BATTLES]
        if typical:
            wait_t = ceil_minute(max(int(r["duration_sec"]) for r in typical))
            L.append(f"- **常规等待(推荐)**: {fmt_dur(wait_t)} "
                     f"(覆盖战斗数<{FULL_TICKET_BATTLES}的全部成功样本, "
                     f"共{len(typical)}次, 上界 "
                     f"{fmt_dur(max(int(r['duration_sec']) for r in typical))})")
        full = [r for r in raid_ok]
        wait_f = ceil_minute(max(int(r["duration_sec"]) for r in full))
        L.append(f"- **保守等待(含长间隔/券满场景)**: {fmt_dur(wait_f)} "
                 f"(全部成功样本上界, 当天首轮或距上次突破>3h时按此安排)")
    else:
        L.append("- 暂无可用样本")

    # ---- 每日成功明细 ----
    L.append("\n## 每日明细(成功运行逐条)\n")
    for d in sorted({r["date"] for r in rows}):
        day_rows = [r for r in rows if r["date"] == d and r["status"] == "成功"]
        L.append(f"\n### {d}\n")
        if day_rows:
            L.append("| 账号 | 任务 | 开始 | 结束 | 耗时 | 战斗(胜/败/发起) |")
            L.append("|---|---|---|---|---|---|")
            for r in sorted(day_rows, key=lambda x: (x["config"], x["start"])):
                L.append(f"| {r['config']} | {TASKS[r['task']]} | {r['start']} | "
                         f"{r['end']} | {fmt_dur(int(r['duration_sec']))} | "
                         f"{r['battles_win']}/{r['battle_lose']}/{r['battle_start']} |")
        else:
            L.append("(无成功运行)")
        bad = [r for r in rows if r["date"] == d and r["status"] != "成功"]
        if bad:
            L.append(f"\n{d} 失败/中止:")
            for r in sorted(bad, key=lambda x: (x["config"], x["start"])):
                st = r["start"] or "?"
                en = r["end"] or "进行中"
                L.append(f"- {r['config']} {TASKS[r['task']]} {st}->{en} "
                         f"{r['status']} (战斗{r['battles_win']}, 异常{r['fatal_errors']})")
    return "\n".join(L) + "\n"


def main():
    files = discover_files()
    if not files:
        print("log/ 下未找到可用日志")
        return
    old_rows = load_csv()
    reparsed_keys = {(d, c) for d, c, _ in files}
    merged = [r for r in old_rows if (r["date"], r["config"]) not in reparsed_keys]

    for date, config, path in files:
        rows = parse_file(date, config, path)
        merged.extend(rows)
        # 控制台输出该文件明细
        print(f"\n{'=' * 76}\n{date} {config}  ({os.path.basename(path)})\n{'=' * 76}")
        for r in rows:
            if not r["start"]:
                print(f"  [{TASKS[r['task']]}|{r['status']}] End={r['end']}")
                continue
            en = r["end"] or "进行中"
            print(f"  [{TASKS[r['task']]}|{r['status']}] {r['start']}->{en} "
                  f"耗时{fmt_dur(r['duration_sec']) if r['end'] else '-':>8} | "
                  f"战斗 胜{r['battles_win']}/败{r['battle_lose']}/发起{r['battle_start']} "
                  f"| 异常{r['fatal_errors']}")

    save_csv(merged)
    report = build_report(merged)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n{'=' * 76}\n已持久化: {len(merged)} 行 -> {CSV_PATH}")
    print(f"报告已更新 -> {REPORT_PATH}")
    # 控制台也打一份推荐结论
    raid_ok = [r for r in merged if r["task"] == "RealmRaid" and r["status"] == "成功"
               and r["config"] in PAIR_CONFIGS]
    for cfg in sorted(PAIR_CONFIGS):
        rs = [r for r in raid_ok if r["config"] == cfg]
        if not rs:
            continue
        durs = [int(r["duration_sec"]) for r in rs]
        wins = sum(int(r["battles_win"]) for r in rs)
        print(f"{cfg}: 突破成功{len(rs)}次 平均{fmt_dur(sum(durs) / len(durs))} "
              f"最大{fmt_dur(max(durs))} 共{wins}场 "
              f"平均{sum(durs) / wins if wins else 0:.1f}s/场")


if __name__ == "__main__":
    main()
