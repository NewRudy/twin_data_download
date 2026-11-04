# twin_data_download — 数字孪生场景基础数据下载

按中心坐标 + 半径，一站式下载并打包数字孪生/Cesium 场景所需的基础数据：

| 数据 | 来源 | 产出 |
|---|---|---|
| 高清影像 | ESRI World Imagery (Maxar) / Google 卫星 | GeoTIFF (EPSG:3857)，z17≈1.2m、z18≈0.6m |
| 高程 DEM | Copernicus GLO-30 | GeoTIFF (EPSG:4326)，标称 30m |
| 建筑物 | OpenStreetMap（Overpass API） | GeoJSON（含高度估算；`--heights cnbh` 可注入 CNBH-10m 栅格高度） |
| 道路/铁路/水系/绿地 | OpenStreetMap（同一次 Overpass 查询） | 各自独立 GeoJSON，"OSM 提供啥就提取啥" |
| 3D 白模 | 由建筑轮廓挤出（封闭屋顶 + 女儿墙 + 屋顶设备盒 + 程序化立面纹理：窗格/亮灯窗/按建筑类型调色；逐栋 batchId；>1500 栋自动分块 LOD） | 3D Tiles 1.0（b3dm + tileset.json + batch table） |
| 三维预览 | CesiumJS 1.142 | 单文件 HTML + serve.sh；悬停/单击高亮拾取+信息卡、按名称搜索飞行、日照时间滑块、道路/铁路/水系/绿地独立开关（真实米宽走廊）、42° 斜视角 |
| 下载报告 | — | download_report.json（数据源/瓦片数/失败数/各图层要素数） |

建筑数据**有就生成、没有就跳过**：OSM 覆盖不到的位置自动跳过建筑与 3D Tiles，
影像和高程不受影响。

## 快速开始

```bash
git clone https://github.com/NewRudy/twin_data_download.git
cd twin_data_download
bash setup_env.sh                      # 创建 ~/.venv_imagery 并装依赖（清华 pip 源）

~/.venv_imagery/bin/python3 scripts/download_scene.py \
  --lon 114.351205 --lat 26.937214 --radius 3000
```

常用参数：

```bash
--geojson 场址边界.geojson --buffer 500
                      # 按 GeoJSON 边界下载(Point/Polygon/FeatureCollection 均可,
                      # 取包围盒; --buffer 向外扩米数), 范围过大/过小会提示
--zoom 17             # 1.2m，更快（16-19；ESRI 无影像层级会自动探测降级）
--source google       # Google 影像源（国内需代理）
--skip-buildings      # 只要影像和高程
--skip-extras         # 不要道路/铁路/水系/绿地配套图层
--skip-3dtiles --skip-preview
--resume              # 断点续传: 复用已有产物, 跳过网络下载, 只重做 3D Tiles/预览/报告
--tile-threshold 1500 # 建筑数超过该值 3D Tiles 自动网格分块 (~350 栋/块)
--heights cnbh        # 无 OSM 标签的建筑注入 CNBH-10m 栅格高度 (Zenodo, 需外网)
--name 场景名 --out 输出目录
```

下载完成后：

```bash
cd <输出目录> && ./serve.sh
# 浏览器打开 http://localhost:8000/cesium_preview.html
```

预览页默认**纹理模式**（3D Tiles 立面带窗格纹理）；「高度分析」切换为按高度配色的
GeoJSON 白模，还有道路/铁路/水系/绿地独立开关（带要素数），默认**贴地模式**+42° 斜视角；
接真实地形渲染时勾选「绝对高程模式」。

## 输出结构

```
<out>/
├── imagery_z18_esri.tif      # 高清影像
├── dem_glo30_30m.tif         # 高程
├── buildings.geojson         # 建筑轮廓 + height_m/base_m/top_m
├── roads.geojson             # OSM 道路（class/name/bridge/tunnel）
├── railways.geojson          # OSM 铁路/轨交
├── water.geojson             # 水系（河流线 + 湖泊面）
├── green.geojson             # 绿地（公园/草坪/林地/球场）
├── buildings_3dtiles/
│   ├── tileset.json          # ENU→ECEF，Cesium 直接加载
│   └── scene.b3dm            # ≤1500 栋单 tile；更多时为 tiles/cell_*.b3dm 分块
├── cesium_preview.html
├── serve.sh
└── download_report.json      # 证据记录
```

接入 Cesium 平台：向场景注册 `buildings_3dtiles/tileset.json` 的完整 URL；
b3dm 内含逐栋 batch table（名称/类型/高度/层数/地址/占地/OSM ID），
平台侧可用 `Cesium3DTileFeature.getProperty()` 直接读栋级属性、`feature.color` 做高亮；
GeoJSON 白模可用 `extrudedHeight=top_m / height=base_m` 客户端挤出，样式可调。

## 证据边界（重要）

- **建筑高度是估算**（OSM `height` 标签 > 层数×3m > 默认 6m），每栋的依据记录在
  `properties.height_source`，不能当测绘成果用。
- **立面纹理是程序化生成的**（窗格/亮灯窗按层高循环排布，非真实照片），
  用于让白模有建筑感；真实立面需无人机倾斜摄影。
- **OSM 建筑在中国基本不可用**：县城 3km 范围实测仅个位数建筑，省会密集城区也只有
  数十栋；Microsoft / Google 的全球建筑轮廓数据集均不覆盖中国。项目现场需要真实
  建筑请用无人机倾斜摄影或授权商业数据。
- **ESRI z19+ 在县城/乡镇多为占位灰瓦片**（"Map data not yet available"），
  本工具会自动探测并降级到有真实影像的层级。
- 分辨率为按纬度换算的估算值；DEM 30m 适合做场景地形底座，非测绘级。
- 几何验证工具：`python3 scripts/dissect_b3dm.py <scene.b3dm>` 可解剖 b3dm，
  校验屋顶法线朝上且位于楼顶高度（曾据此发现并修复屋顶画在楼底的 bug）。

## 数据源致谢与使用边界

- 影像：Esri World Imagery（Esri、Maxar、Earthstar Geographics 版权），在线瓦片仅供
  预览/开发用途，请遵守 Esri 服务条款，勿再分发下载的影像。
- 高程：Copernicus GLO-30 DEM。
- 建筑：© OpenStreetMap contributors (ODbL)。

## 相关

- 本仓库是 `~/skills` 共享技能库中 `gis-data-download` skill 的发布副本。
- 2026-09 永新县 6km 镶嵌影像由本工具前身脚本产出。
