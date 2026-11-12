// iOS Studio hot reload: the in-app loader (debug builds only).
//
// At launch it empties $HOME/Library/Caches/HotReload/, writes runtime.json there and watches the folder.
// When iOS Studio drops in patch-<seq>.dylib (a signed dylib holding the newly compiled versions of the
// changed source files), it dlopens it and rebinds symbols in two directions:
//
//   swap  functions in the patch's __TEXT,__text that the app also defines (except `…Ma` metadata
//         accessors and the `…vau` addressors of globals, see hr_rules.h), and async function pointers
//         (`…Tu`) whose context still fits: the app's symbol pointers (__got / __la_symbol_ptr, reached
//         through the indirect symbol table because the app is linked with -interposable) now point at the
//         patch, and words in the app's __DATA,__data (+ __objc_data) equal to the old address (class vtables;
//         an async method's slot holds its `…Tu`) are replaced.
//   keep  every other symbol the patch defines that the app defines too (type metadata, descriptors,
//         accessors, addressors, witness tables): the patch's own pointers now point at the app's, so new code
//         keeps using the original types and globals.
//
// A patch is refused before anything changes when one of its async functions needs a bigger context than the
// app's callers allocate (they read the size from the app's `…Tu`), or after HR_MAX_LOADED patches in a run.
//
// Then it writes ack-<seq>.json, prunes old patches and, on the main queue, bumps the generation, posts the
// local notification "dev.iosstudio.hot-reload" and logs one line to stderr (the app console) and os_log.
// Full description, protocol and limits: README.md of templates/hot-reload in iOS Studio.
//
// Rules this file keeps: nothing here may crash the app. Every Mach-O offset is bounds-checked, every
// loop is bounded, a rewrite that fails is counted and skipped, allocations are bounded by the images' own
// symbol tables. Work runs on a private serial queue; only the notification runs on the main queue.

#include "HotReloadLoader.h"

// In every configuration, so the app's `-u _iosstudio_hot_reload_loader` always resolves: SwiftPM's build
// (SwiftBuild, what xtool uses) applies the managed block's linker flags to release builds too, even though
// they are marked `.when(configuration: .debug)`. A release build has this constant and nothing else.
__attribute__((visibility("default"), used)) const int iosstudio_hot_reload_loader = 1;

#if defined(IOSSTUDIO_HOT_RELOAD) && defined(__APPLE__) && defined(__LP64__)

#include <CoreFoundation/CoreFoundation.h>
#include <dirent.h>
#include <dispatch/dispatch.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach-o/nlist.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <os/log.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "hr_rules.h"

#define HR_VERSION 1
#define HR_KEEP_BEHIND 3            // patches/acks with seq < newest - 3 are deleted
#define HR_MAX_DIR_ENTRIES 4096     // directory scans stop here
#define HR_MAX_SYMBOLS 1000000      // a patch with more external symbols is refused
#define HR_MAX_SCAN_BYTES (64u << 20) // vtable scan: at most this much of the app's data sections
#define HR_ERROR_MAX 512
#define HR_MAX_LOADED 100           // patches dlopened in one run (never closed); later ones are refused
#define HR_MAX_NAME 1024            // longer symbol names aren't looked up with a suffix (method descriptors)

_Static_assert(HR_VERSION == 1, "iosstudio_hot_reload_loader holds the protocol version");

static _Atomic int g_active;
static _Atomic long g_generation;   // published on the main queue
static long g_applied;              // loader queue only: patches applied so far
static long g_last_seq;             // loader queue only: newest seq looked at
static long g_loaded;               // loader queue only: patches dlopened so far (they stay loaded)
static long g_started;              // unix time of the start, for runtime.json
static char g_caches[PATH_MAX];
static char g_dir[PATH_MAX];
static dispatch_queue_t g_queue;
static dispatch_source_t g_source;

int iosstudio_hot_reload_is_active(void) { return atomic_load(&g_active); }
long iosstudio_hot_reload_generation(void) { return atomic_load(&g_generation); }

// MARK: - Small helpers

// hr_short_dlerror: dyld's message with the long container path cut down, so the reason survives the error cap.
// Every `path` becomes `file`, and of the `tried:` list only the first entry is kept (the others repeat it for
// /private/var and the cryptex prefixes): "dlopen(patch-1.dylib, 0x0002): tried: 'patch-1.dylib' (code
// signature invalid ...), ...". Pure C (no Apple headers), so tests/cli/test_hot_reload_runtime.py compiles it.
static void hr_short_dlerror(char *out, size_t size, const char *e, const char *path, const char *file) {
    if (!size) return;
    size_t plen = strlen(path), flen = strlen(file), n = 0;
    const char *tried = strstr(e, "tried: ");
    int depth = 0, in_list = 0;
    for (const char *c = e; *c && n + 1 < size; ) {
        if (tried && c == tried + 7) in_list = 1;
        if (plen && strncmp(c, path, plen) == 0) {
            for (size_t i = 0; i < flen && n + 1 < size; i++) out[n++] = file[i];
            c += plen;
            continue;
        }
        if (in_list && *c == '(') depth++;
        if (in_list && *c == ')' && --depth == 0) {
            out[n++] = ')';
            if (c[1] && n + 6 <= size) { memcpy(out + n, ", \xe2\x80\xa6", 5); n += 5; }
            break;
        }
        out[n++] = *c++;
    }
    out[n < size ? n : size - 1] = 0;
}
// END hr_short_dlerror

