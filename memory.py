"""Dolphin memory access.

Windows (the supported target): pure ctypes + kernel32, no pip packages. We find Dolphin's emulated
MEM1 (24 MB) / MEM2 (64 MB) mappings inside its process and translate 0x80000000 / 0x90000000
addresses onto them. Other platforms fall back to dolphin-memory-engine if it is installed.

Every call is serialized with a lock because the GUI thread and the engine thread both use this.
"""
import sys
import threading


class MemoryAccessError(RuntimeError):
    """Dolphin isn't running, the game isn't running, or the address can't be read/written."""


MEM1_VIRT, MEM1_LEN = 0x80000000, 0x1800000
MEM2_VIRT, MEM2_LEN = 0x90000000, 0x4000000
MAPPING_MEM1, MAPPING_MEM2 = 0x2000000, 0x4000000   # sizes of Dolphin's mapped regions


def translate_address(address: int, size: int, mem1, mem2) -> int:
    """Wii virtual address -> address inside Dolphin's process."""
    if MEM1_VIRT <= address and address + size <= MEM1_VIRT + MEM1_LEN:
        if mem1 is None:
            raise MemoryAccessError("MEM1 is not available")
        return mem1 + (address - MEM1_VIRT)
    if MEM2_VIRT <= address and address + size <= MEM2_VIRT + MEM2_LEN:
        if mem2 is None:
            raise MemoryAccessError("MEM2 is not available")
        return mem2 + (address - MEM2_VIRT)
    raise MemoryAccessError(f"address 0x{address:08X} (+{size}) is outside MEM1/MEM2")


def choose_ram_regions(regions):
    """regions: [(base, size, valid)] of MEM_MAPPED regions. Returns (mem1_base, mem2_base) or None each.
    MEM1 must be a valid (committed) 0x2000000 region; MEM2 is the 0x4000000 region at MEM1 + 0x10000000
    if present, otherwise the first 0x4000000 region."""
    mem1 = next((b for b, s, ok in regions if s == MAPPING_MEM1 and ok), None)
    if mem1 is None:
        return None, None
    wanted = mem1 + 0x10000000
    mem2 = next((b for b, s, _ in regions if s == MAPPING_MEM2 and b == wanted), None)
    if mem2 is None:
        mem2 = next((b for b, s, _ in regions if s == MAPPING_MEM2), None)
    return mem1, mem2


