#!/usr/bin/env python3
"""
数字孪生场景基础数据一站式下载工具
================================
按中心坐标 + 半径下载三类基础数据并打包:
  1. 高清影像  ESRI World Imagery (Maxar) / Google Satellite 瓦片拼接 → GeoTIFF (EPSG:3857)
  2. 高程 DEM  Copernicus GLO-30 (30m) 窗口裁剪 → GeoTIFF (EPSG:4326)
  3. 建筑物    OpenStreetMap 轮廓 + 估算高度 → GeoJSON (EPSG:4326)
  4. 3D Tiles  建筑白模挤出 → b3dm + tileset.json (Cesium 离线可用)
  5. 预览页    Cesium 本地预览 HTML + serve.sh

证据边界: 建筑高度为 POLICY 估算(层数×3m); 分辨率为 MODELED 估算值;
影像/DEM 像元为数据源标称值。全部记录在 download_report.json。
"""

import argparse
import base64
import concurrent.futures
import datetime
import glob
import io
import json
import math
import os
import struct
import sys
import time

try:
    import requests
    import numpy as np
    from PIL import Image
    import mercantile
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.merge import merge as rio_merge
    from shapely.geometry import Polygon, shape
    import shapefile as pyshp
    import zipfile
    import tempfile
    from pyproj import CRS, Transformer
except ImportError as e:
    print(f"[-] 缺少依赖: {e}")
    print("[-] 请先运行 skill 目录下的 setup_env.sh")
    sys.exit(1)

# ---------------------------------------------------------------- 常量

ESRI_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
GOOGLE_URL = "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"
DEM_BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

FLOOR_HEIGHT = 3.0        # POLICY: 每层估算高度
DEFAULT_HEIGHT = 6.0      # POLICY: 无任何高度信息时的默认值 (按 2 层)
MIN_BUILDING_AREA = 8.0   # m², 过滤碎多边形
ESRI_PLACEHOLDER_BYTES = 4096  # ESRI z19+ 无影像区域返回同一张 ~2.5KB 占位灰瓦片


def probe_esri_tile(zoom, lon, lat):
    """探测指定层级中心瓦片是否真实影像。返回 True=可用, False=占位/不可用。"""
    try:
        t = mercantile.tile(lon, lat, zoom)
        url = ESRI_URL.format(z=zoom, y=t.y, x=t.x)
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        return len(r.content) > ESRI_PLACEHOLDER_BYTES
    except Exception:
        return False


# ---------------------------------------------------------------- 工具函数

def log(msg, **kw):
    print(msg, flush=True, **kw)


def bbox_from_center(lon, lat, radius_m):
    d_lat = radius_m / 111320.0
    d_lon = radius_m / (111320.0 * math.cos(math.radians(lat)))
    return lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat  # w, s, e, n


def geojson_bbox(path, buffer_m=0.0):
    """从 GeoJSON 文件取所有坐标的包围盒。支持 Point/Line/Polygon/Multi*/Feature/FC。"""
    def walk(obj, acc):
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            acc.append(obj)
            return
        if isinstance(obj, list):
            if len(obj) >= 2 and isinstance(obj[0], (int, float)) and isinstance(obj[1], (int, float)) \
                    and not isinstance(obj[0], bool):
                acc.extend([obj[0], obj[1]])
                return
            for item in obj:
                walk(item, acc)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    acc = []
    walk(data.get("coordinates") if data.get("type") != "FeatureCollection"
         else data, acc)
    # FeatureCollection: 递归进每个 feature 的 geometry
    if data.get("type") == "FeatureCollection":
        acc = []
        for feat in data.get("features", []):
            sub = []
            walk(feat.get("geometry", {}).get("coordinates"), sub)
            acc.extend(sub)
    elif data.get("type") == "Feature":
        sub = []
        walk(data.get("geometry", {}).get("coordinates"), sub)
        acc = sub
    if not acc:
        raise ValueError("GeoJSON 中没有找到坐标")
    xs, ys = acc[0::2], acc[1::2]
    w, e, s, n = min(xs), max(xs), min(ys), max(ys)
    if not (-181 <= w <= 181 and -181 <= e <= 181 and -91 <= s <= 91 and -91 <= n <= 91):
        raise ValueError(
            f"GeoJSON 坐标不是经纬度 (bbox {w:.1f},{s:.1f}~{e:.1f},{n:.1f}), "
            "疑似投影平面坐标。请提供带 .prj 的 shapefile 压缩包, 或提供定位参数")
    d_lat = buffer_m / 111320.0
    d_lon = buffer_m / (111320.0 * math.cos(math.radians((s + n) / 2)))
    return w - d_lon, s - d_lat, e + d_lon, n + d_lat


def load_bounds(path, buffer_m=0.0):
    """边界文件 → WGS84 bbox (w,s,e,n)。
    支持: .zip(shapefile, 需含 .prj) / .shp / .geojson / .json。
    投影坐标用 .prj 的 WKT 经 pyproj 换算; 城市局部坐标系(如 Beijing1954 城市投影)
    与 WGS84 间无严格转换参数时为近似换算, 误差通常在百米内。"""
    ext = os.path.splitext(path)[1].lower()
    shp_path = None
    tmp_dir = None
    if ext == ".zip":
        tmp_dir = tempfile.mkdtemp(prefix="gis_bounds_")
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                    raise ValueError(f"zip 内路径异常: {name}")
            zf.extractall(tmp_dir)
        for root, _, files in os.walk(tmp_dir):
            for fn in files:
                if fn.lower().endswith(".shp"):
                    shp_path = os.path.join(root, fn)
        if not shp_path:
            raise ValueError("zip 中没有找到 .shp 文件")
    elif ext == ".shp":
        shp_path = path
    if shp_path:
        prj = os.path.splitext(shp_path)[0] + ".prj"
        if not os.path.exists(prj):
            raise ValueError(f"shapefile 缺少 .prj 坐标系文件: {prj}\n"
                             "请在 ArcGIS/平台导出时勾选坐标系, 或告知 EPSG/投影参数")
        crs = CRS.from_wkt(open(prj, encoding="utf-8", errors="replace").read())
        tf = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
        reader = pyshp.Reader(shp_path)
        if reader.bbox:
            xs = [reader.bbox[0], reader.bbox[2]]
            ys = [reader.bbox[1], reader.bbox[3]]
        else:
            xs, ys = [], []
            for sh in reader.shapes():
                for x, y in sh.points:
                    xs.append(x)
                    ys.append(y)
        lons, lats = tf.transform(xs, ys)
        w, e = min(lons), max(lons)
        s, n = min(lats), max(lats)
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        w, s, e, n = geojson_bbox(path, buffer_m=0.0)
    d_lat = buffer_m / 111320.0
    d_lon = buffer_m / (111320.0 * math.cos(math.radians((s + n) / 2)))
    return w - d_lon, s - d_lat, e + d_lon, n + d_lat


def geodetic_to_ecef(lon, lat, h=0.0):
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lonr, latr = math.radians(lon), math.radians(lat)
    n = a / math.sqrt(1 - e2 * math.sin(latr) ** 2)
    return ((n + h) * math.cos(latr) * math.cos(lonr),
            (n + h) * math.cos(latr) * math.sin(lonr),
            (n * (1 - e2) + h) * math.sin(latr))


def enu_to_ecef_matrix(lon0, lat0, h0=0.0):
    """ENU→ECEF 4x4, glTF 列主序 16 元素。glTF 局部坐标: X=E, Y=U, Z=-N
    (Cesium 对 glTF 内容自动做 Y-up→Z-up 轴修正)。"""
    x0, y0, z0 = geodetic_to_ecef(lon0, lat0, h0)
    lonr, latr = math.radians(lon0), math.radians(lat0)
    s, c, S, C = math.sin(latr), math.cos(latr), math.sin(lonr), math.cos(lonr)
    col_e = (-S, C, 0)
    col_n = (-s * C, -s * S, c)
    col_u = (c * C, c * S, s)
    return [col_e[0], col_e[1], col_e[2], 0.0,
            col_n[0], col_n[1], col_n[2], 0.0,
            col_u[0], col_u[1], col_u[2], 0.0,
            x0, y0, z0, 1.0]