static double now_ms(void) {
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    return (double)mach_absolute_time() * tb.numer / tb.denom / 1e6;
}

static int path_in_dir(char *out, size_t size, const char *name) {
    int n = snprintf(out, size, "%s/%s", g_dir, name);
    return n > 0 && (size_t)n < size;
}

// Writes `data` to dir/name through dir/name.tmp and a rename, so readers never see half a file.
static int write_atomic(const char *name, const char *data, size_t len) {
    char path[PATH_MAX], tmp[PATH_MAX];
    if (!path_in_dir(path, sizeof path, name)) return 0;
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) return 0;
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) return 0;
    size_t done = 0;
    while (done < len) {
        ssize_t w = write(fd, data + done, len - done);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) break;
        done += (size_t)w;
    }
    close(fd);
    if (done != len || rename(tmp, path) != 0) {
        unlink(tmp);
        return 0;
    }
    return 1;
}

// JSON string body (no quotes) of `s`, cut to fit `size`.
static void json_escape(char *out, size_t size, const char *s) {
    size_t o = 0;
    for (; s && *s && o + 7 < size; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { out[o++] = '\\'; out[o++] = (char)c; }
        else if (c == '\n') { out[o++] = '\\'; out[o++] = 'n'; }
        else if (c < 0x20) o += (size_t)snprintf(out + o, size - o, "\\u%04x", c);
        else out[o++] = (char)c;
    }
    out[o] = 0;
}

// Parses "<prefix><digits>" at the start of `name`; returns the seq (>= 0) and the rest in *rest, or -1.
static long parse_seq(const char *name, const char *prefix, const char **rest) {
    size_t pl = strlen(prefix);
    if (strncmp(name, prefix, pl) != 0) return -1;
    const char *p = name + pl;
    long seq = 0;
    int digits = 0;
    for (; *p >= '0' && *p <= '9' && digits < 15; p++, digits++) seq = seq * 10 + (*p - '0');
    if (digits == 0) return -1;
    *rest = p;
    return seq;
}

// MARK: - Logging and the main-queue notification

typedef struct {
    int ok;
    long generation;
    char line[HR_ERROR_MAX + 160];
} hr_notice;

static void log_line(const char *line) {
    char buf[HR_ERROR_MAX + 200];
    int n = snprintf(buf, sizeof buf, "%s\n", line);
    if (n > 0) (void)!write(STDERR_FILENO, buf, (size_t)n < sizeof buf ? (size_t)n : sizeof buf - 1);
    os_log(OS_LOG_DEFAULT, "%{public}s", line);
}

static void notice_on_main(void *ctx) {
    hr_notice *n = ctx;
    if (n->ok) {
        atomic_store(&g_generation, n->generation);
        CFNotificationCenterPostNotification(CFNotificationCenterGetLocalCenter(),
                                             CFSTR("dev.iosstudio.hot-reload"), NULL, NULL, true);
    }
    log_line(n->line);
    free(n);
}

static void post_notice(int ok, long generation, const char *line) {
    hr_notice *n = calloc(1, sizeof *n);
    if (!n) return;
    n->ok = ok;
    n->generation = generation;
    snprintf(n->line, sizeof n->line, "%s", line);
    dispatch_async_f(dispatch_get_main_queue(), n, notice_on_main);
}

// MARK: - Mach-O images

typedef struct {
    const struct mach_header_64 *header;
    intptr_t slide;
    const struct nlist_64 *syms;
    uint32_t nsyms;
    const char *strs;
    uint32_t strsize;
    const uint32_t *indirect;
    uint32_t nindirect;
    uint32_t iextdef, nextdef;
    uint32_t text_ordinal;        // 1-based section number of __TEXT,__text, 0 if none
} hr_image;

static int seg_is(const char *segname, const char *want) { return strncmp(segname, want, 16) == 0; }

// Calls fn(ctx, seg, sect) for every section; stops early when fn returns 0. Returns 0 on a malformed header.
typedef int (*hr_section_fn)(void *ctx, const struct segment_command_64 *seg, const struct section_64 *sect,
                             uint32_t ordinal);

static int for_each_section(const struct mach_header_64 *h, hr_section_fn fn, void *ctx) {
    const uint8_t *p = (const uint8_t *)(h + 1);
    const uint8_t *end = p + h->sizeofcmds;
    uint32_t ordinal = 0;
    for (uint32_t i = 0; i < h->ncmds && i < 4096; i++) {
        if (p + sizeof(struct load_command) > end) return 0;
        const struct load_command *lc = (const struct load_command *)p;
        if (lc->cmdsize < sizeof(struct load_command) || p + lc->cmdsize > end) return 0;
        if (lc->cmd == LC_SEGMENT_64 && lc->cmdsize >= sizeof(struct segment_command_64)) {
            const struct segment_command_64 *seg = (const struct segment_command_64 *)lc;
            uint64_t room = (lc->cmdsize - sizeof *seg) / sizeof(struct section_64);
            const struct section_64 *s = (const struct section_64 *)(seg + 1);
            for (uint32_t k = 0; k < seg->nsects && k < room; k++) {
                ordinal++;
                if (fn && !fn(ctx, seg, &s[k], ordinal)) return 1;
            }
        }
        p += lc->cmdsize;
    }
    return 1;
}

