# Round37-T3D-BMAP 高保真三维底图设计

## 1. 目标与边界

在复用 Round37-B0/T3D-1 未提交工作树增量的前提下，将当前“单窗口 FABDEM + 离线山体阴影 + 默认业务覆盖”的原型升级为可人工视觉验收的舟山三维基础地理环境。

本轮只改只读三维投影、Cesium 展示和验收工具；不修改 schema-v2 `ProjectState`、规划算法、失效链、正式项目或权威数据。所有新接口均为 GET，服务仅绑定 localhost，不新增状态写入者。CesiumJS 固定 1.126.0，FABDEM、EGM2008 geoid、buildings v3 原件只读且保持既有 SHA256。

## 2. 已裁定的影像路线

默认源为 ArcGIS World Imagery，采用当前 ArcGIS Static Basemap Tiles/World Imagery MapServer 的同一官方接口族；实现前以服务元数据和真实瓦片确认 tile size、LOD、版权、token 权限及计费口径，不使用匿名 `services.arcgisonline.com` 成功响应替代授权验证。

运行时凭证只读取 `CNS_ARCGIS_TOKEN`，备用天地图只读取 `CNS_TIANDITU_TK`。凭证不得进入项目状态、Git、前端响应、错误正文、请求日志、截图或交付证据。现有 QGIS 工程只含天地图矢量与注记，且其内嵌 tk 对 `img_w` 实测返回 403，因此既不是候选 A 的卫星影像，也不能作为候选 B 的合法凭证。

后端提供固定路由的瓦片代理：来源、主机、服务路径、舟山 bbox、z/x/y 范围、最大级别、响应 MIME 和最大字节数全部白名单校验；不接受任意 URL，不跟随跨主机重定向，不持久缓存商业影像。浏览器缓存头按上游许可保守透传或收紧。代理记录脱敏后的来源、状态、字节数、耗时与请求计数。

无合法凭证或上游返回 401/403 时，manifest 和页面明确标记 `blocked`；不自动调用匿名 ArcGIS 旧端点，也不把 T3D-1 单色山体阴影宣称为卫星底图。完整 Esri 与数据提供者版权常驻显示，贡献者从官方 attribution/metadata 接口读取并脱敏缓存。

## 3. 多级 FABDEM 地形

采用 Cesium 原生 `CesiumTerrainProvider` 可读取的 `heightmap-1.0` 多级瓦片，而不是继续使用整窗口 `CustomHeightmapTerrainProvider`。新增：

- `GET /api/visualization-3d/terrain/layer.json`
- `GET /api/visualization-3d/terrain/{z}/{x}/{y}.terrain`

瓦片使用 EPSG:4326 TMS、65×65 共边采样，最大 LOD 13（约 30–35 m 采样间隔，与 FABDEM 30 m 匹配），父子瓦片由 Cesium 按屏幕误差自动替换。相邻瓦片边界从相同经纬度采样点生成，避免裂缝；不再提供“替换整个 provider”的手动细化按钮。

每个样本先取得 FABDEM EGM2008 正高 `H`，再用既有 canonical PROJ `vgridshift` 取得逐点 `N`，写入椭球高 `h = H + N`。不更改权威 DEM。海面和 FABDEM NoData 的显示高度使用显式背景源 `EGM2008 orthometric sea level H=0`，即椭球高 `N`；该背景只参与可视化，永不进入规划、碰撞或正式结果。

低级别提供全球显式背景父瓦片，高级别 availability 只声明 FABDEM 舟山覆盖范围。地形响应带来源指纹、ETag 和可长期缓存的私有缓存头；服务端仅做有界内存 LRU，不复制源 DEM。海陆 mask 优先复用现有只读 cartographic land/land-mask 数据生成 water mask；若 mask 不可用，水效应标记为 blocked，不从高程阈值猜海陆。

数值验收保留 T3D-1 高程点检，并新增同级边缘、父子边缘、陡坡、海岸、NoData、覆盖边界和背景不进入规划的测试。

## 4. 场景与数据流

页面启动只请求轻量 manifest、影像配置状态和地形 `layer.json`。默认不请求 `/routes`、铁塔、CNS、网格、RID、Radar、保护体或模拟目标，更不请求 `/api/workflow`。

影像与地形由 Cesium 独立四叉树调度。正常档使用较宽松的 screen-space error 和受限并发；高清档降低 screen-space error，但不替换 terrain provider、不刷新全场。相机运动期间维持父瓦片，静止后继续细化。

业务图层全部默认关闭，首次开启时才请求相应只读端点。buildings v3 属基础地理可视化，但仅在近景且用户开启“建筑”后按 bbox/RTree/上限加载；只显示有来源高度，`predicted` 明确标为估计，`unknown` 不生成假楼高。建筑不反向参与规划事实。

## 5. 视觉与交互

场景保持 terrain exaggeration = 1.0。开启自然太阳光、地形阴影、天空大气和轻量雾；水面依赖合法 water mask，反光强度保持克制。卫星影像、地形、地名注记、建筑为独立开关。

提供六个固定相机预设：舟山群岛全景、R0005 高空俯瞰、桃花岛 45° 斜视、海岸过渡、港口居民区近景、低空近水平。另有俯视/45°/低空模式和一键恢复；所有飞行使用 Cesium `flyTo` 平滑过渡。

页面默认只显示影像、地形、大气/天空/海面。所有规划/CNS 图层关闭且不请求。工程演示范围、垂直基准、影像版权与不可用状态保留为不遮挡地图的折叠信息与底部版权条。

## 6. 安全、错误与性能

瓦片代理只接受整数 z/x/y；验证坐标在 z 对应矩阵范围且瓦片 bbox 与舟山许可范围相交。上游固定 HTTPS、禁止用户提供 URL、拒绝非图像 MIME、HTML/JSON 错误页、过大响应和跨主机跳转。日志只记录 source id 和 tile id，不记录查询串或 Authorization。

影像并发由 Cesium 与服务端有界信号量共同限制；地形生成由有界 LRU 去重。错误遵循 missing/unknown 不等于 pass：凭证缺失、401/403、非图像 200、空白/重复瓦片、来源元数据不匹配均阻断影像 PASS。

性能证据记录首屏、逐级加载、影像/地形请求数与字节、失败和缓存命中、静止 FPS、相机漫游 FPS、浏览器 GPU 信息及进程资源。不得出现 `/api/workflow` 请求。

## 7. 验收与交付

改动前先对 T3D-1 当前页面保存相机状态与 before PNG；改动后用同一状态生成 after PNG。另按六个固定预设生成同尺寸真实浏览器截图和低空漫游帧率记录。截图不得合成或生成。

自动验收覆盖：代理 SSRF/凭证泄漏/响应类型/401/403；heightmap 协议与边缘连续性；高程转换；默认无业务请求和实体；开关懒加载；固定相机；版权；网络与帧率采样。真实影像只有在授权 ArcGIS 瓦片于舟山多个点位和多级缩放通过内容质量检查后才可 PASS。

交付写入 `D:\Projects\_cns_workspaces\deliverables\round37_t3d_basemap`，含来源对比、技术路线、数值与边缘验证、before/after、六组实景、网络/FPS/GPU/磁盘、已知问题和 T3D-2 接口。最终停止服务，更新 CONTROL/BASELINE 并释放本 session 持有的锁；不 commit、push、merge、reset、clean 或 rebase。
