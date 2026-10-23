"""Step5 **中立** land-mask 只读分类源装配（Round 2 P0 收口）。

本模块是 Communication / RID 正式 surface classification 的**唯一**陆域源入口：

* 它只消费 ``surface_classification_policy``（中立策略）+ 显式 land-mask 数据源路径；
* 它**绝不**读取 ``radar_surveillance_policy``，也不依赖 Radar layout 的运行时
  ``facts_provider`` —— 新项目无需先运行任何 Radar layout 就能生成 surface facts；
* 唯一分类实现仍是 :class:`cns_planner.gis.radar_layout_adapter.LandMaskSource`，
  本模块不复制任何 polygon / shapely / 海岸线算法，也不提供"用 DEM NoData 推断海洋"
  的任何路径。

旧项目的兼容参数由 :mod:`cns_planner.domain.surface_classification` 的**一次性**
迁移函数在 normalize/backfill 阶段写入中立策略；运行时不存在任何 Radar 回退。
"""

from __future__ import annotations

from ..domain.source_audit import sha256_file
from ..domain.surface_classification import normalize_surface_classification_policy
from .radar_layout_adapter import LandMaskSource


def land_mask_source_path(state, explicit_path=None):
    """land-mask 数据源路径：显式注入优先，其次项目 state 中已登记的来源记录。

    只解析**数据源路径**这一项事实（与任何 policy 无关）；没有配置时返回 ``None``，
    调用方据此 fail-closed（全部 ``unknown``），绝不猜测。
    """

    if explicit_path:
        return explicit_path
    if not isinstance(state, dict):
        return None
    recorded = state.get("result_index")
    recorded = recorded.get("land_mask") if isinstance(recorded, dict) else None
    if isinstance(recorded, dict) and recorded.get("path"):
        return recorded["path"]
    paths = state.get("data_source_paths")
    value = paths.get("land_mask") if isinstance(paths, dict) else None
    return value or None


def classification_parameters(policy):
    """中立策略里的陆域分类参数（``(layer_name, coastal_buffer_m)``）。

    ``layer_name=None`` 表示由既有读取器按保守关键词自动选层，不是"未配置"。
    """

    normalized = normalize_surface_classification_policy(policy)
    return (
        normalized["land_mask_layer_name"],
        normalized["coastal_uncertainty_buffer_m"],
    )


def build_land_mask_source(path, policy):
    """按**中立** surface classification policy 构造只读陆域分类源。

    未配置数据源路径时返回 ``None``（fail-closed）：``surface_class_facts`` 的每一格
    保持 ``unknown``，**不是**"全部是海"。
    """

    if not path:
        return None
    layer_name, buffer_m = classification_parameters(policy)
    return LandMaskSource(
        path, layer_name=layer_name, coastal_uncertainty_buffer_m=buffer_m,
    )


def audit_manifest_for(state, role="land_mask"):
    """项目既有 source audit manifest（唯一权威的来源审计记录）。"""

    if not isinstance(state, dict):
        return {}
    audits = state.get("source_audits")
    items = audits.get("items") if isinstance(audits, dict) else None
    manifest = items.get(role) if isinstance(items, dict) else None
    return manifest if isinstance(manifest, dict) else {}


def land_mask_content_sha256(path, audit_manifest=None):
    """land-mask 内容 SHA-256：**复用**既有 source audit / ``sha256_file``。

    优先使用 source audit 已记录的 ``verification.sha256``；没有时才用既有的
    :func:`cns_planner.domain.source_audit.sha256_file` 现算一次。绝不新造第二套
    文件 hash 系统。
    """

    manifest = audit_manifest if isinstance(audit_manifest, dict) else {}
    verification = manifest.get("verification")
    recorded = verification.get("sha256") if isinstance(verification, dict) else None
    recorded = str(recorded or manifest.get("sha256") or "").strip()
    if recorded:
        return recorded
    if not path:
        return None
    try:
        return sha256_file(path)
    except (OSError, ValueError):
        return None


