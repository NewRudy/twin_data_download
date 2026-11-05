---
name: gis-data-download
description: 按坐标点一键下载数字孪生场景基础数据——高清影像、高程 DEM、建筑物轮廓+高度、道路/铁路/水系/绿地配套图层，打包成 GeoTIFF/GeoJSON/Cesium 3D Tiles 离线包并附三维预览页。当用户要下载卫星影像、遥感影像、高程数据、DEM、建筑轮廓、白模、OSM 路网水系，或要为数字孪生/Cesium 场景准备基础数据时使用。
---

# 数字孪生基础数据下载

按中心坐标 + 半径，一站式产出数字孪生场景所需的**基础数据**：

| 数据 | 来源 | 标称精度 | 产出 |
|---|---|---|---|
| 高清影像 | ESRI World Imagery (Maxar) / Google | z18≈0.6m/px（MODELED 估算） | GeoTIFF (EPSG:3857) |
| 高程 DEM | Copernicus GLO-30 | 30m（标称） | GeoTIFF (EPSG:4326) |
| 建筑物 | OpenStreetMap (Overpass) | 覆盖度不一（中国县城可能稀疏） | GeoJSON (EPSG:4326) + 属性高度 |
| 道路/铁路/水系/绿地 | OpenStreetMap (Overpass, 同一次查询) | 覆盖随 OSM | 各自独立 GeoJSON |
| 3D 白模 | 上述建筑挤出（封闭屋顶 + 程序化立面纹理：窗格/亮灯窗，按建筑类型调色；逐栋 batchId 可拾取高亮） | 高度 POLICY 估算 | 3D Tiles 1.0 (b3dm + tileset.json + batch table) |
| 预览页 | CesiumJS | 验收用 | cesium_preview.html + serve.sh，悬停/单击高亮拾取+信息卡、图层开关/预设/42° 斜视角 |

另产出 `download_report.json`：记录数据源、瓦片数、失败数、高度估算占比等证据。

## 使用步骤

### 1. 环境检查（每台新机器第一次）

```bash
bash ~/skills/gis-data-download/setup_env.sh
```

创建 `~/.venv_imagery` 并装依赖（默认清华 pip 源，海外机器 `PIP_INDEX=` 覆盖）。
已装好的机器会跳过创建，重复运行幂等。

### 2. 运行下载

```bash
~/.venv_imagery/bin/python3 ~/skills/gis-data-download/scripts/download_scene.py \
  --lon 114.351205 --lat 26.937214 --radius 3000
```

常用变体：

```bash
# 按 GeoJSON 边界下载 (Point/Polygon/FeatureCollection 均可, 取包围盒; --buffer 向外扩米数)
... --geojson 场址边界.geojson --buffer 500

# 更小分辨率更快出结果（z17 = 1.2m）
... --zoom 17 --radius 2000

# 只要数据文件，不要 3D Tiles 和预览页
... --skip-3dtiles --skip-preview

# Google 影像源（国内需代理）
http_proxy=http://127.0.0.1:58309 https_proxy=http://127.0.0.1:58309 all_proxy= ALL_PROXY= \
  ~/.venv_imagery/bin/python3 ... --source google

# 只要建筑数据（影像/DEM 已有）
... --skip-imagery --skip-dem

# 断点续传: 复用输出目录已有的影像/DEM/建筑/配套图层, 跳过全部网络下载,
# 只重做 3D Tiles / 预览页 / 报告 (改完模板或参数后重出交付物用这个, 秒级)
... --resume

# CNBH-10m 建筑高度注入 (无 OSM 标签的栋用 10m 栅格估算值, OSM 标签优先保留;
# 首次按度格下载瓦片 10-50MB 缓存 ~/.cache/cnbh10m, 需外网/代理)
... --heights cnbh

# 离线预览包 (投标/汇报现场无网): 影像转本地 XYZ 瓦片 + CesiumJS 本地化
# + GLO-30 本地地形, 输出目录自包含, 断网打开即用
... --offline

# 不要道路/水系/绿地配套图层
... --skip-extras
```

