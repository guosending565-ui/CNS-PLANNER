"""图例版面几何（**纯计算**，不依赖 QGIS）。

地图渲染器（:mod:`cns_planner.gis.qgis_figure_renderer`）与版面规划
（:mod:`cns_planner.application.map_figure_service`）**共用**这里的同一套分列算法，
因此"图例框高度"与"实际画出来的行"永远一致，不会出现框高与内容不匹配的空白。
"""

from __future__ import annotations

from .figure_style import LAYOUT, LEGEND_GROUPS

#: 最多允许的图例列数（横向展开，避免纵向长列表）。
MAX_LEGEND_COLUMNS = 3


def legend_segments(entries):
    """按语义分组切段：``[{"group": key, "items": [entry, ...]}, ...]``（顺序稳定）。"""

    segments, current = [], None
    for entry in entries:
        group = entry.get("group") or ""
        if current is None or current["group"] != group:
            current = {"group": group, "items": []}
            segments.append(current)
        current["items"].append(entry)
    return segments


def segment_heights(segments, *, row_height=None, group_row=None):
    """每一段的高度（组标题行 + 该组条目行）。"""

    row = float(row_height if row_height is not None else LAYOUT["legend_row_mm"])
    group = float(group_row if group_row is not None else LAYOUT["legend_group_row_mm"])
    gap = float(LAYOUT["legend_group_gap_mm"])
    heights = []
    for segment in segments:
        header = (group + gap) if segment["group"] else 0.0
        heights.append(header + len(segment["items"]) * row)
    return heights


def plan_columns(segments, *, row_height=None, group_row=None, columns=None):
    """把分组段分到各列（保持顺序、整组不拆分）。

    返回 ``(columns_used, [(segment_index, column), ...], heights)``。

    目标：**列数尽量少（横向紧凑）且各列高度尽量均衡**（避免"框很高、只有左侧有内容"）。
    做法：对 1 / 2 / 3 列分别穷举所有保序分列方案（段数很少，枚举代价可忽略），
    取"最大列高最小"的方案；列数相同时取更矮的那一个。
    """

    row = float(row_height if row_height is not None else LAYOUT["legend_row_mm"])
    group = float(group_row if group_row is not None else LAYOUT["legend_group_row_mm"])
    heights = segment_heights(segments, row_height=row, group_row=group)
    limit = MAX_LEGEND_COLUMNS if columns is None else max(1, min(MAX_LEGEND_COLUMNS, int(columns)))
    preferred = limit
    if not heights:
        return 1, [], []
    best = None
    for candidate in range(1, min(limit, len(heights)) + 1):
        for placement in _placements(len(heights), candidate):
            column_heights = [0.0] * candidate
            for index, column in enumerate(placement):
                column_heights[column] += heights[index]
            # 首选列数（模板声明，默认 2 列横向展开）；列数相同时取各列更均衡（最高列最矮）。
            score = (0 if candidate == preferred else 1, candidate,
                     round(max(column_heights), 3))
            if best is None or score < best[0]:
                best = (score, candidate, placement)
    if best is None:  # pragma: no cover - heights 非空时前面的循环必然给出结果
        return 1, [(index, 0) for index in range(len(heights))], heights
    return best[1], list(enumerate(best[2])), heights


def _placements(count, columns):
    """所有**保序且列号非递减**的分列方案（每列非空、列号从 0 连续到 columns-1）。"""

    def walk(index, current, result):
        if index == count:
            if len(set(current)) == columns and max(current) == columns - 1:
                result.append(tuple(current))
            return
        highest = max(current) if current else 0
        for column in range(highest, min(columns, highest + 2)):
            walk(index + 1, current + [column], result)

    result = []
    walk(0, [], result)
    return result


def legend_geometry(entries, *, row_height=None, group_row=None, columns=None,
                    header_height=None):
    """一次性给出图例的列数、每行位置与整框高度。

    ``entries`` 是**图例条目**列表（每项含 ``group``）。返回：

    * ``columns``：实际列数；
    * ``rows``：``[(column, y_offset_mm, kind, text, style_key), ...]``；
    * ``height_mm``：框高（含标题行），列高取最大值。
    """

    row = float(row_height if row_height is not None else LAYOUT["legend_row_mm"])
    group = float(group_row if group_row is not None else LAYOUT["legend_group_row_mm"])
    gap = float(LAYOUT["legend_group_gap_mm"])
    title_header = float(header_height if header_height is not None else LAYOUT["legend_header_mm"])
    segments = legend_segments(entries)
    columns, placement, heights = plan_columns(
        segments, row_height=row, group_row=group, columns=columns,
    )
    titles = dict(LEGEND_GROUPS)
    offsets = [title_header] * columns
    rows = []
    for index, column in placement:
        segment = segments[index]
        if segment["group"]:
            rows.append((column, offsets[column] + gap, "group",
                         titles.get(segment["group"], segment["group"]), None))
            offsets[column] += group + gap
        for entry in segment["items"]:
            rows.append((column, offsets[column], "item", entry["text"], entry["style_key"]))
            offsets[column] += row
    content = max(offsets) if offsets else title_header
    return {
        "columns": columns,
        "rows": rows,
        # 框高 = 内容最大 y（y 已从标题行下方起算），保证"框贴着内容且不截断"。
        "height_mm": content,
        "content_mm": content - title_header,
        "box_height_mm": content,
        "title_height_mm": title_header,
        "row_mm": row,
        "group_row_mm": group,
    }


def legend_height_for(entries, *, row_height=None, group_row=None, columns=None,
                      header_height=None):
    """只算框高（供版面规划使用，避免重复实现分列逻辑）。"""

    return legend_geometry(
        entries, row_height=row_height, group_row=group_row, columns=columns,
        header_height=header_height,
    )["height_mm"]


__all__ = [
    "MAX_LEGEND_COLUMNS", "legend_geometry", "legend_height_for", "legend_segments",
    "plan_columns", "segment_heights",
]