static int find_text(void *ctx, const struct segment_command_64 *seg, const struct section_64 *s, uint32_t ord) {
    if (seg_is(seg->segname, SEG_TEXT) && strncmp(s->sectname, SECT_TEXT, 16) == 0) {
        *(uint32_t *)ctx = ord;
        return 0;
    }
    return 1;
}

static int parse_image(const struct mach_header_64 *h, intptr_t slide, hr_image *img) {
    memset(img, 0, sizeof *img);
    if (!h || h->magic != MH_MAGIC_64) return 0;
    img->header = h;
    img->slide = slide;
    const struct symtab_command *symtab = NULL;
    const struct dysymtab_command *dysym = NULL;
    const struct segment_command_64 *linkedit = NULL;
    const uint8_t *p = (const uint8_t *)(h + 1);
    const uint8_t *end = p + h->sizeofcmds;
    for (uint32_t i = 0; i < h->ncmds && i < 4096; i++) {
        if (p + sizeof(struct load_command) > end) return 0;
        const struct load_command *lc = (const struct load_command *)p;
        if (lc->cmdsize < sizeof(struct load_command) || p + lc->cmdsize > end) return 0;
        if (lc->cmd == LC_SYMTAB && lc->cmdsize >= sizeof *symtab) symtab = (const void *)lc;
        else if (lc->cmd == LC_DYSYMTAB && lc->cmdsize >= sizeof *dysym) dysym = (const void *)lc;
        else if (lc->cmd == LC_SEGMENT_64 && lc->cmdsize >= sizeof *linkedit &&
                 seg_is(((const struct segment_command_64 *)lc)->segname, SEG_LINKEDIT))
            linkedit = (const void *)lc;
        p += lc->cmdsize;
    }
    if (!symtab || !dysym || !linkedit) return 0;
    uint64_t lo = linkedit->fileoff, hi = linkedit->fileoff + linkedit->filesize;
    uint64_t symend = (uint64_t)symtab->symoff + (uint64_t)symtab->nsyms * sizeof(struct nlist_64);
    uint64_t strend = (uint64_t)symtab->stroff + symtab->strsize;
    uint64_t indend = (uint64_t)dysym->indirectsymoff + (uint64_t)dysym->nindirectsyms * sizeof(uint32_t);
    if (symtab->symoff < lo || symend > hi || symtab->stroff < lo || strend > hi) return 0;
    if (dysym->nindirectsyms && (dysym->indirectsymoff < lo || indend > hi)) return 0;
    uintptr_t base = (uintptr_t)slide + (uintptr_t)linkedit->vmaddr - (uintptr_t)linkedit->fileoff;
    img->syms = (const struct nlist_64 *)(base + symtab->symoff);
    img->nsyms = symtab->nsyms;
    img->strs = (const char *)(base + symtab->stroff);
    img->strsize = symtab->strsize;
    img->indirect = dysym->nindirectsyms ? (const uint32_t *)(base + dysym->indirectsymoff) : NULL;
    img->nindirect = img->indirect ? dysym->nindirectsyms : 0;
    if ((uint64_t)dysym->iextdefsym + dysym->nextdefsym <= symtab->nsyms) {
        img->iextdef = dysym->iextdefsym;
        img->nextdef = dysym->nextdefsym;
    }
    for_each_section(h, find_text, &img->text_ordinal);
    return 1;
}

static const char *sym_name(const hr_image *img, uint32_t index) {
    if (!img->syms || !img->strs || index >= img->nsyms) return NULL;
    uint32_t strx = img->syms[index].n_un.n_strx;
    if (strx == 0 || strx >= img->strsize) return NULL;
    const char *name = img->strs + strx;
    if (!memchr(name, 0, img->strsize - strx)) return NULL;
    return name;
}

// MARK: - Name tables (sorted, bsearch)

typedef struct {
    const char *name;
    void *to;       // the address pointers for `name` should hold
    void *from[2];  // vtable scan: words equal to these become `to` (NULL = unused)
    int hit;        // a pointer or vtable word now holds `to`
    int vhit;       // a vtable word was rewritten
    int async;      // an async function pointer (`…Tu`), not a function
} hr_entry;

typedef struct {
    hr_entry *items;
    size_t count, cap;
} hr_table;

static int table_push(hr_table *t, hr_entry e) {
    if (t->count == t->cap) {
        size_t cap = t->cap ? t->cap * 2 : 64;
        hr_entry *items = realloc(t->items, cap * sizeof *items);
        if (!items) return 0;
        t->items = items;
        t->cap = cap;
    }
    t->items[t->count++] = e;
    return 1;
}

static int entry_cmp(const void *a, const void *b) {
    return strcmp(((const hr_entry *)a)->name, ((const hr_entry *)b)->name);
}

static void table_sort(hr_table *t) {
    if (t->count > 1) qsort(t->items, t->count, sizeof *t->items, entry_cmp);
}

static hr_entry *table_find(const hr_table *t, const char *name) {
    if (!t->count) return NULL;
    hr_entry key = {.name = name};
    return bsearch(&key, t->items, t->count, sizeof *t->items, entry_cmp);
}