def triangulate_ring(ring):
    """耳切法三角化 2D 环 (不含闭合重复点)。返回索引三元组列表。"""
    n = len(ring)
    if n < 3:
        return []
    idx = list(range(n))
    # 统一为逆时针
    area2 = sum(ring[i][0] * ring[(i + 1) % n][1] - ring[(i + 1) % n][0] * ring[i][1]
                for i in range(n))
    if area2 < 0:
        idx.reverse()

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    tris = []
    guard = 0
    while len(idx) > 3 and guard < 20000:
        guard += 1
        clipped = False
        m = len(idx)
        for i in range(m):
            a, b, c = idx[(i - 1) % m], idx[i], idx[(i + 1) % m]
            pa, pb, pc = ring[a], ring[b], ring[c]
            cr = cross(pa, pb, pc)
            if abs(cr) < 1e-9:
                idx.remove(b)
                clipped = True
                break
            if cr <= 0:
                continue
            ok = True
            for j in idx:
                if j in (a, b, c):
                    continue
                p = ring[j]
                if (cross(pa, pb, p) >= -1e-12 and cross(pb, pc, p) >= -1e-12
                        and cross(pc, pa, p) >= -1e-12):
                    ok = False
                    break
            if ok:
                tris.append((a, b, c))
                idx.remove(b)
                clipped = True
                break
        if not clipped:
            for k in range(1, len(idx) - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
            break
    if len(idx) == 3:
        tris.append(tuple(idx))
    return tris


# ---------------------------------------------------------------- 1. 影像

def download_imagery(w, s, e, n, zoom, source, out_path, workers=12):
    tiles = list(mercantile.tiles(w, s, e, n, zooms=zoom))
    if not tiles:
        log("[-] 影像: 未找到瓦片")
        return None, {}

    lat_mid = (s + n) / 2
    min_x, max_x = min(t.x for t in tiles), max(t.x for t in tiles)
    min_y, max_y = min(t.y for t in tiles), max(t.y for t in tiles)
    cols, rows = max_x - min_x + 1, max_y - min_y + 1
    res = 156543.03 * math.cos(math.radians(lat_mid)) / (2 ** zoom)
    url_t = GOOGLE_URL if source == "google" else ESRI_URL

    log(f"[*] 影像: zoom {zoom} (~{res:.2f} m/px 估算), 网格 {cols}x{rows}={len(tiles)} 瓦片, 源={source}")

    def fetch(t):
        url = url_t.format(z=zoom, x=t.x, y=t.y)
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                return (t.x, t.y), np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
        except Exception:
            pass
        return (t.x, t.y), None

    canvas = np.zeros((rows * 256, cols * 256, 3), dtype=np.uint8)
    failed, done = 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for (tx, ty), arr in ex.map(fetch, tiles):
            if arr is not None:
                canvas[(ty - min_y) * 256:(ty - min_y + 1) * 256,
                       (tx - min_x) * 256:(tx - min_x + 1) * 256] = arr
            else:
                failed += 1
            done += 1
            if done % 20 == 0 or done == len(tiles):
                log(f"\r[*]   进度 {done}/{len(tiles)} ({done * 100 // len(tiles)}%)"
                    + (f"  失败 {failed}" if failed else ""), end="")
    log("")

    ul = mercantile.xy_bounds(min_x, min_y, zoom)
    lr = mercantile.xy_bounds(max_x, max_y, zoom)
    transform = from_bounds(ul.left, lr.bottom, lr.right, ul.top, canvas.shape[1], canvas.shape[0])
    with rasterio.open(out_path, "w", driver="GTiff", height=canvas.shape[0],
                       width=canvas.shape[1], count=3, dtype="uint8",
                       crs="EPSG:3857", transform=transform, compress="deflate") as dst:
        for b in range(3):
            dst.write(canvas[:, :, b], b + 1)

    info = {"zoom": zoom, "tiles": len(tiles), "failed_tiles": failed,
            "resolution_m_modeled": round(res, 3),
            "size_px": [canvas.shape[1], canvas.shape[0]], "crs": "EPSG:3857"}
    log(f"[✓] 影像 GeoTIFF: {out_path}")
    return out_path, info


# ---------------------------------------------------------------- 2. 高程

def dem_tile_ids(w, s, e, n):
    ids = []
    for lat_i in range(int(math.floor(s)), int(math.floor(n)) + 1):
        for lon_i in range(int(math.floor(w)), int(math.floor(e)) + 1):
            ns = f"{'N' if lat_i >= 0 else 'S'}{abs(lat_i):02d}_00"
            ew = f"{'E' if lon_i >= 0 else 'W'}{abs(lon_i):03d}_00"
            ids.append(f"Copernicus_DSM_COG_10_{ns}_{ew}_DEM")
    return ids


def download_dem(w, s, e, n, out_path):
    ids = dem_tile_ids(w, s, e, n)
    log(f"[*] 高程: Copernicus GLO-30 (标称 30m), 需读取 {len(ids)} 个 1°x1° COG: {', '.join(ids)}")
    srcs = []
    with rasterio.Env(GDAL_HTTP_TIMEOUT="120", GDAL_HTTP_MAX_RETRY="3",
                      GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        for tid in ids:
            url = f"{DEM_BUCKET}/{tid}/{tid}.tif"
            try:
                srcs.append(rasterio.open(f"/vsicurl/{url}"))
            except Exception as ex:
                log(f"[!]   打不开 {tid}: {ex}")
        if not srcs:
            log("[-] 高程: 所有 COG 读取失败")
            return None, {"error": "all cog failed"}
        mosaic, transform = rio_merge(srcs, bounds=(w, s, e, n))
        profile = srcs[0].profile.copy()
        for src in srcs:
            src.close()

    mosaic = mosaic[0].astype("float32")
    with rasterio.open(out_path, "w", driver="GTiff", height=mosaic.shape[0],
                       width=mosaic.shape[1], count=1, dtype="float32",
                       crs="EPSG:4326", transform=transform, compress="deflate") as dst:
        dst.write(mosaic, 1)
        dst.set_band_description(1, "Elevation (m, Copernicus GLO-30)")

    valid = mosaic[mosaic > -10000]
    info = {"source": "Copernicus GLO-30", "nominal_resolution_m": 30,
            "tiles": ids, "crs": "EPSG:4326",
            "elev_min": float(valid.min()) if valid.size else None,
            "elev_max": float(valid.max()) if valid.size else None}
    log(f"[✓] 高程 GeoTIFF: {out_path}  (高程范围 {info['elev_min']:.0f}~{info['elev_max']:.0f} m)")
    return out_path, info


def make_dem_sampler(dem_path):
    if not dem_path or not os.path.exists(dem_path):
        return lambda lon, lat: 0.0
    ds = rasterio.open(dem_path)

    def sample(lon, lat):
        try:
            vals = list(ds.sample([(lon, lat)]))
            v = float(vals[0][0])
            return v if v > -10000 else 0.0
        except Exception:
            return 0.0
    return sample


# ---------------------------------------------------------------- 3. 建筑物

def fetch_osm(w, s, e, n, endpoints=None, timeout=150):
    """一次拉取建筑+道路+铁路+水系+绿地 (分层解析在前端做)。"""
    query = (f"[out:json][timeout:{timeout}];"
             f"(way[\"building\"]({s},{w},{n},{e});relation[\"building\"]({s},{w},{n},{e});"
             f"way[\"highway\"]({s},{w},{n},{e});"
             f"way[\"railway\"~\"^(rail|light_rail|subway|tram|monorail)$\"]({s},{w},{n},{e});"
             f"way[\"waterway\"]({s},{w},{n},{e});"
             f"way[\"natural\"=\"water\"]({s},{w},{n},{e});relation[\"natural\"=\"water\"]({s},{w},{n},{e});"
             f"way[\"landuse\"~\"^(reservoir|basin|grass|forest|meadow|village_green)$\"]({s},{w},{n},{e});"
             f"way[\"leisure\"~\"^(park|garden|pitch|golf_course)$\"]({s},{w},{n},{e});"
             f"relation[\"leisure\"~\"^(park|garden|golf_course)$\"]({s},{w},{n},{e});"
             f");out geom;")
    endpoints = endpoints or OVERPASS_ENDPOINTS
    for url in endpoints:
        try:
            log(f"[*] 建筑/道路/水系/绿地: 查询 Overpass ({url}) ...")
            r = requests.post(url, data={"data": query}, timeout=timeout + 60,
                              headers={"User-Agent": "gis-data-download/1.0"})
            if r.status_code == 200:
                return r.json().get("elements", []), url
            log(f"[!]   HTTP {r.status_code}, 换下一个端点")
        except Exception as ex:
            log(f"[!]   失败: {ex}, 换下一个端点")
    return None, None


def relation_rings(el):
    """relation multipolygon → [[外环, 洞...], ...] (未闭合成员环暂不拼接)。"""
    outers, inners = [], []
    for m in el.get("members") or []:
        if m.get("type") != "way" or not m.get("geometry"):
            continue
        pts = [(p["lon"], p["lat"]) for p in m["geometry"]]
        if len(pts) < 4 or pts[0] != pts[-1]:
            continue
        (outers if m.get("role") != "inner" else inners).append(pts)
    inner_polys = [Polygon(r) for r in inners]
    result = []
    for out in outers:
        op = Polygon(out)
        if not op.is_valid:
            op = op.buffer(0)
        if op.is_empty or op.geom_type != "Polygon":
            continue
        holes = [list(ip.exterior.coords)[:-1] for ip in inner_polys
                 if op.contains(ip.representative_point())]
        result.append([list(out)] + holes)
    return result


def _area_m2(ring_lonlat):
    poly = Polygon(ring_lonlat)
    cen = poly.centroid
    return poly.area * 111320.0 ** 2 * math.cos(math.radians(cen.y))


def osm_extras_to_features(elements):
    """非建筑要素 → {roads, railways, water, green} 四个 GeoJSON 图层。"""
    roads, rails, water, green = [], [], [], []

    def line_feat(pts, props):
        return {"type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[p["lon"], p["lat"]] for p in pts]},
                "properties": props}

    def poly_feat(rings, props):
        return {"type": "Feature",
                "geometry": {"type": "Polygon",
                             "coordinates": [[list(p) for p in r] for r in rings]},
                "properties": props}

    def closed_ring(geom):
        pts = [(p["lon"], p["lat"]) for p in geom]
        if len(pts) < 4:
            return None
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    for el in elements:
        tags = el.get("tags") or {}
        if tags.get("building"):
            continue
        eid = el.get("id")
        geom = el.get("geometry") or []
        if el["type"] == "way" and len(geom) < 2:
            continue

        if tags.get("highway"):
            roads.append(line_feat(geom, {
                "class": tags["highway"], "name": tags.get("name", ""),
                "bridge": tags.get("bridge", "no"), "tunnel": tags.get("tunnel", "no"),
                "osm_id": eid}))
        elif tags.get("railway") in {"rail", "light_rail", "subway", "tram", "monorail"}:
            rails.append(line_feat(geom, {
                "class": tags["railway"], "name": tags.get("name", ""), "osm_id": eid}))
        elif tags.get("waterway"):
            water.append(line_feat(geom, {
                "kind": "line", "class": tags["waterway"],
                "name": tags.get("name", ""), "osm_id": eid}))
        else:
            ring = closed_ring(geom) if el["type"] == "way" else None
            if tags.get("natural") == "water" or tags.get("landuse") in {"reservoir", "basin"}:
                if ring and _area_m2(ring) >= 30:
                    water.append(poly_feat([ring], {
                        "kind": "area", "class": tags.get("landuse", "water"),
                        "name": tags.get("name", ""), "osm_id": eid}))
            elif (tags.get("landuse") in {"grass", "forest", "meadow", "village_green"}
                  or tags.get("leisure") in {"park", "garden", "pitch", "golf_course"}):
                if ring and _area_m2(ring) >= 50:
                    green.append(poly_feat([ring], {
                        "class": tags.get("leisure") or tags.get("landuse"),
                        "name": tags.get("name", ""), "osm_id": eid}))

    for el in elements:
        if el["type"] != "relation":
            continue
        tags = el.get("tags") or {}
        eid = el.get("id")
        if tags.get("natural") == "water":
            for rings in relation_rings(el):
                if _area_m2(rings[0]) >= 30:
                    water.append(poly_feat(rings, {
                        "kind": "area", "class": "water",
                        "name": tags.get("name", ""), "osm_id": eid}))
        elif tags.get("leisure") in {"park", "garden", "golf_course"}:
            for rings in relation_rings(el):
                if _area_m2(rings[0]) >= 50:
                    green.append(poly_feat(rings, {
                        "class": tags["leisure"], "name": tags.get("name", ""), "osm_id": eid}))

    return {"roads": roads, "railways": rails, "water": water, "green": green}


def parse_height(tags):
    """返回 (height_m, min_height_m, height_source)。POLICY 估算。"""
    def num(v):
        try:
            return float(str(v).strip().rstrip("m米").strip())
        except Exception:
            return None
    h = num(tags.get("height"))
    if h and 0 < h < 800:
        mh = num(tags.get("min_height")) or 0.0
        return h, mh, "osm:height"
    lv = num(tags.get("building:levels"))
    if lv and 0 < lv < 200:
        mh = num(tags.get("building:min_level")) or 0.0
        return lv * FLOOR_HEIGHT, mh * FLOOR_HEIGHT, "policy:levels*3"
    return DEFAULT_HEIGHT, 0.0, "policy:default6"


def osm_to_features(elements, dem_sample):
    """OSM elements → 建筑 GeoJSON features (WGS84)。只取带 building 标签的要素。"""
    feats, stats = [], {"from": 0, "no_tag": 0, "skipped_small": 0, "skipped_open": 0}

    def make_ring(geom_pts):
        pts = [(p["lon"], p["lat"]) for p in geom_pts]
        if len(pts) < 4:
            return None
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    def emit(rings_lonlat, tags, osm_id):
        nonlocal feats
        try:
            poly = Polygon(rings_lonlat[0], rings_lonlat[1:] if len(rings_lonlat) > 1 else None)
        except Exception:
            stats["skipped_open"] += 1
            return
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                stats["skipped_open"] += 1
                return
        area_m2 = poly.area * 111320.0 ** 2 * math.cos(math.radians(poly.centroid.y))
        if area_m2 < MIN_BUILDING_AREA:
            stats["skipped_small"] += 1
            return
        h, mh, hsrc = parse_height(tags or {})
        if hsrc == "policy:default6":
            stats["no_tag"] += 1
        else:
            stats["from"] += 1
        cen = poly.centroid
        base = dem_sample(cen.x, cen.y) + mh
        coords = [[list(p) for p in ring] for ring in rings_lonlat]
        t = tags or {}
        addr = ", ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber"),
                                     t.get("addr:city")) if x)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": coords},
            "properties": {
                "osm_id": osm_id,
                "name": t.get("name", ""),
                "building": t.get("building", "yes"),
                "height_m": round(h, 2),
                "base_m": round(base, 2),
                "top_m": round(base + h, 2),
                "height_source": hsrc,
                "area_m2_est": round(area_m2, 1),
                "levels": t.get("building:levels", ""),
                "amenity": t.get("amenity", ""),
                "addr": addr,
            },
        })

    for el in elements:
        tags, eid = el.get("tags") or {}, el.get("id")
        if not tags.get("building"):
            continue
        if el["type"] == "way":
            ring = make_ring(el.get("geometry") or [])
            if ring:
                emit([ring], tags, eid)
            else:
                stats["skipped_open"] += 1
        elif el["type"] == "relation":
            for rings in relation_rings(el):
                emit(rings, tags, eid)
    return feats, stats


