"""helper/iosstudio/features/hotreload/macho.py: the Mach-O reader on synthetic files (tests/support/hot_reload.py)
and small real objects (tests/fixtures/hot-reload/), and robustness against truncated and garbage input."""
from __future__ import annotations

import os
import random
import struct
import time
import unittest

from iosstudio.features.hotreload import macho
from support import FIXTURES
from support.hot_reload import (
    MH_DYLIB,
    MH_OBJECT,
    N_EXT,
    N_PEXT,
    N_SECT,
    N_UNDF,
    S_NON_LAZY_SYMBOL_POINTERS,
    S_SYMBOL_STUBS,
    Sect,
    Sym,
    app_and_patch,
    build_macho,
)

FIX = FIXTURES / "hot-reload"


def sample(**kw) -> bytes:
    text = Sect("__TEXT", "__text", b"\x1f\x20\x03\xd5" * 4, addr=0x100)
    const = Sect("__TEXT", "__const", b"\0" * 16, addr=0x200,
                 relocs=[(8, 2, False, 2, True, 1), (8, 3, False, 2, True, 0), (0, 0x000010, False, 2, False, 10),
                         (4, 3, False, 2, True, 0)])
    got = Sect("__DATA_CONST", "__got", b"\0" * 16, flags=S_NON_LAZY_SYMBOL_POINTERS, reserved1=0, addr=0x300)
    stubs = Sect("__TEXT", "__stubs", b"\0" * 24, flags=S_SYMBOL_STUBS, reserved1=2, reserved2=12, addr=0x400)
    syms = [Sym("_func_a", value=0x100), Sym("_func_b", value=0x104), Sym("_desc", sect=2, value=0x208),
            Sym("_target", type=N_UNDF | N_EXT, sect=0), Sym("_hidden", type=N_SECT | N_EXT | N_PEXT, value=0x108),
            Sym("_local", type=N_SECT, value=0x10C)]
    return build_macho([text, const, got, stubs], syms, indirect=[0, 0x80000000, 3, 1], **kw)


class Reader(unittest.TestCase):
    def test_sections_symbols_and_flags(self):
        m = macho.MachO(sample(filetype=MH_DYLIB, id_dylib="@rpath/patch-3.dylib", code_signature=True))
        self.assertEqual(m.filetype, macho.MH_DYLIB)
        self.assertEqual(m.cputype, macho.CPU_TYPE_ARM64)
        self.assertEqual(m.id_dylib, "@rpath/patch-3.dylib")
        self.assertTrue(m.has_code_signature)
        self.assertEqual([s.name for s in m.sections], ["__TEXT,__text", "__TEXT,__const", "__DATA_CONST,__got",
                                                        "__TEXT,__stubs"])
        self.assertEqual(set(m.exported("__TEXT", "__text")), {"_func_a", "_func_b"})   # not private/local/undef
        self.assertIn("_desc", m.exported())
        self.assertNotIn("_hidden", m.defined())
        self.assertIn("_hidden", m.defined(ext_only=False))
        target = next(s for s in m.symbols if s.name == "_target")
        self.assertTrue(target.undefined and target.ext)

    def test_indirect_entries_map_pointer_and_stub_sections(self):
        m = macho.MachO(sample())
        entries = [(s.sectname, i, n) for s, i, n in m.indirect_entries()]
        self.assertEqual(entries, [("__got", 0, "_func_a"), ("__got", 1, None), ("__stubs", 0, "_target"),
                                   ("__stubs", 1, "_func_b")])
        self.assertEqual(m.indirect_names(), {"_func_a"})
        self.assertEqual(m.indirect_names(pointers_only=False), {"_func_a", "_target", "_func_b"})

    def test_relocations_fold_addends(self):
        m = macho.MachO(sample())
        rel = m.relocations(m.section("__TEXT", "__const"))
        self.assertEqual([(r.address, r.type, r.symbol, r.addend) for r in rel],
                         [(8, 1, "_desc", 0), (8, 0, "_target", 0), (4, 0, "_target", 16)])
        desc = next(s for s in m.symbols if s.name == "_desc")
        self.assertEqual(m.symbol_range(desc), (8, 16))

    def test_fat_file_picks_arm64(self):
        m = macho.MachO(sample(fat=True))
        self.assertEqual(m.filetype, MH_OBJECT)
        self.assertIn("_func_a", m.exported())
        with self.assertRaisesRegex(macho.MachOError, "no arm64"):
            macho.MachO(sample(fat=True, cputype=0x01000007))

    def test_app_and_patch_helper(self):
        app, patch = app_and_patch(["_f"], ["_f", "_g"], ["_f", "_new"])
        a, p = macho.MachO(app), macho.MachO(patch)
        self.assertEqual(a.indirect_names(), {"_f"})
        self.assertEqual(set(p.exported("__TEXT", "__text")), {"_f", "_new"})

    def test_real_object(self):
        m = macho.MachO.load(FIX / "base-any.o")
        self.assertEqual(m.filetype, macho.MH_OBJECT)
        names = {s.name for s in m.symbols}
        self.assertIn("_$s3Fix4CardV4bodyQrvpQOMQ", names)
        body = m.defined(ext_only=False)["_$s3Fix4CardV4bodyQrvg"]      # hidden: built without -enable-testing
        self.assertTrue(body.private_ext)
        self.assertEqual(m.section_by_index(body.sect).name, "__TEXT,__text")
        sect = m.section("__TEXT", "__constg_swiftt")
        self.assertTrue(any(r.symbol and r.symbol.startswith("_symbolic") for r in m.relocations(sect)))


class Robustness(unittest.TestCase):
    def test_not_macho(self):
        for data in (b"", b"abc", b"\0" * 64, b"\x7fELF" + b"\0" * 60):
            with self.subTest(data=data[:4]):
                with self.assertRaises(macho.MachOError):
                    macho.MachO(data)

    def test_wrong_cpu(self):
        with self.assertRaisesRegex(macho.MachOError, "arm64"):
            macho.MachO(sample(cputype=0x01000007))

    def test_every_truncation_fails_cleanly(self):
        data = sample(filetype=MH_DYLIB, id_dylib="@rpath/x.dylib")
        for n in range(0, len(data) - 1, 7):
            try:
                m = macho.MachO(data[:n])
                m.indirect_entries()
                for s in m.sections:
                    m.relocations(s)
            except macho.MachOError:
                pass

    def test_garbage_never_hangs(self):
        rnd = random.Random(13)
        base = bytearray(sample())
        start = time.monotonic()
        for _ in range(400):
            data = bytearray(base)
            for _k in range(rnd.randint(1, 12)):
                data[rnd.randrange(len(data))] = rnd.randrange(256)
            try:
                m = macho.MachO(bytes(data))
                m.indirect_entries()
                for s in m.sections:
                    m.relocations(s)
            except macho.MachOError:
                pass
        self.assertLess(time.monotonic() - start, 10)

    def test_huge_counts_rejected(self):
        data = bytearray(sample())
        struct.pack_into("<I", data, 16, 10_000_000)          # ncmds
        with self.assertRaises(macho.MachOError):
            macho.MachO(bytes(data))

    def test_missing_file(self):
        with self.assertRaisesRegex(macho.MachOError, "Can't read"):
            macho.MachO.load(os.devnull + "-missing")


if __name__ == "__main__":
    unittest.main()
