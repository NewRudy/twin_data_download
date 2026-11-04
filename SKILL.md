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
| 3D 白模 | 上述建筑挤出（含封闭屋顶） | 高度 POLICY 估算 | 3D Tiles 1.0 (b3dm + tileset.json) |
| 预览页 | CesiumJS | 验收用 | cesium_preview.html + serve.sh |

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

# 不要道路/水系/绿地配套图层
... --skip-extras
```

参数：`--zoom 16-19`、`--radius` 米、`--geojson 边界文件`、`--buffer` 外扩米、
`--name 场景名`、`--out 输出目录`、
`--workers 并发数`、`--default-height`（无标签建筑默认高度，默认 6m）。

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

预览页验证点：影像底图加载、建筑双图层 + 道路/铁路/水系/绿地图层都能显示（各自有开关）、
右下角无红色报错；近景确认建筑**屋顶封闭**（白模顶面完整，不是空心管）。
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
│   └── scene.b3dm
├── cesium_preview.html       # 多图层预览（建筑/路网/水系/绿地独立开关）
├── serve.sh                  # python3 -m http.server 封装
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

- **建筑高度是 POLICY 估算**：OSM `height` 标签 > `building:levels`×3m > 默认 6m，
  每栋的估算依据记录在 `properties.height_source`。不能当测绘成果用。
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
- 预览页用 jsdelivr CDN 的 Cesium 1.142，离线环境需自备 Cesium 或改本地路径。
- 产线环境（投标/汇报）引用这些数据时，按 [[evidence-boundary]] 标注层级，别说成"测绘级"。

## 已验证机器

> 发布副本（其他机器直接 clone 用）：https://github.com/NewRudy/twin_data_download
> 改动本 skill 后同步 `SKILL.md requirements.txt setup_env.sh scripts/download_scene.py` 到该仓库。

- **2026-11-04 Rudy Mac**（上海小涞港示范区, shp zip 输入, 6km 框）：
  多图层链路通过——roads 3230 / railways 333 / water 168 / green 123 独立 GeoJSON✓、
  预览页 4 个动态图层开关+分级样式✓、建筑 1014 栋屋顶封闭✓
  （GLB 解剖: 屋顶法线 y>0.5 顶点 10963 个, 修复前为 0）、
  图层开关/贴地-绝对高程切换交互✓、页面零 JS 错误✓
  （截图于 小涞港示范区_scene/acceptance/）。OSM 覆盖仍不均：非中心区大片住宅无建筑。
- **2026-11-03 Rudy Mac**（macOS, `~/.venv_imagery`, Cesium 1.142）：
  永新山区点（0 建筑→软跳过✓）+ 永新县城点全链路通过——影像 GeoTIFF✓、
  GLO-30 DEM✓（114-135m）、GeoJSON✓、3D Tiles 双图层渲染✓、z19 占位自动降级✓、
  预览页贴地/绝对高程双模式✓（ego-browser 截图取证于 skill_test_county/acceptance/）。
  建筑仅 5 栋为 OSM 覆盖所限，非管线问题。
- **2026-09-08 旧脚本**（`~/Documents/work/28 下载影像数据/download_highres.py`）：
  永新县 6km 镶嵌 0.5m 影像 315MB 产出，本 skill 影像模块的前身。