// What earlier patches installed in the app: name (owned) -> original and current address.
static hr_table g_installed;

// MARK: - Rewriting symbol pointers

typedef struct {
    const hr_image *img;
    const hr_table *table;
    size_t rewritten, failed;
} hr_rebind;

static int query_protection(uintptr_t addr, vm_prot_t *prot) {
    vm_address_t a = (vm_address_t)addr;
    vm_size_t size = 0;
    vm_region_basic_info_data_64_t info;
    mach_msg_type_number_t count = VM_REGION_BASIC_INFO_COUNT_64;
    mach_port_t object = MACH_PORT_NULL;
    kern_return_t kr = vm_region_64(mach_task_self(), &a, &size, VM_REGION_BASIC_INFO_64,
                                    (vm_region_info_t)&info, &count, &object);
    if (object != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), object);
    if (kr != KERN_SUCCESS || a > (vm_address_t)addr) return 0;
    *prot = info.protection;
    return 1;
}

static int is_pointer_section(const struct segment_command_64 *seg, const struct section_64 *s) {
    uint32_t type = s->flags & SECTION_TYPE;
    if (type != S_LAZY_SYMBOL_POINTERS && type != S_NON_LAZY_SYMBOL_POINTERS) return 0;
    if (strncmp(seg->segname, "__DATA", 6) != 0 && strncmp(seg->segname, "__AUTH", 6) != 0) return 0;
#if __has_feature(ptrauth_calls)
    // arm64e signs these; an unsigned pointer would fault on use. Third-party iOS apps are arm64.
    if (strncmp(s->sectname, "__auth", 6) == 0 || strncmp(seg->segname, "__AUTH", 6) == 0) return 0;
#endif
    return 1;
}

static int rebind_section(void *ctx, const struct segment_command_64 *seg, const struct section_64 *s,
                          uint32_t ord) {
    (void)ord;
    hr_rebind *r = ctx;
    const hr_image *img = r->img;
    if (!is_pointer_section(seg, s) || s->size == 0) return 1;
    uint64_t count = s->size / sizeof(void *);
    if ((uint64_t)s->reserved1 + count > img->nindirect) return 1;   // malformed: leave it alone
    void **slots = (void **)((uintptr_t)s->addr + (uintptr_t)img->slide);
    const uint32_t *indices = img->indirect + s->reserved1;
    int unprotected = 0, unprotect_failed = 0;
    vm_prot_t old = VM_PROT_READ;
    uintptr_t page = (uintptr_t)vm_page_size;
    uintptr_t start = (uintptr_t)slots & ~(page - 1);
    uintptr_t end = ((uintptr_t)slots + (uintptr_t)s->size + page - 1) & ~(page - 1);
    for (uint64_t i = 0; i < count; i++) {
        uint32_t index = indices[i];
        if (index & (INDIRECT_SYMBOL_LOCAL | INDIRECT_SYMBOL_ABS)) continue;
        const char *name = sym_name(img, index);
        if (!name) continue;
        hr_entry *e = table_find(r->table, name);
        if (!e || !e->to) continue;
        if (slots[i] == e->to) { e->hit = 1; continue; }
        if (!unprotected && !unprotect_failed) {
            vm_prot_t cur;
            if (!query_protection((uintptr_t)&slots[i], &cur)) cur = VM_PROT_READ;
            if (cur & VM_PROT_WRITE) {
                unprotected = -1;   // already writable: nothing to restore
            } else if (vm_protect(mach_task_self(), start, end - start, 0,
                                  VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY) == KERN_SUCCESS) {
                old = cur;
                unprotected = 1;
            } else {
                unprotect_failed = 1;
            }
        }
        if (unprotect_failed) { r->failed++; continue; }
        __atomic_store_n(&slots[i], e->to, __ATOMIC_RELEASE);
        e->hit = 1;
        r->rewritten++;
    }
    if (unprotected == 1) vm_protect(mach_task_self(), start, end - start, 0, old);
    return 1;
}

static void rebind_image(const hr_image *img, const hr_table *table, hr_rebind *out) {
    memset(out, 0, sizeof *out);
    out->img = img;
    out->table = table;
    if (img->indirect && table->count) for_each_section(img->header, rebind_section, out);
}

// MARK: - Vtables: words in the app's data equal to an old address

typedef struct { uintptr_t from; hr_entry *entry; } hr_pair;

static int pair_cmp(const void *a, const void *b) {
    uintptr_t x = ((const hr_pair *)a)->from, y = ((const hr_pair *)b)->from;
    return x < y ? -1 : x > y;
}

typedef struct {
    intptr_t slide;
    const hr_pair *pairs;
    size_t npairs;
    size_t replaced, budget;
} hr_scan;