参数：`--zoom 16-19`、`--radius` 米、`--geojson 边界文件`、`--buffer` 外扩米、
`--name 场景名`、`--out 输出目录`、
`--workers 并发数`、`--default-height`（无标签建筑默认高度，默认 6m）、
`--tile-threshold`（建筑数超过该值 3D Tiles 自动分块，默认 1500）。

**范围确认（必做）**：输出或 `download_report.json` 的 `warnings` 里若有
「范围很小 / 范围较大 / 范围过大」提示，**先把提示转述给用户、确认合适后再继续**
（小范围建议 `--buffer` 外扩；>6km 建议降 zoom 或分块；>12km 强烈建议分块）。

### 3. 验收

**必做**（不要只看脚本退出码）：

```bash
# 输出齐全 + 大小合理
ls -lh <输出目录>/

# GeoTIFF 能打开、GeoJSON 栋数合理
~/.venv_imagery/bin/python3 -c "
import rasterio, json
d='<输出目录>'
for f in ['imagery_z18_esri.tif','dem_glo30_30m.tif']:
    s=rasterio.open(f'{d}/{f}'); print(f, s.shape, s.crs)
fc=json.load(open(f'{d}/buildings.geojson')); print('buildings:', len(fc['features']))"

# 真实浏览器预览（证据最高层级）
<输出目录>/serve.sh          # 然后浏览器开 http://localhost:8000/cesium_preview.html
```

预览页验证点：影像底图加载、纹理 3D Tiles（默认）+ 高度分析白模预设切换、
道路/铁路/水系/绿地图层显示、右下角无红色报错；陡视角(-60°)确认建筑**屋顶封闭**
（白模顶面完整，不是空心管/凹盒）。可用 scripts/dissect_b3dm.py 解剖 b3dm 验证几何
（屋顶法线朝上顶点应在楼顶高度、无贴地异常）。
用 ego-browser 截图留证。

## 输出结构

```
<out>/
├── imagery_z18_esri.tif      # 高清影像 (EPSG:3857)
├── dem_glo30_30m.tif         # 高程 (EPSG:4326)
├── buildings.geojson         # 建筑轮廓+height_m/base_m/top_m 属性
├── roads.geojson             # OSM 道路（LineString，class/name/bridge/tunnel）
├── railways.geojson          # OSM 铁路/轨交（rail/subway/tram 等）
├── water.geojson             # 水系（河流运河 LineString + 湖泊库区 Polygon）
├── green.geojson             # 绿地（公园/草坪/林地/球场 Polygon）
├── buildings_3dtiles/
│   ├── tileset.json          # ENU→ECEF transform, Cesium 直接加载
│   ├── scene.b3dm            # ≤1500 栋: 单 tile；更多时改为 tiles/cell_*.b3dm 分块 (~350 栋/块)
├── tiles/                    # --offline: 影像 XYZ 瓦片 z0-z(native) 全金字塔 jpg
├── cesium/                   # --offline: CesiumJS 1.142 本地副本 (~22MB)
├── terrain/                  # --offline: GLO-30 heightmap-1.0 地形瓦片 + layer.json
├── cesium_preview.html       # 多图层预览（建筑/路网/水系/绿地独立开关）
├── serve.sh                  # macOS/Linux 启动 (自动开浏览器)
├── serve.bat                 # Windows 双击启动 (py→python 回退, 未装 Python 给安装提示)
├── 启动预览.command           # macOS 双击启动
└── download_report.json      # 证据记录（含各图层要素数）
```

## 数字孪生接入

- **3D Tiles**：向 Cesium 场景注册 `tileset.json` 的**完整 URL**（不是目录），
  见既有经验：外部资源注册用完整 tileset.json URL。
- **建筑高度字段**：`height_m`（总高）、`base_m`（DEM 底座高程 + min_height）、`top_m`（=base+height）。
  GeoJSON 白模在 Cesium 里可用 `extrudedHeight=top_m / height=base_m` 直接挤出，样式随时改。
- **影像 GeoTIFF**（EPSG:3857）：接平台前可转切片（gdal2tiles/Cesium 在线瓦片亦可）。
- **DEM**：30m 精度做场景地形底座够用；如需精细地形，需现场无人机航测（本工具不做）。

