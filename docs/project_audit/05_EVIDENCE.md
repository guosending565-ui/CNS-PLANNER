# R0 本轮机器核查证据

日期：2026-10-22。本表保留初次只读审查时的状态；相对基础提交，不是修复后差异。后续仅移植两服务和直接测试，见00。仅记录路径、身份和摘要，不复制运行状态、业务原始数据或凭据。

## 源码差异清单

相等按 Git 常用的 CRLF→LF 归一化比较；SHA256 是现存文件原始字节。相等不代表已验收。

### 开发树 68e2674 加未提交修改

已跟踪修改总数 102；本表源码/测试/工具候选 123。

| 文件 | 相对候选提交 | 当前文件 SHA256 |
|---|---|---|
| `cns_planner/algorithms/building_clearance/v1.py` | 内容已纳入（忽略换行） | `9857ef1ba96ff2b8dc43717190f4c3cf845b34c5e5d2f4c4bc266e39eea36de4` |
| `cns_planner/algorithms/radar_layout/candidates.py` | 内容不同，未自动合并 | `b23d08507695265e0db17dfa6843c06c466baff2caa3bcbb13264c7f0d68ea2c` |
| `cns_planner/algorithms/radar_layout/terrain_los_lookup.py` | 内容不同，未自动合并 | `16a21ffcea1ecf7d2fe961ccc39c36f257bec8925e5225c573438cdcc5fcdfa4` |
| `cns_planner/algorithms/radar_layout/v1.py` | 内容不同，未自动合并 | `e85404f06439c1b2871b7d85807022cacfc35b78027a55e52c4bb9176e433ed2` |
| `cns_planner/api/router.py` | 内容不同，未自动合并 | `9c52e7d233f496bc534deccb8dded4b2806a9478d3d02653b6c6846000e4a1b3` |
| `cns_planner/application/app_context.py` | 内容不同，未自动合并 | `c04af674846e5790ce1c4bad9a3543f876fea2be8742367793e6272ea2e032d3` |
| `cns_planner/application/corridor_site_planning_service.py` | 内容不同，未自动合并 | `943d63ca7cfd65721d8ae3ab39a2c64ed7bf95ae877b3aad78e05c48f718e544` |
| `cns_planner/application/invalidation_service.py` | 内容不同，未自动合并 | `cc79ffd4f144a696ad5d52c5523b4bc034c6207aa0fdecc2283b82813db884b0` |
| `cns_planner/application/layered_route_validation_service.py` | 内容不同，未自动合并 | `00df5f5d9c4f14501b648c12a1a3de8eb1ccf0233a4b567fcb4a3fbd173e7619` |
| `cns_planner/application/map_figure_service.py` | 内容不同，未自动合并 | `111cb8f576f0293c87704d0ef5ae6abd9b3b7750857412cf64ced4e6f843e6de` |
| `cns_planner/application/planning_constraint_field_service.py` | 内容不同，未自动合并 | `c6c80be61dfe5f2f8c26c0ae68e6db0e6dcda5895b818518640134979d2df35c` |
| `cns_planner/application/radar_surveillance_layout_service.py` | 内容不同，未自动合并 | `3fe9fc31e65a976c8e34d3cb04f5c835b51e8c1fa5822afcdf2995b5fae25243` |
| `cns_planner/application/radar_terrain_los_diagnostics_service.py` | 内容不同，未自动合并 | `a98f552499324cb1044685b3a961c1d71537f988de913156ee2956e2fb9b6a14` |
| `cns_planner/application/source_audit_service.py` | 内容不同，未自动合并 | `0b4c6450da51e94255ed390546bfcbf4ef8d70487674b413e1766c3e066ffcd6` |
| `cns_planner/application/workflow_service.py` | 内容不同，未自动合并 | `36330ef2081f85f9d886a9e9413143776f44ed10533d9e0347a5e4f52ac41f1e` |
| `cns_planner/data/mapping/population.py` | 内容已纳入（忽略换行） | `1656a98910a60d6c80275baa4f0cf0e097a043b47fd0e982dc334ac1154e6022` |
| `cns_planner/domain/building_clearance.py` | 内容已纳入（忽略换行） | `c4d2ed184ee784b7e4f36255e5fae9e47e78f705a2e5804ddc7a5c11410b9e4c` |
| `cns_planner/domain/cns_service_registry.py` | 内容不同，未自动合并 | `ea009a9cbdcd870dbef3e30652b0ecd1e8523b991882b2f1697b7f0b1a89361c` |
| `cns_planner/domain/layered_route.py` | 内容已纳入（忽略换行） | `21ecbdb28c97ea6bc631811c17329df78d8229764eab3f5594af2c3618879eb8` |
| `cns_planner/domain/layered_route_validation.py` | 内容不同，未自动合并 | `d89977b84671e80f6ed23d46c4b85916b69a799bbc45178d75a49852a1819099` |
| `cns_planner/domain/layered_theta_v2.py` | 内容已纳入（忽略换行） | `dd79ce5fea516823549bb899be89cfa65dfee7280084308c4419cd584b2e12da` |
| `cns_planner/domain/navigation_integrity_monitoring.py` | 内容不同，未自动合并 | `b40f9bef8aeaf943d5178f168cc0c2152068fab2c4490b341c16c6609902ba7e` |
| `cns_planner/domain/path_migration.py` | 内容已纳入（忽略换行） | `f58c1d2191bc1d6ff5f8840dd022b9af857c1dc99c8be5f834e99594a9b3d21c` |
| `cns_planner/domain/planning_exposure.py` | 内容已纳入（忽略换行） | `cefb5306226e42771923188252dd822b5ab20d7e43268f03108022a197dc6b45` |
| `cns_planner/domain/population_density.py` | 内容已纳入（忽略换行） | `7e1b067915f275abd00228b45259c03c7980d4d426c8e7500b983607a1cb7125` |
| `cns_planner/domain/population_nodata.py` | 内容已纳入（忽略换行） | `887281d46213eb426dbcb9e07f62c83d277b7ffc4df5447b661a76f82d164585` |
| `cns_planner/domain/population_shelter.py` | 内容已纳入（忽略换行） | `cf67511f62a810e9711de99f55348708c2bd3c8561e5d1d472eb73b202f09d2d` |
| `cns_planner/domain/radar_terrain_los.py` | 内容已纳入（忽略换行） | `68bd81a3e70a274996cf436059ed6e0399ef9ea551db766de3b832783929a074` |
| `cns_planner/domain/radar_terrain_los_evidence.py` | 内容已纳入（忽略换行） | `c9e442a3fecd4eda219e6be9da4245d8097584d4cc1b71b13e8dc1b3d9487e18` |
| `cns_planner/domain/risk_v2.py` | 内容已纳入（忽略换行） | `0d837e30a0f362c0fe8f4d486707007528485dfeec16881cc60bcc9ce89b8479` |
| `cns_planner/domain/surface_classification.py` | 内容已纳入（忽略换行） | `c43cbadc42ed656ddd30fa49d6d41ba136246d35dd99a35464c7563ccce67f43` |
| `cns_planner/gis/building_clearance_adapter.py` | 内容已纳入（忽略换行） | `f5546c9945cfcb2623a44d7f3e5759166337899edb6883026ef6417d1e197a44` |
| `cns_planner/gis/building_footprint_aggregation.py` | 内容已纳入（忽略换行） | `c3381d0b0d056feca3ff4114346126acb94bb5dbbb82347db6678525ea1f4276` |
| `cns_planner/gis/figure_legend.py` | 内容不同，未自动合并 | `a116d25f72f279ff6ad536354ea428197dba820cc14de94e07db06cfb26122f0` |
| `cns_planner/gis/figure_style.py` | 内容不同，未自动合并 | `25d4e5ee09d2a28f97995344f1a4470203ce028a7afbeef7581cca90b9f992ca` |
| `cns_planner/gis/fine_environment_adapter.py` | 内容不同，未自动合并 | `f858147e416190b1b6b8ecec9573d13d50c0c669cf7f33b236a2fe36856930b3` |
| `cns_planner/gis/land_mask_source.py` | 内容已纳入（忽略换行） | `942da5820cebdae34d618f6b60fa02ecf721d34955af825ae2355fc7732cf209` |
| `cns_planner/gis/layered_feasibility_adapter.py` | 内容不同，未自动合并 | `e782a9d586d09b2f8b807ac43b7a929f40e43a0ae9cf5a1068eb3052f0e3d27a` |
| `cns_planner/gis/map_data.py` | 内容不同，未自动合并 | `615f8b99a7a907a29f16f27efecf6409b7e6e32d5d65051e7abbb67e770e105e` |
| `cns_planner/gis/population_nodata_evidence.py` | 内容已纳入（忽略换行） | `8e2d07dcce420f11f3d3604245feaaffdefe7e00178f6c71cab0fea1c4ea6b30` |
| `cns_planner/gis/qgis_figure_renderer.py` | 内容不同，未自动合并 | `a6f368847a190bf557a6a28deaf664b7502602567ef515cc9783448adc10e75f` |
| `cns_planner/gis/radar_layout_adapter.py` | 内容不同，未自动合并 | `eb833ab4b2d2eb111c4d93f3793a474392c947c791dba78591c3e2b9b11517b2` |
| `cns_planner/gis/radar_terrain_los_adapter.py` | 内容已纳入（忽略换行） | `bac1ec84fb7b4828d708f8300e0004165c60959ecb067359b66bb4c73cc7e54e` |
| `cns_planner/gis/raster_adapter.py` | 内容已纳入（忽略换行） | `d13f5a35a1566060ffe5086f74daaabf9fc35031444770c074b0c781cc42e150` |
| `cns_planner/gis/source_inspection.py` | 内容不同，未自动合并 | `a8189ba7595d937d1aee2e658922e3157263040ce6cbc34c5c57720bd7de2035` |
| `cns_planner/gis/tower_obstacle_adapter.py` | 内容已纳入（忽略换行） | `e8e40fc79dc4c5f96a68bc2f138b24d8833dcf57d4d9065ea1d53883c80e6d95` |
| `cns_planner/gis/v3_environment_adapter.py` | 内容已纳入（忽略换行） | `87284b69d6e00c5da7bbabb1735ad8d152f6fb260b2600b33899c95f3d88c162` |
| `cns_planner/layered_route_planner/feasibility.py` | 内容已纳入（忽略换行） | `d61fe3b819a7da17e29e7fd4c884a85214f5f27d182017ea7f1a58ae0e2b1d20` |
| `cns_planner/layered_route_planner/theta_star_v2.py` | 内容已纳入（忽略换行） | `1ef968aaaeaafabe328cbd9941e3d507265e19732de3ec3cc20821d5d9c0e259` |
| `cns_planner/reporting/map_templates/__init__.py` | 内容不同，未自动合并 | `4df0238267172f4a53e66e8f03d6dad27b71287a6599c8ee22204ba06b3f8132` |
| `cns_planner/reporting/map_templates/template_catalog.py` | 内容不同，未自动合并 | `b445d24a0693d03685f37381134bc33b0383706f6f646611a645a5058083135f` |
| `cns_planner/risk/factors_v2.py` | 内容已纳入（忽略换行） | `866b418c6e54875cd9c285a9c6349e3cd2f54b2abbaf58fea3e8f1316a332dc0` |
| `cns_planner/route_planner_v3/continuous_synthetic.py` | 内容已纳入（忽略换行） | `a1b8cc6e615a56a3b3894cb051c0e786d0ecb962c876968d0ddb865f7f04373d` |
| `cns_planner/tasks/handlers.py` | 内容不同，未自动合并 | `61c48b28511683236c3feca4c77554d0caf5cd7ab5f7c1106846c97bf009571a` |
| `cns_planner/tasks/service.py` | 内容不同，未自动合并 | `fa7c164b2a60540451f554211f6e5502638bfd5c816a951aeb24dd280bd2519d` |
| `cns_planner/tasks/task_specs.py` | 内容不同，未自动合并 | `d9a14f77d84fb8de16f3a81fa0a00ee23b0f7d6a9fdbead5bfa6537693197129` |
| `cns_planner/validation/continuous_validators.py` | 内容不同，未自动合并 | `73118f5883c099f13a26a1718213c3888b50ff0ff881aed8bebcc57599084b5c` |
| `cns_planner/web/js/main.js` | 内容不同，未自动合并 | `971695537b2f76b4eccf394eeed42e2551af32c856d654382d1dc7a5cc4ed9c8` |
| `cns_planner/web/js/tasks.js` | 内容不同，未自动合并 | `8a575ef84970fc239501bce5983c895898449cf5906680bb4884a2151f9bf41c` |
| `cns_planner/web/js/workflow/layered_route_planner.js` | 内容已纳入（忽略换行） | `1042cd5754663ef1fb66280268a18e08dff9805062151692d01fdc7c461317cc` |
| `cns_planner/web/js/workflow/layered_route_validation.js` | 内容已纳入（忽略换行） | `a10496b601329ce7ea24faee947a7ff3da5226c279430a8575c244f3c29a7e2d` |
| `cns_planner/web/js/workflow/radar_surveillance_layout.js` | 内容不同，未自动合并 | `84664b9ed23795829dc7a02772b8969442ecc559e3683af3a15cce3913cf131c` |
| `cns_planner/web/js/workflow/step02_workspace.js` | 内容已纳入（忽略换行） | `369e2bf05042ddebc7e4ee52f44cb79df24e7e3ce3f5deab8567ba1525d57e31` |
| `cns_planner/web/js/workflow/step03_routes.js` | 内容不同，未自动合并 | `63898e30d34fa617d40617821320f7d3904c3b81a16ff0ffa582d77cc82c0e48` |
| `cns_planner/web/js/workflow/step05_cns.js` | 内容不同，未自动合并 | `e8c338992cf01491c3bfd474de4305fdf8d94eee3dcba0a156be0ba4320d6f09` |
| `tests/frontend_modules.test.mjs` | 内容不同，未自动合并 | `d88781243fe2989c67a84d13f86b1afa09c6743c55e3effb723b956bb9ca157c` |
| `tests/grid_popup_hydration_frontend.test.mjs` | 内容已纳入（忽略换行） | `e0062b82eb4fe3b17052324aa6f5e438b8d2e980bcdc1c691946ed06ddb6e597` |
| `tests/layered_validation_adoption_frontend.test.mjs` | 内容不同，未自动合并 | `435e91ab2aec7276b466bf3a0c7be562bb555b79447e40b4f4edd7c728a5b9da` |
| `tests/population_nodata_frontend.test.mjs` | 内容已纳入（忽略换行） | `61241add2acc9eabb712bbf3d83892b6dc2e4bcca9b6ccffeac9d1648e83f933` |
| `tests/round31a_p16_background_task.test.mjs` | 内容已纳入（忽略换行） | `008cf6d717f2d9d187a00d4ea597cd82ff4b6494e1c05569ad374390a383c793` |
| `tests/round34_t07b_terrain_los_frontend.test.mjs` | 内容已纳入（忽略换行） | `25d23bf1bbe3158d6d11bb5ffaa41d88b1674439fb5fd53ef2a6c14590fa45de` |
| `tests/run_qgis_map_figure_tests.py` | 内容不同，未自动合并 | `7f703915302e0a7aac86b6e41a2c1235faf7986e1a7734b02d2db5fda910a585` |
| `tests/test_algorithm_registry.py` | 内容不同，未自动合并 | `c398934979fd9fba90ca6a0405c880bc79750907fe37acee776714588a65c157` |
| `tests/test_bug_map_route_end_001.py` | 内容已纳入（忽略换行） | `048ebe8ffb21c840db714e852bf27ba410129a7439c9b0be888e70d1b9f7a1f9` |
| `tests/test_bug_route_003_runtime_rebind.py` | 内容已纳入（忽略换行） | `c9c4d9f04eabb6c0037f13613690668fb9e45dd5b59d0b8a4faf48c4272f9027` |
| `tests/test_bug_route_004_theta_objective.py` | 内容已纳入（忽略换行） | `c6b4645913e455f4e0f30d0259b6492fffbb3741b57a2e8682e83393a56b693f` |
| `tests/test_building_level_independent_facts.py` | 内容已纳入（忽略换行） | `d03388bcd060498764e5d038700ebb9704f0d39ea59e70acbab6f46f9913e3e5` |
| `tests/test_data_semantics.py` | 内容已纳入（忽略换行） | `07d9e82b7bf171258850b1b7e9489869219aa83edcd624dc4a8c8ea7db3e9934` |
| `tests/test_drive_migration_portability.py` | 内容已纳入（忽略换行） | `ab27703c1dd8f2378882cd37061f2e26134508fde8d33b765c2dbc4e526e6b4a` |
| `tests/test_layered_route_validation_adoption.py` | 内容不同，未自动合并 | `366fd6d77975da37788d89a4dc9e2a80223de07fa5d273e797d0efed83ecd910` |
| `tests/test_layered_theta_star_v2.py` | 内容不同，未自动合并 | `dac43ed86f68cd4deaf5236d46b69565f1b4cd23a2f539edda5051a3d51e4e51` |
| `tests/test_map_figures_cartographic_polish.py` | 内容不同，未自动合并 | `f93cddc8d0310d3fe85637b0f9fecd5cc6cd3a1e70603e5e01c576fd6ef724e7` |
| `tests/test_map_figures_cartographic_polish_qgis.py` | 内容不同，未自动合并 | `15f6fcd6db155d8e2568427e45bd9c94266141c0fba1f339167a00def842eb56` |
| `tests/test_map_figures_cns_templates.py` | 内容不同，未自动合并 | `8862e5020b29c3d52951214d3053b4aaa7b93400ca0b1cdf313a8545d1191213` |
| `tests/test_map_figures_final_tuning.py` | 内容不同，未自动合并 | `7be21b9ef6fa769f84f44d18b58cf49276acabcea71216dce79ba7006f060768` |
| `tests/test_map_figures_final_tuning_qgis.py` | 内容不同，未自动合并 | `7a5efaa8df80bbdf3b2045acca39a33eda1d9c73a49956b7915dd3f71ec1230a` |
| `tests/test_map_figures_formal_polish.py` | 内容不同，未自动合并 | `0282b870405ff2487e1e323aa0734feacbf29619a67d9c4c1b3cc1f22768e89b` |
| `tests/test_map_figures_qgis_render.py` | 内容不同，未自动合并 | `aa981bbad49ce1b65a7abca7e6811c52e0b6389b9f4892a25f68ac57fd2742f9` |
| `tests/test_phase4_b3x_planning_constraints.py` | 内容已纳入（忽略换行） | `c1536fcda79925181eb7fce7751f22b5bc9a5f1d30dfe3a1dfca2289346af0fb` |
| `tests/test_phase4_b6r_immutable_task_input.py` | 内容不同，未自动合并 | `c4b697784806261a732b0b232dc8fa843ff4d1b410a6c6d01d4be80cf6f1ed4f` |
| `tests/test_phase4_b6x_heavy_task_runtime.py` | 内容已纳入（忽略换行） | `b97e5a5468272df6f6d8e60d9530f30c3f4ab232a407f2889ba5154d219b5319` |
| `tests/test_phase4_b7r_delete_gates.py` | 内容已纳入（忽略换行） | `63e1789bee68be972c7e07a9d6bc7f0eba25484b5690f3fca394d228f37ab207` |
| `tests/test_phase4_b9r1_async_admission.py` | 内容已纳入（忽略换行） | `bc0871f8c0745bce4d5202361eb4d544d933589099e5bcc3792afceb458fc974` |
| `tests/test_population_nodata_semantics.py` | 内容已纳入（忽略换行） | `f45b179079ac60b2873a613d02329314826c0887b1294aa7eb67cc1592f3705f` |
| `tests/test_radar_surveillance_layout.py` | 内容不同，未自动合并 | `5fe07712c42a06dbb428599452c45f54683ada5f8ff1261ca11644a97aff5a9e` |
| `tests/test_risk_model.py` | 内容已纳入（忽略换行） | `d65edd9eff2ef356d3147f0512bda6c1baf2070dc56c46419fc41b93348619ef` |
| `tests/test_risk_v2_canonical_integration.py` | 内容已纳入（忽略换行） | `8bb3bfe1147c1edcb2477c73c500ad5b223ab1cae7280da18a587fde4859a5af` |
| `tests/test_round2_surface_wiring.py` | 内容已纳入（忽略换行） | `3fb13f521c6bdeb8a8817c0c4c12da654a9cb282106ba614117ae693e405e36b` |
| `tests/test_round30_c3b_selection_limit_probe.py` | 内容已纳入（忽略换行） | `cb8d69bd30f488dbbd34fe6becb70859c9f9a53eb7aab6adc41e5cf06b3412c3` |
| `tests/test_round31a_p16_background_task.py` | 内容不同，未自动合并 | `98792c31c7028405fd007dc9b35803cca4e40a67c275b5f5e0987ca7a655830e` |
| `tests/test_round34_t07a_radar_terrain_los.py` | 内容已纳入（忽略换行） | `c5566d5a9aafe889e6fa95f57a0eb93704a6bbe8b6a9af465712e18b3ebe97df` |
| `tests/test_round34_t07b_radar_terrain_los_evidence.py` | 内容已纳入（忽略换行） | `650453752a811644266249eb02adf5fc5048c977362eb0687f3bbf9ff53efc05` |
| `tests/test_round34_t07d1_radar_los_preintegration.py` | 内容已纳入（忽略换行） | `6a4e50cc9f4c4383e64065f1d3b2c8e9e79d5baf32bcdbc84eee0e811d5dc99c` |
| `tests/test_round34_t07d2a_los_capacity.py` | 内容已纳入（忽略换行） | `feb5c1d4d802cdd0c9eb66611e9514728e6d89aeedbee8bbcdf9397382bd9eb1` |
| `tests/test_round35d_open_sea_nodata.py` | 内容已纳入（忽略换行） | `048bdf5e38cfb1a3ebba348f021f79eccbf2762fe308ee5f601472afebb7d2e5` |
| `tests/test_round35d_population_density.py` | 内容已纳入（忽略换行） | `f447267c0a80eed4b7c51b59e4fbd703a189f7cb715fd6ebeee0220ac440f6b6` |
| `tests/test_round35d_source_identity.py` | 内容已纳入（忽略换行） | `7e048b33aa66ea3efb48ed8b1be69b4bac8af0218121f8487eee600632974b4d` |
| `tests/test_round35f1_building_grid_invalidation.py` | 内容已纳入（忽略换行） | `c50040b1605b58183e62e89a8b4a2f8b8bcd120dd127cb5f896e9635c999852f` |
| `tests/test_round35f1_building_occupancy.py` | 内容已纳入（忽略换行） | `d8ce0b13d12c29a59540dfb163d5c33ae33478e15961885f0c5da9138e1485e4` |
| `tests/test_round35f3_height_evidence.py` | 内容不同，未自动合并 | `eca6ad864922a23b7616be160925073a8b6f950d5f5fd30f0d5ddb4d2849affc` |
| `tests/test_round35s6_theta_semantics_version.py` | 内容已纳入（忽略换行） | `2e08844a13dda3d9da9a7298924d707ce44a964495b4f762eab64998c1d8cc47` |
| `tests/test_round40a_navigation_integrity_v2.py` | 提交中缺少该文件 | `8551beac2c34c179cb38eb90cc5c136179bd8205cff5dd7f02f9eda450bc6f07` |
| `tests/test_route_planner_v3_continuous.py` | 内容已纳入（忽略换行） | `62fcf6bfdbcd5d1744fcab43b63c5668712ac00bfa06ab39ca493c3c7bcdba9d` |
| `tests/test_route_risk_profile.py` | 内容已纳入（忽略换行） | `1f76009a7eb4bb9da91481369d4fda77867fb7211d8cd227f40afdacc2adb0d0` |
| `tests/test_zhoushan_golden_case_e2e.py` | 内容已纳入（忽略换行） | `73c0f9fce3c1b91f92df3524d63e8d4224f3444f0a7a291f099e1e0af684b99c` |
| `tools/b7x_delete_gates.py` | 内容已纳入（忽略换行） | `2bcae4e4632565ececab02369b43f72a91acc674f0bfe2822f737a706c59d79b` |
| `tools/export_cns_five_figures.py` | 提交中缺少该文件 | `205f28790190ea57cb50e238bcad8b2adec3d8c25f7c336e69bdfc92e514bec9` |
| `tools/registry_snapshot.py` | 内容已纳入（忽略换行） | `12389890870a0f4e0b01829baf9705e3f00b6dbedec461da3889b750cdbd2795` |
| `tools/round34_t07a_real_data_terrain_los.py` | 内容已纳入（忽略换行） | `d5e5bf581a6bb46afeebe1cae43a9bd750ca6c04f818a386cb09795c3e93a1d8` |
| `tools/round34_t07a_tower_top_formula_audit.py` | 内容已纳入（忽略换行） | `4d6cc6c40df32428cf74294683db8e252cb7ffddf4e9be8e8d38edfc78dd526c` |
| `tools/round34_t07b_real_data_terrain_los_wiring.py` | 内容已纳入（忽略换行） | `6b8c1b54db1b0fb56d8110816614d2e3c3cade4210aa29529cbcc211037796c0` |
| `tools/round34_t07d2a_los_capacity.py` | 内容已纳入（忽略换行） | `1823c93aa6e5a97ed37b01d80208548bf66eebe10e4dfde42c6dd3e074aebc90` |
| `tools/verify_doc_paths.py` | 内容已纳入（忽略换行） | `88333e3b6a6ebb9d4e854ee6b9b705c664ac479b90bdca54e61338a7f5566ab6` |

