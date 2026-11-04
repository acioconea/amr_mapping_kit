"""Corelare webcam (RGB) + camera termica Mini2, temperatura medie pe un AprilTag, benchmark de calcul.

Subcomenzi (detalii: python rgbt.py <comanda> -h):

  calib-capture  live, ambele camere; SPACE salveaza o pereche in care tag-ul e detectat in AMBELE imagini
                 (tinta dual-modala: tag vizibil si termic). Scrie perechile in --pairs (JSON lines).
  calib-solve    din perechi: intrinseci RGB + termic, extrinseci (R, T) intre camere si homografia H.
                 Imparte perechile in calibrare / validare si raporteaza eroarea de reproiectie pe distante.
  measure        live: detecteaza tag-ul pe RGB, il proiecteaza pe imaginea termica (metoda H sau 3d),
                 calculeaza media pixelilor termici din interiorul tag-ului, scrie un rand CSV pe cadru.
  record         salveaza N perechi sincronizate (PNG + NPY) pentru benchmark reproductibil.
  bench          reproceseaza un set inregistrat si cronometreaza fiecare etapa (median, p95, p99, FPS,
                 CPU, RAM). Acelasi set rulat pe laptop / Raspberry Pi / Jetson = comparatie corecta.
  selftest       verifica tot lantul pe imagini sintetice (nu are nevoie de camere).

Necesita: numpy, opencv-python (sau opencv-contrib-python pentru selftest), pupil-apriltags, psutil.
"""
import argparse
import csv
import json
import os
import platform
import statistics
import sys
import time

import cv2
import numpy as np

try:
    from pupil_apriltags import Detector
except ImportError:
    sys.exit("Lipseste pupil-apriltags: pip install pupil-apriltags")

FAMILY = os.environ.get("RGBT_FAMILY", "tag36h11")   # suprascris de --family


# ---------------------------------------------------------------- utilitare comune

def make_detector(threads=4, decimate=2.0):
    return Detector(families=FAMILY, nthreads=threads, quad_decimate=decimate,
                    refine_edges=1, decode_sharpening=0.25)


def tag_object_points(size):
    """Colturile tag-ului (latura conturului negru = size, in metri), in ordinea data de detector
    si ceruta de cv2.SOLVEPNP_IPPE_SQUARE."""
    s = size / 2
    return np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], dtype=np.float64)


def y16_to_u8(frame, lo_hi=None):
    lo, hi = lo_hi if lo_hi else np.percentile(frame, (1, 99))
    return np.clip((frame.astype(np.float32) - lo) / max(hi - lo, 1) * 255, 0, 255).astype(np.uint8)


def detect_rgb(det, bgr_or_gray, min_margin=35):
    gray = bgr_or_gray if bgr_or_gray.ndim == 2 else cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2GRAY)
    return [d for d in det.detect(gray) if d.decision_margin >= min_margin]


def detect_thermal(det, y16, min_margin=20):
    """Tag-ul e vizibil termic doar pe o tinta dual-modala. Polaritatea depinde de tinta
    (negru cald sau negru rece), deci incercam ambele variante."""
    g = y16_to_u8(y16)
    out = {}
    for img in (g, 255 - g):
        for d in det.detect(img):
            if d.decision_margin >= min_margin and d.tag_id not in out:
                out[d.tag_id] = d
    return list(out.values())


def load_temp_calib(path):
    """Dreapta grade_C = a*brut + b din mini2_calib.json (minim 2 puncte), altfel None."""
    try:
        pts = json.load(open(path))
    except (OSError, ValueError):
        return None
    if len(pts) < 2 or len({p[0] for p in pts}) < 2:
        return None
    a, b = np.polyfit([p[0] for p in pts], [p[1] for p in pts], 1)
    return float(a), float(b)