## 边界与证据（必读）

- **建筑高度是估算**：`--heights cnbh` 时无 OSM 标签的栋注入 CNBH-10m 栅格估算
  (Wu et al. 2023, Zenodo 7923866, 2020 年度, 10m MODELED——比默认 6m 合理得多,
  但仍是模型估算非测绘)；其余栋为 POLICY (OSM 标签 > 层数×3m > 默认 6m)。
  每栋依据在 `properties.height_source` (osm:height / cnbh10m / policy:*),
  汇总在 report `buildings.height_cnbh`。**CNBH 只补高度不补轮廓**——
  OSM 覆盖稀疏地区的建筑栋数问题依然存在。不能当测绘成果用。
- **OSM 建筑在中国基本不可用**（2026-11-03 实测）：永新县城 3km 框仅 9 栋、
  贵阳花果园 1.7km 框仅 10 栋（实际数万栋）。三大全球免费源中
  Microsoft Global ML Building Footprints 和 Google Open Buildings 均不覆盖中国。
  县城/乡镇项目建筑数据通常为空或残缺，**这不算脚本故障**，要向用户说明并给出替代：
  ① 现场无人机倾斜摄影（测绘级）② 商业数据（水经注等，来源授权需注意）
  ③ 学术数据 CBRA（Zenodo 7500612，全国 2.5m 栅格，按年整包 3.4-4.5GB，需自行矢量化）。
  城市（尤其沿海大城市）OSM 覆盖可能可用，下载后先看 report 的栋数再下结论。
- **ESRI z19+ 在县城/乡镇多为占位灰瓦片**（"Map data not yet available"，
  ~2.5KB 同一张图）：脚本会自动探测并降级到真实影像层级；预览页 maximumLevel 固定 18。
- **`--skip-dem` 时 `base_m` 全为 0**：绝对高程字段失效，仅适合纯椭球预览。
- **z18≈0.6m 是 MODELED 估算值**（按纬度换算），实际成图质量取决于 Maxar 当地拍摄批次。
- **Google 源在国内需要代理**；ESRI、Copernicus、Overpass 一般可直连。
  Overpass 限流时脚本会自动换端点（共 3 个）。
- 预览页用 jsdelivr CDN 的 Cesium 1.142；`--offline` 时全部本地化（影像 XYZ 瓦片 +
  cesium/ 目录 + terrain/ 地形），**无网可用**；但下载的 ESRI 影像衍生瓦片仅供
  现场/内部演示预览，**不得再分发**（Esri 服务条款）。
- `--offline` 地形：GLO-30 正高直接当椭球高（忽略大地水准面差距，平原误差约
  ±10m 级），与绝对高程模式同一近似；heightmap-1.0 格式细节已按 Cesium 1.142
  源码核对（65×65 Uint16LE 高度格 + childTileMask + 1 字节水mask，u16=(h+1000)*5）。
- 产线环境（投标/汇报）引用这些数据时，按 [[evidence-boundary]] 标注层级，别说成"测绘级"。

## 已验证机器

> 发布副本（其他机器直接 clone 用）：https://github.com/NewRudy/twin_data_download
> 改动本 skill 后同步 `SKILL.md requirements.txt setup_env.sh scripts/download_scene.py scripts/dissect_b3dm.py` 到该仓库。