static int scan_section(void *ctx, const struct segment_command_64 *seg, const struct section_64 *s, uint32_t ord) {
    (void)ord;
    hr_scan *sc = ctx;
    if (!seg_is(seg->segname, SEG_DATA) && !seg_is(seg->segname, "__DATA_DIRTY")) return 1;
    if (strncmp(s->sectname, SECT_DATA, 16) != 0 && strncmp(s->sectname, "__objc_data", 16) != 0) return 1;
    uint32_t type = s->flags & SECTION_TYPE;
    if (type == S_ZEROFILL || type == S_GB_ZEROFILL || type == S_THREAD_LOCAL_ZEROFILL) return 1;
    uint64_t size = s->size;
    if (size > sc->budget) size = sc->budget;
    sc->budget -= size;
    uintptr_t addr = ((uintptr_t)s->addr + (uintptr_t)sc->slide + 7) & ~(uintptr_t)7;
    uintptr_t stop = (uintptr_t)s->addr + (uintptr_t)sc->slide + (uintptr_t)size;
    for (uintptr_t *w = (uintptr_t *)addr; (uintptr_t)(w + 1) <= stop; w++) {
        uintptr_t v = *w;
        if (v < sc->pairs[0].from || v > sc->pairs[sc->npairs - 1].from) continue;
        hr_pair key = {.from = v};
        const hr_pair *hit = bsearch(&key, sc->pairs, sc->npairs, sizeof key, pair_cmp);
        if (!hit) continue;
        __atomic_store_n(w, (uintptr_t)hit->entry->to, __ATOMIC_RELEASE);
        hit->entry->hit = 1;
        hit->entry->vhit = 1;
        sc->replaced++;
    }
    return sc->budget > 0;
}

static size_t rewrite_vtables(const hr_image *app, hr_table *table) {
    hr_pair *pairs = calloc(table->count * 2 + 1, sizeof *pairs);
    if (!pairs) return 0;
    size_t n = 0;
    for (size_t i = 0; i < table->count; i++)
        for (int k = 0; k < 2; k++) {
            void *from = table->items[i].from[k];
            if (from && from != table->items[i].to) pairs[n++] = (hr_pair){(uintptr_t)from, &table->items[i]};
        }
    size_t replaced = 0;
    if (n) {
        qsort(pairs, n, sizeof *pairs, pair_cmp);
        hr_scan sc = {.slide = app->slide, .pairs = pairs, .npairs = n, .budget = HR_MAX_SCAN_BYTES};
        for_each_section(app->header, scan_section, &sc);
        replaced = sc.replaced;
    }
    free(pairs);
    return replaced;
}

// MARK: - Applying one patch

typedef struct {
    int ok;
    double ms;
    size_t swapped, kept, vtable, reverted;
    char error[HR_ERROR_MAX];
} hr_result;

static int find_app(hr_image *app) {
    uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; i++) {
        const struct mach_header *h = _dyld_get_image_header(i);
        if (h && h->magic == MH_MAGIC_64 && h->filetype == MH_EXECUTE)
            return parse_image((const struct mach_header_64 *)h, _dyld_get_image_vmaddr_slide(i), app);
    }
    return 0;
}

// The patch's image: the newest loaded image whose path ends in "/HotReload/<file>" (dyld may report
// /private/var for /var), or failing that the same file by device and inode.
static int find_patch(const char *path, const char *file, hr_image *patch) {
    char suffix[128];
    snprintf(suffix, sizeof suffix, "/HotReload/%s", file);
    size_t sl = strlen(suffix);
    struct stat want;
    int have_stat = stat(path, &want) == 0;
    uint32_t count = _dyld_image_count();
    for (uint32_t i = count; i-- > 0;) {
        const char *name = _dyld_get_image_name(i);
        const struct mach_header *h = _dyld_get_image_header(i);
        if (!name || !h || h->magic != MH_MAGIC_64 || h->filetype != MH_DYLIB) continue;
        size_t nl = strlen(name);
        struct stat st;
        int match = nl >= sl && strcmp(name + nl - sl, suffix) == 0;
        if (!match && have_stat && stat(name, &st) == 0) match = st.st_dev == want.st_dev && st.st_ino == want.st_ino;
        if (match) return parse_image((const struct mach_header_64 *)h, _dyld_get_image_vmaddr_slide(i), patch);
    }
    return 0;
}

typedef struct { uintptr_t lo, hi; intptr_t slide; int found; } hr_span;

static int span_section(void *ctx, const struct segment_command_64 *seg, const struct section_64 *s, uint32_t ord) {
    (void)seg, (void)ord;
    hr_span *sp = ctx;
    uintptr_t start = (uintptr_t)s->addr + (uintptr_t)sp->slide;
    if (sp->lo >= start && sp->hi <= start + (uintptr_t)s->size) {
        sp->found = 1;
        return 0;
    }
    return 1;
}

// [addr, addr + len) lies in one of the image's sections (so reading it can't fault).
static int in_image(const hr_image *img, const void *addr, size_t len) {
    hr_span sp = {.lo = (uintptr_t)addr, .hi = (uintptr_t)addr + len, .slide = img->slide};
    for_each_section(img->header, span_section, &sp);
    return sp.found;
}

// The app exports `name` (with its leading '_') followed by `suffix`.
static int app_has_suffixed(const char *name, const char *suffix) {
    char buf[HR_MAX_NAME];
    int n = snprintf(buf, sizeof buf, "%s%s", name + 1, suffix);
    return n > 0 && (size_t)n < sizeof buf && dlsym(RTLD_MAIN_ONLY, buf) != NULL;
}

static const hr_entry *find_suffixed(const hr_table *t, const char *name, const char *suffix) {
    char buf[HR_MAX_NAME];
    int n = snprintf(buf, sizeof buf, "%s%s", name, suffix);
    return n > 0 && (size_t)n < sizeof buf ? table_find(t, buf) : NULL;
}