class Mapper:
    """Transfera colturile tag-ului din imaginea RGB in imaginea termica."""

    def __init__(self, calib_path, method):
        c = json.load(open(calib_path))
        self.method = method
        self.H = np.array(c["H"], dtype=np.float64)
        self.K1, self.D1 = np.array(c["K_rgb"]), np.array(c["D_rgb"])
        self.K2, self.D2 = np.array(c["K_th"]), np.array(c["D_th"])
        self.R, self.T = np.array(c["R"]), np.array(c["T"]).reshape(3, 1)
        self.th_size = tuple(c["th_size"])

    def pose(self, corners, tag_size):
        ok, rvec, tvec = cv2.solvePnP(tag_object_points(tag_size), corners.astype(np.float64),
                                      self.K1, self.D1, flags=cv2.SOLVEPNP_IPPE_SQUARE)
        return (rvec, tvec) if ok else (None, None)

    def map(self, corners, tag_size, pose=None):
        if self.method == "H":
            return cv2.perspectiveTransform(corners.reshape(-1, 1, 2).astype(np.float64), self.H).reshape(-1, 2)
        rvec, tvec = pose if pose else self.pose(corners, tag_size)
        if rvec is None:
            return None
        Rr, _ = cv2.Rodrigues(rvec)
        R2 = self.R @ Rr
        t2 = self.R @ tvec + self.T
        pts, _ = cv2.projectPoints(tag_object_points(tag_size), cv2.Rodrigues(R2)[0], t2, self.K2, self.D2)
        return pts.reshape(-1, 2)


def tag_stats(y16, poly, margin_px):
    """Statistici pe pixelii termici din interiorul poligonului, dupa eroziune cu margin_px
    (elimina pixelii de margine amestecati cu fundalul)."""
    mask = np.zeros(y16.shape, np.uint8)
    cv2.fillConvexPoly(mask, np.round(poly).astype(np.int32), 1)
    if margin_px > 0:
        k = 2 * margin_px + 1
        mask = cv2.erode(mask, np.ones((k, k), np.uint8))
    vals = y16[mask.astype(bool)]
    if vals.size == 0:
        return None
    v = vals.astype(np.float64)
    return {"n_px": int(v.size), "raw_mean": float(v.mean()), "raw_std": float(v.std()),
            "raw_median": float(np.median(v)), "raw_min": float(v.min()), "raw_max": float(v.max())}


def corner_error(pred, th_det):
    return float(np.sqrt(((pred - th_det) ** 2).sum(1)).mean())


def open_rgb(index, width, height):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        sys.exit(f"Nu pot deschide webcam-ul {index}")
    return cap


def read_pair(cap, th):
    """Citeste cate un cadru din fiecare camera; intoarce si decalajul de timp dintre ele."""
    ok, bgr = cap.read()
    t_rgb = time.perf_counter()
    t_th, y16 = th.read()
    if not ok:
        raise RuntimeError("webcam: cadru lipsa")
    return bgr, y16, t_rgb, t_th


def open_thermal(args):
    from mini2_cam import Mini2
    return Mini2(args.dll)