def write_layer_geojson(feats, out_path, bbox, label="layer", extra_meta=None):
    fc = {"type": "FeatureCollection",
          "metadata": {"source": "OpenStreetMap via Overpass API",
                       "layer": label,
                       "bbox_wgs84": [round(x, 6) for x in bbox],
                       "count": len(feats),
                       "generated": datetime.datetime.now().isoformat(timespec="seconds")},
          "features": feats}
    if extra_meta:
        fc["metadata"].update(extra_meta)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    log(f"[✓] {label} GeoJSON: {out_path}  ({len(feats)} 要素)")


# ---------------------------------------------------------------- 4. 3D Tiles

def project_enu(lon, lat, lon0, lat0):
    e = (lon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    n = (lat - lat0) * 110540.0
    return e, n


def make_facade_png():
    """程序化立面纹理图集 256x1024 (4 个 3m×3m 模块纵向排列, REPEAT 循环)。

    v 方向随楼层循环: 普通窗 / 宽窗 / 亮灯窗 / 条形高窗, 25% 楼层亮灯。
    """
    import io
    from PIL import Image, ImageDraw
    W, M = 256, 256
    H = M * 4
    img = Image.new("RGB", (W, H))
    dr = ImageDraw.Draw(img)
    rows = [
        {"bg": (217, 212, 203), "win": (70, 80, 92),   "frame": (231, 228, 222),
         "rect": (64, 64, 192, 192), "sill": True},
        {"bg": (213, 209, 201), "win": (77, 88, 102),  "frame": (229, 225, 219),
         "rect": (56, 72, 200, 168), "sill": True},
        {"bg": (215, 212, 205), "win": (227, 199, 143), "frame": (222, 217, 208),
         "rect": (64, 64, 192, 192), "sill": False},
        {"bg": (205, 208, 211), "win": (88, 99, 110),  "frame": (224, 222, 218),
         "rect": (44, 44, 212, 96), "sill": False},
    ]
    for i, r in enumerate(rows):
        y0 = i * M
        dr.rectangle([0, y0, W - 1, y0 + M - 1], fill=r["bg"])
        x0, yy0, x1, yy1 = r["rect"]
        # 窗框 (略外扩)
        dr.rectangle([x0 - 5, y0 + yy0 - 5, x1 + 5, y0 + yy1 + 5], fill=r["frame"])
        # 玻璃 (上深下浅模拟反光)
        gx0, gy0, gx1, gy1 = x0, y0 + yy0, x1, y0 + yy1
        dr.rectangle([gx0, gy0, gx1, gy1], fill=r["win"])
        dr.rectangle([gx0, gy0, gx1, gy0 + (gy1 - gy0) // 3], fill=tuple(
            max(0, c - 12) for c in r["win"]))
        if r["sill"]:  # 窗台
            dr.rectangle([x0 - 9, gy1, x1 + 9, gy1 + 8], fill=(178, 172, 163))
    arr = np.asarray(img).astype("int16")
    noise = np.random.default_rng(20261104).normal(0, 3.2, arr.shape).astype("int16")
    arr = np.clip(arr + noise, 0, 255).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG", optimize=True)
    return buf.getvalue()


# 按建筑类型的 COLOR_0 色调 (乘在纹理上)
_WARM = {"residential", "apartments", "house", "detached", "terrace",
         "semidetached_house", "bungalow", "yes", "dormitory"}
_COOL = {"commercial", "retail", "office", "supermarket", "hotel",
         "government", "civic", "mixed"}
_GRAY = {"industrial", "warehouse", "garage", "shed", "service", "farm_auxiliary"}
_CYAN = {"school", "university", "hospital", "public"}


def _bldg_tint(btype, rnd):
    if btype in _COOL:
        base = (238, 243, 251)
    elif btype in _GRAY:
        base = (229, 231, 235)
    elif btype in _CYAN:
        base = (249, 248, 254)
    else:
        base = (252, 249, 242)
    j = rnd.randint(-8, 3)
    return tuple(max(0, min(255, c + j)) for c in base) + (255,)


def build_glb(prims, materials, texture_png=None):
    """多 primitive glTF 2.0 GLB。prims: [{positions,normals,uvs?,colors?,material}]。

    坐标: X=E, Y=U, Z=-N。texture_png 非空时嵌入 image 0 (sampler REPEAT)。
    """
    views, accs, bin_parts = [], [], []
    offset = 0

    def add_view(data, target=34962):
        nonlocal offset
        pad = (4 - offset % 4) % 4
        if pad:
            bin_parts.append(b"\0" * pad)
            offset += pad
        v = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        bin_parts.append(data)
        offset += len(data)
        return len(views) - 1

    prims_json = []

    def add_acc(view, ctype, count, vtype, mn=None, mx=None, normalized=False):
        a = {"bufferView": view, "componentType": ctype, "count": count, "type": vtype}
        if normalized:
            a["normalized"] = True
        if mn is not None:
            a["min"], a["max"] = mn, mx
        accs.append(a)
        return len(accs) - 1

    for pr in prims:
        pos = np.asarray(pr["positions"], dtype="<f4").reshape(-1, 3)
        nor = np.asarray(pr["normals"], dtype="<f4").reshape(-1, 3)
        attrs = {}
        attrs["POSITION"] = add_acc(add_view(pos.tobytes()), 5126, len(pos), "VEC3",
                                    pos.min(axis=0).tolist(), pos.max(axis=0).tolist())
        attrs["NORMAL"] = add_acc(add_view(nor.tobytes()), 5126, len(nor), "VEC3")
        if pr.get("uvs") is not None:
            uv = np.asarray(pr["uvs"], dtype="<f4").reshape(-1, 2)
            attrs["TEXCOORD_0"] = add_acc(add_view(uv.tobytes()), 5126, len(uv), "VEC2")
        if pr.get("colors") is not None:
            col = np.asarray(pr["colors"], dtype=np.uint8).reshape(-1, 4)
            attrs["COLOR_0"] = add_acc(add_view(col.tobytes()), 5121,
                                       len(col), "VEC4", normalized=True)
        if pr.get("batchids") is not None:
            bid = np.asarray(pr["batchids"], dtype="<f4")
            attrs["_BATCHID"] = add_acc(add_view(bid.tobytes()), 5126,
                                        len(bid), "SCALAR")
        prims_json.append({"attributes": attrs, "material": pr["material"]})

    json_data = {
        "asset": {"version": "2.0", "generator": "gis-data-download/1.1"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": prims_json}],
        "materials": materials,
        "buffers": [{"byteLength": offset}],
        "bufferViews": views,
        "accessors": accs,
    }
    if texture_png is not None:
        iv = add_view(texture_png, target=None)
        json_data["images"] = [{"bufferView": iv, "mimeType": "image/png"}]
        json_data["samplers"] = [{"wrapS": 10497, "wrapT": 10497,
                                  "magFilter": 9729, "minFilter": 9729}]
        json_data["textures"] = [{"sampler": 0, "source": 0}]
    # 图片 view 在上面才追加, buffer 总长必须最后再写
    json_data["buffers"][0]["byteLength"] = offset

    js = json.dumps(json_data, separators=(",", ":")).encode()
    js_padded = js + b" " * ((4 - len(js) % 4) % 4)
    bin_data = b"".join(bin_parts)
    bin_padded = bin_data + b"\0" * ((4 - len(bin_data) % 4) % 4)

    total = 12 + 8 + len(js_padded) + 8 + len(bin_padded)
    glb = b"glTF" + struct.pack("<II", 2, total)
    glb += struct.pack("<I", len(js_padded)) + b"JSON" + js_padded
    glb += struct.pack("<I", len(bin_padded)) + b"BIN\0" + bin_padded
    return glb


def wrap_b3dm(glb, batch_table=None, batch_length=0):
    ft_json = json.dumps({"BATCH_LENGTH": batch_length}, separators=(",", ":")).encode()
    ft_json += b" " * ((8 - len(ft_json) % 8) % 8)
    bt_json = b""
    if batch_table:
        bt_json = json.dumps(batch_table, ensure_ascii=False,
                             separators=(",", ":")).encode()
        bt_json += b" " * ((8 - len(bt_json) % 8) % 8)
    total = 28 + len(ft_json) + len(bt_json) + len(glb)
    # 28 字节头: magic + version + byteLength + ftJSONlen + ftBINlen + btJSONlen + btBINlen
    return (b"b3dm" + struct.pack("<IIIIII", 1, total, len(ft_json), 0, len(bt_json), 0)
            + ft_json + bt_json + glb)


MODULE_M = 3.0  # 纹理模块对应实地 3m (窗宽/层高)


_BT_KEYS = ("osm_id", "name", "building", "height_m", "top_m",
            "levels", "amenity", "addr", "area_m2_est", "height_source")
_MATS = [
    {"pbrMetallicRoughness": {           # 0 立面 (纹理)
        "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
        "baseColorTexture": {"index": 0},
        "metallicFactor": 0.0, "roughnessFactor": 0.95},
     "doubleSided": False},
    {"pbrMetallicRoughness": {           # 1 屋顶 (素色)
        "baseColorFactor": [0.545, 0.573, 0.596, 1.0],
        "metallicFactor": 0.0, "roughnessFactor": 1.0},
     "doubleSided": False},
]


def _building_prims(feats, lon0, lat0, rnd):
    """建筑挤出几何: 墙面(立面纹理 UV + 类型调色) / 封闭屋顶 / 女儿墙 / 屋顶设备盒。

    单 tile 与分块两条路径共用。返回 (prims, batch_table, bounds, skipped);
    bounds=(min_e, min_n, max_e, max_n, min_u, max_u) 为 ENU 局部米,
    batch_table 键序 = _BT_KEYS, 与 batchId 0..N-1 对齐; 无可挤出建筑时 prims=None。
    """
    wall_pos, wall_nor, wall_uv, wall_col, wall_bids = [], [], [], [], []
    roof_pos, roof_nor, roof_col, roof_bids = [], [], [], []
    max_e = max_n = max_u = -1e18
    min_e = min_n = min_u = 1e18
    skipped = 0
    next_bid = 0
    picked_props = []

    def push(target_pos, target_nor, v1, v2, v3):
        target_pos.extend([v1, v2, v3])
        ax, ay, az = v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2]
        bx, by, bz = v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2]
        nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        target_nor.extend([(nx / ln, ny / ln, nz / ln)] * 3)

    def gl(v):  # ENU(x=e, y=n, z=up) → glTF(x=e, y=up, z=-n)
        return (v[0], v[2], -v[1])

    for f in feats:
        p = f["properties"]
        base, top = p["base_m"], p["top_m"]
        if top - base < 1.0:
            top = base + 1.0
        ring_ll = f["geometry"]["coordinates"][0]
        if len(ring_ll) < 4:
            skipped += 1
            continue
        ring2d = [project_enu(x, y, lon0, lat0) for x, y in ring_ll[:-1]]
        tris = triangulate_ring(ring2d)
        if not tris:
            skipped += 1
            continue
        ring3d = [(e, n, base) for e, n in ring2d]
        ring_top = [(e, n, top) for e, n in ring2d]
        tint = _bldg_tint((p.get("building") or "yes").lower(), rnd)
        h_m = top - base
        phase = rnd.choice((0.0, 0.25, 0.5, 0.75))
        bid = next_bid
        next_bid += 1
        picked_props.append(p)

        # 墙面 (带 UV + COLOR_0; 环逆时针, 边朝外法线, gl 映射后朝外)
        cum = 0.0
        m = len(ring3d)
        for i in range(m):
            v1, v2 = ring3d[i], ring3d[(i + 1) % m]
            v1t, v2t = ring_top[i], ring_top[(i + 1) % m]
            edge = math.hypot(v2[0] - v1[0], v2[1] - v1[1])
            u0, u1 = cum / MODULE_M, (cum + edge) / MODULE_M
            cum += edge
            v_b, v_t = phase, phase + h_m / MODULE_M
            uv_a, uv_b, uv_c, uv_d = (u0, v_b), (u1, v_b), (u0, v_t), (u1, v_t)
            push(wall_pos, wall_nor, gl(v1), gl(v2), gl(v1t))
            wall_uv.extend([uv_a, uv_b, uv_c])
            push(wall_pos, wall_nor, gl(v2), gl(v2t), gl(v1t))
            wall_uv.extend([uv_b, uv_d, uv_c])
            wall_col.extend([tint] * 6)
            wall_bids.extend([bid] * 6)

        # 屋顶 (环已在 2D 归一化为逆时针; gl 映射 z=-n 后 (a,b,c) 顺序法线朝 +Y)
        for a, b, c in tris:
            push(roof_pos, roof_nor, gl(ring_top[a]), gl(ring_top[b]), gl(ring_top[c]))
            roof_col.extend([tint] * 3)
            roof_bids.extend([bid] * 3)

        # 女儿墙: 檐口再起 0.7m, 顶盖内收 0.8m (小建筑跳过)
        cx = sum(e for e, _ in ring2d) / len(ring2d)
        cy = sum(n for _, n in ring2d) / len(ring2d)
        dmin = min(math.hypot(cx - e, cy - n) for e, n in ring2d)
        if dmin > 2.5:
            PARA_H, INSET = 0.7, 0.8
            ptop = top + PARA_H
            ring_p = [(e, n, ptop) for e, n in ring2d]
            inner2d = []
            for e, n in ring2d:
                d = math.hypot(cx - e, cy - n) or 1.0
                k = max(0.0, (d - INSET) / d)
                inner2d.append((cx + (e - cx) * k, cy + (n - cy) * k))
            ring_i = [(e, n, ptop) for e, n in inner2d]
            for i in range(m):
                o1, o2 = ring_p[i], ring_p[(i + 1) % m]
                i1, i2 = ring_i[i], ring_i[(i + 1) % m]
                # 外立面 (同墙面绕序)
                push(roof_pos, roof_nor, gl(ring_top[i]), gl(ring_top[(i + 1) % m]), gl(o1))
                push(roof_pos, roof_nor, gl(ring_top[(i + 1) % m]), gl(o2), gl(o1))
                # 顶盖 (外→外→内 CCW)
                push(roof_pos, roof_nor, gl(o1), gl(o2), gl(i2))
                push(roof_pos, roof_nor, gl(o1), gl(i2), gl(i1))
                roof_col.extend([tint] * 12)
                roof_bids.extend([bid] * 12)

        # 屋顶设备盒: 大屋顶随机放 1-4 个 (水箱/机房/空调组)
        area_est = p.get("area_m2_est") or 0
        if area_est > 800:
            es = [v[0] for v in ring2d], [v[1] for v in ring2d]
            e0, e1, n0, n1 = min(es[0]), max(es[0]), min(es[1]), max(es[1])
            for _ in range(min(4, max(1, int(area_est // 900)))):
                bw, bl = rnd.uniform(3, 7), rnd.uniform(3, 7)
                if (e1 - e0) < bw * 2.4 or (n1 - n0) < bl * 2.4:
                    continue
                bx0 = rnd.uniform(e0 + bw, e1 - bw * 2)
                by0 = rnd.uniform(n0 + bl, n1 - bl * 2)
                bh = rnd.uniform(1.8, 3.2)
                bcorner = [(bx0, by0), (bx0 + bw, by0), (bx0 + bw, by0 + bl), (bx0, by0 + bl)]
                bb = [(e, n, top) for e, n in bcorner]
                bt = [(e, n, top + bh) for e, n in bcorner]
                for i in range(4):  # 四面
                    j = (i + 1) % 4
                    push(roof_pos, roof_nor, gl(bb[i]), gl(bb[j]), gl(bt[i]))
                    push(roof_pos, roof_nor, gl(bb[j]), gl(bt[j]), gl(bt[i]))
                    roof_col.extend([tint] * 6)
                    roof_bids.extend([bid] * 6)
                for a, b, c in ((0, 1, 2), (0, 2, 3)):  # 顶盖
                    push(roof_pos, roof_nor, gl(bt[a]), gl(bt[b]), gl(bt[c]))
                    roof_col.extend([tint] * 3)
                    roof_bids.extend([bid] * 3)

        for v in ring3d:
            max_e, max_n = max(max_e, v[0]), max(max_n, v[1])
            min_e, min_n = min(min_e, v[0]), min(min_n, v[1])
            max_u, min_u = max(max_u, top), min(min_u, base)

    if not wall_pos:
        return None, None, None, skipped
    prims = [
        {"positions": wall_pos, "normals": wall_nor, "uvs": wall_uv,
         "colors": wall_col, "batchids": wall_bids, "material": 0},
        {"positions": roof_pos, "normals": roof_nor, "colors": roof_col,
         "batchids": roof_bids, "material": 1},
    ]
    batch_table = {k: [pp.get(k, "") for pp in picked_props] for k in _BT_KEYS}
    bounds = (min_e, min_n, max_e, max_n, min_u, max_u)
    return prims, batch_table, bounds, skipped


def _write_tile_b3dm(prims, batch_table, path, tex):
    glb = build_glb(prims, _MATS, texture_png=tex)
    with open(path, "wb") as fh:
        fh.write(wrap_b3dm(glb, batch_table=batch_table,
                           batch_length=len(batch_table["osm_id"])))


def _enu_box(bounds):
    """bounds → glTF y-up 局部包围盒 (x=e, y=up, z=-n), 外扩 20m。"""
    min_e, min_n, max_e, max_n, min_u, max_u = bounds
    pad = 20.0
    return [(min_e + max_e) / 2, (min_u + max_u) / 2, -(min_n + max_n) / 2,
            (max_e - min_e) / 2 + pad, 0, 0,
            0, (max_u - min_u) / 2 + pad, 0,
            0, 0, (max_n - min_n) / 2 + pad]


def write_3dtiles(feats, lon0, lat0, out_dir, dem_info, tile_threshold=1500):
    """建筑挤出为 3D Tiles (tileset.json + b3dm), 坐标系 ENU(z-up)。

    ≤ tile_threshold 栋 → 单 tile scene.b3dm; 更大场景按质心均匀网格分块
    (tiles/cell_XX_YY.b3dm, 约 350 栋/块, 网格 ≤16×16), 避免单个 b3dm 过大
    无法流式加载。双材质: 墙面 = 程序化立面纹理 (REPEAT 按层高循环,
    COLOR_0 按建筑类型调色), 屋顶 = 素色深灰; 逐栋 batchId + batch table。
    """
    import random as _random
    os.makedirs(out_dir, exist_ok=True)
    tex = make_facade_png()
    cand = [f for f in feats if len(f["geometry"]["coordinates"][0]) >= 4]

    if len(cand) <= tile_threshold:            # ---- 单 tile (默认路径)
        rnd = _random.Random(20261104)
        prims, batch_table, bounds, skipped = _building_prims(cand, lon0, lat0, rnd)
        if prims is None:
            log("[-] 3D Tiles: 没有可挤出的建筑")
            return None, {"error": "no buildings"}
        b3dm_path = os.path.join(out_dir, "scene.b3dm")
        _write_tile_b3dm(prims, batch_table, b3dm_path, tex)
        tileset = {
            "asset": {"version": "1.0", "generator": "gis-data-download/1.2"},
            "geometricError": 1000,
            "root": {
                "transform": enu_to_ecef_matrix(lon0, lat0, 0.0),
                "boundingVolume": {"box": _enu_box(bounds)},
                "geometricError": 0,
                "refine": "REPLACE",
                "content": {"uri": "scene.b3dm"},
            },
        }
        ts_path = os.path.join(out_dir, "tileset.json")
        with open(ts_path, "w", encoding="utf-8") as fh:
            json.dump(tileset, fh, indent=2)
        size_mb = os.path.getsize(b3dm_path) / 1048576
        log(f"[✓] 3D Tiles: {ts_path}  (单 tile, b3dm {size_mb:.1f} MB, "
            f"{len(feats) - skipped} 栋, 跳过 {skipped})")
        info = {"buildings": len(feats) - skipped, "skipped": skipped,
                "b3dm_mb": round(size_mb, 2), "tiles": 1,
                "format": "3D Tiles 1.0 / b3dm, ENU 局部坐标系",
                "dem_base": bool(dem_info)}
        return ts_path, info

    # ---- 分块路径: 质心均匀网格, 每格独立 b3dm + batch table (batchId 从 0 重排)
    cents = []
    for f in cand:
        ring = f["geometry"]["coordinates"][0][:-1] or f["geometry"]["coordinates"][0]
        es = ns = 0.0
        for x, y in ring:
            ee, nn = project_enu(x, y, lon0, lat0)
            es += ee
            ns += nn
        cents.append((es / len(ring), ns / len(ring)))
    nb = len(cand)
    grid = max(1, min(16, math.ceil(math.sqrt(nb / 350.0))))
    min_ce = min(c[0] for c in cents)
    max_ce = max(c[0] for c in cents)
    min_cn = min(c[1] for c in cents)
    max_cn = max(c[1] for c in cents)
    we = max((max_ce - min_ce) / grid, 1.0)
    wn = max((max_cn - min_cn) / grid, 1.0)
    cells = {}
    for f, (ce, cn) in zip(cand, cents):
        ix = min(grid - 1, int((ce - min_ce) / we))
        iy = min(grid - 1, int((cn - min_cn) / wn))
        cells.setdefault((ix, iy), []).append(f)
    tdir = os.path.join(out_dir, "tiles")
    os.makedirs(tdir, exist_ok=True)
    children = []
    u_box = None
    total_mb = 0.0
    total_skipped = 0
    for ix, iy in sorted(cells):
        rnd = _random.Random(20261104 + ix * 131 + iy)
        prims, bt, bounds, sk = _building_prims(cells[(ix, iy)], lon0, lat0, rnd)
        total_skipped += sk
        if prims is None:
            continue
        name = f"cell_{ix:02d}_{iy:02d}.b3dm"
        _write_tile_b3dm(prims, bt, os.path.join(tdir, name), tex)
        total_mb += os.path.getsize(os.path.join(tdir, name)) / 1048576
        cbox = _enu_box(bounds)
        children.append({"boundingVolume": {"box": cbox},
                         "geometricError": 0,
                         "content": {"uri": f"tiles/{name}"}})
        e0, e1 = cbox[0] - cbox[3], cbox[0] + cbox[3]
        uu0, uu1 = cbox[1] - cbox[7], cbox[1] + cbox[7]
        n0, n1 = -cbox[2] - cbox[11], -cbox[2] + cbox[11]
        u_box = (e0, n0, e1, n1, uu0, uu1) if u_box is None else (
            min(u_box[0], e0), min(u_box[1], n0), max(u_box[2], e1),
            max(u_box[3], n1), min(u_box[4], uu0), max(u_box[5], uu1))
    if u_box is None:
        log("[-] 3D Tiles: 没有可挤出的建筑")
        return None, {"error": "no buildings"}
    span = max(max_ce - min_ce, max_cn - min_cn)
    tileset = {
        "asset": {"version": "1.0", "generator": "gis-data-download/1.2"},
        "geometricError": span * 4 + 100,
        "root": {
            "transform": enu_to_ecef_matrix(lon0, lat0, 0.0),
            "boundingVolume": {"box": _enu_box(u_box)},
            "refine": "REPLACE",
            "geometricError": span / 2 + 50,
            "children": children,
        },
    }
    ts_path = os.path.join(out_dir, "tileset.json")
    with open(ts_path, "w", encoding="utf-8") as fh:
        json.dump(tileset, fh, indent=2)
    log(f"[✓] 3D Tiles: {ts_path}  (分块 {len(children)} 块 / 网格 {grid}×{grid}, "
        f"b3dm 合计 {total_mb:.1f} MB, {nb - total_skipped} 栋, 跳过 {total_skipped})")
    info = {"buildings": nb - total_skipped, "skipped": total_skipped,
            "b3dm_mb": round(total_mb, 2), "tiles": len(children),
            "format": "3D Tiles 1.0 / b3dm 分块 (网格入口)",
            "dem_base": bool(dem_info)}
    return ts_path, info


# ---------------------------------------------------------- CNBH-10m 高度注入

CNBH_RECORD = "https://zenodo.org/api/records/7923866"  # Wu et al. 2023, RSE


def cnbh_tile_index(cache):
    """CNBH-10m (Zenodo 7923866) tif 文件清单 → {文件名: 下载URL}, 本地缓存。"""
    idx_path = os.path.join(cache, "record_7923866.json")
    if os.path.isfile(idx_path):
        with open(idx_path, encoding="utf-8") as fh:
            return json.load(fh)
    import requests
    r = requests.get(CNBH_RECORD, timeout=60)
    r.raise_for_status()
    idx = {f["key"]: f["links"]["self"]
           for f in r.json().get("files", []) if f["key"].endswith(".tif")}
    if not idx:
        raise RuntimeError("Zenodo 清单里没有 tif 文件")
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump(idx, fh)
    return idx


def _download_cnbh_tile(url, path):
    import requests
    tmp = path + ".part"
    with requests.get(url, stream=True, timeout=600, allow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    os.replace(tmp, path)


def inject_cnbh_heights(feats, bbox):
    """CNBH-10m 建筑高度注入 (10m 栅格逐栋质心采样, MODELED 估算非测绘)。

    有 OSM height 标签的栋保留标签 (更可信); 其余用 10m 像素值 (0.1~400m)
    覆盖 height_m/top_m 并记 height_source='cnbh10m'。瓦片缓存在
    ~/.cache/cnbh10m (10-50MB/度格)。返回注入栋数; 下载/清单失败返回 0 不抛,
    建筑保留 POLICY 估算。
    """
    w, s, e, n = bbox
    cache = os.path.expanduser("~/.cache/cnbh10m")
    os.makedirs(cache, exist_ok=True)
    try:
        idx = cnbh_tile_index(cache)
        import rasterio
    except Exception as exc:
        log(f"[!] CNBH-10m 不可用 ({exc}), 建筑保留 POLICY 估算")
        return 0
    cands = {f"CNBH10m_X{x}Y{y}.tif" if f"CNBH10m_X{x}Y{y}.tif" in idx
             else f"CNBH10m_X{x:03d}Y{y:02d}.tif"
             for x in {int(math.floor(v)) for v in (w, e)}
             for y in {int(math.floor(v)) for v in (s, n)}}
    srcs = []
    for name in sorted(cands):
        if name not in idx:
            log(f"[!] CNBH-10m 无瓦片 {name}, 该区域保留 POLICY 估算")
            continue
        tpath = os.path.join(cache, name)
        if not os.path.isfile(tpath):
            log(f"[*] CNBH-10m: 下载 {name} (约 10-50MB, 缓存 {cache}) ...")
            try:
                _download_cnbh_tile(idx[name], tpath)
            except Exception as exc:
                log(f"[!] {name} 下载失败 ({exc}), 保留 POLICY 估算")
                continue
        srcs.append(rasterio.open(tpath))
    if not srcs:
        return 0
    injected = 0
    remaining = []
    for i, f in enumerate(feats):
        p = f["properties"]
        if "osm" in (p.get("height_source") or ""):
            continue                     # OSM height 标签优先, 不覆盖
        ring = f["geometry"]["coordinates"][0]
        pts = ring[:-1] if len(ring) > 4 else ring
        remaining.append((i, sum(v[0] for v in pts) / len(pts),
                          sum(v[1] for v in pts) / len(pts)))
    from rasterio.warp import transform as _warp4326
    for src in srcs:                     # 瓦片多为 UTM 投影, 需把质心转到瓦片 CRS 采样
        if not remaining:
            break
        b = src.bounds
        xs, ys = _warp4326("EPSG:4326", src.crs,
                           [c[1] for c in remaining], [c[2] for c in remaining])
        todo = [k for k, (x, y) in enumerate(zip(xs, ys))
                if b.left <= x <= b.right and b.bottom <= y <= b.top]
        if not todo:
            continue
        samples = src.sample([(xs[k], ys[k]) for k in todo])
        done = set()
        for k, v in zip(todo, samples):
            v = float(v[0])
            if src.nodata is not None and v == src.nodata:
                v = 0.0
            if 0.1 <= v <= 400:          # 合理建筑高度范围, 过滤 nodata/噪声
                p = feats[remaining[k][0]]["properties"]
                p["height_m"] = round(v, 1)
                p["top_m"] = round(p.get("base_m", 0.0) + v, 1)
                p["height_source"] = "cnbh10m"
                injected += 1
                done.add(k)
        remaining = [c for k, c in enumerate(remaining) if k not in done]
    for src in srcs:
        src.close()
    log(f"[*] CNBH-10m 高度注入: {injected}/{len(feats)} 栋 (其余保留 POLICY 估算)")
    return injected


# ---------------------------------------------------------------- 5. 预览页

PREVIEW_TPL = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__NAME__ — 场景基础数据预览</title>
<script>window.CESIUM_BASE_URL="https://cdn.jsdelivr.net/npm/cesium@1.142.0/Build/Cesium/";</script>
<script src="https://cdn.jsdelivr.net/npm/cesium@1.142.0/Build/Cesium/Cesium.js"></script>
<link href="https://cdn.jsdelivr.net/npm/cesium@1.142.0/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
<style>
  html,body,#cesium{width:100%;height:100%;margin:0;padding:0;overflow:hidden;background:#000;}
  #panel{position:absolute;top:12px;left:12px;z-index:9;background:rgba(20,30,40,.88);color:#eee;
         padding:14px 16px;border-radius:10px;font:13px/1.7 system-ui,sans-serif;min-width:230px;}
  #panel h3{margin:0 0 8px;font-size:14px;}
  #panel label{display:block;cursor:pointer;}
  #panel .presets{margin:6px 0 2px;display:flex;gap:6px;}
  #panel .presets button{flex:1;background:#2c4a66;color:#dfeaf3;border:0;border-radius:6px;
         padding:3px 0;font-size:12px;cursor:pointer;}
  #panel .presets button:hover{background:#3a6288;}
  #panel #layers label{margin-top:2px;}
  #panel .meta{color:#9ab;font-size:12px;margin-top:8px;border-top:1px solid #345;padding-top:8px;}
  #status{color:#fa0;}
  #panel input[type=text]{width:100%;box-sizing:border-box;background:#22323f;color:#eee;
        border:1px solid #345;border-radius:6px;padding:3px 8px;font-size:12px;outline:none;}
  #panel input[type=text]:focus{border-color:#5c96ff;}
  #panel #qlistBox{max-height:132px;overflow:auto;font-size:12px;display:none;}
  #panel #qlistBox div{padding:2px 6px;cursor:pointer;border-radius:4px;}
  #panel #qlistBox div:hover{background:#2c4a66;}
  #panel .sunrow{margin-top:6px;font-size:12px;color:#9ab;display:flex;align-items:center;gap:6px;}
  #panel .sunrow input[type=range]{flex:1;accent-color:#f7b731;}
  #panel #summary{margin-top:4px;}
  #pick{position:absolute;left:12px;bottom:12px;z-index:9;background:rgba(255,255,255,.94);
        color:#1b2733;padding:12px 14px;border-radius:10px;font:13px/1.6 system-ui,sans-serif;
        width:212px;box-shadow:0 2px 14px rgba(0,0,0,.35);}
  #pick h4{margin:0 0 6px;font-size:15px;border-bottom:1px solid rgba(0,0,0,.08);padding-bottom:5px;}
  #pick .row{display:flex;justify-content:space-between;gap:8px;}
  #pick .row span{color:#5a6a78;white-space:nowrap;}
  #pick .row b{font-weight:600;text-align:right;word-break:break-all;}
  #pick .tip{color:#8a97a3;font-size:11px;margin-top:6px;}
  #pick #pkMore{color:#2c5fa8;cursor:pointer;font-size:12px;margin-top:6px;user-select:none;}
  #pick #pkAll{display:none;font-size:11.5px;color:#41525f;margin-top:4px;max-height:180px;overflow:auto;
        border-top:1px dashed #ccd4da;padding-top:4px;}
  #pick #pkAll div{display:flex;justify-content:space-between;gap:6px;padding:1px 0;}
</style>
</head>
<body>
<div id="cesium"></div>
<div id="pick" hidden>
  <h4 id="pkName"></h4>
  <div id="pkRows"></div>
  <div id="pkMore">全部属性 ▾</div>
  <div id="pkAll"></div>
  <div class="tip">单击选择 / 空白处取消 / Esc 取消</div>
</div>
<div id="panel">
  <h3>__NAME__</h3>
  <input type="text" id="q" placeholder="搜索建筑名称 (Enter 选中并飞过去)…" autocomplete="off">
  <div id="qlistBox"></div>
  <label style="margin-top:6px"><input type="checkbox" id="ckTiles" checked> 建筑·纹理白模（3D Tiles 离线包）</label>
  <label><input type="checkbox" id="ckGeo"> 白模·按高度配色（GeoJSON）</label>
  <label><input type="checkbox" id="ckAbs"> 绝对高程模式（接真实地形时勾选；默认贴地预览）</label>
  <div class="presets">
    <button data-p="tex" title="只看纹理 3D Tiles">纹理模式</button><button data-p="heat" title="只看按高度配色的 GeoJSON 白模">高度分析</button><button data-p="all" title="两层叠加">全部显示</button>
  </div>
  <div class="sunrow">☀ <input type="range" id="sun" min="5" max="20" step="0.25"><span id="sunV" style="min-width:52px"></span></div>
  <div id="layers"></div>
  <div class="meta">
    中心: __LON__, __LAT__<br>
    影像: ESRI World Imagery 在线瓦片<br>
    建筑: <span id="cnt">…</span> 栋（OSM 轮廓, 高度估算见信息卡）<br>
    高程底座: Copernicus GLO-30<br>
    <div id="summary"></div>
    <span id="status">加载中…</span>
  </div>
</div>
<script>
const LON=__LON__, LAT=__LAT__, RADIUS=__RADIUS__;
const LON0=__LON__, LAT0=__LAT__, MEDBASE=__MEDBASE__;
const EXTRAS_META = __EXTRAS__;   // {文件名: 要素数}
Cesium.Ion.defaultAccessToken = undefined;
const viewer = new Cesium.Viewer("cesium", {
  baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    maximumLevel: 18, credit: "Esri, Maxar"
  })),
  terrainProvider: new Cesium.EllipsoidTerrainProvider(),
  baseLayerPicker: false, geocoder: false, homeButton: false,
  sceneModePicker: false, navigationHelpButton: false,
  animation: false, timeline: false, fullscreenButton: false, infoBox: false,
  selectionIndicator: false
});