### 验收树 9404d16 加未提交修改

已跟踪修改总数 24；本表源码/测试/工具候选 32。

| 文件 | 相对候选提交 | 当前文件 SHA256 |
|---|---|---|
| `cns_planner/algorithms/corridor/v1.py` | 内容不同，未自动合并 | `b5fc5e426afeca67a04302626c7f448d21e636d76fbb41d304554e108365bb1c` |
| `cns_planner/api/server.py` | 内容不同，未自动合并 | `9ef700905d21362539c81337ad7fb7597f8c456d19e4f0b99d4dc2bf7b018cbc` |
| `cns_planner/application/corridor_service.py` | 内容不同，未自动合并 | `9c40f753dee6ce8cad2beeb1fd165af0f779d9ec92b8a919a9850379d8b40661` |
| `cns_planner/application/corridor_site_planning_service.py` | 内容不同，未自动合并 | `28cdeb0a8cdccbc93c59ef49c023f94860c67a576fb818cdeadaef6c18267df5` |
| `cns_planner/application/export_service.py` | 内容不同，未自动合并 | `accf350baf51d614dc7c6f748cef5834ce6cbbdd3e769214a6f7650060c067a8` |
| `cns_planner/application/layered_operational_adoption_service.py` | 内容不同，未自动合并 | `2567b87d06014a01fa1e02cd24d2ecde3313a343399734bb74d8557c15bd0b28` |
| `cns_planner/application/layered_route_planner_service.py` | 内容不同，未自动合并 | `493a19fe0919f485cd2882f85f237fd96c787df8df89cc6744dec5c2a30eb3c0` |
| `cns_planner/application/layered_route_validation_service.py` | 内容不同，未自动合并 | `2c226b50b42243bb51e59f2cf5bd6f884ae5286fda98f8d68a5dc31bc70f4cec` |
| `cns_planner/application/research_real_c_evaluator.py` | 内容不同，未自动合并 | `bca4956a4b6b5b58dc3bc588b2d0353830b01b2dabd3384920579e16cb5641c6` |
| `cns_planner/application/research_worker.py` | 内容不同，未自动合并 | `6c2a301d4360274a94517157ba3b5abfb7cfcc14f3088480f8d28990e2ab8963` |
| `cns_planner/application/session.py` | 内容不同，未自动合并 | `70c73fa6bb8ca4475fed28d30f80b7c5cf37d0e8e0b3dfb4bb4a38c5fb20fd80` |
| `cns_planner/domain/cns_service_registry.py` | 内容不同，未自动合并 | `ea009a9cbdcd870dbef3e30652b0ecd1e8523b991882b2f1697b7f0b1a89361c` |
| `cns_planner/domain/navigation_integrity_monitoring.py` | 内容不同，未自动合并 | `b40f9bef8aeaf943d5178f168cc0c2152068fab2c4490b341c16c6609902ba7e` |
| `cns_planner/domain/path_migration.py` | 内容不同，未自动合并 | `f2ce054a6fb6d51f3c9fadf63b627139f0176c0e842490b69478d0e843d16ac5` |
| `cns_planner/domain/reporting.py` | 内容不同，未自动合并 | `41697685d774504bec9f7bb78f388ecb4779c03c7c116db32bd18b21f8b35b39` |
| `cns_planner/layered_route_planner/theta_star_v2.py` | 内容不同，未自动合并 | `9d5928482a06ea826dbd725adcf53fed77496a7f93ca7b5cb78b0a9d7d6db0ba` |
| `cns_planner/persistence/project_compaction.py` | 内容不同，未自动合并 | `3741d38d11cb1d1422d6940b9c9ecf2477eadec540dd8be43a058d36e8566d21` |
| `cns_planner/reporting/builder.py` | 内容不同，未自动合并 | `054b50b4c40f22e1f48f73b1319cd23639607ecb5fe46370f303ae419cb23e8d` |
| `cns_planner/reporting/html_renderer.py` | 内容不同，未自动合并 | `9e190f553fc6b4eeac3ff5af05c06f645a1b74202cff9690a5af06d66fff1665` |
| `cns_planner/web/js/workflow/layered_theta_v2.js` | 内容不同，未自动合并 | `9a51519eb58bafd65f17276641c2df1635e16fdb47715a1b2ab100b0b08e7dc1` |
| `tests/test_g4_p16_external_scope.py` | 提交中缺少该文件 | `b9be17966cba8e2f3a308b653952194df2551419bdfba013c0585ffe6fd66008` |
| `tests/test_layered_route_validation_adoption.py` | 内容不同，未自动合并 | `b69c99fd79f856263125b86803dca99948d36fd93e7edca84fcb4ba087f5f8f4` |
| `tests/test_layered_theta_star_v2.py` | 内容不同，未自动合并 | `f27753923cdb51f1855651066d882373f75e561d8d678b896b63c16b4bb3b6d2` |
| `tests/test_path_migration_r19.py` | 提交中缺少该文件 | `d853bf5de26d93ecf3f12f67724d7bf54cfe2c2d8bb537a3756aae4ff700ffaa` |
| `tests/test_r15_candidate_identity.py` | 提交中缺少该文件 | `7070678d5e86864d0167112456403423e43bfdff36b524e9c434de861ff5f6ef` |
| `tests/test_r15_report_lock.py` | 提交中缺少该文件 | `96f754d7698436874132821937cfb56cf4c9873318783cb892745867a79aff0d` |
| `tests/test_r21_report_disclosure.py` | 提交中缺少该文件 | `168ba801b92af7d44f43abf7b276a20f3b25f322da8257b534e19561608a9278` |
| `tests/test_r22prime_html_bundle.py` | 提交中缺少该文件 | `0274d5d1ca07c04f6226f45ff17f1db4d96b037e54c6b6c64b1f1a1e7f0f12d0` |
| `tests/test_r26_owned_restore.py` | 提交中缺少该文件 | `587827675f2bbfa13aace5cf61119f137786e2da1097165da73ff730c5337b79` |
| `tests/test_research_real_c_evaluation.py` | 内容不同，未自动合并 | `9a260cbf4b1acc7f86ed60d586c7f47030e81d7d40afa72e07247a1c40cdb4ed` |
| `tests/test_research_worker.py` | 内容不同，未自动合并 | `21d7c2b696c374056c3b8d25bcd20c2f897555dd67e9f62f982953bb82ae82b7` |
| `tests/test_round40a_navigation_integrity_v2.py` | 提交中缺少该文件 | `8551beac2c34c179cb38eb90cc5c136179bd8205cff5dd7f02f9eda450bc6f07` |

