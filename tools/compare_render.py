import sys
import numpy as np
import pymupdf

ref, ours = sys.argv[1], sys.argv[2]
dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 144
A = pymupdf.open(ref)
B = pymupdf.open(ours)
print(f"参考 {len(A)} 页 / 自动 {len(B)} 页  @{dpi}dpi")
bad = 0
for i in range(len(A)):
    pa, pb = A[i].get_pixmap(dpi=dpi), B[i].get_pixmap(dpi=dpi)
    if (pa.width, pa.height) != (pb.width, pb.height):
        print(f"p{i+1}: 渲染尺寸不同 {pa.width}x{pa.height} vs {pb.width}x{pb.height}")
        bad += 1
        continue
    na = np.frombuffer(pa.samples, dtype=np.uint8).reshape(pa.height, pa.width, pa.n).astype(int)
    nb = np.frombuffer(pb.samples, dtype=np.uint8).reshape(pb.height, pb.width, pb.n).astype(int)
    d = np.abs(na - nb).max(axis=2)
    cnt = int((d > 12).sum())
    n = pa.width * pa.height
    if cnt > 0:
        ys, xs = np.where(d > 12)
        print(f"p{i+1:>2}: 不同像素 {cnt:>6d} ({cnt/n*100:.3f}%) 差异区 x{xs.min()}-{xs.max()} y{ys.min()}-{ys.max()}")
        bad += 1
    else:
        print(f"p{i+1:>2}: 完全一致")
print("有差异页数:", bad)
