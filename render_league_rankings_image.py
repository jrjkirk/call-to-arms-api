"""Render the league rankings table as a PNG, mirroring the public league
table style.

Ported from the original Streamlit app's render_league_rankings_image().
Input rows use the shape main.py's GET /league/rankings produces
(rank/name/most_played_faction/rating/wins/draws/losses/total_games) —
adapted here to the Rank/ELO/Name/Most Played Faction/W/D/L/Games Played
names the layout code was written against.

No database access — pure rendering given a list of ranking dicts.
"""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.image as mpimg

from render_pairings_image import _icon_png_path


def _to_rankings_render_rows(rankings: list[dict]) -> list[dict]:
    """Adapt GET /league/rankings's output shape to the field names this
    renderer's layout code was written against."""
    out = []
    for r in rankings:
        out.append({
            "Rank": r.get("rank"),
            "ELO": round(r.get("rating", 0)),
            "Name": r.get("name"),
            "Most Played Faction": r.get("most_played_faction") or "—",
            "W/D/L": f"{r.get('wins', 0)}/{r.get('draws', 0)}/{r.get('losses', 0)}",
            "Games Played": r.get("total_games", 0),
        })
    return out


def render_league_rankings_image(rankings: list[dict]) -> io.BytesIO | None:
    """Render the league rankings as a PNG.

    rankings: list of dicts in GET /league/rankings's output shape
    (rank/name/most_played_faction/rating/wins/draws/losses/total_games).
    Returns a BytesIO buffer positioned at start, or None if rankings is empty.
    """
    rows = _to_rankings_render_rows(rankings)
    if not rows:
        return None

    # ---- Style constants matching the public league table ----
    bg_color = "#161620"
    table_bg = "#1e1e28"
    text_color = "#f4e9c8"
    muted_color = "#d4c8a0"
    faction_color = "#d4c8a0"
    accent = "#c9a14a"
    border_color = "#5a4a26"
    header_bg = "#0c0c12"

    podium_tints = {1: "#3a2e0f", 2: "#2c2c30", 3: "#3a2818"}
    medal_colors = {
        1: ("#d4af37", "#3a2e0f"),
        2: ("#bfc1c2", "#2c2c30"),
        3: ("#c98349", "#3a2818"),
    }

    n = len(rows)
    fig_width_in = 12.0
    header_h_in = 0.55
    row_h_in = 0.62
    pad_top = 0.20
    pad_bot = 0.20
    fig_height_in = pad_top + pad_bot + header_h_in + n * row_h_in

    fig = plt.figure(figsize=(fig_width_in, fig_height_in), dpi=200)
    fig.patch.set_facecolor(bg_color)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_width_in)
    ax.set_ylim(0, fig_height_in)
    ax.set_aspect("equal")
    ax.axis("off")

    container = mpatches.FancyBboxPatch(
        (0.18, 0.10), fig_width_in - 0.36, fig_height_in - 0.20,
        boxstyle="round,pad=0,rounding_size=0.10",
        linewidth=1.2,
        edgecolor=border_color,
        facecolor=table_bg,
        zorder=1,
    )
    ax.add_patch(container)

    inner_left = 0.40
    inner_right = fig_width_in - 0.40
    col_rank_x = inner_left + 0.30
    col_elo_x = inner_left + 0.95
    col_name_x = inner_left + 1.55
    col_faction_x = inner_left + 4.30
    col_wdl_x = inner_right - 1.55
    col_games_x = inner_right - 0.40

    header_y = fig_height_in - pad_top - header_h_in / 2
    header_bg_rect = mpatches.Rectangle(
        (0.18, fig_height_in - pad_top - header_h_in),
        fig_width_in - 0.36, header_h_in,
        linewidth=0, facecolor=header_bg, zorder=2,
    )
    ax.add_patch(header_bg_rect)

    ax.plot(
        [0.18, fig_width_in - 0.18],
        [fig_height_in - pad_top - header_h_in, fig_height_in - pad_top - header_h_in],
        color=accent, alpha=0.35, linewidth=1.0, zorder=3,
    )

    header_kwargs = dict(
        color=accent, fontsize=10, fontweight="bold",
        va="center", zorder=4,
    )
    ax.text(col_rank_x, header_y, "RANK", ha="center", **header_kwargs)
    ax.text(col_elo_x, header_y, "ELO", ha="center", **header_kwargs)
    ax.text(col_name_x, header_y, "NAME", ha="left", **header_kwargs)
    ax.text(col_faction_x, header_y, "MOST PLAYED FACTION", ha="left", **header_kwargs)
    ax.text(col_wdl_x, header_y, "W/D/L", ha="center", **header_kwargs)
    ax.text(col_games_x, header_y, "GAMES", ha="center", **header_kwargs)

    for i, r in enumerate(rows):
        row_top = fig_height_in - pad_top - header_h_in - i * row_h_in
        row_bottom = row_top - row_h_in
        cy = (row_top + row_bottom) / 2
        rank = r.get("Rank")

        if rank in podium_tints:
            tint = mpatches.Rectangle(
                (0.18, row_bottom), fig_width_in - 0.36, row_h_in,
                linewidth=0, facecolor=podium_tints[rank], alpha=0.35, zorder=2,
            )
            ax.add_patch(tint)

        if i < n - 1:
            ax.plot(
                [0.40, fig_width_in - 0.40],
                [row_bottom, row_bottom],
                color=border_color, alpha=0.4, linewidth=0.6, zorder=3,
                linestyle=(0, (3, 3)),
            )

        if rank in medal_colors:
            fill, text_col = medal_colors[rank]
            medal = mpatches.Circle(
                (col_rank_x, cy), 0.18,
                facecolor=fill, edgecolor="#1e1e28", linewidth=1.5, zorder=4,
            )
            ax.add_patch(medal)
            ax.text(col_rank_x, cy, str(rank),
                    ha="center", va="center",
                    color=text_col, fontsize=12, fontweight="bold", zorder=5)
        else:
            ax.text(col_rank_x, cy, str(rank), ha="center", va="center",
                    color=text_color, fontsize=14, fontweight="bold", zorder=4)

        ax.text(col_elo_x, cy, str(r.get("ELO", "")), ha="center", va="center",
                color=text_color, fontsize=13, fontweight="bold", zorder=4)

        ax.text(col_name_x, cy, str(r.get("Name", "")), ha="left", va="center",
                color=text_color, fontsize=13, fontweight="bold", zorder=4)

        faction_name = str(r.get("Most Played Faction") or "—")
        icon_path = _icon_png_path(faction_name) if faction_name and faction_name != "—" else None
        icon_size = 0.42
        if icon_path:
            try:
                img = mpimg.imread(icon_path)
                ax.imshow(img,
                          extent=[col_faction_x, col_faction_x + icon_size,
                                  cy - icon_size / 2, cy + icon_size / 2],
                          aspect="auto", zorder=5)
            except Exception:
                pass
            faction_text_x = col_faction_x + icon_size + 0.18
        else:
            faction_text_x = col_faction_x
        ax.text(faction_text_x, cy, faction_name, ha="left", va="center",
                color=faction_color, fontsize=11, style="italic", zorder=4)

        ax.text(col_wdl_x, cy, str(r.get("W/D/L", "0/0/0")), ha="center", va="center",
                color=muted_color, fontsize=12, fontweight="bold", zorder=4)

        ax.text(col_games_x, cy, str(r.get("Games Played", 0)), ha="center", va="center",
                color=muted_color, fontsize=12, fontweight="bold", zorder=4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg_color, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    buf.seek(0)
    return buf