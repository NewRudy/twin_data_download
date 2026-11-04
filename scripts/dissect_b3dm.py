"""解剖 b3dm 内 GLB: 正确跳过 JSON/BIN chunk 头, 校验屋顶/墙面法线与高度。

用法: python dissect_b3dm.py <scene.b3dm>
输出: 顶点/三角形统计, 屋顶(法线朝上)顶点的高度分布, 是否有楼底板。
"""
import json
import struct
import sys

import numpy as np


def dissect(path):
    data = open(path, "rb").read()
    magic, ver, total, ftj, ftb, btj, btb = struct.unpack("<4sIIIIII", data[:28])
    assert magic == b"b3dm", magic
    glb = data[28 + ftj + ftb + btj + btb:]
    assert glb[:4] == b"glTF", glb[:4]
    jlen, jtype = struct.unpack("<II", glb[12:20])
    assert jtype == 0x4E4F534A, hex(jtype)  # 'JSON'
    gj = json.loads(glb[20:20 + jlen])
    off = 20 + jlen
    blen, btype = struct.unpack("<II", glb[off:off + 8])
    assert btype == 0x004E4942, hex(btype)  # 'BIN\0'
    binc = glb[off + 8: off + 8 + blen]

    prims = gj["meshes"][0]["primitives"]
    print(f"primitives: {len(prims)}")
    for pi, p in enumerate(prims):
        mat = p.get("material")
        n_acc = gj["accessors"][p["attributes"]["NORMAL"]]
        pos_acc = gj["accessors"][p["attributes"]["POSITION"]]

        def read(a):
            bv = gj["bufferViews"][a["bufferView"]]
            o = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
            return np.frombuffer(binc, dtype="<f4",
                                 count=a["count"] * 3, offset=o).reshape(-1, 3)

        pos, nor = read(pos_acc), read(n_acc)
        cnt = pos_acc["count"]
        up = nor[:, 1] > 0.5          # glTF y = up
        down = nor[:, 1] < -0.5
        side = ~(up | down)
        tris = cnt // 3
        print(f"[prim {pi}] material={mat}  顶点 {cnt} (三角形 {tris})")
        print(f"  高度范围 y: {pos[:,1].min():.1f} ~ {pos[:,1].max():.1f}")
        if up.sum():
            hu = pos[up, 1]
            print(f"  屋顶(法线y>0.5): {int(up.sum())} 顶点, 高度 {hu.min():.1f}~{hu.max():.1f} 均值 {hu.mean():.1f}")
            near_ground = int((hu < pos[:,1].min() + 1.0).sum())
            print(f"  屋顶贴地(异常)顶点: {near_ground}")
        else:
            print(f"  屋顶(法线y>0.5): 0 顶点")
        if down.sum():
            print(f"  楼底(法线y<-0.5): {int(down.sum())} 顶点, 高度 {pos[down,1].min():.1f}~{pos[down,1].max():.1f}")
        else:
            print(f"  楼底(法线y<-0.5): 0 顶点")
        print(f"  侧面: {int(side.sum())} 顶点")
        if "TEXCOORD_0" in p.get("attributes", {}):
            t_acc = gj["accessors"][p["attributes"]["TEXCOORD_0"]]
            bv = gj["bufferViews"][t_acc["bufferView"]]
            uv = np.frombuffer(binc, dtype="<f4",
                               count=t_acc["count"] * 2,
                               offset=bv.get("byteOffset", 0) + t_acc.get("byteOffset", 0)).reshape(-1, 2)
            print(f"  UV: {int(t_acc['count'])} 个, u {uv[:,0].min():.2f}~{uv[:,0].max():.2f}, v {uv[:,1].min():.2f}~{uv[:,1].max():.2f}")
        if "materials" in gj and mat is not None:
            m = gj["materials"][mat]
            print(f"  material: {json.dumps(m)[:200]}")
    return gj


if __name__ == "__main__":
    dissect(sys.argv[1] if len(sys.argv) > 1 else "scene.b3dm")
