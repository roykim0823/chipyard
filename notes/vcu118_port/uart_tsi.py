#!/usr/bin/env python3
"""uart_tsi.py - load an ELF into board DRAM over UART-TSI and run it.

A portable (Windows/Linux) reimplementation of the host side of
generators/testchipip/uart_tsi/uart_tsi. The original is Linux-only: it uses
termios and links against fesvr, whose tsi_t switches contexts with ucontext
(pthread as a fallback). A real board only needs a simple polling loop, so
this script implements the TSI and HTIF protocols directly.
Only dependency: pyserial.

  pip install pyserial

Find the port:
  Windows: Device Manager -> Ports (COM & LPT)   e.g. COM3
  Linux:   ls /dev/ttyUSB*                        e.g. /dev/ttyUSB1

  # 1. Check the link first: read one word, no load, no boot
  python uart_tsi.py --tty COM3 --baud 921600 --read 0x80000000

  # 2. Bare-metal program (console output comes back over HTIF)
  python uart_tsi.py --tty COM3 --baud 921600 mvin_mvout-baremetal

Exit codes: 0 success, 1 program exited non-zero, 2 port/response error,
130 interrupted (Ctrl+C).
"""

import argparse, os, struct, sys, time

try:
    import serial
except ImportError:
    sys.exit("pyserial 이 필요합니다:  pip install pyserial")

_WINDOWS = sys.platform.startswith("win")

# ── TSI protocol (fesvr/tsi.h, tsi.cc) ────────────────────────────────
TSI_CMD_READ, TSI_CMD_WRITE = 0, 1             # SAI_CMD_READ / SAI_CMD_WRITE
CHUNK_MAX_BYTES = 1024                         # tsi_t::chunk_max_size()
BATCH_MAX_BYTES = 64 * 1024                    # bytes per serial write (see Tsi.write)
MSIP_BASE = 0x2000000                          # tsi_t::reset()

# ── HTIF ──────────────────────────────────────────────────────────────
SYS_write, SYS_exit = 64, 93


