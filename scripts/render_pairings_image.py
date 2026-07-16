"""Render a system's weekly pairings as a stack of card-style PNGs, mirroring
the public /pairings matchup card UI.

Ported from the original Streamlit app's render_pairings_image(). Input rows
use the same field shape as admin.py's _build_display_row() output
(a_name/a_faction/b_name/b_faction/type/eta/points) — adapted here to the
internal A/Faction A/B/Faction B/Type/ETA/Points names the layout code uses.

No database access — pure rendering given a list of display-row dicts.
"""
import io
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.image as mpimg

def _fit_fontsize(fig, text: str, max_width_in: float, base_size: int = 15, min_size: int = 9) -> int:
    """Return the largest fontsize <= base_size (down to min_size) at which
    `text` rendered bold fits within max_width_in inches, measured via the
    figure's renderer."""
    if not text:
        return base_size
    renderer = fig.canvas.get_renderer()
    for size in range(base_size, min_size - 1, -1):
        t = fig.text(0, 0, text, fontsize=size, fontweight="bold")
        bbox = t.get_window_extent(renderer=renderer)
        width_in = bbox.width / fig.dpi
        t.remove()
        if width_in <= max_width_in:
            return size
    return min_size

def _faction_slug(name: str | None) -> str:
    """Convert a faction name into a filename slug.
    e.g. 'Orc & Goblin Tribes' -> 'orc_and_goblin_tribes'."""
    if not name:
        return ""
    s = name.lower().strip()
    s = s.replace("&", "and")
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    s = "_".join(s.split())
    return s


def _icon_png_path(faction_name: str | None) -> str | None:
    if not faction_name:
        return None
    slug = _faction_slug(faction_name)
    # This file lives one directory below the repo root (scripts/); icons/ is
    # a repo-root asset, not a sibling of this file, so go up one level.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icons_root = os.path.join(repo_root, "icons")
    search_dirs = [
        icons_root,
        os.path.join(icons_root, "TOW"),
        os.path.join(icons_root, "HH"),
        os.path.join(icons_root, "KT"),
    ]
    for ext in ("png", "jpg"):
        for d in search_dirs:
            p = os.path.join(d, f"{slug}.{ext}")
            if os.path.exists(p):
                return p
    return None


def _to_render_rows(display_rows: list[dict]) -> list[dict]:
    """Adapt _build_display_row()'s output shape to the field names this
    renderer's layout code was written against."""
    out = []
    for r in display_rows:
        out.append({
            "A": r.get("a_name"),
            "Faction A": r.get("a_faction"),
            "B": r.get("b_name"),
            "Faction B": r.get("b_faction"),
            "Type": r.get("type"),
            "ETA": r.get("eta"),
            "Points": r.get("points"),
        })
    return out