typedef char *(*hr_demangle_fn)(const char *, size_t, char *, size_t *, uint32_t);

// The first `len` bytes of a Swift symbol as people read it (`Fix.load() async -> Swift.Int`), through the Swift
// runtime's swift_demangle when it is there, else the mangled name.
static void readable_name(char *out, size_t size, const char *name, size_t len) {
    static hr_demangle_fn demangle;
    static int looked;
    if (!looked) {
        demangle = (hr_demangle_fn)dlsym(RTLD_DEFAULT, "swift_demangle");
        looked = 1;
    }
    if (len && name[0] == '_') name++, len--;
    char *text = demangle ? demangle(name, len, NULL, NULL, 0) : NULL;
    if (text) snprintf(out, size, "%s", text);
    else snprintf(out, size, "%.*s", (int)(len < 200 ? len : 200), name);
    free(text);
}

static void apply_patch(long seq, hr_result *res) {
    memset(res, 0, sizeof *res);
    double t0 = now_ms();
    char file[64], path[PATH_MAX];
    snprintf(file, sizeof file, "patch-%ld.dylib", seq);
    hr_table swap = {0}, patch_table = {0}, app_table = {0};
    if (!path_in_dir(path, sizeof path, file)) {
        snprintf(res->error, sizeof res->error, "patch path too long");
        goto done;
    }
    if (g_loaded >= HR_MAX_LOADED) {
        snprintf(res->error, sizeof res->error, "Too many reloads in this run (%d). Press Run to start fresh.",
                 HR_MAX_LOADED);
        goto done;
    }
    void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        const char *e = dlerror();
        char why[HR_ERROR_MAX];
        hr_short_dlerror(why, sizeof why, e ? e : "unknown error", path, file);
        snprintf(res->error, sizeof res->error, "could not load the patch: %s", why);
        goto done;
    }
    g_loaded++;
    // Never dlclose: old code may still be running on another thread, and Swift registered its metadata.
    hr_image app, patch;
    if (!find_app(&app)) {
        snprintf(res->error, sizeof res->error, "could not read the app's own image");
        goto done;
    }
    if (!find_patch(path, file, &patch)) {
        snprintf(res->error, sizeof res->error, "loaded the patch but could not find its image");
        goto done;
    }
    if (!app.indirect) {
        snprintf(res->error, sizeof res->error, "the app has no symbol pointers to rebind (built without -interposable?)");
        goto done;
    }
    if (patch.nextdef > HR_MAX_SYMBOLS) {
        snprintf(res->error, sizeof res->error, "the patch exports too many symbols (%u)", patch.nextdef);
        goto done;
    }

    // Sort the patch's exported symbols (hr_rules.h): swap (new function bodies for the app), keep (the patch
    // uses the app's: types, metadata, accessors, globals) and, in patch_table, where the patch's own pointers
    // must point: keep -> the app's, everything else -> the patch's own. That last part matters: -interposable
    // links self-references as flat-namespace binds, which dyld resolves by searching the app (and older
    // patches) first, so the patch's calls between its own changed or new functions could land in old copies.
    // Nothing changes yet, so a patch refused here leaves the app as it was.
    size_t grew = 0;
    char grew_name[HR_ERROR_MAX / 2] = "";
    for (uint32_t i = patch.iextdef; i < patch.iextdef + patch.nextdef; i++) {
        const struct nlist_64 *nl = &patch.syms[i];
        if ((nl->n_type & N_STAB) || !(nl->n_type & N_EXT) || (nl->n_type & N_TYPE) != N_SECT) continue;
        const char *name = sym_name(&patch, i);
        if (!name || name[0] != '_') continue;
        void *in_patch = (void *)((uintptr_t)nl->n_value + (uintptr_t)patch.slide);
        void *in_app = dlsym(RTLD_MAIN_ONLY, name + 1);
        if (in_app == in_patch) continue;
        int in_text = patch.text_ordinal && nl->n_sect == patch.text_ordinal;
        hr_sym_kind kind = hr_classify(name, in_text, in_app != NULL);
        if (kind == HR_SYM_ASYNC && (!in_image(&patch, in_patch, 8) || !in_image(&app, in_app, 8)))
            kind = HR_SYM_KEEP;   // not a record we can read: leave it alone
        int ok = 1;
        switch (kind) {
        case HR_SYM_NEW:
            ok = table_push(&patch_table, (hr_entry){.name = name, .to = in_patch});
            break;
        case HR_SYM_ASYNC:
            // The app's callers allocate the context size of the app's record, then call through the swapped
            // pointers: a bigger context would overflow their allocation.
            if (hr_async_context_grew(in_app, in_patch)) {
                if (!grew++) readable_name(grew_name, sizeof grew_name, name, strlen(name) - 2);
                break;
            }
            ok = table_push(&swap, (hr_entry){.name = name, .to = in_patch, .from = {in_app, NULL}, .async = 1}) &&
                 table_push(&patch_table, (hr_entry){.name = name, .to = in_patch});
            break;
        case HR_SYM_SWAP:
            ok = table_push(&swap, (hr_entry){.name = name, .to = in_patch, .from = {in_app, NULL}}) &&
                 table_push(&patch_table, (hr_entry){.name = name, .to = in_patch});
            break;
        case HR_SYM_KEEP:
            if ((ok = table_push(&patch_table, (hr_entry){.name = name, .to = in_app}))) res->kept++;
            break;
        }
        if (!ok) {
            snprintf(res->error, sizeof res->error, "out of memory");
            goto done;
        }
    }
    if (grew == 1)
        snprintf(res->error, sizeof res->error, "%s now needs more memory per call than the running app reserved. "
                 "Press Run to rebuild.", grew_name);
    else if (grew)
        snprintf(res->error, sizeof res->error, "%s and %zu more async functions now need more memory per call "
                 "than the running app reserved. Press Run to rebuild.", grew_name, grew - 1);
    if (grew) goto done;
    table_sort(&swap);
    table_sort(&patch_table);

    // The patch's own pointers first, before anything can call its code.
    hr_rebind rb;
    rebind_image(&patch, &patch_table, &rb);
    if (rb.failed) {
        snprintf(res->error, sizeof res->error, "could not point %zu of the patch's references at the app", rb.failed);
        goto done;
    }

    // The app's table: this patch's functions, plus earlier patches' functions this one no longer has
    // (their file went back to how it was built), which go back to the app's own code.
    for (size_t i = 0; i < swap.count; i++) {
        hr_entry e = swap.items[i];
        hr_entry *prev = table_find(&g_installed, e.name);
        if (prev && prev->to != e.from[0]) e.from[1] = prev->to;
        if (!table_push(&app_table, e)) { snprintf(res->error, sizeof res->error, "out of memory"); goto done; }
    }
    size_t first_revert = app_table.count;
    for (size_t i = 0; i < g_installed.count; i++) {
        const hr_entry *old = &g_installed.items[i];
        if (table_find(&swap, old->name)) continue;
        hr_entry e = {.name = old->name, .to = old->from[0], .from = {old->to, NULL}, .async = old->async};
        if (!table_push(&app_table, e)) { snprintf(res->error, sizeof res->error, "out of memory"); goto done; }
    }
    res->reverted = app_table.count - first_revert;
    table_sort(&app_table);

    rebind_image(&app, &app_table, &rb);
    res->vtable = rewrite_vtables(&app, &app_table);
    // `swapped` counts the patch's functions the app now reaches. A class method (it has a method descriptor,
    // `…Tq`) is called through its class's vtable, so it counts only when a vtable word was rewritten for it or
    // its `…Tu`: a generic class's vtable is built at run time and is never found.
    for (size_t i = 0; i < app_table.count; i++) {
        const hr_entry *e = &app_table.items[i];
        if (e->async || !table_find(&swap, e->name)) continue;
        const hr_entry *tu = find_suffixed(&app_table, e->name, "Tu");
        int via_vtable = e->vhit || (tu && tu->vhit);
        if (app_has_suffixed(e->name, "Tq") ? via_vtable : (e->hit || (tu && tu->hit))) res->swapped++;
    }

    // Remember what is installed now (exactly this patch's functions), with owned names.
    hr_table installed = {0};
    for (size_t i = 0; i < swap.count; i++) {
        char *name = strdup(swap.items[i].name);
        if (!name || !table_push(&installed, (hr_entry){.name = name, .to = swap.items[i].to,
                                                         .from = {swap.items[i].from[0], NULL},
                                                         .async = swap.items[i].async})) {
            free(name);
            break;
        }
    }
    table_sort(&installed);
    for (size_t i = 0; i < g_installed.count; i++) free((void *)g_installed.items[i].name);
    free(g_installed.items);
    g_installed = installed;

    if (rb.failed) {
        snprintf(res->error, sizeof res->error, "could not rewrite %zu of the app's symbol pointers", rb.failed);
        goto done;
    }
    res->ok = 1;