def land_mask_file_signature(path):
    """land-mask 文件的**廉价**签名（``size_bytes`` + ``mtime_ns``），读不到返回 ``None``。

    Round35-D：读取层缓存键过去只含 ``(path, layer_name, buffer_m)``，因此**同一路径替换
    文件内容**后，同一进程内会继续复用旧对象。这里只读一次 ``stat()`` 元数据 ——
    **不**重新扫描大型 GIS 文件，所以普通请求依然廉价；同时内容一换，签名即变，缓存失效。
    """

    if not path:
        return None
    try:
        from pathlib import Path

        stat = Path(str(path)).stat()
        return (int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return None


#: 参与 ``input_fingerprint`` 的身份字段白名单（Round35-D）。
#:
#: 只有**真正决定分类结果**且**取值与调用时序无关**的字段才允许进入指纹：
#:
#: * ``source_role`` / ``source_type`` —— 来源角色（land_mask / real）；
#: * ``layer_name`` —— 选中的图层身份（构造期参数，不依赖 ``prepare()``）；
#: * ``content_sha256`` —— 文件内容身份（复用 source audit 的 ``verification.sha256``）；
#: * ``identity_basis`` —— 身份依据（``content_sha256`` / ``declared_source_facts``）。
#:
#: **不进入指纹**的字段（它们是"读法"与"审计事实"，不是来源身份）：
#: ``layer_resolution`` / ``source_crs`` / ``polygon_count`` / ``size_bytes`` /
#: ``mtime_ns``。它们在 ``describe()`` 早于 / 晚于 ``prepare()`` 时取值不同，
#: 曾经让**同一份文件、同一策略、同一逐格分类**产出三个不同指纹（Round35-C §5）。
#: 这些字段改由 :func:`land_mask_source_audit_facts` 输出，仅作审计展示。
STABLE_IDENTITY_FIELDS = (
    "source_role", "source_type", "layer_name", "content_sha256", "identity_basis",
)


def land_mask_source_identity(describe=None, *, audit_manifest=None, content_sha256=None):
    """surface facts 的**稳定来源身份**（进入 input fingerprint）。

    稳定性规则：

    * 身份**只**由 :data:`STABLE_IDENTITY_FIELDS` 组成：来源角色 / 来源类型 / 图层名 /
      内容 SHA-256 / 身份依据；它们都与 ``describe()`` 相对 ``prepare()`` 的调用时序无关；
    * ``identity_basis = "content_sha256"`` 时身份由**内容**决定，因此同一份来源搬迁
      目录、或 mtime 变化都不会造成假变化；来源内容改变即使逐格分类结果相同，
      指纹也一定改变；
    * 拿不到 hash 时退回**声明事实**（role / layer），并显式标记
      ``declared_source_facts``——此时逐格分类本身就是 fail-closed 的 ``unknown``；
    * ``layer_resolution`` / ``source_crs`` / ``polygon_count`` / ``size_bytes`` 等
      "审计事实"**绝不**进入身份（见 :data:`STABLE_IDENTITY_FIELDS` 的说明）；
    * **绝不**把绝对文件路径放进身份（项目 C:/D: 搬迁不得造成假变化）。
    """

    describe = describe if isinstance(describe, dict) else {}
    manifest = audit_manifest if isinstance(audit_manifest, dict) else {}
    digest = str(content_sha256 or "").strip() or None
    return {
        "source_role": describe.get("source_role") or manifest.get("role") or "land_mask",
        "source_type": describe.get("source_type") or "real",
        "layer_name": describe.get("layer_name"),
        "content_sha256": digest,
        "identity_basis": "content_sha256" if digest else "declared_source_facts",
    }


def land_mask_source_audit_facts(describe=None, *, audit_manifest=None,
                                 source_path=None):
    """**不参与指纹**的审计事实（几何与读法），供来源登记与界面展示。

    这些字段的取值依赖 ``describe()`` 是否在 ``prepare()`` 之后调用，因此**不得**
    进入 ``input_fingerprint``；但它们对"登记层几何事实不为 null"是可审计的补充。
    拿不到时保持 ``None``，绝不猜测。
    """

    describe = describe if isinstance(describe, dict) else {}
    manifest = audit_manifest if isinstance(audit_manifest, dict) else {}
    path = source_path or describe.get("configured_path")
    size_bytes = manifest.get("size_bytes")
    if size_bytes is None and path:
        try:
            from pathlib import Path

            candidate = Path(str(path))
            if candidate.is_file():
                size_bytes = int(candidate.stat().st_size)
        except OSError:
            size_bytes = None
    return {
        "layer_resolution": describe.get("layer_resolution"),
        "source_crs": describe.get("source_crs"),
        "metric_crs": describe.get("metric_crs"),
        "crs_status": describe.get("crs_status"),
        "polygon_count": describe.get("polygon_count"),
        "size_bytes": size_bytes,
        "purpose": "audit_only_not_in_input_fingerprint",
    }


__all__ = [
    "STABLE_IDENTITY_FIELDS", "audit_manifest_for", "build_land_mask_source",
    "classification_parameters", "land_mask_content_sha256", "land_mask_file_signature",
    "land_mask_source_audit_facts", "land_mask_source_identity", "land_mask_source_path",
]