def side_by_side(bgr, y16, rgb_polys=(), th_polys=(), text=""):
    th = cv2.applyColorMap(y16_to_u8(y16), cv2.COLORMAP_INFERNO)
    for p in rgb_polys:
        cv2.polylines(bgr, [np.round(p).astype(np.int32)], True, (0, 255, 0), 2)
    for p, col in th_polys:
        cv2.polylines(th, [np.round(p).astype(np.int32)], True, col, 1)
    h = th.shape[0]
    rgb_s = cv2.resize(bgr, (int(bgr.shape[1] * h / bgr.shape[0]), h))
    view = np.hstack([rgb_s, th])
    if text:
        cv2.putText(view, text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return view


# ---------------------------------------------------------------- calibrare

def cmd_calib_capture(args):
    det = make_detector(args.threads, 1.0)
    cap, th = open_rgb(args.rgb, args.width, args.height), open_thermal(args)
    os.makedirs(args.frames_dir, exist_ok=True)
    n = sum(1 for _ in open(args.pairs)) if os.path.exists(args.pairs) else 0
    print("SPACE = salveaza perechea (tag detectat in ambele), ESC = iesire")
    try:
        while True:
            bgr, y16, t_rgb, t_th = read_pair(cap, th)
            dr = {d.tag_id: d for d in detect_rgb(det, bgr)}
            dt = {d.tag_id: d for d in detect_thermal(det, y16)}
            common = sorted(set(dr) & set(dt))
            view = side_by_side(bgr.copy(), y16, [d.corners for d in dr.values()],
                                [(d.corners, (0, 255, 0)) for d in dt.values()],
                                f"perechi salvate: {n}   tag-uri comune: {common}")
            cv2.imshow("calib-capture", view)
            k = cv2.waitKey(1) & 0xFF
            if k == 32 and common:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                cv2.imwrite(os.path.join(args.frames_dir, f"{stamp}_{n:04d}_rgb.png"), bgr)
                np.save(os.path.join(args.frames_dir, f"{stamp}_{n:04d}_th.npy"), y16)
                with open(args.pairs, "a") as fh:
                    for i in common:
                        fh.write(json.dumps({"view": n, "id": int(i), "tag_size": args.tag_size,
                                             "rgb": dr[i].corners.tolist(), "th": dt[i].corners.tolist(),
                                             "rgb_size": [bgr.shape[1], bgr.shape[0]],
                                             "th_size": [y16.shape[1], y16.shape[0]],
                                             "dt_ms": (t_th - t_rgb) * 1000}) + "\n")
                n += 1
                print(f"pereche {n} salvata, tag-uri {common}")
            elif k in (27, ord("q")):
                break
    finally:
        cv2.destroyAllWindows()
        cap.release()
        th.close()


def solve_calibration(recs, flags_intr):
    obj = [tag_object_points(r["tag_size"]).astype(np.float32) for r in recs]
    p1 = [np.array(r["rgb"], np.float32) for r in recs]
    p2 = [np.array(r["th"], np.float32) for r in recs]
    s1, s2 = tuple(recs[0]["rgb_size"]), tuple(recs[0]["th_size"])
    rms1, K1, D1, _, _ = cv2.calibrateCamera(obj, p1, s1, None, None, flags=flags_intr)
    rms2, K2, D2, _, _ = cv2.calibrateCamera(obj, p2, s2, None, None, flags=flags_intr)
    rms_s, K1, D1, K2, D2, R, T, _, _ = cv2.stereoCalibrate(
        obj, p1, p2, K1, D1, K2, D2, s1, flags=cv2.CALIB_FIX_INTRINSIC,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 200, 1e-9))
    H, _ = cv2.findHomography(np.concatenate(p1), np.concatenate(p2), cv2.RANSAC, 3.0)
    return {"K_rgb": K1.tolist(), "D_rgb": D1.ravel().tolist(), "K_th": K2.tolist(), "D_th": D2.ravel().tolist(),
            "R": R.tolist(), "T": T.ravel().tolist(), "H": H.tolist(),
            "rms_rgb_px": rms1, "rms_th_px": rms2, "rms_stereo_px": rms_s,
            "rgb_size": list(s1), "th_size": list(s2), "baseline_m": float(np.linalg.norm(T)),
            "n_tags": len(recs)}