- **2026-11-05 Rudy Mac**（上海小涞港示范区）：
  **断点续传 `--resume`**：复用已有影像/DEM/建筑/配套图层、跳过 Overpass，只重做
  3D Tiles/预览/报告，全程无网络请求秒级完成✓（配套图层计数取自已有文件）。
  **3D Tiles 分块 LOD**：>1500 栋自动按质心均匀网格分块（~350 栋/块，
  tiles/cell_XX_YY.b3dm + 根 tileset 并集包围盒），压阈值到 100 对同一场景出 2×2=4 块
  浏览器验证：4/4 块加载、1014 栋全部渲染、位置与影像对齐✓（单 tile 路径回归一致✓；
  教训：测试时中心坐标手打错一位（31.1807→31.8071）导致包围盒漂移 68km、建筑被视锥剔除，
  症状是"加载了但全黑"，先核对坐标再怀疑代码）。
  **CNBH-10m 高度注入 `--heights cnbh`**：Zenodo 7923866 瓦片按度格缓存
  (~/.cache/cnbh10m)；**瓦片是 UTM 投影 (EPSG:32651) 不是 4326**——采样前须把
  质心 rasterio.warp.transform 到瓦片 CRS（小涞港窗按 4326 读窗得零尺寸数组）。
  小涞港 937/1014 栋注入（中位 20.2m / 最大 37.8m，14 栋 OSM 标签保留、63 栋无像素
  保留 POLICY），高度分析预设从"清一色 6m 白板"变成真实城市高度梯度（商务区红≥24m、
  居住橙金、仓储绿）✓ 截图 acceptance/2026-11-05-cnbh-heights.png；面板/报告显示
  注入数✓。
  **离线预览包 `--offline`**：影像 GeoTIFF→本地 XYZ jpg 瓦片（**z0-native 全金字塔**,
  小涞港 3294 张/75MB——z0 根必须生成, 否则初始帧请求祖先瓦片 404; 写瓦片时读窗
  **必须裁剪到栅格范围再用 out_shape 降采样**, boundless 填充在 z0 会按 0.6m/px
  请求 6.7TB 内存直接被系统 SIGKILL, 顺带消除了 109 张纯填充黑瓦片）
  + CesiumJS 1.142 本地化（npmmirror tgz→~/.cache→复制进输出目录）+ 图层/3D Tiles
  全部本地。浏览器验收：**外部请求数 0**、就绪状态✓ 截图 acceptance/2026-11-05-offline-preview.png。
  **无网现场实测（2026-11-05 补）**：演示包打 zip→解压到全新路径→headless Chrome
  双重断网（--host-resolver-rules MAP * ~NOTFOUND + --proxy-server 死端口）+ CDP
  驱动（**Network.setCacheDisabled 必开**——Chrome 启发式缓存会跨 target 喂旧 HTML,
  造成假绿/假红测量）。结果: 外部请求 0、JS 异常 0、勾地形→getHeight ✓、截图
  acceptance/2026-11-05-offline-nonetwork-*.png。三连教训: ①**初始相机必须
  setView 且在 viewer 创建后任何 await 之前**——flyTo 动画首帧是全球视角,
  Cesium 对整个地球做四叉树影像请求（实测冷启动 200-450 条 404）; ②**不要给
  ImageryLayer 加 rectangle 限定场景范围**——能消掉全球剪影请求, 但 SwiftShader/
  无头渲染下影像被竖条纹拉进天空, 视觉严重损坏（视觉>控制台整洁, 已回退）;
  ③**不要补 EPSG:4326 地形 z0 西半球根**——缺根 Cesium 按不可用剪枝只 1 条 404,
  补了全 0 根反而让它认为全球地形可遍历。已知边界: 冷启动仍有约 150 条瞬时 404
  （视锥远端地面场景外瓦片, Cesium 重试 2 次）, 视觉无影响, 打开 DevTools 才可见。
  **Windows 现场启动器（2026-11-05 补）**：Windows 无 python3 命令且 .sh 双击不执行,
  包内新增 serve.bat（双击自动起服务+开浏览器; 内容纯 ASCII+CRLF——cmd 代码页 GBK,
  UTF-8 中文 echo 乱码; py 启动器优先, Windows Store 的 python3 假别名不碰; 未装
  Python 时给出 winget 安装提示）和 启动预览.command（mac 双击）; serve.sh 升级为
  自动开浏览器。bat 在 mac 上无法真跑, 现场首用要留心。
  **本地地形 `--offline` 附带**：GLO-30→heightmap-1.0 瓦片（789 块 z0-16），格式逐项
  从 Cesium 1.142 源码核对（fIt 解包: 65×65 Uint16LE 高度格+childTileMask+1 字节水mask,
  ion 编码 u16=(h+1000)*5）。排查三连：①**UrlTemplateImageryProvider.minimumLevel>0
  在 Cesium 1.142 触发 "Too many properties to enumerate" 渲染崩溃**（二分定位:
  本地瓦片+minLevel 崩, 本地无 minLevel 不崩, ESRI+minLevel 不崩）→ 影像 provider
  minimumLevel 恒 0, 低缩放瓦片多生成几级兜底; ②**CesiumTerrainProvider.fromUrl
  参数是瓦片目录**不是 layer.json 路径（传 layer.json 会变成 目录/layer.json/layer.json
  404 并触发无 layer.json 的回退路径）; ③**地形四叉树必须从 z0 根下降**——只生成
  z8+ 会卡"加载本地地形"（ getHeight 永远 undefined）, min_zoom=0 后 bbox 相交的
  低级瓦片仅十几张。验收: 勾选地形后 globe.getHeight(中心)=4.39m（椭球模式下≈0）,
  建筑随地形就位, 零外部请求✓ 截图 acceptance/2026-11-05-terrain.png。
  另: 诊断脚本踩 const 作用域坑——页面内 `const viewer` 不挂 window, 跨轮 evaluate
  访问不到, 模板已显式 `window.__viewer = viewer`。
  **配套图层 bug 修复**：水系/绿地面要素的 fromDegreesArray 被传入嵌套数组
  （RangeError: Invalid array length），被 per-layer try/catch 静默吞掉导致开关消失；
  改为正确遍历 Polygon 外环后 4 图层齐全✓（教训：图层加载失败不该静默，
  至少 console.warn 并在面板标注失败）。另：道路 corridor 宽度/透明度按等级调优
  （motorway 26m/0.6 → service 3.5m/0.8）、日照滑块默认 10:30、搜索飞行+信息卡属性展开。
  截图于 小涞港示范区_scene/acceptance/2026-11-05-*.png。