class _WinBackend:
    NAMES = {"dolphin.exe", "dolphinqt2.exe", "dolphinwx.exe"}
    ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400   # VM_OPERATION | VM_READ | VM_WRITE | QUERY_INFORMATION
    MEM_MAPPED = 0x40000
    STILL_ACTIVE = 259

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ct = ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k = k
        V, SZ = ctypes.c_void_p, ctypes.c_size_t

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", SZ), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        class MemInfo(ctypes.Structure):
            _fields_ = [("BaseAddress", SZ), ("AllocationBase", SZ), ("AllocationProtect", wt.DWORD),
                        ("PartitionId", wt.WORD), ("RegionSize", SZ), ("State", wt.DWORD),
                        ("Protect", wt.DWORD), ("Type", wt.DWORD)]

        class WsEx(ctypes.Structure):
            _fields_ = [("VirtualAddress", SZ), ("VirtualAttributes", SZ)]

        self.ProcessEntry, self.MemInfo, self.WsEx = ProcessEntry, MemInfo, WsEx
        k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(ProcessEntry)]
        k.Process32FirstW.restype = k.Process32NextW.restype = wt.BOOL
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = wt.HANDLE
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.CloseHandle.restype = wt.BOOL
        k.VirtualQueryEx.argtypes = [wt.HANDLE, V, ctypes.POINTER(MemInfo), SZ]
        k.VirtualQueryEx.restype = SZ
        k.K32QueryWorkingSetEx.argtypes = [wt.HANDLE, ctypes.POINTER(WsEx), wt.DWORD]
        k.K32QueryWorkingSetEx.restype = wt.BOOL
        k.ReadProcessMemory.argtypes = [wt.HANDLE, V, V, SZ, ctypes.POINTER(SZ)]
        k.ReadProcessMemory.restype = wt.BOOL
        k.WriteProcessMemory.argtypes = [wt.HANDLE, V, V, SZ, ctypes.POINTER(SZ)]
        k.WriteProcessMemory.restype = wt.BOOL
        k.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        k.GetExitCodeProcess.restype = wt.BOOL
        self.wt = wt
        self.handle = None
        self.mem1 = self.mem2 = None

    def _find_pid(self):
        ct, k = self.ct, self.k
        snap = k.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == ct.c_void_p(-1).value:
            return None
        try:
            entry = self.ProcessEntry()
            entry.dwSize = ct.sizeof(entry)
            ok = k.Process32FirstW(snap, ct.byref(entry))
            while ok:
                if entry.szExeFile.lower() in self.NAMES:
                    return entry.th32ProcessID
                ok = k.Process32NextW(snap, ct.byref(entry))
        finally:
            k.CloseHandle(snap)
        return None

    def _scan(self, handle):
        ct, k = self.ct, self.k
        regions, addr, info = [], 0, self.MemInfo()
        while k.VirtualQueryEx(handle, addr, ct.byref(info), ct.sizeof(info)):
            base, size = info.BaseAddress, info.RegionSize
            if info.Type == self.MEM_MAPPED and size in (MAPPING_MEM1, MAPPING_MEM2):
                ws = self.WsEx(base, 0)
                valid = bool(k.K32QueryWorkingSetEx(handle, ct.byref(ws), ct.sizeof(ws)) and ws.VirtualAttributes & 1)
                regions.append((base, size, valid))
            nxt = base + size
            if size == 0 or nxt <= addr or nxt > 0x7FFFFFFFFFFF:
                break
            addr = nxt
        return regions

    def is_hooked(self) -> bool:
        if not self.handle:
            return False
        code = self.wt.DWORD()
        return bool(self.k.GetExitCodeProcess(self.handle, self.ct.byref(code))) and code.value == self.STILL_ACTIVE

    def hook(self) -> bool:
        self.unhook()
        pid = self._find_pid()
        if pid is None:
            return False
        handle = self.k.OpenProcess(self.ACCESS, False, pid)
        if not handle:
            raise MemoryAccessError("Found Dolphin but couldn't open it (try running both as the same user)")
        mem1, mem2 = choose_ram_regions(self._scan(handle))
        if mem1 is None:
            self.k.CloseHandle(handle)
            raise MemoryAccessError("Dolphin found, but no game memory yet (start the game)")
        self.handle, self.mem1, self.mem2 = handle, mem1, mem2
        return True

    def unhook(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
        self.handle = None
        self.mem1 = self.mem2 = None

    def read(self, address: int, size: int) -> bytes:
        ct = self.ct
        target = translate_address(address, size, self.mem1, self.mem2)
        buf = ct.create_string_buffer(size)
        done = ct.c_size_t(0)
        if not self.k.ReadProcessMemory(self.handle, target, buf, size, ct.byref(done)) or done.value != size:
            raise MemoryAccessError(f"read of {size} byte(s) at 0x{address:08X} failed")
        return buf.raw[:size]

    def write(self, address: int, data: bytes):
        ct = self.ct
        target = translate_address(address, len(data), self.mem1, self.mem2)
        buf = ct.create_string_buffer(bytes(data), len(data))
        done = ct.c_size_t(0)
        if not self.k.WriteProcessMemory(self.handle, target, buf, len(data), ct.byref(done)) or done.value != len(data):
            raise MemoryAccessError(f"write of {len(data)} byte(s) at 0x{address:08X} failed")


class _DmeBackend:
    """Non-Windows fallback using the dolphin-memory-engine package."""

    def __init__(self):
        try:
            import dolphin_memory_engine as dme
        except ImportError as e:
            raise MemoryAccessError("Windows is required (or install dolphin-memory-engine on other systems)") from e
        self.dme = dme

    def is_hooked(self):
        return bool(self.dme.is_hooked())

    def hook(self):
        try:
            self.dme.hook()
        except Exception:
            return False
        return bool(self.dme.is_hooked())

    def unhook(self):
        try:
            self.dme.un_hook()
        except Exception:
            pass

    def read(self, address, size):
        try:
            return bytes(self.dme.read_bytes(address, size))
        except Exception as e:
            raise MemoryAccessError(f"read of {size} byte(s) at 0x{address:08X} failed: {e}") from e

    def write(self, address, data):
        try:
            self.dme.write_bytes(address, bytes(data))
        except Exception as e:
            raise MemoryAccessError(f"write of {len(data)} byte(s) at 0x{address:08X} failed: {e}") from e


class DolphinMemory:
    def __init__(self):
        self._backend = None
        self._lock = threading.RLock()

    def _be(self):
        if self._backend is None:
            self._backend = _WinBackend() if sys.platform == "win32" else _DmeBackend()
        return self._backend

    def is_hooked(self) -> bool:
        with self._lock:
            try:
                return self._be().is_hooked()
            except MemoryAccessError:
                return False

    def hook(self) -> bool:
        """True when attached. False = Dolphin not running. Raises MemoryAccessError for other problems."""
        with self._lock:
            return self._be().hook()

    def unhook(self) -> None:
        with self._lock:
            try:
                self._be().unhook()
            except MemoryAccessError:
                pass

    def read(self, address: int, size: int) -> bytes:
        with self._lock:
            be = self._be()
            if not be.is_hooked():
                raise MemoryAccessError("Dolphin is not hooked")
            return be.read(address, size)

    def write(self, address: int, data: bytes) -> None:
        with self._lock:
            be = self._be()
            if not be.is_hooked():
                raise MemoryAccessError("Dolphin is not hooked")
            be.write(address, data)
