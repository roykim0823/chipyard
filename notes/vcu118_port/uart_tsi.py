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

import serial

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
        # No port in the constructor: pyserial then leaves it closed until open().
        self.ser = serial.Serial(baudrate=baud, bytesize=8,
                                 parity=serial.PARITY_NONE, stopbits=1,
                                 timeout=timeout, rtscts=False, dsrdtr=False,
                                 xonxoff=False)
        self.ser.port = port

    def open(self):
        self.ser.open()

    def close(self):
        if not self.ser.is_open:
            return
        # Flush before closing so the last write (e.g. MSIP) is actually sent.
        # A failed flush is only reported, so that it does not replace the
        # exception that is already propagating when close() runs from __exit__.
        try:
            self.ser.flush()
        except serial.SerialException as e:
            sys.stderr.write(f"[uart_tsi.py] flush failed on close: {e}\n")
        finally:
            self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

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
                    f"output queue did not drain in {timeout:.0f} s "
                    f"(out_waiting={self.ser.out_waiting}). Check the port.")
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
                    f"no response from target ({len(buf)}/{need} bytes). "
                    "Check board reset, port and baud rate.")
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
            raise ValueError(f"{path}: not a 64-bit ELF")
        if self.b[5] != 1:
            raise ValueError(f"{path}: not little-endian")
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
        sys.stdout.write(f"\r  loading {pct:5.1f}%  ({el:.0f} s)")
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
            print(f"  segment 0x{addr:x}-0x{addr+len(payload):x}  {human(len(payload))}")
            tsi.write(addr, payload, progress=prog)
            loaded[0] += len(payload)
            sys.stdout.write("\n")
        if not bss_len:
            continue
        print(f"  .bss    0x{bss_addr:x}-0x{bss_addr+bss_len:x}  {human(bss_len)} zero-filled")
        zero_fill(bss_addr, bss_len)
        loaded[0] += bss_len
        sys.stdout.write("\n")
    print(f"  loaded {human(total)} in {time.time()-t0:.1f} s")


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
            sys.stderr.write(f"\n[uart_tsi.py] no progress for {stall_warn:.0f} s: {msg}\n")
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
                    idle(f"fromhost=0x{fh:x} is unchanged; "
                         "the target is not consuming our response.")
            else:
                idle("tohost=0; the target is not sending any request.")
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
            sys.stderr.write(f"\n[uart_tsi.py] unsupported HTIF: dev={dev} cmd={cmd}\n")


def aligned_addr(s):
    """argparse type for --read. A ValueError from int() is reported by
    argparse itself as "invalid aligned_addr value"."""
    addr = int(s, 0)
    if addr % 4:
        raise argparse.ArgumentTypeError(f"address must be 4-byte aligned: {s}")
    return addr


def read_word(tsi, addr):
    print(f"  [0x{addr:x}] = 0x{struct.unpack('<I', tsi.read_chunk(addr, 4))[0]:08x}")
    return 0


def run_elf(tsi, elf):
    """Load the ELF, wake hart0 and serve HTIF until the program exits."""
    load(tsi, elf)

    th, fh = elf.symbols.get("tohost"), elf.symbols.get("fromhost")
    # The BootROM parks hart0 in WFI until its MSIP is set, then jumps to
    # DRAM_BASE (0x80000000). elf.entry is only printed, not used, so the
    # binary must start at 0x80000000.
    print("  waking hart0")
    tsi.set_msip()
    if not th or not fh:
        print("  no tohost/fromhost symbols; exiting without HTIF.")
        print("  (The ELF was built without HTIF. Watch the board's UART console for output.)")
        return 0
    print(f"  HTIF attached  tohost=0x{th:x} fromhost=0x{fh:x}\n")
    code = htif_loop(tsi, th, fh)
    print(f"\n  exit code {code}")
    return 0 if code == 0 else 1


def main():
    ap = argparse.ArgumentParser(
        description="Load an ELF into board DRAM over UART-TSI and print its HTIF output")
    ap.add_argument("binary", nargs="?", help="ELF to load (omit with --read)")
    ap.add_argument("--tty", required=True, help="Windows: COM3 / Linux: /dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200,
                    help="must match WithUARTTSIClient(initBaudRate=...) in the bitstream "
                         "(default 115200, e.g. 921600)")
    ap.add_argument("--read", metavar="ADDR", type=aligned_addr,
                    help="read the 32-bit word at ADDR and exit, without loading or booting. "
                         "Use it to check the link (ADDR must be 4-byte aligned)")
    a = ap.parse_args()

    # Validate arguments and parse the ELF before opening the port.
    if a.read is None and not a.binary:
        ap.error("binary or --read is required")

    print(f"uart_tsi.py  port {a.tty}  {a.baud} baud")
    elf = None
    if a.read is None:
        try:
            elf = Elf(a.binary)
        except (OSError, ValueError) as e:
            ap.error(str(e))
        print(f"  {os.path.basename(a.binary)}  entry 0x{elf.entry:x}")

    tsi = Tsi(a.tty, a.baud)
    try:
        tsi.open()
    except serial.SerialException as e:
        print(f"  cannot open port: {e}")
        print("  Check the port name and that no other program (e.g. a terminal) holds it.")
        return 2
    with tsi:
        try:
            return read_word(tsi, a.read) if a.read is not None else run_elf(tsi, elf)
        except TimeoutError as e:
            print(f"\n  {e}")
            return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  interrupted")
        sys.exit(130)