- **2026-11-04 Rudy Mac**（上海小涞港示范区, shp zip 输入, 6km 框）：
  全链路复验——roads 3230 / railways 333 / water 168 / green 123✓、预览 4 图层开关+要素数✓、
  预设模式切换✓、零 JS 错误✓。**屋顶 bug 修复**：此前屋顶画在楼底(楼顶敞口, doubleSided
  材质下呈"凹进去的空盒")，修复后解剖确认屋顶 12192 顶点全部位于 3~53m 楼顶高度、
  贴地异常 0；陡视角截图封闭平顶✓。**材质升级**：程序化立面纹理(窗格/亮灯窗/COLOR_0
  类型调色)嵌入 b3dm(2.0MB)，近景窗格可见✓。**高亮拾取**：b3dm 带 _BATCHID+batch table
  (10 属性×1014 栋)，悬停浅蓝/单击深蓝高亮+信息卡(名称/类型/高度(标注估算)/层数/地址/
  占地/OSM ID)✓。教训×2：自写 GLB 解剖脚本曾漏跳 BIN chunk 8 字节头导致"验证通过"的
  假证据；build_glb 曾把 buffers.byteLength 在图片 view 追加前写死导致模型渲染异常
  （dissect_b3dm.py 已加 buffer 长度断言防复发）。另: 巨大灰板有时不是 bug——
  虹桥站在 OSM 里就是几百米宽、6m 高的建筑足迹。
  截图于 小涞港示范区_scene/acceptance/2026-11-04-*.png。
- **2026-11-03 Rudy Mac**（macOS, `~/.venv_imagery`, Cesium 1.142）：
  永新山区点（0 建筑→软跳过✓）+ 永新县城点全链路通过——影像 GeoTIFF✓、
  GLO-30 DEM✓（114-135m）、GeoJSON✓、3D Tiles 双图层渲染✓、z19 占位自动降级✓、
  预览页贴地/绝对高程双模式✓（ego-browser 截图取证于 skill_test_county/acceptance/）。
  建筑仅 5 栋为 OSM 覆盖所限，非管线问题。
- **2026-09-08 旧脚本**（`~/Documents/work/28 下载影像数据/download_highres.py`）：
  永新县 6km 镶嵌 0.5m 影像 315MB 产出，本 skill 影像模块的前身。