def render_pairings_image(display_rows: list[dict], week: str, system: str) -> io.BytesIO | None:
    """Render pairings as a stack of card-style PNGs.

    display_rows: list of dicts in _build_display_row() output shape
    (a_name/a_faction/b_name/b_faction/type/eta/points).
    Returns a BytesIO buffer positioned at start, or None if rows is empty.
    """
    rows = _to_render_rows(display_rows)
    if not rows:
        return None

    # ---- Card style constants (mirroring the public CSS) ----
    bg_color = "#161620"
    card_bg = "#1e1e28"
    name_color = "#f4e9c8"
    faction_color = "#b8a878"
    meta_label_color = "#b8a878"
    meta_value_color = "#f0e4bc"
    vs_color = "#c9a14a"
    bye_color = "#8a8270"
    border_default = "#5a4a26"

    accent_by_type = {
        ("The Old World", "intro"):       "#6eb46e",
        ("The Old World", "casual"):      "#c9a14a",
        ("The Old World", "competitive"): "#d25050",
        ("The Horus Heresy", "intro"):    "#6eb46e",
        ("The Horus Heresy", "standard"): "#c9a14a",
        ("Kill Team", "intro"):           "#6eb46e",
        ("Kill Team", "standard"):        "#c9a14a",
    }

    def _accent(game_type: str | None) -> str:
        gt = (game_type or "").strip().lower()
        return accent_by_type.get((system, gt), border_default)

    n = len(rows)
    fig_width_in = 13.0
    card_height_in = 1.45
    gap_in = 0.18
    top_pad_in = 0.18
    bot_pad_in = 0.18
    fig_height_in = top_pad_in + bot_pad_in + n * card_height_in + (n - 1) * gap_in

    fig = plt.figure(figsize=(fig_width_in, fig_height_in), dpi=200)
    fig.patch.set_facecolor(bg_color)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_width_in)
    ax.set_ylim(0, fig_height_in)
    ax.set_aspect("equal")
    ax.axis("off")

    pad_x = 0.18
    inner_pad = 0.30

    for i, r in enumerate(rows):
        card_top = fig_height_in - top_pad_in - i * (card_height_in + gap_in)
        card_bottom = card_top - card_height_in
        card_left = pad_x
        card_right = fig_width_in - pad_x
        card_w = card_right - card_left

        accent = _accent(r.get("Type"))
        rect = mpatches.FancyBboxPatch(
            (card_left, card_bottom), card_w, card_height_in,
            boxstyle="round,pad=0,rounding_size=0.10",
            linewidth=2.0,
            edgecolor=accent,
            facecolor=card_bg,
            zorder=1,
        )
        ax.add_patch(rect)

        idx_x = card_left + inner_pad + 0.08
        cy = (card_top + card_bottom) / 2

        icon_size = 0.78
        icon_a_x = idx_x + 0.45
        name_a_x = icon_a_x + icon_size + 0.18

        # ---- Compute meta-block geometry first — depends only on
        # Type/ETA/Points and fixed icon sizes, not on player names ----
        right_edge = card_right - inner_pad
        meta_pairs = []
        if r.get("Type"):    meta_pairs.append(("TYPE", str(r["Type"])))
        if r.get("ETA"):     meta_pairs.append(("ETA", str(r["ETA"])))
        if r.get("Points"):  meta_pairs.append(("PTS", str(r["Points"])))

        char_w_value = 0.10
        FIXED_COL_WIDTHS = {
            "TYPE": len("Competitive") * char_w_value,
            "ETA":  len("18:30") * char_w_value,
            "PTS":  len("10000") * char_w_value,
        }
        meta_gap = 0.40

        meta_x_positions = []
        running_x = right_edge
        for label, value in reversed(meta_pairs):
            block_w = FIXED_COL_WIDTHS.get(label, len(value) * char_w_value)
            x_left = running_x - block_w
            meta_x_positions.append((x_left + block_w / 2, label, value, block_w))
            running_x = x_left - meta_gap
        meta_x_positions.reverse()

        if meta_x_positions:
            first_x_center, _, _, first_w = meta_x_positions[0]
            meta_left = first_x_center - first_w / 2
        else:
            meta_left = right_edge

        # ---- Compute icon B / separator / VS geometry — also independent
        # of player names ----
        b_name = r.get("B")
        is_bye = (not b_name) or str(b_name).strip().upper().startswith("BYE")
        b_text = "BYE / Standby" if is_bye else str(b_name)

        icon_b_x_right = meta_left - 0.50
        icon_b_x_left = icon_b_x_right - icon_size
        name_b_right = icon_b_x_left - 0.18

        left_content_end = name_a_x
        sep_x = (icon_b_x_right + meta_left) / 2
        vs_x = left_content_end + (sep_x - left_content_end) * 0.40

        # ---- Now draw everything, fitting names to the real space
        # available before VS on each side ----
        ax.text(idx_x, cy, f"{i + 1}", color=name_color, fontsize=18, fontweight="bold",
                ha="left", va="center", zorder=3)

        icon_a_path = _icon_png_path(r.get("Faction A"))
        if icon_a_path:
            try:
                img = mpimg.imread(icon_a_path)
                ax.imshow(img,
                          extent=[icon_a_x, icon_a_x + icon_size,
                                  cy - icon_size / 2, cy + icon_size / 2],
                          aspect="auto", zorder=4)
            except Exception:
                pass

        name_a = str(r.get("A") or "").strip()
        faction_a = str(r.get("Faction A") or "").strip() or "—"
        name_a_max_w = max(vs_x - name_a_x - 0.15, 0.5)
        name_a_fontsize = _fit_fontsize(fig, name_a, max_width_in=name_a_max_w)
        ax.text(name_a_x, cy + 0.18, name_a, color=name_color, fontsize=name_a_fontsize,
                fontweight="bold", ha="left", va="center", zorder=3)
        ax.text(name_a_x, cy - 0.18, faction_a, color=faction_color, fontsize=11,
                style="italic", ha="left", va="center", zorder=3)

        for x_center, label, value, _w in meta_x_positions:
            ax.text(x_center, cy + 0.22, label, color=meta_label_color, fontsize=8,
                    fontweight="bold", ha="center", va="center", zorder=3)
            ax.text(x_center, cy - 0.13, value, color=meta_value_color, fontsize=12,
                    fontweight="bold", ha="center", va="center", zorder=3)

        if not is_bye:
            icon_b_path = _icon_png_path(r.get("Faction B"))
            if icon_b_path:
                try:
                    img = mpimg.imread(icon_b_path)
                    ax.imshow(img,
                              extent=[icon_b_x_left, icon_b_x_right,
                                      cy - icon_size / 2, cy + icon_size / 2],
                              aspect="auto", zorder=4)
                except Exception:
                    pass

        b_color = bye_color if is_bye else name_color
        b_style = "italic" if is_bye else "normal"
        b_weight = "normal" if is_bye else "bold"
        name_b_max_w = max(name_b_right - vs_x - 0.15, 0.5)
        name_b_fontsize = _fit_fontsize(fig, b_text, max_width_in=name_b_max_w)
        ax.text(name_b_right, cy + 0.18, b_text, color=b_color, fontsize=name_b_fontsize,
                fontweight=b_weight, ha="right", va="center", zorder=3, style=b_style)
        if not is_bye:
            faction_b = str(r.get("Faction B") or "").strip() or "—"
            ax.text(name_b_right, cy - 0.18, faction_b, color=faction_color, fontsize=11,
                    style="italic", ha="right", va="center", zorder=3)

        sep_top = card_top - 0.22
        sep_bot = card_bottom + 0.22
        ax.plot([sep_x, sep_x], [sep_bot, sep_top],
                color=accent, alpha=0.35, linewidth=1.0, zorder=2,
                solid_capstyle="round")

        ax.text(vs_x, cy, "VS", color=vs_color, fontsize=18, fontweight="bold",
                ha="center", va="center", zorder=3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg_color, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    buf.seek(0)
    return buf