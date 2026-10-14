"""Figure 3 profiles: five agent sectors, nine strata, and three metric panels.

Read evaluation strata from CSV. Use --allow-partial to display profile gaps."""
import argparse
import csv
import math
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Wedge, Circle
from matplotlib.lines import Line2D

AGENTS = ["Agentless", "AutoCodeRover", "Augment", "OpenHands", "SWE-agent"]
MODELS = ["Claude 4.5", "DeepSeek-v3.2", "GPT-5.5"]
COLORS = ["#0072B2", "#D55E00", "#7B3294"]
MARKERS = ["o", "s", "D"]
PALE = ["#EEF5FB", "#FFF2E7", "#EFF7EF", "#F4EFFA", "#FFF0F2"]
STRATA = [("category", "None", "none"), ("category", "Configuration", "Config."),
          ("category", "Lexical", "Lex."), ("category", "Security FP", "Sec. FP"),
          ("category", "Structural", "Struct."), ("category", "Symptom", "Sympt."),
          ("hop", "1-3", "1-3"), ("hop", "4-5", "4-5"), ("hop", "6+", "6+")]
PANELS = [("Localization & trace", ["File@5", "HNegOv", "TV"]),
          ("Exploration", ["Detour", "Recovery"]), ("Resources", ["Tokens", "Time"])]
STYLES = ["-", "--", ":"]


def coord(value, metric, args):
    if value is None:
        return float("nan")
    if metric == "Tokens":
        return 1-value/args.token_scale
    if metric == "Time":
        return 1-value/args.time_scale
    if metric == "Detour":
        return 1-value/100
    return value/100


def draw(ax, data, metrics, args):
    ax.axis("off"); ax.set_aspect("equal"); ax.set_xlim(-1.5, 1.5); ax.set_ylim(-1.5, 1.5)
    for i, agent in enumerate(AGENTS):
        center = 54-72*i; a = np.deg2rad(center)
        ax.add_patch(Wedge((0, 0), 1.49, center-36, center+36, fc=PALE[i], ec="none", zorder=-10))
        rotation = (center-90+180)%360-180
        if rotation < -90: rotation += 180
        if rotation > 90: rotation -= 180
        ax.text(1.4*np.cos(a), 1.4*np.sin(a), agent, rotation=rotation, ha="center", va="center", fontsize=7, fontweight="bold")
    for r in (.25, .5, .75, 1):
        ax.add_patch(Circle((0,0), r, fill=False, ec="#ADB8C3", lw=.4))
    for r in (0, .25, .5, .75, 1):
        ax.text(-.04, r, str(int(r*100)), fontsize=6, ha="right", va="center")
    for i, agent in enumerate(AGENTS):
        angles = np.deg2rad(90-i*72-(np.arange(9)+.5)*8)
        units = np.c_[np.cos(angles), np.sin(angles)]
        for s, (axis, stratum, label) in enumerate(STRATA):
            x, y = units[s]
            ax.text(1.07*x, 1.07*y, label, rotation=np.degrees(angles[s])+(180 if x < 0 else 0),
                    ha="left" if x >= 0 else "right", va="center", rotation_mode="anchor", fontsize=6)
        for m, model in enumerate(MODELS):
            for j, metric in enumerate(metrics):
                values = [coord(data.get((agent, model, axis, stratum), {}).get(metric), metric, args) for axis, stratum, label in STRATA]
                points = units * np.array(values)[:,None]
                for segment in (slice(0,6), slice(6,9)):
                    ax.plot(points[segment,0], points[segment,1], color=COLORS[m], ls=STYLES[j], lw=.7,
                            marker=MARKERS[m], markersize=2.3, markerfacecolor="none")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True); p.add_argument("--out", default="work/figures")
    p.add_argument("--token-scale", type=float, default=7300); p.add_argument("--time-scale", type=float, default=310)
    p.add_argument("--allow-partial", action="store_true"); args = p.parse_args()
    if args.token_scale <= 0 or args.time_scale <= 0:
        raise ValueError("Resource normalization scales must be positive")
    metrics = [m for title, mm in PANELS for m in mm]
    with open(args.data, encoding="utf-8") as f: rows = list(csv.DictReader(f))
    data = {}
    for r in rows:
        if r.get("condition", "baseline") != "baseline": continue
        key = (r["agent"], r["model"], r["axis"], r["stratum"])
        if key in data: raise ValueError("Repeated agent/model/stratum")
        data[key] = {m: float(r[m]) if r.get(m) not in (None, "", "None") else None for m in metrics}
    if not args.allow_partial:
        for a in AGENTS:
            for m in MODELS:
                for axis, s, label in STRATA:
                    row = data.get((a,m,axis,s), {})
                    if any(row.get(k) is None or not math.isfinite(row[k]) for k in metrics):
                        raise ValueError("Incomplete measured profiles; use --allow-partial to show gaps")
    plt.rcParams.update({"font.family": "DejaVu Sans", "pdf.fonttype": 42, "svg.fonttype": "none"})
    fig = plt.figure(figsize=(10.8,4.35))
    for i, (title, mm) in enumerate(PANELS):
        ax = fig.add_axes([.003+i*.332, .165, .329, .795]); draw(ax, data, mm, args)
        ax.text(.5,1.018, f"({'abc'[i]}) {title}", transform=ax.transAxes, ha="center", fontsize=8, fontweight="bold")
        handles = [Line2D([], [], color="#333", ls=STYLES[j], label={"HNegOv": "Overlap", "Recovery": "Return"}.get(m,m)) for j,m in enumerate(mm)]
        ax.legend(handles=handles, ncol=len(mm), loc="upper center", bbox_to_anchor=(.5,-.007), frameon=False, fontsize=7)
    handles = [Line2D([], [], color=c, marker=m, ls="none", markerfacecolor="none", label=n) for c,m,n in zip(COLORS, MARKERS, MODELS)]
    fig.legend(handles=handles, ncol=3, loc="lower center", bbox_to_anchor=(.5,.007), frameon=False, fontsize=7.5)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "svg", "png"):
        fig.savefig(out / ("ptrace_profiles."+ext), dpi=280, transparent=True)
    plt.close(fig)
    print("Saved Figure 3 profiles")


if __name__ == "__main__":
    main()