done:
    free(swap.items);
    free(patch_table.items);
    free(app_table.items);
    res->ms = now_ms() - t0;
}

// MARK: - The folder

typedef enum { HR_PRUNE_ALL, HR_PRUNE_OLD } hr_prune_mode;

static void prune(hr_prune_mode mode, long newest) {
    DIR *d = opendir(g_dir);
    if (!d) return;
    struct dirent *ent;
    for (int n = 0; n < HR_MAX_DIR_ENTRIES && (ent = readdir(d)); n++) {
        const char *rest;
        long seq = parse_seq(ent->d_name, "patch-", &rest);
        if (seq < 0) seq = parse_seq(ent->d_name, "ack-", &rest);
        if (seq < 0) continue;
        if (mode == HR_PRUNE_ALL || seq < newest - HR_KEEP_BEHIND) {
            char path[PATH_MAX];
            if (path_in_dir(path, sizeof path, ent->d_name)) unlink(path);
        }
    }
    closedir(d);
}

static long newest_patch(void) {
    DIR *d = opendir(g_dir);
    if (!d) return -1;
    long newest = -1;
    struct dirent *ent;
    for (int n = 0; n < HR_MAX_DIR_ENTRIES && (ent = readdir(d)); n++) {
        const char *rest;
        long seq = parse_seq(ent->d_name, "patch-", &rest);
        if (seq > newest && strcmp(rest, ".dylib") == 0) newest = seq;
    }
    closedir(d);
    return newest;
}