function rampColor(h){
  if(h<6)   return Cesium.Color.fromCssColorString("#dfe7ee");
  if(h<12)  return Cesium.Color.fromCssColorString("#c7d8a8");
  if(h<24)  return Cesium.Color.fromCssColorString("#e8b96a");
  return Cesium.Color.fromCssColorString("#d1706a");
}

// 贴地/绝对高程模式。base_m 是 DEM 绝对高程, 椭球地形(0m)下直接用会悬空,
// 贴地模式把底座归零; 3D Tiles 用 modelMatrix 沿 ECEF "向下"平移。
let geoSrc = null, tiles = null;
function applyMode(absolute){
  if (geoSrc) for (const ent of geoSrc.entities.values) {
    const p = ent.properties;
    const base = p.base_m ? p.base_m.getValue() : 0;
    const top  = p.top_m  ? p.top_m.getValue()  : base + 6;
    ent.polygon.height = absolute ? base : 0.15;          // +0.15m 防与 3D Tiles 重叠闪烁
    ent.polygon.extrudedHeight = absolute ? top : top - base;
  }
  if (tiles) {
    if (absolute) { tiles.modelMatrix = Cesium.Matrix4.IDENTITY; }
    else {
      const up = Cesium.Cartesian3.normalize(
        Cesium.Cartesian3.fromDegrees(LON0, LAT0), new Cesium.Cartesian3());
      tiles.modelMatrix = Cesium.Matrix4.fromTranslation(
        Cesium.Cartesian3.multiplyByScalar(up, -MEDBASE, new Cesium.Cartesian3()));
    }
  }
}

