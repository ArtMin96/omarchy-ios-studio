"""A small Mach-O 64 reader for hot reload (stdlib only): thin arm64 files, or the arm64 slice of a fat file.

    m = MachO.load(path)               # or MachO(data)
    m.filetype, m.cputype, m.segments, m.sections, m.symbols, m.has_code_signature
    m.indirect_entries()               # [(section, index, symbol name | None)] for pointer and stub sections
    m.relocations(section)             # [Relocation] of a section (an MH_OBJECT's)
    m.exported()                       # {name: Symbol} of the external symbols defined in a section

Every read is bounds-checked and every count is capped, so a truncated or garbage file raises MachOError (a
ValueError) at once instead of reading past the end or looping. Only what the hot-reload checks need is decoded.
"""
from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
CPU_TYPE_ARM64 = 0x0100000C

MH_OBJECT = 0x1
MH_EXECUTE = 0x2
MH_DYLIB = 0x6
MH_BUNDLE = 0x8

LC_SYMTAB = 0x2
LC_DYSYMTAB = 0xB
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_SEGMENT_64 = 0x19
LC_CODE_SIGNATURE = 0x1D
LC_REQ_DYLD = 0x80000000

N_STAB = 0xE0
N_PEXT = 0x10
N_TYPE = 0x0E
N_EXT = 0x01
N_UNDF = 0x0
N_ABS = 0x2
N_SECT = 0xE
N_INDR = 0xA

SECTION_TYPE = 0xFF
S_NON_LAZY_SYMBOL_POINTERS = 0x6
S_LAZY_SYMBOL_POINTERS = 0x7
S_SYMBOL_STUBS = 0x8
S_LAZY_DYLIB_SYMBOL_POINTERS = 0x10
POINTER_SECTIONS = (S_NON_LAZY_SYMBOL_POINTERS, S_LAZY_SYMBOL_POINTERS, S_LAZY_DYLIB_SYMBOL_POINTERS)

INDIRECT_SYMBOL_LOCAL = 0x80000000
INDIRECT_SYMBOL_ABS = 0x40000000

ARM64_RELOC_ADDEND = 10

MAX_FILE = 512 * 1024 * 1024      # an app binary bigger than this isn't something we patch
MAX_CMDS = 4096
MAX_SECTS = 1024                  # per segment
MAX_SYMS = 4_000_000
MAX_INDIRECT = 4_000_000
MAX_RELOCS = 2_000_000
MAX_NAME = 64 * 1024              # longest symbol name read (Swift manglings stay far below)


class MachOError(ValueError):
    """Not a Mach-O file we can read (wrong magic, no arm64 slice, or truncated/garbage structures)."""


@dataclass(frozen=True)
class Section:
    index: int          # 1-based, as n_sect numbers them
    segname: str
    sectname: str
    addr: int
    size: int
    offset: int
    reloff: int
    nreloc: int
    flags: int
    reserved1: int
    reserved2: int

    @property
    def type(self) -> int:
        return self.flags & SECTION_TYPE

    @property
    def name(self) -> str:
        return f"{self.segname},{self.sectname}"


@dataclass(frozen=True)
class Segment:
    name: str
    vmaddr: int
    vmsize: int
    fileoff: int
    filesize: int
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class Symbol:
    index: int
    name: str
    type: int           # n_type
    sect: int           # n_sect (1-based; 0 = none)
    desc: int
    value: int

    @property
    def stab(self) -> bool:
        return bool(self.type & N_STAB)

    @property
    def ext(self) -> bool:
        return bool(self.type & N_EXT) and not self.stab

    @property
    def private_ext(self) -> bool:
        return bool(self.type & N_PEXT) and not self.stab

    @property
    def kind(self) -> int:
        return self.type & N_TYPE

    @property
    def defined(self) -> bool:
        return not self.stab and self.kind in (N_SECT, N_ABS, N_INDR)

    @property
    def undefined(self) -> bool:
        return not self.stab and self.kind == N_UNDF


@dataclass(frozen=True)
class Relocation:
    address: int        # offset in the section
    symbolnum: int      # a symbol index (extern) or a section number, or the addend of an ADDEND reloc
    pcrel: bool
    length: int         # log2 of the size
    extern: bool
    type: int
    symbol: str | None  # the target symbol's name for extern relocations
    addend: int = 0     # from a preceding ARM64_RELOC_ADDEND


