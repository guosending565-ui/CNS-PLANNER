"""图例版面几何（**纯计算**，不依赖 QGIS）。

地图渲染器（:mod:`cns_planner.gis.qgis_figure_renderer`）与版面规划
（:mod:`cns_planner.application.map_figure_service`）**共用**这里的同一套分列算法，
因此"图例框高度"与"实际画出来的行"永远一致，不会出现框高与内容不匹配的空白。
"""

from __future__ import annotations

from .figure_style import LAYOUT, LEGEND_GROUPS

#: 最多允许的图例列数（横向展开，避免纵向长列表）。
MAX_LEGEND_COLUMNS = 3

#: CNS 专题图的语义分列**均衡回退阈值**（见 :func:`plan_columns`）。
#: 语义分列的最大列高超过自动最优方案的该倍数时改用自动均衡，
#: 保证条目更多的 CNS 图不会出现"一列到底、另一列大半空白"。
CNS_LEGEND_BALANCE_TOLERANCE = 1.10


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


def segment_heights(segments, *, row_height=None, group_row=None, group_gap=None,
                    group_item_gap=None):
    """每一段的高度（组标题行 + 组内条目行 + 组与组之间的统一间距）。"""

    row = float(row_height if row_height is not None else LAYOUT["legend_row_mm"])
    group = float(group_row if group_row is not None else LAYOUT["legend_group_row_mm"])
    gap = float(group_gap if group_gap is not None else LAYOUT["legend_group_gap_mm"])
    item_gap = float(
        group_item_gap if group_item_gap is not None
        else LAYOUT["legend_group_item_gap_mm"]
    )
    heights = []
    for segment in segments:
        header = (group + item_gap + gap) if segment["group"] else 0.0
        heights.append(header + len(segment["items"]) * row)
    return heights


def plan_columns(segments, *, row_height=None, group_row=None, columns=None,
                 group_gap=None, group_item_gap=None, group_columns=None,
                 balance_tolerance=None):
    """把分组段分到各列（保持顺序、整组不拆分）。

    返回 ``(columns_used, [(segment_index, column), ...], heights)``。

    分列优先级：

    1. ``group_columns`` 给出**语义分列**（组名 → 列号）且实际出现的组都有登记
       → 直接采用。这正是产品建议的两列分组：左列 = 地理环境 + 障碍物，
       右列 = 既有设施 + 规划航路；4 : 5 的条目分布本身就很均衡；
    2. 否则回退到**自动均衡**：对 1 / 2 / 3 列分别穷举保序分列方案（段数很少，
       枚举代价可忽略），取"最大列高最小"的方案；列数相同时取更矮的那一个。

    ``balance_tolerance`` 给出一个**均衡回退阈值**：语义分列只有在它的最大列高不超过
    自动最优方案 ``max * balance_tolerance`` 时才被采用。CNS 专题图的分组更多，
    纯语义分列会让某一列明显偏长；此时改用自动均衡（仍然整组不拆、仍然保序，
    因此分组语义不受影响），图例框才不会出现"一列到底、另一列大半空白"。
    ``None`` 表示始终采用语义分列（``route_overview_v1`` 的历史行为不变）。
    """

    row = float(row_height if row_height is not None else LAYOUT["legend_row_mm"])
    group = float(group_row if group_row is not None else LAYOUT["legend_group_row_mm"])
    heights = segment_heights(
        segments, row_height=row, group_row=group, group_gap=group_gap,
        group_item_gap=group_item_gap,
    )
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
    semantic = _semantic_placement(segments, group_columns, limit)
    if semantic is not None:
        used, placement = semantic
        if balance_tolerance is None or best is None:
            return used, list(enumerate(placement)), heights
        semantic_heights = [0.0] * used
        for index, column in enumerate(placement):
            semantic_heights[column] += heights[index]
        automatic_peak = max(
            sum(heights[index] for index, value in enumerate(best[2]) if value == column)
            for column in range(best[1])
        )
        if max(semantic_heights) <= automatic_peak * float(balance_tolerance):
            return used, list(enumerate(placement)), heights
    if best is None:  # pragma: no cover - heights 非空时前面的循环必然给出结果
        return 1, [(index, 0) for index in range(len(heights))], heights
    return best[1], list(enumerate(best[2])), heights


