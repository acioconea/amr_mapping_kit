"""Camera termica Mini2 (640x512, Y16) ca obiect reutilizabil.

Aceeasi secventa de apeluri SDK ca in mini2_y16.py (testat pe Windows 11, driver libusbK),
doar impachetata intr-o clasa ca sa poata fi folosita din alte scripturi:

    cam = Mini2(dll_dir)      # sau Mini2() daca folderul AC020 e langa script
    ts, frame = cam.read()    # frame: numpy (512, 640) uint16, ts: time.perf_counter()
    cam.close()

Pe Linux (Raspberry Pi / Jetson) SDK-ul RS300 ar trebui sa aiba biblioteci .so cu aceleasi
nume; varianta Linux NU a fost testata.
"""
import ctypes as C
import glob
import os
import sys
import time

import numpy as np

W, H = 640, 512
VID, PID = 0x3474, 0x43E1
P = C.c_void_p


def _vfn(*a):
    return C.CFUNCTYPE(C.c_int, *a)


class _IrVideoHandle(C.Structure):
    _fields_ = [("ir_video_handle", P),
                ("open", P), ("init", P), ("start_stream", P), ("frame_get", P),
                ("stop_stream", P), ("release", P), ("close", P)]


class _DevParam(C.Structure):
    _fields_ = [("pid", C.c_uint), ("vid", C.c_uint), ("same_idx", C.c_uint)]


class _CamParam(C.Structure):
    _fields_ = [("format", C.c_char_p), ("width", C.c_uint), ("height", C.c_uint),
                ("frame_size", C.c_uint), ("fps", C.c_uint), ("timeout_ms_delay", C.c_uint)]


class _UserCb(C.Structure):
    _fields_ = [("iruvc_handle", P), ("usr_func", P), ("usr_param", P)]


class _StreamParams(C.Structure):
    _fields_ = [("camera_param", _CamParam), ("usr_callback", _UserCb), ("reserved", C.c_uint8 * 48)]


class _StopParams(C.Structure):
    _fields_ = [("stop_mode", C.c_int), ("reserved", C.c_uint8 * 60)]


def find_dll_dir(start):
    name = "libircmd.dll" if sys.platform == "win32" else "libircmd.so"
    hits = glob.glob(os.path.join(start, "**", "AC020", name), recursive=True) or \
        glob.glob(os.path.join(start, "**", name), recursive=True)
    return os.path.dirname(hits[0]) if hits else None


class Mini2:
    def __init__(self, dll_dir=None, mode=1):
        here = os.path.dirname(os.path.abspath(__file__))
        dll_dir = dll_dir or find_dll_dir(here)
        if not dll_dir:
            raise RuntimeError("Nu gasesc folderul AC020 cu bibliotecile SDK; foloseste --dll")
        ext = ".dll" if sys.platform == "win32" else ".so"
        if sys.platform == "win32":
            os.add_dll_directory(dll_dir)
        cwd = os.getcwd()
        os.chdir(dll_dir)
        try:
            self.ircam = C.CDLL(os.path.join(dll_dir, "libircam" + ext))
            self.iruvc = C.CDLL(os.path.join(dll_dir, "libiruvc" + ext))
            self.ircmd = C.CDLL(os.path.join(dll_dir, "libircmd" + ext))
        finally:
            os.chdir(cwd)
        iruvc, ircmd, ircam = self.iruvc, self.ircmd, self.ircam
        iruvc.iruvc_camera_handle_create.restype = P
        iruvc.iruvc_camera_handle_create.argtypes = [P]
        iruvc.iruvc_usb_handle_create_with_exist_instance.argtypes = [P, P]
        ircmd.ircmd_create_handle.restype = P
        ircmd.ircmd_create_handle.argtypes = [P]
        ircmd.adv_stream_source_mode_set.argtypes = [P, C.c_int]

        vh = P()
        self._chk("ir_video_handle_create", ircam.ir_video_handle_create(C.byref(vh)))
        self.vid = C.cast(vh, C.POINTER(_IrVideoHandle)).contents
        self.uvc = iruvc.iruvc_camera_handle_create(vh)
        self.dev = _DevParam(PID, VID, 0)
        self._chk("open", self._call(self.vid.open, self.uvc, C.byref(self.dev)))
        self._chk("init", self._call(self.vid.init, self.uvc, C.byref(self.dev)))
        ch = P()
        ircam.ir_control_handle_create(C.byref(ch))
        iruvc.iruvc_usb_handle_create_with_exist_instance(ch, self.uvc)
        self.cmd = ircmd.ircmd_create_handle(ch)
        ircmd.adv_stream_source_mode_set(self.cmd, mode)   # 1 = KBC (date brute)

        self._fmt = C.create_string_buffer(b"YUYV")
        sp = _StreamParams()
        sp.camera_param.format = C.cast(self._fmt, C.c_char_p).value
        sp.camera_param.width, sp.camera_param.height = W, H
        sp.camera_param.frame_size = W * H * 2
        sp.camera_param.fps = 30
        sp.camera_param.timeout_ms_delay = 2000
        sp.usr_callback.iruvc_handle = self.uvc
        self._chk("start_stream", self._call(self.vid.start_stream, self.uvc, C.byref(sp)))
        # SDK-ul pastreaza pointeri catre aceste structuri; daca Python le elibereaza,
        # urmatorul apel crapa cu access violation (0xC0000005)
        self._keep = (vh, ch, sp)
        self.buf = (C.c_uint8 * (W * H * 2))()
        self._get = _vfn(P, P, P, C.c_int)(self.vid.frame_get)

    @staticmethod
    def _call(f, *a):
        return _vfn(*([P] * len(a)))(f)(*a)

    @staticmethod
    def _chk(what, rc):
        if rc != 0:
            raise RuntimeError(f"SDK Mini2: {what} a returnat {rc} (driver libusbK? Thermal Cam inchis?)")

    def read(self, retries=50):
        """Intoarce (timestamp perf_counter, cadru uint16 512x640)."""
        for _ in range(retries):
            if self._get(self.uvc, None, C.cast(self.buf, P), W * H * 2) == 0:
                ts = time.perf_counter()
                return ts, np.frombuffer(bytes(self.buf), dtype="<u2").reshape(H, W).copy()
        raise RuntimeError("Mini2: nu primesc cadre")

    def close(self):
        self._call(self.vid.stop_stream, self.uvc, C.byref(_StopParams()))
        self._call(self.vid.release, self.uvc, C.byref(self.dev))
        self._call(self.vid.close, self.uvc)