// 配套图层样式：线状图层用 corridor 真实米宽（道路按等级分色分宽；河流按级别米宽），面状用 GeoJSON
const ROAD_STYLE = {  // class → [米宽, 颜色, 透明度]
  motorway:  [26, "#f7b731", 0.6], trunk: [22, "#f7b731", 0.6],
  primary:   [17, "#ffffff", 0.75], secondary: [13, "#eef3f6", 0.75],
  tertiary:  [9, "#e8eef2", 0.8], residential: [6, "#cfd8dc", 0.8],
  unclassified: [5.5, "#cfd8dc", 0.8], service: [3.5, "#b8c2c8", 0.8],
  living_street: [4.5, "#c8d2d8", 0.8], pedestrian: [3.5, "#d8dee2", 0.8]
};
const WATER_W = { river: 22, canal: 16, stream: 5, ditch: 2.5 };  // 河流等级 → 米宽
function addCorridor(ds, coords, widthM, cssColor, alpha) {
  if (!coords || coords.length < 2) return;
  const pos = coords.map(c => Cesium.Cartesian3.fromDegrees(c[0], c[1]));
  ds.entities.add({ corridor: {
    positions: pos, width: widthM, cornerType: Cesium.CornerType.ROUNDED,
    heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
    material: Cesium.Color.fromCssColorString(cssColor).withAlpha(alpha) } });
}
async function loadCorridors(file, perFeature) {
  const gj = await (await fetch(file)).json();
  const ds = new Cesium.CustomDataSource(file);
  for (const f of gj.features) perFeature(ds, f);
  return ds;
}
function styleRoad(ds, f) {
  const cls = f.properties.class || "default";
  const st = ROAD_STYLE[cls] || [5.5, "#cfd8dc", 0.8];
  addCorridor(ds, f.geometry.coordinates, st[0], st[1], st[2]);
}
function styleRail(ds, f) {
  addCorridor(ds, f.geometry.coordinates, 4, "#9aa4b0", 0.85);
}
function styleWaterLine(ds, f) {
  const cls = f.properties.class || "stream";
  addCorridor(ds, f.geometry.coordinates, WATER_W[cls] || 8, "#3d9bff", 0.8);
}
async function loadWater(file) {
  const gj = await (await fetch(file)).json();
  const ds = new Cesium.CustomDataSource(file);
  for (const f of gj.features) {
    if (f.geometry.type === "LineString" || f.geometry.type === "MultiLineString") {
      const lines = f.geometry.type === "LineString" ? [f.geometry.coordinates] : f.geometry.coordinates;
      for (const line of lines) styleWaterLine(ds, { geometry: { coordinates: line }, properties: f.properties });
    } else {
      const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
      for (const poly of polys) {
        const outer = poly[0];   // 外环 (OSM 水面很少带洞, 忽略内环)
        if (!outer || outer.length < 3) continue;
        ds.entities.add({ polygon: {
          hierarchy: Cesium.Cartesian3.fromDegreesArray(outer.flat()),
          height: 0.08,
          material: Cesium.Color.fromCssColorString("#3d9bff").withAlpha(0.85) } });
      }
    }
  }
  return ds;
}
async function loadGreen(file) {
  const gj = await (await fetch(file)).json();
  const ds = new Cesium.CustomDataSource(file);
  for (const f of gj.features) {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const poly of polys) {
      const outer = poly[0];
      if (!outer || outer.length < 3) continue;
      ds.entities.add({ polygon: {
        hierarchy: Cesium.Cartesian3.fromDegreesArray(outer.flat()),
        height: 0.06,
        material: Cesium.Color.fromCssColorString("#69a854").withAlpha(0.45) } });
    }
  }
  return ds;
}

