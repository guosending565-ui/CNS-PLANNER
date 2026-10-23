"""Round37-T3D-2B：vendored Cesium 的许可与版权核对（D1 收口）。

要求（任务第 5 节「Cesium 许可」）：

* 从 CesiumGS 的 **1.126 官方标签**核对许可原文
  （``https://github.com/CesiumGS/cesium/blob/1.126/LICENSE.md``）；
* 把核实后的许可与必需第三方声明补入 vendor 目录；
* **不得**直接相信未经比对的本地草案（Round37-G2 的
  ``vendor_license/cesium-1.126-LICENSE.md`` 只是标准 Apache-2.0 全文 + 中文说明，
  **不含** Cesium 官方 LICENSE.md 的第三方许可声明）；
* **不得**修改现有压缩文件字节。

因此这里断言的是**逐字节**的官方副本（SHA-256），以及 bundle 头部声明的版本与
许可，而不是"文件存在"这种弱断言。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parents[1] / "cns_planner" / "web" / "vendor" / "cesium"
LICENSE_COPY = VENDOR / "LICENSE.md"
BUNDLE = VENDOR / "Cesium.js"
WIDGETS = VENDOR / "Widgets" / "widgets.css"

#: ``https://raw.githubusercontent.com/CesiumGS/cesium/1.126/LICENSE.md`` 的 SHA-256
#: （2026-10-12 获取；同时核对 ``package.json`` 的 ``version = 1.126.0`` 与
#: ``license = Apache-2.0``）。
CESIUM_1_126_LICENSE_SHA256 = (
    "3111d10e1630bfa975251018149c973a0a3f42d11e793733a8ce417d52baa59a"
)

#: 压缩产物必须保持**逐字节不变**（任务明确禁止改动）。
CESIUM_JS_SHA256 = "b796f8a6f695d610cef6b86a2a51adb759b20c71ba643403a9b4489144a697e0"
WIDGETS_CSS_SHA256 = "702c5adf3d16ec7f1c03cc5fb7ecb823af6c59fa192789fb2c859ac7a11f4a0c"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def test_official_license_copy_is_byte_identical_to_tag_1_126():
    assert LICENSE_COPY.is_file(), "vendor 目录必须带独立许可副本"
    assert sha256_of(LICENSE_COPY) == CESIUM_1_126_LICENSE_SHA256
    text = LICENSE_COPY.read_text(encoding="utf-8")
    assert text.startswith("Copyright 2011-2022 CesiumJS Contributors")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text


def test_license_copy_carries_the_third_party_notices():
    """官方 LICENSE.md 的第三方声明部分**必须**一并落库（草案缺的正是这部分）。"""

    text = LICENSE_COPY.read_text(encoding="utf-8")
    assert "# Third-Party Code" in text
    for component in ("zip.js", "Knockout", "earcut", "pako", "Draco", "URI.js",
                      "meshoptimizer", "topojson-client"):
        assert component in text, f"缺少第三方声明：{component}"
    headings = re.findall(r"^#{1,3} .+$", text, flags=re.MULTILINE)
    assert len(headings) >= 40


def test_bundled_artifacts_are_byte_unchanged():
    assert sha256_of(BUNDLE) == CESIUM_JS_SHA256, "Cesium.js 字节被改动"
    assert sha256_of(WIDGETS) == WIDGETS_CSS_SHA256, "widgets.css 字节被改动"


def test_bundle_header_declares_the_same_version_and_license():
    with BUNDLE.open("r", encoding="utf-8", errors="replace") as handle:
        header = handle.read(1200)
    assert "@license" in header
    assert "Cesium - https://github.com/CesiumGS/cesium" in header
    assert "Version 1.126" in header
    assert "Apache License, Version 2.0" in header
    assert "Copyright 2011-2022 Cesium Contributors" in header
    # 版权声明本身明示"部分内容另有许可" ⇒ 独立许可副本（含第三方声明）是必需的。
    assert "Portions licensed separately" in header
    assert "LICENSE.md" in header


def test_provenance_record_documents_the_verification():
    record = (VENDOR / "LICENSE-PROVENANCE.md")
    assert record.is_file(), "必须留下许可来源与核对记录"
    text = record.read_text(encoding="utf-8")
    assert "github.com/CesiumGS/cesium" in text
    assert "1.126" in text
    assert CESIUM_1_126_LICENSE_SHA256 in text.lower()
    assert CESIUM_JS_SHA256 in text.lower()
    assert "Apache-2.0" in text
    # 明确记录"本地草案未被采信"的原因，避免下一轮又拿草案当权威。
    assert "草案" in text
