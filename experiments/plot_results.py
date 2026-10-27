"""
Turns results/summary.csv and results/timeline_*.csv into graphs for the report.

    python3 experiments/plot_results.py        ->  results/plots/*.png
"""
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
OUT = RES / "plots"
COLORS = {"static": "#b5483a", "dynamic": "#2f5d8a"}
TITLES = {"normal": "Normal operation", "link_failure": "Link failure (s1-s2 goes down)",
          "collector_failure": "Collector c1 fails", "flood": "UDP flood from h4", "rogue": "Unauthorised host h4"}
ORDER = ["normal", "link_failure", "collector_failure", "flood", "rogue"]


def read_summary():
    with open(RES / "summary.csv") as f:
        return list(csv.DictReader(f))


def read_timeline(mode, scenario):
    path = RES / f"timeline_{mode}_{scenario}.csv"
    if not path.exists():
        return None
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return [int(r["second"]) for r in rows], [int(r["logs_per_s"]) for r in rows]


def timelines(rows):
    for scenario in ORDER:
        sel = {r["mode"]: r for r in rows if r["scenario"] == scenario}
        if not sel:
            continue
        fig, ax = plt.subplots(figsize=(7.5, 3.6))
        for mode in ("static", "dynamic"):
            t = read_timeline(mode, scenario)
            if t:
                ax.plot(t[0], t[1], label=f"{mode} controller", color=COLORS[mode], linewidth=1.8)
        event = int(next(iter(sel.values()))["event_s"])
        if scenario not in ("normal", "rogue"):
            ax.axvline(event, color="#444", linestyle="--", linewidth=1)
            ax.annotate("event", (event, ax.get_ylim()[1] * 0.92), xytext=(4, 0), textcoords="offset points", fontsize=8)
        ax.set_title(TITLES[scenario])
        ax.set_xlabel("time (s)")
        ax.set_ylabel("logs stored per second")
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.25)
        ax.legend(frameon=False)
        fig.tight_layout()
        fig.savefig(OUT / f"timeline_{scenario}.png", dpi=160)
        plt.close(fig)


def grouped_bars(rows, field, title, ylabel, filename, scenarios, never_label=False):
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    width = 0.38
    top = 1
    for i, mode in enumerate(("static", "dynamic")):
        values, hatches = [], []
        for s in scenarios:
            r = next((r for r in rows if r["mode"] == mode and r["scenario"] == s), None)
            if r is None:
                values.append(0)
                hatches.append(False)
            elif never_label and r["never_recovered"] == "True":
                values.append(-1)                                   # filled in below once the scale is known
                hatches.append(True)
            else:
                values.append(float(r[field] or 0))
                hatches.append(False)
        top = max(top, max(values))
        values = [top * 1.1 if v == -1 else v for v in values]
        xs = [k + (i - 0.5) * width for k in range(len(scenarios))]
        bars = ax.bar(xs, values, width, label=f"{mode} controller", color=COLORS[mode])
        for bar, hatch in zip(bars, hatches):
            if hatch:
                bar.set_hatch("//")
                ax.annotate("never", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels([TITLES[s].split(" (")[0] for s in scenarios], fontsize=8)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / filename, dpi=160)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = read_summary()
    timelines(rows)
    grouped_bars(rows, "recovery_s", "Time until delivery recovers after the event", "seconds",
                 "recovery_time.png", ["link_failure", "collector_failure", "flood"], never_label=True)
    grouped_bars(rows, "logs_undelivered", "Logs still undelivered when the run ends", "logs",
                 "undelivered.png", ["link_failure", "collector_failure", "flood"])
    grouped_bars(rows, "rogue_logs_accepted", "Logs accepted from the unauthorised host", "logs",
                 "rogue_logs.png", ["rogue"])
    grouped_bars(rows, "share_c1_pct", "Share of logs stored on collector c1 (normal run)", "percent",
                 "load_balance.png", ["normal"])
    print(f"graphs written to {OUT}")


if __name__ == "__main__":
    main()
