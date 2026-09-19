# 数据可信度与规划输入就绪报告

报告只读取现有证据；不造数据、不猜 CRS、不解析 ET、不自动确认 policy。

## Source audit


## DATA-1 / DATA-2

- **DATA-1** `blocked` reference CRS；原因：reference_landing_sites, reference_routes；人工动作：人工输入有证据的 CRS 并确认
- **DATA-2** `blocked` ET→XLSX/CSV；原因：not_calculated；人工动作：转换 ET 后预览并确认导入 CSV/XLSX/GeoJSON

## Geometry health

```json
{
  "reference_landing_sites": {
    "status": "not_calculated",
    "feature_count": 0,
    "null": 0,
    "empty": 0,
    "invalid": 0,
    "unsupported": 0,
    "extent": null,
    "crs": {
      "source_crs": {
        "value": null,
        "status": "pending_confirmation",
        "axis_order": null,
        "confirmed": false,
        "source": null,
        "evidence": []
      },
      "representation_crs": {
        "value": null,
        "status": "pending_confirmation",
        "axis_order": null,
        "declared_by_format": false,
        "source": null,
        "evidence": []
      },
      "note": "源表未声明 CRS；坐标仅按源数值临时展示，不得自动假定 WGS84/CGCS2000。"
    },
    "repair_applied": false
  },
  "reference_routes": {
    "status": "not_calculated",
    "feature_count": 0,
    "null": 0,
    "empty": 0,
    "invalid": 0,
    "unsupported": 0,
    "extent": null,
    "crs": {
      "source_crs": {
        "value": null,
        "status": "pending_confirmation",
        "axis_order": null,
        "confirmed": false,
        "source": null,
        "evidence": []
      },
      "representation_crs": {
        "value": null,
        "status": "pending_confirmation",
        "axis_order": null,
        "declared_by_format": false,
        "source": null,
        "evidence": []
      },
      "note": "源文件未声明 CRS；CSV/XLSX 不得被自动假定为 WGS84/CGCS2000。source_crs 未确认前不输出正式 length_m，也不输出米制几何相似度。"
    },
    "repair_applied": false
  },
  "airspace": {
    "status": "not_calculated"
  }
}
```