def cmd_calib_solve(args):
    recs = [json.loads(l) for l in open(args.pairs) if l.strip()]
    if len(recs) < 10:
        sys.exit(f"Doar {len(recs)} observatii; ai nevoie de cel putin 10 (recomandat 40+)")
    views = sorted({r["view"] for r in recs})
    rng = np.random.default_rng(args.seed)
    val_views = set(rng.choice(views, size=max(1, int(len(views) * args.val_frac)), replace=False).tolist())
    train = [r for r in recs if r["view"] not in val_views]
    val = [r for r in recs if r["view"] in val_views]
    flags = cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K3
    calib = solve_calibration(train, flags)
    calib.update({"n_train_views": len(views) - len(val_views), "n_val_views": len(val_views),
                  "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    json.dump(calib, open(args.out, "w"), indent=2)
    print(json.dumps({k: calib[k] for k in ("rms_rgb_px", "rms_th_px", "rms_stereo_px", "baseline_m",
                                           "n_tags", "n_train_views", "n_val_views")}, indent=2))
    # validare pe perechile nefolosite la calibrare
    rows = []
    for method in ("H", "3d"):
        m = Mapper(args.out, method)
        for r in val:
            c = np.array(r["rgb"])
            rv, tv = m.pose(c, r["tag_size"])
            pred = m.map(c, r["tag_size"], (rv, tv))
            if pred is None:
                continue
            rows.append({"method": method, "view": r["view"], "id": r["id"],
                         "dist_m": float(np.linalg.norm(tv)) if tv is not None else float("nan"),
                         "err_px": corner_error(pred, np.array(r["th"]))})
    with open(args.eval_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nEroare medie de colt pe setul de validare (px termici), scrisa si in {args.eval_csv}:")
    for method in ("H", "3d"):
        e = [r["err_px"] for r in rows if r["method"] == method]
        print(f"  {method:>2}: medie {np.mean(e):.2f}  mediana {np.median(e):.2f}  p95 {np.percentile(e, 95):.2f}  (n={len(e)})")


# ---------------------------------------------------------------- masurare live

def cmd_measure(args):
    det = make_detector(args.threads, args.decimate)
    det_th = make_detector(args.threads, 1.0)
    mapper = Mapper(args.calib, args.method)
    tcal = load_temp_calib(args.temp_calib)
    cap, th = open_rgb(args.rgb, args.width, args.height), open_thermal(args)
    new = not os.path.exists(args.log)
    fh = open(args.log, "a", newline="")
    fields = ["time", "label", "frame", "tag_id", "method", "dist_m", "n_px", "raw_mean", "raw_std",
              "raw_median", "raw_min", "raw_max", "T_mean_C", "T_std_C", "T_ref_C", "corner_err_px",
              "dt_cams_ms", "t_detect_ms", "t_map_ms", "t_stats_ms"]
    w = csv.DictWriter(fh, fieldnames=fields)
    if new:
        w.writeheader()
    n = frame = 0
    try:
        while args.frames <= 0 or n < args.frames:
            bgr, y16, t_rgb, t_th = read_pair(cap, th)
            frame += 1
            t0 = time.perf_counter()
            dets = detect_rgb(det, bgr)
            t1 = time.perf_counter()
            th_polys, txt = [], "tag nedetectat"
            for d in dets:
                if args.tag_id >= 0 and d.tag_id != args.tag_id:
                    continue
                ta = time.perf_counter()
                rv, tv = mapper.pose(d.corners, args.tag_size)
                poly = mapper.map(d.corners, args.tag_size, (rv, tv))
                tb = time.perf_counter()
                st = tag_stats(y16, poly, args.margin) if poly is not None else None
                tc = time.perf_counter()
                if st is None:
                    continue
                err = ""
                if args.check_thermal:   # doar cu tinta dual-modala: eroarea fata de colturile detectate termic
                    for dth in detect_thermal(det_th, y16):
                        if dth.tag_id == d.tag_id:
                            err = round(corner_error(poly, dth.corners), 3)
                Tm = Ts = ""
                if tcal:
                    Tm, Ts = round(tcal[0] * st["raw_mean"] + tcal[1], 3), round(abs(tcal[0]) * st["raw_std"], 3)
                w.writerow({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "label": args.label, "frame": frame,
                            "tag_id": d.tag_id, "method": args.method,
                            "dist_m": round(float(np.linalg.norm(tv)), 4) if tv is not None else "",
                            **{k: round(v, 3) if isinstance(v, float) else v for k, v in st.items()},
                            "T_mean_C": Tm, "T_std_C": Ts, "T_ref_C": args.tref if args.tref is not None else "",
                            "corner_err_px": err, "dt_cams_ms": round((t_th - t_rgb) * 1000, 2),
                            "t_detect_ms": round((t1 - t0) * 1000, 3), "t_map_ms": round((tb - ta) * 1000, 3),
                            "t_stats_ms": round((tc - tb) * 1000, 3)})
                n += 1
                th_polys.append((poly, (0, 255, 0)))
                txt = f"id {d.tag_id}: {st['n_px']} px, brut {st['raw_mean']:.1f}" + \
                      (f" = {Tm:.2f} C" if Tm != "" else "") + f"   [{n}/{args.frames or 'inf'}]"
            if not args.no_view:
                cv2.imshow("measure", side_by_side(bgr, y16, [d.corners for d in dets], th_polys, txt))
                if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
                    break
    finally:
        fh.close()
        cv2.destroyAllWindows()
        cap.release()
        th.close()
    print(f"{n} masuratori scrise in {args.log}")


# ---------------------------------------------------------------- inregistrare + benchmark

def cmd_record(args):
    cap, th = open_rgb(args.rgb, args.width, args.height), open_thermal(args)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "index.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["i", "rgb", "th", "t_rgb", "t_th"])
        try:
            for i in range(args.n):
                bgr, y16, t_rgb, t_th = read_pair(cap, th)
                cv2.imwrite(os.path.join(args.out_dir, f"{i:05d}_rgb.png"), bgr)
                np.save(os.path.join(args.out_dir, f"{i:05d}_th.npy"), y16)
                w.writerow([i, f"{i:05d}_rgb.png", f"{i:05d}_th.npy", f"{t_rgb:.6f}", f"{t_th:.6f}"])
                if i % 50 == 0:
                    print(f"{i}/{args.n}")
        finally:
            cap.release()
            th.close()
    print(f"{args.n} perechi in {args.out_dir}")


def platform_info():
    info = {"machine": platform.machine(), "system": f"{platform.system()} {platform.release()}",
            "processor": platform.processor(), "python": platform.python_version(),
            "opencv": cv2.__version__, "cpu_count": os.cpu_count()}
    try:
        info["model"] = open("/proc/device-tree/model").read().strip("\x00\n")   # Pi / Jetson
    except OSError:
        pass
    return info


def cpu_temp():
    try:
        import psutil
        t = psutil.sensors_temperatures()
        vals = [s.current for v in t.values() for s in v if s.current]
        return max(vals) if vals else None
    except Exception:
        return None


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def cmd_bench(args):
    import psutil
    idx = list(csv.DictReader(open(os.path.join(args.data, "index.csv"))))
    pairs = [(cv2.imread(os.path.join(args.data, r["rgb"])), np.load(os.path.join(args.data, r["th"]))) for r in idx]
    print(f"{len(pairs)} perechi incarcate in RAM (citirea de pe disc nu intra in timpi)")
    mapper = Mapper(args.calib, args.method)
    proc = psutil.Process()
    info = platform_info()
    os.makedirs(args.out_dir, exist_ok=True)
    tag = args.tag or platform.node()
    summary = []
    for threads in [int(x) for x in args.threads.split(",")]:
        for dec in [float(x) for x in args.decimate.split(",")]:
            det = make_detector(threads, dec)
            det_th = make_detector(threads, 1.0) if args.with_thermal_detect else None
            rows = []
            for i in range(args.warmup):                 # incalzire: cache, JIT OpenCV, frecventa CPU
                detect_rgb(det, pairs[i % len(pairs)][0])
            proc.cpu_percent(None)
            psutil.cpu_percent(None)
            t_wall = time.perf_counter()
            for rep in range(args.repeat):
                for i, (bgr, y16) in enumerate(pairs):
                    t0 = time.perf_counter()
                    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                    t1 = time.perf_counter()
                    dets = detect_rgb(det, gray)
                    t2 = time.perf_counter()
                    polys = [mapper.map(d.corners, args.tag_size) for d in dets]
                    t3 = time.perf_counter()
                    stats = [tag_stats(y16, p, args.margin) for p in polys if p is not None]
                    t4 = time.perf_counter()
                    if det_th is not None:
                        detect_thermal(det_th, y16)
                    t5 = time.perf_counter()
                    rows.append({"rep": rep, "i": i, "n_tags": len(dets), "n_ok": sum(s is not None for s in stats),
                                 "gray_ms": (t1 - t0) * 1e3, "detect_ms": (t2 - t1) * 1e3, "map_ms": (t3 - t2) * 1e3,
                                 "stats_ms": (t4 - t3) * 1e3, "thermal_detect_ms": (t5 - t4) * 1e3,
                                 "total_ms": (t5 - t0) * 1e3})
            wall = time.perf_counter() - t_wall
            cpu_proc, cpu_sys = proc.cpu_percent(None), psutil.cpu_percent(None)
            name = f"{tag}_th{threads}_dec{dec:g}_{args.method}"
            with open(os.path.join(args.out_dir, name + "_frames.csv"), "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            s = {"run": name, "threads": threads, "decimate": dec, "method": args.method, "frames": len(rows),
                 "detect_rate": sum(r["n_tags"] > 0 for r in rows) / len(rows),
                 "throughput_fps": len(rows) / wall, "cpu_proc_pct": cpu_proc, "cpu_sys_pct": cpu_sys,
                 "rss_mb": proc.memory_info().rss / 2**20, "cpu_temp_C": cpu_temp()}
            for k in ("gray_ms", "detect_ms", "map_ms", "stats_ms", "thermal_detect_ms", "total_ms"):
                a = [r[k] for r in rows]
                s[k] = {"mean": statistics.fmean(a), "median": pct(a, 50), "p95": pct(a, 95),
                        "p99": pct(a, 99), "max": max(a)}
            summary.append(s)
            print(f"{name:40s} total median {s['total_ms']['median']:7.2f} ms  p95 {s['total_ms']['p95']:7.2f} ms  "
                  f"detect median {s['detect_ms']['median']:7.2f} ms  {s['throughput_fps']:6.1f} FPS  "
                  f"CPU {cpu_proc:5.0f}%  detectie {s['detect_rate']*100:5.1f}%")
    json.dump({"platform": info, "args": {k: v for k, v in vars(args).items() if k != "fn"}, "runs": summary},
              open(os.path.join(args.out_dir, f"{tag}_summary.json"), "w"), indent=2)
    print(f"Rezultate in {args.out_dir}")


# ---------------------------------------------------------------- test sintetic

def _render_scene(tag_img, size_m, K, R, t, out_wh, bg, plane_half):
    """Randeaza planul tag-ului (patrat de latura 2*plane_half metri, in sistemul tag-ului) intr-o camera."""
    P = tag_img.shape[0]
    A = np.array([[2 * plane_half / P, 0, -plane_half], [0, 2 * plane_half / P, -plane_half], [0, 0, 1]])
    Hm = K @ np.column_stack([R[:, 0], R[:, 1], t.ravel()]) @ A
    return cv2.warpPerspective(tag_img, Hm, out_wh, flags=cv2.INTER_AREA, borderValue=bg)


def cmd_selftest(args):
    """Doua camere virtuale (RGB 1280x720, termic 640x512, baza 4 cm), tag 36h11 de 10 cm la 0,5-3 m.
    Verifica: detectia, calibrarea, ambele metode de proiectie si media pe tag (valoare cunoscuta)."""
    rng = np.random.default_rng(1)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    inner = cv2.aruco.generateImageMarker(d, 0, 800)                 # contur negru 8x8 celule
    tag = np.pad(inner, 100, constant_values=255)                     # + chenar alb de 1 celula
    S = 0.10                                                          # latura conturului negru, m
    plane_half = S / 2 * 1000 / 800                                   # tag + chenar alb
    K1 = np.array([[900, 0, 640], [0, 900, 360], [0, 0, 1.0]])
    K2 = np.array([[690, 0, 320], [0, 690, 256], [0, 0, 1.0]])
    R12 = cv2.Rodrigues(np.array([0.004, -0.006, 0.002]))[0]
    T12 = np.array([[-0.04], [0.002], [0.003]])
    RAW_BLACK, RAW_WHITE, RAW_BG = 21200.0, 20800.0, 20000.0           # tinta dual-modala (negru mai cald)
    th_tag = np.where(tag < 128, RAW_BLACK, RAW_WHITE).astype(np.float32)

    def view(dist):
        ang = np.radians(rng.uniform(-35, 35, 3) * np.array([1, 1, 0.3]))
        Rt = cv2.Rodrigues(ang)[0]
        tt = np.array([[rng.uniform(-0.25, 0.25) * dist], [rng.uniform(-0.15, 0.15) * dist], [dist]])
        rgb = _render_scene(tag, S, K1, Rt, tt, (1280, 720), 128, plane_half)
        rgb = np.clip(rgb + rng.normal(0, 2, rgb.shape), 0, 255).astype(np.uint8)
        R2, t2 = R12 @ Rt, R12 @ tt + T12
        th = _render_scene(th_tag, S, K2, R2, t2, (640, 512), RAW_BG, plane_half)
        th = cv2.GaussianBlur(th, (0, 0), 0.8) + rng.normal(0, 4, th.shape)
        return rgb, th.astype(np.uint16), Rt, tt, R2, t2

    det = make_detector(4, 1.0)
    recs = []
    for k in range(80):
        dist = rng.uniform(0.4, 1.2)
        rgb, th, *_ = view(dist)
        a, b = detect_rgb(det, rgb), detect_thermal(det, th)
        if a and b:
            recs.append({"view": k, "id": 0, "tag_size": S, "rgb": a[0].corners.tolist(), "th": b[0].corners.tolist(),
                         "rgb_size": [1280, 720], "th_size": [640, 512]})
    print(f"detectat in ambele camere: {len(recs)}/80 vederi de calibrare")
    calib = solve_calibration(recs, cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K3)
    path = os.path.join(args.out_dir, "selftest_calib.json")
    os.makedirs(args.out_dir, exist_ok=True)
    json.dump(calib, open(path, "w"), indent=2)
    print(f"RMS calibrare: RGB {calib['rms_rgb_px']:.3f}px, termic {calib['rms_th_px']:.3f}px, stereo "
          f"{calib['rms_stereo_px']:.3f}px; baza estimata {calib['baseline_m']*100:.2f} cm (real 4.02)")
    f_est = np.array(calib["K_th"])[0, 0]
    print(f"f termic estimat {f_est:.1f} px (real 690)")
    mappers = {m: Mapper(path, m) for m in ("H", "3d")}
    ok = True
    print("\ndist[m]  px pe latura  eroare_H[px]  eroare_3d[px]  media_bruta_3d  asteptat  ")
    for dist in (0.5, 1.0, 1.5, 2.0, 3.0):
        eh, e3, mv, ex, side = [], [], [], [], []
        for _ in range(15):
            rgb, th, Rt, tt, R2, t2 = view(dist)
            a = detect_rgb(det, rgb)
            if not a:
                continue
            # randarea foloseste sistemul tag-ului cu X spre dreapta, Y in jos; tag_object_points
            # e acelasi sistem rotit 180 grade in jurul lui Z (asa numeroteaza detectorul colturile)
            gt, _ = cv2.projectPoints(tag_object_points(S) * [-1, -1, 1], cv2.Rodrigues(R2)[0], t2, K2, None)
            gt = gt.reshape(-1, 2)
            side.append(np.linalg.norm(gt[0] - gt[1]))
            ph = mappers["H"].map(a[0].corners, S)
            p3 = mappers["3d"].map(a[0].corners, S)
            eh.append(corner_error(ph, gt))
            e3.append(corner_error(p3, gt))
            st = tag_stats(th.astype(np.float32), p3, 2)
            # valoarea asteptata: aceeasi masca aplicata pe imaginea fara zgomot
            clean = _render_scene(th_tag, S, K2, R2, t2, (640, 512), RAW_BG, plane_half)
            st_gt = tag_stats(cv2.GaussianBlur(clean, (0, 0), 0.8), gt, 2)
            if st and st_gt:
                mv.append(st["raw_mean"])
                ex.append(st_gt["raw_mean"])
        print(f"{dist:6.1f}  {np.mean(side):10.1f}  {np.mean(eh):12.2f}  {np.mean(e3):13.2f}  "
              f"{np.mean(mv) if mv else float('nan'):14.1f}  {np.mean(ex) if ex else float('nan'):8.1f}")
        ok &= np.mean(e3) < 1.5
    if args.dataset > 0:   # set sintetic in formatul lui `record`, ca sa poti incerca `bench` fara camere
        ddir = os.path.join(args.out_dir, "dataset")
        os.makedirs(ddir, exist_ok=True)
        with open(os.path.join(ddir, "index.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["i", "rgb", "th", "t_rgb", "t_th"])
            for i in range(args.dataset):
                rgb, th, *_ = view(rng.uniform(0.5, 3.0))
                cv2.imwrite(os.path.join(ddir, f"{i:05d}_rgb.png"), cv2.cvtColor(rgb, cv2.COLOR_GRAY2BGR))
                np.save(os.path.join(ddir, f"{i:05d}_th.npy"), th)
                w.writerow([i, f"{i:05d}_rgb.png", f"{i:05d}_th.npy", 0, 0])
        print(f"set sintetic: {args.dataset} perechi in {ddir} (calibrare: {path})")
    print("\nSELFTEST", "OK" if ok else "ESUAT (eroarea metodei 3d > 1.5 px)")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cams(p):
        p.add_argument("--rgb", type=int, default=0, help="indexul webcam-ului")
        p.add_argument("--width", type=int, default=1280)
        p.add_argument("--height", type=int, default=720)
        p.add_argument("--dll", help="folderul AC020 cu bibliotecile SDK Mini2")
        p.add_argument("--threads", type=int, default=4)
        p.add_argument("--family", help="familia AprilTag, ex. tag36h11 (implicit) sau tag16h5")

    p = sub.add_parser("calib-capture")
    cams(p)
    p.add_argument("--tag-size", type=float, required=True, help="latura conturului negru al tag-ului, m")
    p.add_argument("--pairs", default="calib_pairs.jsonl")
    p.add_argument("--frames-dir", default="calib_frames")
    p.set_defaults(fn=cmd_calib_capture)

    p = sub.add_parser("calib-solve")
    p.add_argument("--pairs", default="calib_pairs.jsonl")
    p.add_argument("--out", default="calib_rgbt.json")
    p.add_argument("--eval-csv", default="calib_validare.csv")
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_calib_solve)

    p = sub.add_parser("measure")
    cams(p)
    p.add_argument("--calib", default="calib_rgbt.json")
    p.add_argument("--temp-calib", default="mini2_calib.json")
    p.add_argument("--method", choices=("H", "3d"), default="3d")
    p.add_argument("--tag-size", type=float, required=True)
    p.add_argument("--tag-id", type=int, default=-1, help="-1 = toate tag-urile")
    p.add_argument("--margin", type=int, default=2, help="eroziunea mastii, pixeli termici")
    p.add_argument("--decimate", type=float, default=2.0)
    p.add_argument("--frames", type=int, default=100, help="se opreste dupa N masuratori (0 = continuu)")
    p.add_argument("--tref", type=float, help="temperatura de referinta (sonda de contact), C")
    p.add_argument("--label", default="", help="eticheta conditiei, ex. d1.0_a0_T35")
    p.add_argument("--check-thermal", action="store_true", help="eroare de colt fata de detectia termica")
    p.add_argument("--log", default="masuratori.csv")
    p.add_argument("--no-view", action="store_true")
    p.set_defaults(fn=cmd_measure)

    p = sub.add_parser("record")
    cams(p)
    p.add_argument("--n", type=int, default=600)
    p.add_argument("--out-dir", default="dataset")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("bench")
    p.add_argument("--data", default="dataset")
    p.add_argument("--calib", default="calib_rgbt.json")
    p.add_argument("--method", choices=("H", "3d"), default="3d")
    p.add_argument("--tag-size", type=float, required=True)
    p.add_argument("--margin", type=int, default=2)
    p.add_argument("--threads", default="1,2,4", help="lista, ex. 1,2,4")
    p.add_argument("--decimate", default="1,2", help="lista, ex. 1,2,4")
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--family", help="familia AprilTag, ex. tag36h11 (implicit) sau tag16h5")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--with-thermal-detect", action="store_true")
    p.add_argument("--tag", help="numele platformei in fisierele rezultat (implicit hostname)")
    p.add_argument("--out-dir", default="bench_results")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("selftest")
    p.add_argument("--out-dir", default="selftest_out")
    p.add_argument("--dataset", type=int, default=0, help="scrie si N perechi sintetice pentru bench")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args()
    global FAMILY
    if getattr(args, "family", None):
        FAMILY = args.family
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
