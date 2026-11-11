"""
Turns results/summary.csv and results/timeline_*.csv into graphs for the report.

    python3 experiments/plot_results.py                 ->  results/plots/*.png
    python3 experiments/plot_results.py --focus 15      (the congestion level used for the per-severity bars)
"""
import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
OUT = RES / "plots"
COLORS = {"static": "#b5483a", "dynamic": "#2f5d8a"}
LABEL = {"static": "static rules (no priority)", "dynamic": "dynamic SDN (priority)"}
SEVERITIES = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_summary():
    with open(RES / "summary.csv") as f:
        return list(csv.DictReader(f))


def pick(rows, mode, flood, severity, phase="all"):
    return next((r for r in rows if r["mode"] == mode and float(r["flood_mbps"]) == flood and r["severity"] == severity
                 and r["phase"] == phase), None)


def bars_by_severity(rows, flood, field, title, ylabel, filename):
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    width = 0.38
    for i, mode in enumerate(("static", "dynamic")):
        values = []
        for sev in SEVERITIES:
            r = pick(rows, mode, flood, sev)
            values.append(num(r[field]) or 0 if r else 0)
        xs = [k + (i - 0.5) * width for k in range(len(SEVERITIES))]
        ax.bar(xs, values, width, label=LABEL[mode], color=COLORS[mode])
    ax.set_xticks(range(len(SEVERITIES)))
    ax.set_xticklabels(SEVERITIES, fontsize=8)
    ax.set_title(f"{title}\n{flood:g} Mbit/s background traffic on a 10 Mbit/s link", fontsize=10)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / filename, dpi=160)
    plt.close(fig)


def critical_vs_flood(rows, field, title, ylabel, filename, log=False):
    floods = sorted({float(r["flood_mbps"]) for r in rows})
    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    for mode in ("static", "dynamic"):
        ys = [num(pick(rows, mode, f, "CRITICAL")[field]) if pick(rows, mode, f, "CRITICAL") else None for f in floods]
        pts = [(f, y) for f, y in zip(floods, ys) if y is not None]
        if pts:
            ax.plot(*zip(*pts), marker="o", label=LABEL[mode], color=COLORS[mode], linewidth=1.8)
    ax.set_xlabel("background traffic from h4 (Mbit/s)")
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10)
    if log:
        ax.set_yscale("log")
    ax.axvline(10, color="#888", linestyle=":", linewidth=1)
    ax.annotate("link capacity", (10, ax.get_ylim()[1]), xytext=(4, -12), textcoords="offset points", fontsize=8, color="#555")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / filename, dpi=160)
    plt.close(fig)


def onset_vs_sustained(rows, flood):
    """CRITICAL logs: the first seconds after the flood starts (controller not yet reacting) vs the rest."""
    phases = [("onset", "first seconds\n(before the rules are in place)"), ("sustained", "rest of the congestion\n(rules in place)")]
    if not any(pick(rows, "dynamic", flood, "CRITICAL", ph) for ph, _ in phases):
        return
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8))
    for ax, field, ylabel, log in ((axes[0], "delay_p95_ms", "CRITICAL delay, 95th percentile (ms)", True),
                                   (axes[1], "loss_pct", "CRITICAL loss (%)", False)):
        width = 0.38
        for i, mode in enumerate(("static", "dynamic")):
            vals = []
            for ph, _ in phases:
                r = pick(rows, mode, flood, "CRITICAL", ph)
                vals.append(num(r[field]) or 0 if r else 0)
            xs = [k + (i - 0.5) * width for k in range(len(phases))]
            ax.bar(xs, vals, width, label=LABEL[mode], color=COLORS[mode])
        ax.set_xticks(range(len(phases)))
        ax.set_xticklabels([label for _, label in phases], fontsize=8)
        ax.set_ylabel(ylabel)
        if log:
            ax.set_yscale("log")
        ax.grid(axis="y", alpha=0.25)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, frameon=False, fontsize=9)
    fig.suptitle(f"Critical logs at the start of the congestion and afterwards ({flood:g} Mbit/s background traffic)", fontsize=10)
    fig.tight_layout(rect=(0, 0.07, 1, 0.96))
    fig.savefig(OUT / "critical_onset_vs_sustained.png", dpi=160)
    plt.close(fig)


def timeline(flood):
    fig, axes = plt.subplots(2, 1, figsize=(7.5, 5.2), sharex=True)
    drawn = False
    for mode in ("static", "dynamic"):
        path = RES / f"timeline_{mode}_flood{flood:g}.csv"
        if not path.exists():
            continue
        with open(path) as f:
            t = list(csv.DictReader(f))
        xs = [int(r["second"]) for r in t]
        delay = [num(r["critical_delay_ms"]) for r in t]
        loss = [100 * (1 - int(r["critical_received"]) / int(r["critical_sent"])) if int(r["critical_sent"]) else None for r in t]
        axes[0].plot([x for x, d in zip(xs, delay) if d is not None], [d for d in delay if d is not None],
                     color=COLORS[mode], label=LABEL[mode], linewidth=1.6)
        axes[1].plot([x for x, l in zip(xs, loss) if l is not None], [l for l in loss if l is not None],
                     color=COLORS[mode], linewidth=1.6)
        on = [x for x, r in zip(xs, t) if r["flood_on"] == "1"]
        if on and not drawn:
            for ax in axes:
                ax.axvspan(min(on), max(on) + 1, color="#999", alpha=0.15)
            drawn = True
    if not drawn and not any((RES / f"timeline_{m}_flood{flood:g}.csv").exists() for m in ("static", "dynamic")):
        plt.close(fig)
        return
    axes[0].set_ylabel("CRITICAL delay (ms)")
    axes[0].set_yscale("log")
    axes[1].set_ylabel("CRITICAL loss (%)")
    axes[1].set_xlabel("time (s)   grey = congestion")
    axes[0].set_title(f"Critical logs during congestion ({flood:g} Mbit/s background traffic)", fontsize=10)
    for ax in axes:
        ax.grid(alpha=0.25)
    axes[0].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / f"timeline_critical_flood{flood:g}.png", dpi=160)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--focus", type=float, default=15.0, help="congestion level (Mbit/s) for the per-severity graphs")
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rows = read_summary()
    if rows and "phase" not in rows[0]:
        raise SystemExit("results/summary.csv is from an older version (no 'phase' column). Run the experiments again.")
    floods = sorted({float(r["flood_mbps"]) for r in rows})
    focus = a.focus if a.focus in floods else max(floods)
    bars_by_severity(rows, focus, "loss_pct", "Packet loss per severity", "packet loss (%)", "loss_by_severity.png")
    bars_by_severity(rows, focus, "delay_p95_ms", "Delivery time per severity, 95th percentile", "delay (ms)", "delay_by_severity.png")
    critical_vs_flood(rows, "loss_pct", "Loss of CRITICAL logs as congestion grows", "packet loss (%)", "critical_loss_vs_load.png")
    critical_vs_flood(rows, "delay_p95_ms", "Delivery time of CRITICAL logs as congestion grows, 95th percentile",
                      "delay (ms, log scale)", "critical_delay_vs_load.png", log=True)
    onset_vs_sustained(rows, focus)
    timeline(focus)
    print(f"graphs written to {OUT}")


if __name__ == "__main__":
    main()