def _semantic_placement(segments, group_columns, limit):
    """语义分列：``{组名: 列号}``。任何未登记的组都让整体回退到自动分列。"""

    if not group_columns:
        return None
    placement, used = [], set()
    for segment in segments:
        group = segment.get("group") or ""
        column = group_columns.get(group)
        if column is None:
            return None
        column = int(column)
        if column < 0 or column >= limit:
            return None
        placement.append(column)
        used.add(column)
    if not used or sorted(used) != list(range(len(used))):
        return None
    return len(used), placement


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
                    header_height=None, group_gap=None, group_item_gap=None,
                    group_columns=None, top_padding=None, balance_tolerance=None):
    """一次性给出图例的列数、每行位置与整框高度。

    ``entries`` 是**图例条目**列表（每项含 ``group``）。返回：

    * ``columns``：实际列数；
    * ``rows``：``[(column, y_offset_mm, kind, text, style_key), ...]``；
    * ``height_mm`` / ``box_height_mm``：框高（含标题行），列高取最大值。

    统一的二维网格模型（版式收口的关键）：

    * **标题独占一行**（``title_height_mm``），位于框内左上；
    * 每列内容从 ``header_height + top_padding`` 开始，标题与内容之间不留大块空白；
    * 组标题与组内条目行高统一（``group_row_mm`` / ``row_mm``），符号框宽度固定、
      文本起始 x 固定，因此面 / 线 / 点符号在同一基线上对齐；
    * 组标题 → 组内条目用**小**间距（``group_item_gap``），组与组之间用**更大但统一**
      的间距（``group_gap``）。
    """

    row = float(row_height if row_height is not None else LAYOUT["legend_row_mm"])
    group = float(group_row if group_row is not None else LAYOUT["legend_group_row_mm"])
    gap = float(group_gap if group_gap is not None else LAYOUT["legend_group_gap_mm"])
    item_gap = float(
        group_item_gap if group_item_gap is not None
        else LAYOUT["legend_group_item_gap_mm"]
    )
    padding = float(
        top_padding if top_padding is not None else LAYOUT["legend_top_padding_mm"]
    )
    title_header = float(header_height if header_height is not None else LAYOUT["legend_header_mm"])
    segments = legend_segments(entries)
    columns, placement, _heights = plan_columns(
        segments, row_height=row, group_row=group, columns=columns,
        group_gap=gap, group_item_gap=item_gap, group_columns=group_columns,
        balance_tolerance=balance_tolerance,
    )
    titles = dict(LEGEND_GROUPS)
    offsets = [title_header + padding] * columns
    started = [False] * columns
    rows = []
    for index, column in placement:
        segment = segments[index]
        if segment["group"]:
            if started[column]:
                offsets[column] += gap
            rows.append((column, offsets[column], "group",
                         titles.get(segment["group"], segment["group"]), None))
            offsets[column] += group
            started[column] = True
        first = True
        for entry in segment["items"]:
            if first and segment["group"]:
                offsets[column] += item_gap
                first = False
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
        "group_gap_mm": gap,
        "group_item_gap_mm": item_gap,
        "top_padding_mm": padding,
    }


def legend_height_for(entries, *, row_height=None, group_row=None, columns=None,
                      header_height=None):
    """只算框高（供版面规划使用，避免重复实现分列逻辑）。"""

    return legend_geometry(
        entries, row_height=row_height, group_row=group_row, columns=columns,
        header_height=header_height,
    )["height_mm"]


__all__ = [
    "CNS_LEGEND_BALANCE_TOLERANCE", "MAX_LEGEND_COLUMNS", "legend_geometry",
    "legend_height_for", "legend_segments", "plan_columns", "segment_heights",
]