## 候选 Git 历史体积与凭据初筛

候选可达历史：检查 3525 个唯一 blob，合计 294172409 字节（含历史版本重复内容）；大于 10 MiB 的 blob 0 个；高置信格式规则命中 0 项。

这不是完整秘密认证：未做通用口令、个人信息、授权权利的人工全历史审阅。本轮审查快照使用本记录并增量检查新增暂存内容；不声称穷尽所有未知秘密。

| 对象 | 路径 | 大小或规则 |
|---|---|---|

## rev523 来源审计增量核对

以下来自明确指定的 S4A 实验副本；仅复用来源登记，未重新读取原始 GIS 文件。verified 是项目内记录状态，不是本轮独立认证。

| 数据角色 | 审计状态 | 登记 SHA256 | CRS（声明/确认） |
|---|---|---|---|
| `basemap` | `configured_unverified` | `未知` | `未知` |
| `population` | `configured_unverified` | `未知` | `EPSG:4326` |
| `terrain` | `configured_unverified` | `未知` | `EPSG:4326` |
| `terrain_dtm` | `verified` | `21406bd901fbebae211528a158ca95eef899e2074322625717b215b13d8799b8` | `EPSG:4326` |
| `reference_landing_sites` | `verified` | `55b60344d5cc9eef0acef285e33ee15e3e77a3ee8e39bedc62d095cc1c3cc0e5` | `EPSG:4490` |
| `reference_routes` | `configured_unverified` | `未知` | `EPSG:4490` |
| `equipment_reference_catalog` | `invalid_source_path` | `未知` | `未知` |
| `buildings` | `verified` | `4f1657495d8422ac0d23973949d4bcbca59dcf07d59ed380ff9877afc7391b97` | `EPSG:4326` |
| `building_grid` | `verified` | `13fca1ee7183035a280ad066f84a6d116303e0b3797c206bdfd98f7e50bbce37` | `EPSG:4326` |
| `towers` | `verified` | `d722c111596acb4766bcc9d4cb5ce326f3a4b1f8364a5db871cb60220e2a411b` | `EPSG:4490` |
| `land_mask` | `verified` | `fe7918c96636178b7931b3f6887e2755db191dc6fce51ad0c5b5bfcbb6180aef` | `EPSG:4326` |
| `cartographic_land` | `configured_unverified` | `未知` | `EPSG:4326` |

## 保存保护

未对正式状态执行 save、Adopt、Confirm 或 Apply；未更改旧工作树源码、分支、索引或历史。本轮仅新增本审查目录、工作区外的测试副本及原生工具创建的隔离工作树。

## 本次最小固化补充

原始失败已在隔离审查分支复现；两服务修复逐字节取自验收树，相关测试加直接Apply六种用例后101项通过。P16外置恢复、报告、研究等差异未移植。正式state未写；本次提交和上传范围见00。

## 发布前增量安全检查

实际暂存仅11个授权文件（两个服务、两个测试、六份审查Markdown及README入口）；暂存文件内容合计225233字节，单文件均小于1MiB。新增内容凭据规则无命中；另外检查基础历史439个配置类blob，无待处理凭据候选或敏感文件名命中。基础全历史初筛沿用上表不可变提交记录。git diff --cached --check通过。未禁用TLS校验，实际git ls-remote经本机既有代理核对远端main，指定审查分支在创建前不存在。