static void write_ack(long seq, const hr_result *res, long generation) {
    char error[HR_ERROR_MAX * 2], body[HR_ERROR_MAX * 2 + 256], name[64];
    json_escape(error, sizeof error, res->error);
    int n = snprintf(body, sizeof body,
                     "{\"seq\": %ld, \"ok\": %s, \"ms\": %.1f, \"swapped\": %zu, \"kept\": %zu, \"vtable\": %zu, "
                     "\"reverted\": %zu, \"error\": %s%s%s, \"generation\": %ld}\n",
                     seq, res->ok ? "true" : "false", res->ms, res->swapped, res->kept, res->vtable, res->reverted,
                     res->ok ? "" : "\"", res->ok ? "null" : error, res->ok ? "" : "\"", generation);
    snprintf(name, sizeof name, "ack-%ld.json", seq);
    if (n > 0 && (size_t)n < sizeof body) write_atomic(name, body, (size_t)n);
}

static void check_folder(void) {
    long seq = newest_patch();
    if (seq <= g_last_seq) return;
    g_last_seq = seq;
    hr_result res;
    apply_patch(seq, &res);
    if (res.ok) g_applied++;
    write_ack(seq, &res, g_applied);
    prune(HR_PRUNE_OLD, seq);
    char line[HR_ERROR_MAX + 160];
    if (res.ok)
        snprintf(line, sizeof line, "[HotReload] patch %ld: swapped %zu function%s (%zu kept) in %.1f ms", seq,
                 res.swapped, res.swapped == 1 ? "" : "s", res.kept, res.ms);
    else
        snprintf(line, sizeof line, "[HotReload] patch %ld failed: %s", seq, res.error);
    post_notice(res.ok, g_applied, line);
}

static void watch_folder(void);

// Makes Library/Caches (it may have been cleared as a whole) and Library/Caches/HotReload. 0 with errno on failure.
static int make_folder(void) {
    mkdir(g_caches, 0755);
    return mkdir(g_dir, 0755) == 0 || errno == EEXIST;
}

static void write_runtime(void) {
    char body[160];
    int n = snprintf(body, sizeof body, "{\"version\": %d, \"pid\": %d, \"started\": %ld}\n", HR_VERSION,
                     (int)getpid(), g_started);
    if (n > 0) write_atomic("runtime.json", body, (size_t)n);
}

static void on_event(void *ctx) {
    (void)ctx;
    unsigned long flags = dispatch_source_get_data(g_source);
    if (flags & (DISPATCH_VNODE_DELETE | DISPATCH_VNODE_RENAME | DISPATCH_VNODE_REVOKE)) {
        // The folder went away (the app's caches were cleared): make it (and runtime.json, so iOS Studio still
        // sees a hot-reload build running) again and watch the new one.
        dispatch_source_cancel(g_source);
        dispatch_release(g_source);
        g_source = NULL;
        if (make_folder()) write_runtime();
        watch_folder();
        return;
    }
    check_folder();
}

static void on_cancel(void *ctx) { close((int)(intptr_t)ctx); }

static void watch_folder(void) {
    int fd = open(g_dir, O_EVTONLY | O_CLOEXEC);
    if (fd < 0) {
        atomic_store(&g_active, 0);
        return;
    }
    g_source = dispatch_source_create(DISPATCH_SOURCE_TYPE_VNODE, (uintptr_t)fd,
                                      DISPATCH_VNODE_WRITE | DISPATCH_VNODE_DELETE | DISPATCH_VNODE_RENAME |
                                          DISPATCH_VNODE_REVOKE,
                                      g_queue);
    if (!g_source) {
        close(fd);
        atomic_store(&g_active, 0);
        return;
    }
    dispatch_set_context(g_source, (void *)(intptr_t)fd);
    dispatch_source_set_event_handler_f(g_source, on_event);
    dispatch_source_set_cancel_handler_f(g_source, on_cancel);
    dispatch_resume(g_source);
    atomic_store(&g_active, 1);
    check_folder();   // a patch that arrived before the watch started
}

static void start(void *ctx) {
    (void)ctx;
    const char *home = getenv("HOME");
    if (!home || !*home) return;
    if (snprintf(g_caches, sizeof g_caches, "%s/Library/Caches", home) >= (int)sizeof g_caches) return;
    if (snprintf(g_dir, sizeof g_dir, "%s/HotReload", g_caches) >= (int)sizeof g_dir) return;
    g_started = (long)time(NULL);
    if (!make_folder()) {
        char line[PATH_MAX + 64];
        snprintf(line, sizeof line, "[HotReload] off: could not create %s (%s)", g_dir, strerror(errno));
        post_notice(0, 0, line);
        return;
    }
    prune(HR_PRUNE_ALL, 0);   // before runtime.json, so no patch pushed for this run is deleted
    write_runtime();
    watch_folder();
}

__attribute__((constructor)) static void iosstudio_hot_reload_init(void) {
    g_last_seq = 0;   // seqs start at 1
    dispatch_queue_attr_t attr = dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL, QOS_CLASS_UTILITY, 0);
    g_queue = dispatch_queue_create("dev.iosstudio.hot-reload", attr);
    // Set up off the launch path: the folder work must not slow the app's start.
    dispatch_async_f(g_queue, NULL, start);
}

#endif