(async () => {
  const st = document.getElementById("status");
  try {
    let n = 0;
    try {
      geoSrc = await Cesium.GeoJsonDataSource.load("buildings.geojson");
      for (const ent of geoSrc.entities.values) {
        ent.polygon.material = rampColor(
          (ent.properties.top_m ? ent.properties.top_m.getValue() : 6) -
          (ent.properties.base_m ? ent.properties.base_m.getValue() : 0)).withAlpha(0.92);
        ent.polygon.outline = false;
        n++;
      }
      document.getElementById("cnt").textContent = n;
      geoSrc.show = false;   // 默认看纹理 3D Tiles, GeoJSON 白模作高度分析用
      viewer.dataSources.add(geoSrc);
    } catch(e) { st.textContent = "GeoJSON 加载失败: " + e; }

    try {
      tiles = await Cesium.Cesium3DTileset.fromUrl("buildings_3dtiles/tileset.json");
      viewer.scene.primitives.add(tiles);
    } catch(e) { st.textContent = "3D Tiles 加载失败: " + e; }

    // OSM 配套图层（存在才加载，动态生成开关 + 要素数；线状 = corridor 真实米宽）
    const EXTRAS = [
      { file: "roads.geojson",    label: "道路路网", load: () => loadCorridors("roads.geojson", styleRoad) },
      { file: "railways.geojson", label: "铁路轨道", load: () => loadCorridors("railways.geojson", styleRail) },
      { file: "water.geojson",    label: "水系",     load: () => loadWater("water.geojson") },
      { file: "green.geojson",    label: "绿地",     load: () => loadGreen("green.geojson") },
    ];
    const layerSrc = {};
    for (const L of EXTRAS) {
      try {
        const ds = await L.load();
        viewer.dataSources.add(ds);
        layerSrc[L.file] = ds;
        const cnt = EXTRAS_META[L.file] != null ? ` (${EXTRAS_META[L.file]})` : "";
        const ck = document.createElement("label");
        ck.innerHTML = `<input type="checkbox" checked data-f="${L.file}"> ${L.label}${cnt}`;
        document.getElementById("layers").appendChild(ck);
      } catch(e) { /* 图层文件不存在则静默跳过 */ }
    }
    document.getElementById("layers").onchange = e => {
      const f = e.target.dataset.f;
      if (f && layerSrc[f]) layerSrc[f].show = e.target.checked;
    };

    // 日照时间滑块 (拖动改太阳方位, 建筑光影实时变化; 默认上午 10:30 利演示)
    viewer.scene.globe.enableLighting = true;
    const sunSlider = document.getElementById("sun");
    sunSlider.value = 10.5;
    function setSun(h) {
      const d = new Date();
      viewer.clock.currentTime = Cesium.JulianDate.fromDate(
        new Date(d.getFullYear(), d.getMonth(), d.getDate(), Math.floor(h), Math.round((h % 1) * 60)));
      viewer.clock.shouldAnimate = false;
      document.getElementById("sunV").textContent = String(Math.floor(h)).padStart(2, "0") + ":" + String(Math.round((h % 1) * 60)).padStart(2, "0");
    }
    sunSlider.oninput = e => setSun(parseFloat(e.target.value));
    setSun(parseFloat(sunSlider.value));

    // 预设模式: 纹理 / 高度分析 / 全部
    document.querySelectorAll("#panel .presets button").forEach(btn => {
      btn.onclick = () => {
        const p = btn.dataset.p;
        document.getElementById("ckTiles").checked = (p !== "heat");
        document.getElementById("ckGeo").checked = (p !== "tex");
        if (tiles) tiles.show = (p !== "heat");
        if (geoSrc) geoSrc.show = (p !== "tex");
      };
    });

    // —— 高亮拾取 (对标 map3d: 悬停浅高亮 + 单击选中 + 信息卡) ——
    const pickEl = document.getElementById("pick");
    const HOVER_C = Cesium.Color.fromCssColorString("#a9ccff");
    const SELECT_C = Cesium.Color.fromCssColorString("#5c96ff");
    let hoveredF = null, selectedF = null;
    const setClr = (f, c) => { if (f && f.color) f.color = c; };
    function showInfo(f) {
      const g = k => {
        const v = f.getProperty(k);
        return (v === undefined || v === null) ? "" : String(v);
      };
      document.getElementById("pkName").textContent = g("name") || "建筑信息";
      const rows = [];
      const add = (label, val) => {
        if (val) rows.push(`<div class="row"><span>${label}</span><b>${val}</b></div>`);
      };
      const b = g("building");
      if (b && b !== "yes") add("类型", b);
      const h = g("height_m");
      if (h) add("高度", `${h} m${g("height_source") && g("height_source").indexOf("osm") < 0 ? " (估算)" : ""}`);
      if (g("levels")) add("层数", g("levels"));
      if (g("amenity")) add("设施", g("amenity"));
      if (g("addr")) add("地址", g("addr"));
      if (g("area_m2_est")) add("占地", `${g("area_m2_est")} m²`);
      add("OSM ID", g("osm_id"));
      document.getElementById("pkRows").innerHTML = rows.join("");
      const all = ["name", "building", "height_m", "base_m", "top_m", "levels",
                   "amenity", "addr", "area_m2_est", "height_source", "osm_id"];
      document.getElementById("pkAll").innerHTML =
        all.map(k => `<div><span>${k}</span><b>${g(k) || "—"}</b></div>`).join("");
      document.getElementById("pkMore").textContent = "全部属性 ▾";
      pickEl.hidden = false;
    }
    document.getElementById("pkMore").onclick = () => {
      const el = document.getElementById("pkAll");
      const show = el.style.display !== "block";
      el.style.display = show ? "block" : "none";
      document.getElementById("pkMore").textContent = show ? "收起 ▴" : "全部属性 ▾";
    };

    // 建筑名称搜索 → 选中并飞过去 (从 GeoJSON 取质心)
    let namedBldgs = [];
    try {
      const gj = await (await fetch("buildings.geojson")).json();
      namedBldgs = gj.features.filter(f => f.properties.name).map(f => {
        const pts = f.geometry.coordinates[0];
        let sx = 0, sy = 0;
        for (const q of pts) { sx += q[0]; sy += q[1]; }
        return { name: f.properties.name, height: f.properties.height_m,
                 top: f.properties.top_m, base: f.properties.base_m,
                 lon: sx / pts.length, lat: sy / pts.length, props: f.properties };
      });
      const box = document.getElementById("qlistBox");
      const q = document.getElementById("q");
      q.oninput = () => {
        const kw = q.value.trim().toLowerCase();
        box.style.display = kw ? "block" : "none";
        box.innerHTML = namedBldgs.filter(b => b.name.toLowerCase().includes(kw))
          .slice(0, 8).map((b, i) => `<div data-i="${namedBldgs.indexOf(b)}">${b.name} <span style="color:#7a8a98">${b.height}m</span></div>`).join("");
      };
      box.onclick = async e => {
        const d = e.target.closest("div[data-i]");
        if (!d) return;
        box.style.display = "none";
        q.value = "";
        const b = namedBldgs[+d.dataset.i];
        await viewer.camera.flyTo({
          destination: Cesium.Cartesian3.fromDegrees(b.lon, b.lat - 0.0004, (b.top - (b.base || 0)) * 4 + 160),
          orientation: { heading: 0, pitch: Cesium.Math.toRadians(-40), roll: 0 },
          duration: 1.8 });
        // 飞完在其顶部中心投影拾取, 复用选中高亮+信息卡
        const carto = Cesium.Cartesian3.fromDegrees(b.lon, b.lat, (b.top || 10) + 1);
        await new Promise(r2 => setTimeout(r2, 400));
        const win = Cesium.SceneTransforms.worldToWindowCoordinates(viewer.scene, carto);
        if (win) {
          const pk = viewer.scene.pick(win);
          if (pk instanceof Cesium.Cesium3DTileFeature) {
            if (selectedF) setClr(selectedF, Cesium.Color.WHITE);
            selectedF = pk;
            setClr(selectedF, SELECT_C);
            showInfo(pk);
            return;
          }
        }
        // 拾取失败也直接显示属性卡
        const proxy = { getProperty: k => b.props[k] };
        if (selectedF) setClr(selectedF, Cesium.Color.WHITE);
        selectedF = null;
        showInfo(proxy);
      };
    } catch(e) { /* buildings.geojson 缺失则无搜索 */ }

    // 数据完整度概况 (读 download_report.json)
    try {
      const rep = await (await fetch("download_report.json")).json();
      const o = rep.outputs || {};
      const parts = [];
      const img = o.imagery || {};
      if (img.zoom) parts.push(`影像 z${img.zoom}${img.failed_tiles ? ` (失败 ${img.failed_tiles})` : ""}`);
      const dem = o.dem || {};
      if (dem.elev_min != null) parts.push(`DEM ${Math.round(dem.elev_min)}~${Math.round(dem.elev_max)}m`);
      const b = o.buildings || {};
      if (b.count) parts.push(`建筑 ${b.count}${b.height_cnbh != null ? ` (CNBH-10m 注入 ${b.height_cnbh})` : b.height_from_osm_tag != null ? ` (OSM标高 ${b.height_from_osm_tag})` : ""}`);
      const ly = b.layers || {};
      const lname = { roads: "路网", railways: "铁路", water: "水系", green: "绿地" };
      const lyStr = Object.entries(ly).map(([k, v]) => `${lname[k] || k} ${v}`).join(" · ");
      if (lyStr) parts.push(lyStr);
      document.getElementById("summary").innerHTML = parts.join("<br>") ||
        `<span style="color:#6a7a88">report 缺少数据概况</span>`;
    } catch(e) { /* report 缺失忽略 */ }

    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction(m => {
      const pk = viewer.scene.pick(m.position);
      if (selectedF) setClr(selectedF, Cesium.Color.WHITE);
      if (pk instanceof Cesium.Cesium3DTileFeature) {
        selectedF = pk;
        setClr(selectedF, SELECT_C);
        showInfo(pk);
      } else {
        selectedF = null;
        pickEl.hidden = true;
      }
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    handler.setInputAction(m => {
      if (hoveredF && hoveredF !== selectedF) setClr(hoveredF, Cesium.Color.WHITE);
      const pk = viewer.scene.pick(m.endPosition);
      hoveredF = (pk instanceof Cesium.Cesium3DTileFeature) ? pk : null;
      if (hoveredF && hoveredF !== selectedF) setClr(hoveredF, HOVER_C);
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
    window.addEventListener("keydown", e => {
      if (e.key === "Escape" && selectedF) {
        setClr(selectedF, Cesium.Color.WHITE);
        selectedF = null;
        pickEl.hidden = true;
      }
    });

    document.getElementById("ckGeo").onchange = e => { if(geoSrc) geoSrc.show = e.target.checked; };
    document.getElementById("ckTiles").onchange = e => { if(tiles) tiles.show = e.target.checked; };
    document.getElementById("ckAbs").onchange = e => applyMode(e.target.checked);
    applyMode(false);  // 默认贴地模式

    // 初始视角: 42° 斜视, 更有三维感 (比正俯视好看)
    const dLat = RADIUS/111320, dLon = RADIUS/(111320*Math.cos(LAT*Math.PI/180));
    await viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(LON, LAT - dLat*1.35, RADIUS*1.9),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(-42), roll: 0 },
      duration: 1.8
    });
    if (st.textContent.indexOf("加载") === 0) st.textContent = "就绪 ✓（在线影像需可访问 arcgisonline.com）";
  } catch(e) { st.textContent = "初始化失败: " + e; }
})();
</script>
</body>
</html>
"""

SERVE_SH = """#!/usr/bin/env bash
cd "$(dirname "$0")" && exec python3 -m http.server "${1:-8000}"
"""


def write_preview(out_dir, name, lon, lat, radius_m, median_base=0.0, extras=None):
    html = (PREVIEW_TPL.replace("__NAME__", name).replace("__LON__", repr(lon))
            .replace("__LAT__", repr(lat)).replace("__RADIUS__", repr(radius_m))
            .replace("__MEDBASE__", repr(round(float(median_base), 2)))
            .replace("__EXTRAS__", json.dumps(extras or {})))
    html_path = os.path.join(out_dir, "cesium_preview.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    serve_path = os.path.join(out_dir, "serve.sh")
    with open(serve_path, "w", encoding="utf-8") as f:
        f.write(SERVE_SH)
    os.chmod(serve_path, 0o755)
    log(f"[✓] 预览页: {html_path}  (运行 {serve_path} 后浏览器打开 http://localhost:8000/cesium_preview.html)")
    return html_path


# ---------------------------------------------------------------- 主流程

def main():
    global DEFAULT_HEIGHT
    ap = argparse.ArgumentParser(description="数字孪生场景基础数据一站式下载 (影像+高程+建筑+3D Tiles+预览)")
    ap.add_argument("--lon", type=float, default=None, help="中心经度 (WGS84); 与 --geojson 二选一")
    ap.add_argument("--lat", type=float, default=None, help="中心纬度 (WGS84); 与 --geojson 二选一")
    ap.add_argument("--geojson", type=str, default=None,
                    help="边界文件: GeoJSON 或 shapefile 压缩包(.zip/.shp, 需带 .prj), 按包围盒下载")
    ap.add_argument("--buffer", type=float, default=0.0, help="--geojson 时向外扩展的米数 (默认 0)")
    ap.add_argument("--radius", type=float, default=1500.0, help="半径米数 (默认 1500, 即 3km x 3km); --geojson 时忽略")
    ap.add_argument("--zoom", type=int, choices=[16, 17, 18, 19], default=18,
                    help="影像缩放级别 17=1.2m 18=0.6m 19=0.3m (分辨率均为估算)")
    ap.add_argument("--source", choices=["esri", "google"], default="esri",
                    help="影像源; google 在国内需代理")
    ap.add_argument("--name", type=str, default=None, help="场景名 (默认 scene_lon_lat)")
    ap.add_argument("--out", type=str, default=None, help="输出目录 (默认 ./<name>_scene)")
    ap.add_argument("--workers", type=int, default=12, help="影像下载并发数")
    ap.add_argument("--default-height", type=float, default=DEFAULT_HEIGHT,
                    help="无高度标签建筑的默认高度 (POLICY)")
    ap.add_argument("--skip-imagery", action="store_true")
    ap.add_argument("--skip-dem", action="store_true")
    ap.add_argument("--skip-buildings", action="store_true")
    ap.add_argument("--skip-3dtiles", action="store_true")
    ap.add_argument("--skip-preview", action="store_true")
    ap.add_argument("--skip-extras", action="store_true",
                    help="不提取道路/铁路/水系/绿地等 OSM 配套图层")
    ap.add_argument("--tile-threshold", type=int, default=1500,
                    help="建筑数超过该值时 3D Tiles 自动分块 (默认 1500)")
    ap.add_argument("--heights", choices=["cnbh"], default=None,
                    help="建筑高度注入: cnbh = CNBH-10m 栅格估算 (Zenodo 7923866, "
                         "需外网或代理, 瓦片缓存 ~/.cache/cnbh10m); OSM height 标签优先保留")
    ap.add_argument("--resume", action="store_true",
                    help="断点续传: 复用输出目录已有的影像/DEM/建筑/配套图层, 只重做 3D Tiles/预览/报告")
    args = ap.parse_args()

    DEFAULT_HEIGHT = args.default_height

    if args.geojson:
        w, s, e, n = load_bounds(args.geojson, buffer_m=args.buffer)
        lon0, lat0 = (w + e) / 2, (s + n) / 2
        ext_m = max((e - w) * 111320.0 * math.cos(math.radians(lat0)), (n - s) * 110540.0)
        name = args.name or (os.path.splitext(os.path.basename(args.geojson))[0] or "scene_geojson")
        src_desc = (f"GeoJSON 边界: {os.path.basename(args.geojson)}"
                    + (f" (外扩 {args.buffer:.0f}m)" if args.buffer else ""))
    else:
        if args.lon is None or args.lat is None:
            ap.error("必须提供 --lon/--lat 或 --geojson 之一")
        lon0, lat0 = args.lon, args.lat
        w, s, e, n = bbox_from_center(lon0, lat0, args.radius)
        ext_m = args.radius * 2
        name = args.name or f"scene_{lon0:.4f}_{lat0:.4f}"
        src_desc = (f"中心 ({lon0:.6f}, {lat0:.6f}), 半径 {args.radius}m "
                    f"(约 {args.radius*2/1000:.1f}km x {args.radius*2/1000:.1f}km)")

    out_dir = args.out or f"{name}_scene"
    os.makedirs(out_dir, exist_ok=True)
    started = datetime.datetime.now().isoformat(timespec="seconds")

    log("=" * 62)
    log(f"    数字孪生场景基础数据下载 — {name}")
    log("=" * 62)
    log(f"[*] {src_desc}")
    log(f"[*] 覆盖范围: {w:.5f},{s:.5f} ~ {e:.5f},{n:.5f} "
        f"(约 {(e-w)*111320*math.cos(math.radians(lat0))/1000:.1f}km x {(n-s)*110540/1000:.1f}km)")
    log(f"[*] 输出目录: {os.path.abspath(out_dir)}")

    # 范围合理性提示 (太大/太小), 写入 report["warnings"], 由 agent 向用户确认
    warnings = []
    tile_w_m = 156543.03 * math.cos(math.radians(lat0)) / (2 ** args.zoom) * 256  # 单瓦片地面宽(m)
    est_cols = max(1, math.ceil((e - w) * 111320.0 * math.cos(math.radians(lat0)) / tile_w_m))
    est_rows = max(1, math.ceil((n - s) * 110540.0 / tile_w_m))
    est_tiles = est_cols * est_rows
    size_km = ext_m / 1000.0
    if ext_m < 100:
        warnings.append(f"范围很小 (约 {ext_m:.0f}m): 瓦片极少, 若本意是下载周边区域, 请用 --buffer 外扩或改用 --lon/--lat --radius")
    if size_km > 12:
        warnings.append(f"范围过大 (约 {size_km:.1f}km): z{args.zoom} 预计 {est_tiles} 瓦片, 下载慢且易被限流; 强烈建议分块下载或降低 --zoom")
    elif size_km > 6:
        warnings.append(f"范围较大 (约 {size_km:.1f}km): z{args.zoom} 预计约 {est_tiles} 瓦片、GeoTIFF 可能几百 MB; 建议 --zoom 17, 或 --skip-buildings 减负")
    for msg in warnings:
        log(f"[!] {msg}")
    if warnings:
        log("[!] 请确认范围是否合适后继续 (Ctrl+C 可中止)")

    report = {"command": vars(args), "name": name,
              "center_wgs84": [round(lon0, 6), round(lat0, 6)],
              "extent_m": round(ext_m, 1),
              "bbox_wgs84": [round(v, 6) for v in (w, s, e, n)],
              "warnings": warnings,
              "started": started, "evidence_note":
              "影像/DEM 分辨率为数据源标称或 MODELED 估算; 建筑高度为 POLICY 估算; "
              "建筑覆盖依赖 OSM 完整度 (中国县城可能稀疏)。非测绘级成果, 仅供场景搭建参考。",
              "outputs": {}}

    # 断点续传 (--resume): 已有产物直接复用, 跳过对应网络下载; 3D Tiles/预览/报告重做
    resumed = []
    old_report = {}
    if args.resume:
        rp_old = os.path.join(out_dir, "download_report.json")
        if os.path.isfile(rp_old):
            try:
                with open(rp_old, encoding="utf-8") as fh:
                    old_report = json.load(fh)
            except Exception:
                old_report = {}
        if not args.skip_imagery:
            hit = sorted(glob.glob(os.path.join(out_dir, "imagery_z*_*.tif")))
            if hit:
                args.skip_imagery = True
                resumed.append(os.path.basename(hit[0]))
        if not args.skip_dem and os.path.isfile(os.path.join(out_dir, "dem_glo30_30m.tif")):
            args.skip_dem = True
            resumed.append("dem_glo30_30m.tif")
        if resumed:
            log("[*] 续传: 复用已有产物 → " + ", ".join(resumed))
    if resumed:
        report["resumed"] = resumed

    # ESRI z19+ 在很多县城/乡镇区域是占位灰瓦片 ("Map data not yet available"),
    # 直接下载会得到灰色马赛克。先探测, 占位则自动降级。
    if args.source == "esri" and not args.skip_imagery and args.zoom >= 19:
        log(f"[*] 探测 ESRI z{args.zoom} 中心瓦片是否为真实影像 ...")
        if not probe_esri_tile(args.zoom, lon0, lat0):
            z = args.zoom - 1
            log(f"[!] ESRI z{args.zoom} 在此区域是占位灰瓦片 (无真实影像), 自动降级 z{z}")
            while z >= 16 and not probe_esri_tile(z, lon0, lat0):
                log(f"[!] z{z} 仍是占位, 继续降级")
                z -= 1
            args.zoom = z
        else:
            log(f"[✓] ESRI z{args.zoom} 有真实影像")

    # 1. 影像
    if not args.skip_imagery:
        p, info = download_imagery(w, s, e, n, args.zoom,
                                   args.source, os.path.join(out_dir, f"imagery_z{args.zoom}_{args.source}.tif"),
                                   workers=args.workers)
        report["outputs"]["imagery"] = info
    else:
        p = os.path.join(out_dir, f"imagery_z{args.zoom}_{args.source}.tif")
        if args.resume and os.path.isfile(p):
            report["outputs"]["imagery"] = dict(
                (old_report.get("outputs") or {}).get("imagery") or {"file": os.path.basename(p)},
                resumed=True)

    # 2. 高程
    if not args.skip_dem:
        dem_path, dem_info = download_dem(w, s, e, n, os.path.join(out_dir, "dem_glo30_30m.tif"))
        report["outputs"]["dem"] = dem_info
    else:
        dem_path = os.path.join(out_dir, "dem_glo30_30m.tif")
        dem_info = dict(
            (old_report.get("outputs") or {}).get("dem") or {},
            resumed=True) if args.resume and os.path.isfile(dem_path) else {}

    # 3. 建筑 + OSM 配套图层 (道路/铁路/水系/绿地)
    dem_sample = make_dem_sampler(dem_path)
    buildings_info = {}
    extras_counts = {}
    feats = None
    resumed_bldg = (args.resume and not args.skip_buildings
                    and os.path.isfile(os.path.join(out_dir, "buildings.geojson")))
    if not args.skip_buildings:
        if resumed_bldg:
            with open(os.path.join(out_dir, "buildings.geojson"), encoding="utf-8") as fh:
                feats = json.load(fh)["features"]
            n_osm = sum(1 for f in feats if "osm" in (f["properties"].get("height_source") or ""))
            buildings_info = {"resumed": True, "count": len(feats),
                              "height_from_osm_tag": n_osm,
                              "height_policy_estimate": len(feats) - n_osm}
            log(f"[*] 续传: 复用已有 buildings.geojson ({len(feats)} 栋), 跳过 Overpass")
        else:
            elements, used_ep = fetch_osm(w, s, e, n)
            if elements is None:
                log("[-] OSM: 所有 Overpass 端点失败 (国内可尝试加 astar 代理)")
                buildings_info = {"error": "overpass unreachable"}
            else:
                feats, stats = osm_to_features(elements, dem_sample)
                geo_path = os.path.join(out_dir, "buildings.geojson")
                write_layer_geojson(feats, geo_path, (w, s, e, n), "建筑", {
                    "height_policy": f"OSM height 标签 > building:levels×{FLOOR_HEIGHT} > 默认 {DEFAULT_HEIGHT}m (POLICY 估算)",
                    "unit": "栋"})
                buildings_info = {"endpoint_used": used_ep, "count": len(feats),
                                  "height_from_osm_tag": stats["from"],
                                  "height_policy_estimate": stats["no_tag"],
                                  "skipped_small": stats["skipped_small"],
                                  "skipped_invalid": stats["skipped_open"]}
                log(f"[*]   高度来源: OSM 标签 {stats['from']} 栋, POLICY 估算 {stats['no_tag']} 栋")

                if not args.skip_extras:
                    layers = osm_extras_to_features(elements)
                    layer_counts = {}
                    layer_labels = {"roads": "道路路网", "railways": "铁路轨道",
                                    "water": "水系", "green": "绿地"}
                    for lname, fts in layers.items():
                        layer_counts[lname] = len(fts)
                        if fts:
                            write_layer_geojson(fts, os.path.join(out_dir, f"{lname}.geojson"),
                                                (w, s, e, n), layer_labels[lname], {"unit": "要素"})
                    buildings_info["layers"] = layer_counts
                    extras_counts = {f"{k}.geojson": v for k, v in layer_counts.items()}
                    log(f"[*]   配套图层: " + ", ".join(f"{k} {v}" for k, v in layer_counts.items()))

        if resumed_bldg and not args.skip_extras:
            for lname in ("roads", "railways", "water", "green"):
                fp = os.path.join(out_dir, f"{lname}.geojson")
                cnt = 0
                if os.path.isfile(fp):
                    try:
                        with open(fp, encoding="utf-8") as fh:
                            cnt = len(json.load(fh)["features"])
                    except Exception:
                        cnt = 0
                extras_counts[f"{lname}.geojson"] = cnt
            buildings_info["layers"] = {"roads": extras_counts["roads.geojson"],
                                        "railways": extras_counts["railways.geojson"],
                                        "water": extras_counts["water.geojson"],
                                        "green": extras_counts["green.geojson"]}
            log("[*] 续传: 配套图层计数取自已有文件")

        # 3.5 CNBH-10m 高度注入 (新建/续传都生效; OSM 标签优先保留)
        if args.heights and feats:
            n_inj = inject_cnbh_heights(feats, (w, s, e, n))
            if n_inj:
                buildings_info["height_cnbh"] = n_inj
                write_layer_geojson(feats, os.path.join(out_dir, "buildings.geojson"),
                                    (w, s, e, n), "建筑", {
                    "height_policy": f"CNBH-10m 注入 > OSM height 标签 > "
                                     f"building:levels×{FLOOR_HEIGHT} > 默认 {DEFAULT_HEIGHT}m (估算)",
                    "unit": "栋"})

        # 4. 3D Tiles (新建与续传都要重做: 建筑或模板可能已更新)
        if not args.skip_3dtiles and feats:
            _, tiles_info = write_3dtiles(feats, lon0, lat0,
                                          os.path.join(out_dir, "buildings_3dtiles"), dem_info,
                                          tile_threshold=args.tile_threshold)
            buildings_info["3d_tiles"] = tiles_info

    # 5. 预览页
    if not args.skip_preview:
        median_base = 0.0
        if buildings_info.get("count"):
            bases = sorted(f["properties"]["base_m"] for f in feats)
            median_base = bases[len(bases) // 2]
        write_preview(out_dir, name, lon0, lat0, ext_m / 2, median_base, extras=extras_counts)

    report["outputs"]["buildings"] = buildings_info
    report["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    rp = os.path.join(out_dir, "download_report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"[✓] 下载报告: {rp}")
    log("[完成] 全部输出见 " + os.path.abspath(out_dir))


if __name__ == "__main__":
    main()