class Tsi:
    """TSI over UART. Words are 32-bit little-endian.

    On Windows, every write must wait until the driver's output queue is
    empty (_drain); otherwise back-to-back writes can lose bytes.
    pyserial's flush() does wait, but polls in 50 ms steps, which is too
    slow per chunk, so _drain polls with a much shorter sleep instead.
    """

    def __init__(self, port, baud, timeout=10.0):
        self.ser = serial.Serial(port=port, baudrate=baud, bytesize=8,
                                 parity=serial.PARITY_NONE, stopbits=1,
                                 timeout=timeout, rtscts=False, dsrdtr=False,
                                 xonxoff=False)

    def close(self):
        # Flush before closing so the last write (e.g. MSIP) is actually sent.
        try: self.ser.flush()
        except Exception: pass
        try: self.ser.close()
        except Exception: pass

    def _drain(self, timeout=30.0):
        """Wait until the output queue is empty.

        Raises TimeoutError if it never empties (e.g. port unplugged).
        """
        if not _WINDOWS:
            self.ser.flush()         # POSIX: tcdrain(), no sleep loop
            return
        t0 = time.time()
        while self.ser.out_waiting:  # finer than pyserial's 50 ms (actual resolution depends on OS/Python)
            if time.time() - t0 > timeout:
                raise TimeoutError(
                    f"출력 큐가 {timeout:.0f}초 동안 비지 않습니다 "
                    f"(out_waiting={self.ser.out_waiting}). 포트 상태를 확인하세요.")
            time.sleep(0.0005)

    def _write_raw(self, data):
        """One serial write, then one drain."""
        self.ser.write(data)
        self._drain()

    @staticmethod
    def _pack(words):
        return b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words)

    def _send_words(self, words):
        self._write_raw(self._pack(words))

    def _recv_words(self, n):
        need = n * 4
        buf = bytearray()
        while len(buf) < need:
            got = self.ser.read(need - len(buf))
            if not got:
                raise TimeoutError(
                    f"타깃이 응답하지 않습니다 ({len(buf)}/{need} 바이트). "
                    "보드 리셋, 포트, baud 를 확인하세요.")
            buf += got
        return list(struct.unpack("<%dI" % n, bytes(buf)))

    @staticmethod
    def _split64(v):
        return [v & 0xFFFFFFFF, (v >> 32) & 0xFFFFFFFF]

    def write_chunk(self, addr, data):
        """Write data (bytes) to addr. Length must be a multiple of 4."""
        assert len(data) % 4 == 0
        nwords = len(data) // 4
        hdr = [TSI_CMD_WRITE] + self._split64(addr) + self._split64(nwords - 1)
        self._write_raw(self._pack(hdr) + data)   # header and data in one write

    def read_chunk(self, addr, nbytes):
        assert nbytes % 4 == 0
        nwords = nbytes // 4
        hdr = [TSI_CMD_READ] + self._split64(addr) + self._split64(nwords - 1)
        self._send_words(hdr)
        return b"".join(struct.pack("<I", w) for w in self._recv_words(nwords))

    def write(self, addr, data, progress=None):
        """Write data of any size.

        Splits it into TSI chunks (<= 1 KB) and sends up to 64 KB of chunks
        per serial write. The bytes on the wire are identical to sending
        chunks one by one; only the number of drains goes down.
        """
        off = 0
        while off < len(data):
            batch = bytearray()
            while off < len(data) and len(batch) < BATCH_MAX_BYTES:
                n = min(CHUNK_MAX_BYTES, len(data) - off)
                batch += self._pack([TSI_CMD_WRITE] + self._split64(addr + off)
                                    + self._split64(n // 4 - 1))
                batch += data[off:off + n]
                off += n
            self._write_raw(bytes(batch))
            if progress: progress(off)

    def read(self, addr, nbytes):
        out = bytearray()
        while len(out) < nbytes:
            n = min(CHUNK_MAX_BYTES, nbytes - len(out))
            out += self.read_chunk(addr + len(out), n)
        return bytes(out)

    def read_u64(self, addr):
        return struct.unpack("<Q", self.read_chunk(addr, 8))[0]

    def write_u64(self, addr, v):
        self.write_chunk(addr, struct.pack("<Q", v))

    def set_msip(self):
        """Wake hart0 (fesvr tsi_t::reset)."""
        self.write_chunk(MSIP_BASE, struct.pack("<I", 1))


# ── Minimal ELF64 reader ──────────────────────────────────────────────
class Elf:
    """Reads only what the loader needs: PT_LOAD segments (what to load)
    and the symbol table (tohost/fromhost addresses)."""

    def __init__(self, path):
        with open(path, "rb") as f:
            self.b = f.read()
        if self.b[:4] != b"\x7fELF" or self.b[4] != 2:
            raise ValueError(f"{path}: 64비트 ELF 가 아닙니다")
        if self.b[5] != 1:
            raise ValueError(f"{path}: 리틀엔디언이 아닙니다")
        (self.entry, phoff, shoff) = struct.unpack_from("<QQQ", self.b, 24)
        (phentsize, phnum, shentsize, shnum) = struct.unpack_from("<HHHH", self.b, 54)

        self.segments = []          # (paddr, bytes, memsz); p_paddr, as in fesvr load_elf
        for i in range(phnum):
            o = phoff + i * phentsize
            p_type, = struct.unpack_from("<I", self.b, o)
            if p_type != 1:         # PT_LOAD
                continue
            p_offset, _va, p_paddr, p_filesz, p_memsz = struct.unpack_from("<QQQQQ", self.b, o + 8)
            self.segments.append((p_paddr, self.b[p_offset:p_offset + p_filesz], p_memsz))

        self.symbols = {}
        secs = []
        for i in range(shnum):
            o = shoff + i * shentsize
            _nm, s_type, _fl, _ad, off, size, link, _inf, _al, entsz = \
                struct.unpack_from("<IIQQQQIIQQ", self.b, o)
            secs.append((s_type, off, size, link, entsz))
        for s_type, off, size, link, entsz in secs:
            if s_type not in (2, 11) or entsz == 0:   # SYMTAB, DYNSYM
                continue
            stroff = secs[link][1]
            for k in range(size // entsz):
                so = off + k * entsz
                st_name, _info, _other, _shndx, st_value, _sz = struct.unpack_from("<IBBHQQ", self.b, so)
                if st_name == 0:
                    continue
                end = self.b.index(b"\0", stroff + st_name)
                self.symbols[self.b[stroff + st_name:end].decode("latin1")] = st_value


def human(n):
    return f"{n/1048576:.1f} MB" if n >= 1048576 else f"{n/1024:.1f} KB"


ZERO_BLOCK = b"\0" * 65536      # .bss fill buffer, same size as a Tsi.write batch


def load(tsi, elf):
    """Load the ELF into DRAM.

    The memsz > filesz tail of a PT_LOAD segment (.bss) is not in the file,
    so it is zero-filled here, as fesvr's load_elf does. riscv-tests' crt.S
    does not clear .bss because simulator memory starts zeroed. Skipping this
    passes on spike/Verilator but fails on the board: static arrays assumed
    to be zero read garbage from DDR after power-up.
    (Seen with gemmini matmul, matmul_os, matmul_ws, matmul_spad.)
    """
    segs = []                       # (paddr, payload, bss_addr, bss_len)
    for addr, data, memsz in elf.segments:
        payload = data + b"\0" * ((-len(data)) % 4)
        tail = memsz - len(payload)
        segs.append((addr, payload, addr + len(payload),
                     ((tail + 3) & ~3) if tail > 0 else 0))

    total = sum(len(p) + n for _, p, _, n in segs)
    t0 = time.time()
    loaded = [0]

    def prog(cur):
        pct = 100.0 * (loaded[0] + cur) / total if total else 100.0
        el = time.time() - t0
        sys.stdout.write(f"\r  로딩 {pct:5.1f}%  ({el:.0f}초)")
        sys.stdout.flush()

    def zero_fill(addr, n):
        """Zero-fill a large .bss by reusing one buffer."""
        off = 0
        while off < n:
            k = min(len(ZERO_BLOCK), n - off)
            tsi.write(addr + off, ZERO_BLOCK[:k])
            off += k
            prog(off)

    for addr, payload, bss_addr, bss_len in segs:
        if payload:
            print(f"  세그먼트 0x{addr:x}-0x{addr+len(payload):x}  {human(len(payload))}")
            tsi.write(addr, payload, progress=prog)
            loaded[0] += len(payload)
            sys.stdout.write("\n")
        if not bss_len:
            continue
        print(f"  .bss    0x{bss_addr:x}-0x{bss_addr+bss_len:x}  {human(bss_len)} 0으로")
        zero_fill(bss_addr, bss_len)
        loaded[0] += bss_len
        sys.stdout.write("\n")
    print(f"  로딩 완료 {human(total)} / {time.time()-t0:.1f}초")


def htif_loop(tsi, tohost, fromhost, stall_warn=30.0):
    """Poll tohost; handle console output and exit.

    After stall_warn seconds without progress, report which side is stuck.
    Both look like a hang, but the causes are opposite:

      tohost == 0, quiet   : target sends no requests (computing or hung)
      fromhost != 0, quiet : target does not consume our response
                             (handshake out of sync)
    """
    pending = []
    last_event, warned = time.time(), False

    def idle(msg):
        nonlocal warned
        if not warned and time.time() - last_event > stall_warn:
            sys.stderr.write(f"\n[uart_tsi.py] {stall_warn:.0f}초째 진전 없음 — {msg}\n")
            sys.stderr.flush()
            warned = True

    while True:
        v = tsi.read_u64(tohost)
        if v == 0:
            if pending:
                fh = tsi.read_u64(fromhost)
                if fh == 0:
                    tsi.write_u64(fromhost, pending.pop(0))
                    last_event, warned = time.time(), False
                else:
                    idle(f"fromhost=0x{fh:x} 가 그대로입니다. "
                         "타깃이 우리 응답을 안 가져갑니다.")
            else:
                idle("tohost=0 입니다. 타깃이 아무 요청도 안 냅니다.")
            continue
        tsi.write_u64(tohost, 0)
        last_event, warned = time.time(), False

        # tohost = dev[63:56] | cmd[55:48] | payload[47:0]
        #   dev 0, cmd 0: syscall. Odd payload = exit (code = payload >> 1),
        #                 even payload = address of the syscall argument block
        #   dev 1, cmd 1: console putchar
        dev, cmd, payload = (v >> 56) & 0xFF, (v >> 48) & 0xFF, v & 0xFFFFFFFFFFFF
        if dev == 0 and cmd == 0 and payload & 1:   # exit (fesvr syscall_t::handle_syscall)
            sys.stdout.flush()
            return payload >> 1

        if dev == 1 and cmd == 1:                   # putchar; no response, same as fesvr
            sys.stdout.write(chr(payload & 0xFF)); sys.stdout.flush()
        elif dev == 0 and cmd == 0:                 # proxied syscall
            mm = tsi.read(payload, 64)              # argument block (magic_mem): n, a0, a1, a2, ...
            n, a0, a1, a2 = struct.unpack_from("<QQQQ", mm)
            if n == SYS_write and a0 in (1, 2):
                base = a1 & ~3              # TSI takes 4-byte aligned addresses only
                buf = tsi.read(base, (a1 - base + a2 + 3) & ~3)
                sys.stdout.write(buf[a1 - base:a1 - base + a2].decode("latin1", "replace"))
                sys.stdout.flush()
                ret = a2
            elif n == SYS_exit:
                return a0
            else:
                ret = -38                           # -ENOSYS
            tsi.write(payload, struct.pack("<Q", ret & 0xFFFFFFFFFFFFFFFF))
            pending.append(1)
        else:
            # No response: an unexpected fromhost would break the handshake.
            sys.stderr.write(f"\n[uart_tsi.py] 미구현 HTIF: dev={dev} cmd={cmd}\n")


def main():
    ap = argparse.ArgumentParser(
        description="UART-TSI 로 ELF 를 보드 DRAM 에 올리고 실행 결과(HTIF)를 출력합니다")
    ap.add_argument("binary", nargs="?", help="올릴 ELF (--read 를 쓸 때는 생략)")
    ap.add_argument("--tty", required=True, help="Windows: COM3 / Linux: /dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200,
                    help="비트스트림의 WithUARTTSIClient(initBaudRate=...) 값과 같아야 함 "
                         "(기본 115200, 예: 921600)")
    ap.add_argument("--read", metavar="ADDR",
                    help="ADDR 의 32비트 워드를 읽고 종료. 로딩·기동은 하지 않음. "
                         "보드 연결 확인용 (ADDR 는 4바이트 정렬)")
    a = ap.parse_args()

    # Validate arguments before opening the port.
    if a.read:
        try:
            rd_addr = int(a.read, 0)
        except ValueError:
            ap.error(f"주소 형식이 잘못됐습니다: {a.read!r}")
        if rd_addr % 4:
            ap.error(f"주소는 4바이트 정렬이어야 합니다: {a.read}")
    elif not a.binary:
        ap.error("binary 또는 --read 가 필요합니다")

    print(f"uart_tsi.py  포트 {a.tty}  {a.baud} baud")
    try:
        tsi = Tsi(a.tty, a.baud)
    except serial.SerialException as e:
        print(f"  포트를 열 수 없습니다: {e}")
        print("  포트 이름을 확인하고, 다른 프로그램(터미널 등)이 포트를 잡고 있지 않은지 보세요.")
        return 2
    try:
        if a.read:
            print(f"  [0x{rd_addr:x}] = 0x{struct.unpack('<I', tsi.read_chunk(rd_addr, 4))[0]:08x}")
            return 0

        elf = Elf(a.binary)
        print(f"  {os.path.basename(a.binary)}  entry 0x{elf.entry:x}")
        load(tsi, elf)

        th, fh = elf.symbols.get("tohost"), elf.symbols.get("fromhost")
        # The BootROM parks hart0 in WFI until its MSIP is set, then jumps to
        # DRAM_BASE (0x80000000). elf.entry is only printed, not used, so the
        # binary must start at 0x80000000.
        print("  hart0 기동")
        tsi.set_msip()
        if not th or not fh:
            print("  tohost/fromhost 심볼이 없습니다 — HTIF 없이 종료합니다.")
            print("  (HTIF 없이 빌드된 ELF 입니다. 출력은 보드의 UART 콘솔로 보세요.)")
            return 0
        print(f"  HTIF 부착  tohost=0x{th:x} fromhost=0x{fh:x}\n")
        code = htif_loop(tsi, th, fh)
        print(f"\n  종료 코드 {code}")
        return 0 if code == 0 else 1
    except KeyboardInterrupt:
        print("\n  중단됨"); return 130
    except TimeoutError as e:
        print(f"\n  {e}"); return 2
    finally:
        tsi.close()


if __name__ == "__main__":
    sys.exit(main())
