"""Render a player's league record as a shareable "trading card" PNG.

Input is exactly main.py's `_build_player_detail(...)` output (the same
dict GET /players/{id} returns as JSON) — no separate query logic, no
adapter function needed, unlike the rankings/pairings renderers which
adapt a differently-shaped input.

No database access — pure rendering given that payload dict.
"""
import io
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.image as mpimg

# render_pairings_image resolves as a bare module name only when scripts/
# itself is on sys.path — true when a script here is run directly (Python
# adds the running script's own directory), but not when this module is
# imported as `scripts.render_player_card_image` from main.py at the repo
# root. Ensure it either way, without changing the sibling renderers'
# existing (bare-name) import convention.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_pairings_image import _icon_png_path, _fit_fontsize


def _most_played_faction(faction_usage: dict) -> str | None:
    """faction_usage is {system: {faction: count}} — combine across all
    systems and return the single most-played faction overall."""
    totals: dict[str, int] = {}
    for per_system in (faction_usage or {}).values():
        for faction, count in (per_system or {}).items():
            totals[faction] = totals.get(faction, 0) + count
    return max(totals, key=totals.get) if totals else None


def render_player_card_image(payload: dict) -> io.BytesIO | None:
    """Render a player card as a PNG.

    payload: main.py's `_build_player_detail(...)` output shape
    (player/club/titles/faction_usage/league with rating/rank/wins/
    draws/losses/total_games/elo_history).
    Returns a BytesIO buffer positioned at start, or None if the player
    has no league rating yet (nothing meaningful to show on a card).
    """
    league = payload.get("league") or {}
    if league.get("rating") is None:
        return None

    player = payload["player"]
    player_name = player.name if hasattr(player, "name") else player.get("name", "")
    club = payload.get("club")
    titles = payload.get("titles") or []
    faction = _most_played_faction(payload.get("faction_usage") or {})
    elo_history = league.get("elo_history") or []

    # ---- Style constants matching the public gilt-on-gunmetal theme ----
    bg_color = "#161620"
    panel_bg = "#1e1e28"
    text_color = "#f5edd7"
    muted_color = "#d4c8a0"
    accent = "#c9a14a"
    border_color = "#5a4a26"
    win_color = "#6fae72"
    loss_color = "#cf5a54"

    fig_w_in, fig_h_in = 5.4, 7.6
    fig = plt.figure(figsize=(fig_w_in, fig_h_in), dpi=150)
    fig.patch.set_facecolor(bg_color)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w_in)
    ax.set_ylim(0, fig_h_in)
    ax.set_aspect("equal")
    ax.axis("off")

    # Card panel — square corners, matching the app's "no notch" rule.
    margin = 0.22
    panel = mpatches.FancyBboxPatch(
        (margin, margin), fig_w_in - 2 * margin, fig_h_in - 2 * margin,
        boxstyle="round,pad=0,rounding_size=0.04",
        linewidth=1.4,
        edgecolor=accent,
        facecolor=panel_bg,
        zorder=1,
    )
    ax.add_patch(panel)

    cx = fig_w_in / 2
    y = fig_h_in - margin - 0.42

    if club:
        ax.text(cx, y, club["name"].upper(), ha="center", va="center",
                 color=muted_color, fontsize=10, fontweight="bold",
                 zorder=3)
        y -= 0.36

    name_size = _fit_fontsize(fig, player_name, fig_w_in - 2 * margin - 0.5, base_size=26, min_size=10)
    ax.text(cx, y, player_name, ha="center", va="center",
            color=text_color, fontsize=name_size, fontweight="bold", zorder=3)
    y -= 0.5

    icon_path = _icon_png_path(faction)
    icon_size = 1.05
    if icon_path:
        try:
            img = mpimg.imread(icon_path)
            ax.imshow(img,
                      extent=[cx - icon_size / 2, cx + icon_size / 2,
                              y - icon_size, y],
                      aspect="auto", zorder=3)
        except Exception:
            pass
        y -= icon_size + 0.16
    else:
        y -= 0.16
    if faction:
        ax.text(cx, y, faction, ha="center", va="center",
                 color=muted_color, fontsize=11, style="italic", zorder=3)
        y -= 0.36

    ax.plot([margin + 0.35, fig_w_in - margin - 0.35], [y, y],
            color=border_color, alpha=0.6, linewidth=1.0, zorder=2)
    y -= 0.55

    # Big rank / ELO stat row
    rank = league.get("rank")
    ax.text(cx - 0.9, y, f"#{rank}" if rank else "—", ha="center", va="center",
            color=accent, fontsize=30, fontweight="bold", zorder=3)
    ax.text(cx - 0.9, y - 0.34, "RANK", ha="center", va="center",
            color=muted_color, fontsize=9, fontweight="bold", zorder=3)

    ax.text(cx + 0.9, y, str(round(league["rating"])), ha="center", va="center",
            color=text_color, fontsize=30, fontweight="bold", zorder=3)
    ax.text(cx + 0.9, y - 0.34, "ELO", ha="center", va="center",
            color=muted_color, fontsize=9, fontweight="bold", zorder=3)
    y -= 0.85

    # W/D/L row
    wdl_y = y
    ax.text(cx - 0.9, wdl_y, str(league.get("wins", 0)), ha="center", va="center",
            color=win_color, fontsize=17, fontweight="bold", zorder=3)
    ax.text(cx, wdl_y, str(league.get("draws", 0)), ha="center", va="center",
            color=muted_color, fontsize=17, fontweight="bold", zorder=3)
    ax.text(cx + 0.9, wdl_y, str(league.get("losses", 0)), ha="center", va="center",
            color=loss_color, fontsize=17, fontweight="bold", zorder=3)
    y -= 0.3
    ax.text(cx - 0.9, y, "WINS", ha="center", va="center", color=muted_color, fontsize=8, zorder=3)
    ax.text(cx, y, "DRAWS", ha="center", va="center", color=muted_color, fontsize=8, zorder=3)
    ax.text(cx + 0.9, y, "LOSSES", ha="center", va="center", color=muted_color, fontsize=8, zorder=3)
    y -= 0.55

    # Titles, as small chips (top 2 keeps the card uncluttered)
    for title in titles[:2]:
        chip_w = min(fig_w_in - 2 * margin - 0.7, 0.32 + 0.11 * len(title))
        chip = mpatches.FancyBboxPatch(
            (cx - chip_w / 2, y - 0.24), chip_w, 0.32,
            boxstyle="round,pad=0,rounding_size=0.02",
            linewidth=1.0, edgecolor=accent, facecolor="none", zorder=3,
        )
        ax.add_patch(chip)
        ax.text(cx, y - 0.08, title, ha="center", va="center",
                color=accent, fontsize=9, fontweight="bold", zorder=4)
        y -= 0.46

    # ELO sparkline, flowing from wherever the title chips left off down to
    # a fixed footer clearance — never a fixed-position block, so it can't
    # collide with a variable number of title chips above it.
    spark_bottom = margin + 0.5
    spark_top_limit = y - 0.3  # clearance for the "ELO TREND" label
    spark_h = min(0.85, spark_top_limit - spark_bottom)
    if len(elo_history) > 1 and spark_h > 0.3:
        spark_left = margin + 0.5
        spark_right = fig_w_in - margin - 0.5
        elos = [h["elo"] for h in elo_history]
        lo, hi = min(elos), max(elos)
        rng = (hi - lo) or 1
        xs = [spark_left + (spark_right - spark_left) * i / (len(elos) - 1) for i in range(len(elos))]
        ys = [spark_bottom + spark_h * (e - lo) / rng for e in elos]
        ax.plot(xs, ys, color=accent, linewidth=1.8, zorder=3)
        ax.fill_between(xs, [spark_bottom] * len(xs), ys, color=accent, alpha=0.12, zorder=2)
        ax.text(cx, spark_bottom + spark_h + 0.22, "ELO TREND", ha="center", va="center",
                color=muted_color, fontsize=8, fontweight="bold", zorder=3)

    ax.text(cx, margin + 0.22, "CALL TO ARMS", ha="center", va="center",
            color=border_color, fontsize=8, fontweight="bold", zorder=3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg_color, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    buf.seek(0)
    return buf