@dataclass
class MachO:
    data: bytes
    base: int = 0                       # offset of the arm64 slice in a fat file
    cputype: int = 0
    filetype: int = 0
    flags: int = 0
    segments: list[Segment] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    indirect: list[int] = field(default_factory=list)
    load_dylibs: list[str] = field(default_factory=list)
    id_dylib: str | None = None
    code_signature: tuple[int, int] | None = None    # (dataoff, datasize)
    _end: int = 0
    _memo: dict = field(default_factory=dict, init=False, repr=False, compare=False)   # defined(), indirect_names()

    def __post_init__(self) -> None:
        if len(self.data) > MAX_FILE:
            raise MachOError("The file is too big to be an app binary.")
        self.base, self._end = _slice(self.data)
        self._parse()

    @classmethod
    def load(cls, path: str | Path) -> "MachO":
        p = Path(path)
        try:
            size = p.stat().st_size
            if size > MAX_FILE:
                raise MachOError(f"{p.name} is too big to be an app binary.")
            return cls(p.read_bytes())
        except OSError as exc:
            raise MachOError(f"Can't read {p.name}: {exc.strerror or exc}") from None

    # ---------------------------------------------------------------- reading
    def _unpack(self, fmt: str, off: int) -> tuple:
        size = struct.calcsize(fmt)
        start = self.base + off
        if off < 0 or start + size > self._end:
            raise MachOError("The Mach-O file is truncated.")
        return struct.unpack_from(fmt, self.data, start)

    def _bytes(self, off: int, size: int) -> bytes:
        start = self.base + off
        if off < 0 or size < 0 or start + size > self._end:
            raise MachOError("The Mach-O file is truncated.")
        return self.data[start:start + size]

    def _parse(self) -> None:
        magic, cputype, _sub, filetype, ncmds, sizeofcmds, flags, _res = self._unpack("<IiiIIIII", 0)
        if magic != MH_MAGIC_64:
            raise MachOError("Not a 64-bit Mach-O file.")
        if cputype != CPU_TYPE_ARM64:
            raise MachOError("Not an arm64 Mach-O file.")
        if ncmds > MAX_CMDS or 32 + sizeofcmds > self._end - self.base:
            raise MachOError("The Mach-O load commands are corrupt.")
        self.cputype, self.filetype, self.flags = cputype, filetype, flags
        off, end = 32, 32 + sizeofcmds
        symtab = dysymtab = None
        for _ in range(ncmds):
            if off + 8 > end:
                raise MachOError("The Mach-O load commands are corrupt.")
            cmd, cmdsize = self._unpack("<II", off)
            if cmdsize < 8 or cmdsize % 4 or off + cmdsize > end:
                raise MachOError("The Mach-O load commands are corrupt.")
            base_cmd = cmd & ~LC_REQ_DYLD
            if cmd == LC_SEGMENT_64:
                self._segment(off, cmdsize)
            elif cmd == LC_SYMTAB:
                symtab = self._unpack("<IIII", off + 8)
            elif cmd == LC_DYSYMTAB:
                dysymtab = self._unpack("<18I", off + 8)
            elif cmd == LC_CODE_SIGNATURE:
                self.code_signature = self._unpack("<II", off + 8)
            elif base_cmd in (LC_LOAD_DYLIB, LC_ID_DYLIB) or cmd in (0x18 | LC_REQ_DYLD, 0x1F | LC_REQ_DYLD):
                name = self._lc_str(off, cmdsize)
                if base_cmd == LC_ID_DYLIB:
                    self.id_dylib = name
                else:
                    self.load_dylibs.append(name)
            off += cmdsize
        if symtab:
            self._symtab(*symtab)
        if dysymtab:
            indirectsymoff, nindirect = dysymtab[12], dysymtab[13]
            if nindirect > MAX_INDIRECT:
                raise MachOError("The indirect symbol table is corrupt.")
            raw = self._bytes(indirectsymoff, nindirect * 4)
            self.indirect = list(struct.unpack(f"<{nindirect}I", raw))

    def _lc_str(self, off: int, cmdsize: int) -> str:
        (name_off,) = self._unpack("<I", off + 8)
        if name_off >= cmdsize:
            return ""
        raw = self._bytes(off + name_off, cmdsize - name_off)
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    def _segment(self, off: int, cmdsize: int) -> None:
        segname, vmaddr, vmsize, fileoff, filesize, _maxp, _initp, nsects, _fl = self._unpack("<16sQQQQiiII", off + 8)
        if nsects > MAX_SECTS or 72 + nsects * 80 > cmdsize:
            raise MachOError("A Mach-O segment is corrupt.")
        sects = []
        for i in range(nsects):
            (sectname, segn, addr, size, offset, _align, reloff, nreloc, flags, r1, r2,
             _r3) = self._unpack("<16s16sQQIIIIIIII", off + 72 + i * 80)
            sects.append(Section(len(self.sections) + 1, _cstr(segn), _cstr(sectname), addr, size, offset, reloff,
                                 nreloc, flags, r1, r2))
            self.sections.append(sects[-1])
        self.segments.append(Segment(_cstr(segname), vmaddr, vmsize, fileoff, filesize, tuple(sects)))

    def _symtab(self, symoff: int, nsyms: int, stroff: int, strsize: int) -> None:
        if nsyms > MAX_SYMS:
            raise MachOError("The symbol table is corrupt.")
        strings = self._bytes(stroff, strsize)
        raw = self._bytes(symoff, nsyms * 16)
        for i, (strx, ntype, nsect, ndesc, value) in enumerate(struct.iter_unpack("<IBBHQ", raw)):
            name = ""
            if 0 < strx < strsize:
                stop = strings.find(b"\0", strx, strx + MAX_NAME)
                name = strings[strx:stop if stop >= 0 else min(strsize, strx + MAX_NAME)].decode("utf-8", "replace")
            self.symbols.append(Symbol(i, name, ntype, nsect, ndesc, value))

    # ---------------------------------------------------------------- queries
    @property
    def has_code_signature(self) -> bool:
        return self.code_signature is not None

    def section(self, segname: str, sectname: str) -> Section | None:
        return next((s for s in self.sections if s.segname == segname and s.sectname == sectname), None)

    def section_by_index(self, index: int) -> Section | None:
        return self.sections[index - 1] if 0 < index <= len(self.sections) else None

    def section_data(self, sect: Section) -> bytes:
        if sect.type in (0x1, 0xC, 0x12):    # S_ZEROFILL, S_GB_ZEROFILL, S_THREAD_LOCAL_ZEROFILL: no file bytes
            return b"\0" * min(sect.size, MAX_FILE)
        return self._bytes(sect.offset, sect.size)

    def defined(self, ext_only: bool = True) -> Mapping[str, Symbol]:
        """Defined symbols by name (external ones only by default; private externs count as not external).
        Read once per object and cached (read-only), so a cached app binary answers every save in O(1)."""
        key = ("defined", ext_only)
        if key not in self._memo:
            out: dict[str, Symbol] = {}
            for s in self.symbols:
                if s.defined and s.name and (not ext_only or (s.ext and not s.private_ext)):
                    out.setdefault(s.name, s)
            self._memo[key] = MappingProxyType(out)
        return self._memo[key]

    def exported(self, segname: str | None = None, sectname: str | None = None) -> dict[str, Symbol]:
        """External defined symbols, optionally only those in one section (e.g. __TEXT,__text: the functions)."""
        out = {}
        for name, s in self.defined().items():
            if segname is None:
                out[name] = s
                continue
            sect = self.section_by_index(s.sect)
            if sect and sect.segname == segname and (sectname is None or sect.sectname == sectname):
                out[name] = s
        return out

    def symbol_name(self, index: int) -> str | None:
        return self.symbols[index].name if 0 <= index < len(self.symbols) else None

    def indirect_entries(self) -> list[tuple[Section, int, str | None]]:
        """Every entry of the symbol-pointer and stub sections with the symbol it binds (None for local/abs)."""
        out = []
        for sect in self.sections:
            if sect.type in POINTER_SECTIONS:
                width = 8
            elif sect.type == S_SYMBOL_STUBS:
                width = sect.reserved2
            else:
                continue
            if width <= 0:
                continue
            count = sect.size // width
            if count > MAX_INDIRECT:
                raise MachOError("A symbol pointer section is corrupt.")
            for i in range(count):
                k = sect.reserved1 + i
                if k >= len(self.indirect):
                    raise MachOError("The indirect symbol table is truncated.")
                idx = self.indirect[k]
                name = None if idx & (INDIRECT_SYMBOL_LOCAL | INDIRECT_SYMBOL_ABS) else self.symbol_name(idx)
                out.append((sect, i, name))
        return out

    def indirect_names(self, pointers_only: bool = True) -> frozenset[str]:
        """Names bound through pointer sections (__got, __la_symbol_ptr …; also stubs unless pointers_only).
        Cached per object like defined()."""
        key = ("indirect", pointers_only)
        if key not in self._memo:
            self._memo[key] = frozenset(n for sect, _i, n in self.indirect_entries()
                                        if n and (sect.type in POINTER_SECTIONS or not pointers_only))
        return self._memo[key]

    def relocations(self, sect: Section) -> list[Relocation]:
        """The section's relocations (object files keep them per section), ADDEND pairs folded into the next."""
        if sect.nreloc > MAX_RELOCS:
            raise MachOError("A relocation table is corrupt.")
        raw = self._bytes(sect.reloff, sect.nreloc * 8)
        out: list[Relocation] = []
        addend = 0
        for address, info in struct.iter_unpack("<iI", raw):
            symnum = info & 0xFFFFFF
            pcrel = bool((info >> 24) & 1)
            length = (info >> 25) & 3
            extern = bool((info >> 27) & 1)
            rtype = (info >> 28) & 0xF
            if rtype == ARM64_RELOC_ADDEND:
                addend = symnum - (1 << 24) if symnum & 0x800000 else symnum
                continue
            name = self.symbol_name(symnum) if extern else None
            out.append(Relocation(address, symnum, pcrel, length, extern, rtype, name, addend))
            addend = 0
        return out

    def symbols_in(self, sect: Section) -> list[Symbol]:
        """The named (non-stab) symbols defined in a section, by address."""
        return sorted((s for s in self.symbols if not s.stab and s.kind == N_SECT and s.sect == sect.index and s.name),
                      key=lambda s: (s.value, s.index))

    def symbol_range(self, sym: Symbol) -> tuple[int, int] | None:
        """[start, end) of a section-defined symbol as offsets in its section: up to the next symbol's address
        (or the section's end)."""
        sect = self.section_by_index(sym.sect)
        if not sect or sym.kind != N_SECT:
            return None
        nxt = [s.value for s in self.symbols_in(sect) if s.value > sym.value]
        end = min(nxt) if nxt else sect.addr + sect.size
        return sym.value - sect.addr, end - sect.addr


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _slice(data: bytes) -> tuple[int, int]:
    """(start, end) of the arm64 Mach-O in `data`: the whole file when thin, the arm64 slice of a fat file."""
    if len(data) < 8:
        raise MachOError("The file is too short to be a Mach-O file.")
    (magic_be,) = struct.unpack_from(">I", data, 0)
    if magic_be not in (FAT_MAGIC, FAT_MAGIC_64):
        return 0, len(data)
    (nfat,) = struct.unpack_from(">I", data, 4)
    if nfat > 64:
        raise MachOError("The fat header is corrupt.")
    width = 20 if magic_be == FAT_MAGIC else 32
    for i in range(nfat):
        off = 8 + i * width
        if off + width > len(data):
            raise MachOError("The fat header is truncated.")
        if magic_be == FAT_MAGIC:
            cputype, _sub, offset, size, _align = struct.unpack_from(">iiIII", data, off)
        else:
            cputype, _sub, offset, size, _align, _res = struct.unpack_from(">iiQQII", data, off)
        if cputype == CPU_TYPE_ARM64:
            if offset + size > len(data) or size < 32:
                raise MachOError("The arm64 slice is truncated.")
            return offset, offset + size
    raise MachOError("The fat file has no arm64 slice.")
