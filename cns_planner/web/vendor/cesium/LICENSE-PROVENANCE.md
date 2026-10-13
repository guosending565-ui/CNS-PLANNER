# vendored CesiumJS 许可来源与核对记录

本目录的 `LICENSE.md` 是 **CesiumJS 1.126 官方许可原文的逐字节副本**，
用于补齐 Round37-G2 审计发现 D1（vendored Cesium 缺独立许可全文副本）。
本文件记录**来源、核对方式与结论**，供后续轮次复核，不再重复下载。

## 1. 来源

| 项 | 值 |
|---|---|
| 上游仓库 | `https://github.com/CesiumGS/cesium` |
| 标签 | `1.126`（**不是** `main`，避免"许可随主干漂移"） |
| 取用 URL | `https://raw.githubusercontent.com/CesiumGS/cesium/1.126/LICENSE.md` |
| 版本核对 URL | `https://raw.githubusercontent.com/CesiumGS/cesium/1.126/package.json` |
| 获取时间 | 2026-10-12T18:06:32Z |
| `package.json.version` | `1.126.0` |
| `package.json.license` | `Apache-2.0` |

## 2. 逐字节核对

| 文件 | 字节数 | SHA-256 |
|---|---|---|
| 官方 `LICENSE.md` @1.126 | 63630 | `3111d10e1630bfa975251018149c973a0a3f42d11e793733a8ce417d52baa59a` |
| 本目录 `LICENSE.md` | 63630 | `3111d10e1630bfa975251018149c973a0a3f42d11e793733a8ce417d52baa59a` |

两者 SHA-256 **完全一致**（`tests/test_cesium_vendor_license.py` 会持续断言该值）。

官方原文结构：

* 首行版权：`Copyright 2011-2022 CesiumJS Contributors`；
* `Apache License` / `Version 2.0, January 2004` 全文；
* `# Third-Party Code` 起的**第三方许可声明**（`zip.js`、`Knockout`、`earcut`、
  `pako`、`Draco`、`URI.js`、`meshoptimizer`、`topojson-client` 等，
  全文共 40+ 个 markdown 标题段落）。

## 3. 为什么**没有**采用本地草案

Round37-G2 在交付目录留下过一份草案
（`round37_git_checkpoint/vendor_license/cesium-1.126-LICENSE.md`，
13317 B，SHA-256 `756ec9abdd903fa72192c1c308c1d1955e2885ec9ffedd8a4dd3ebab2656eccc`）。
核对结论：

* 它由**标准 Apache-2.0 全文**（取自本机 `archspec` 包内的一份 Apache 副本）加中文说明拼成；
* 它**不含** Cesium 官方 `LICENSE.md` 的 `# Third-Party Code` 第三方声明部分
  —— 而 bundle 头部明确写着 `Portions licensed separately`，缺失部分正是合规必需项；
* 因此**不采信草案**，改用官方标签原文。草案仅作为历史记录保留在交付目录，未进入 Git。

## 4. 压缩产物**未被改动**

| 文件 | SHA-256（本轮前后一致） |
|---|---|
| `Cesium.js` | `b796f8a6f695d610cef6b86a2a51adb759b20c71ba643403a9b4489144a697e0` |
| `Widgets/widgets.css` | `702c5adf3d16ec7f1c03cc5fb7ecb823af6c59fa192789fb2c859ac7a11f4a0c` |

`Cesium.js` 头部声明与许可副本一致：

```
/**
 * @license
 * Cesium - https://github.com/CesiumGS/cesium
 * Version 1.126
 *
 * Copyright 2011-2022 Cesium Contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * ...
 * Portions licensed separately.
 * See https://github.com/CesiumGS/cesium/blob/main/LICENSE.md for full licensing details.
 */
```

本轮只**新增**许可副本与本记录，未对任何既有 vendored 字节做修改
（G2 审计 D2 记录的"裁剪子集"未在本轮补齐，属独立事项）。
