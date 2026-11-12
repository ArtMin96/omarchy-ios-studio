// iOS Studio hot reload: which of a patch's symbols the loader swaps, keeps or checks (pure name and byte rules,
// no Darwin API), so tests/cli/test_hot_reload_runtime.py can compile them for the host and check them against
// real Swift symbols. Included by loader.c only; not part of the module's public headers.
//
// Mangling references (swift/docs/ABI/Mangling.rst):
//   …Ma                      type metadata accessor
//   …v a|l u|O|o|p [Z]       addressor of a variable: 'a' mutable, 'l' immutable; 'u' unsafe ('O', 'o', 'p' are old
//                            owning/pinning kinds); 'Z' static. Swift emits `…vau` for every global and static
//                            stored property: it runs the `Wz` once token / `WZ` initializer and returns the storage.
//   …Tu                      async function pointer: { int32 relative offset to the function, uint32 context size }

#ifndef IOSSTUDIO_HOT_RELOAD_RULES_H
#define IOSSTUDIO_HOT_RELOAD_RULES_H

#include <stdint.h>
#include <string.h>

typedef enum {
    HR_SYM_NEW,     // the app doesn't define it: the patch's references keep the patch's own
    HR_SYM_SWAP,    // a function both define: the app's references now reach the patch's
    HR_SYM_ASYNC,   // an async function pointer both define: swapped like a function if the context still fits
    HR_SYM_KEEP,    // anything else both define: the patch's references now reach the app's
} hr_sym_kind;

static inline int hr_is_swift_symbol(const char *name) {
    return strncmp(name, "_$s", 3) == 0 || strncmp(name, "_$S", 3) == 0 || strncmp(name, "__T0", 4) == 0;
}

static inline int hr_ends_with(const char *name, size_t n, const char *suffix) {
    size_t k = strlen(suffix);
    return n >= k && memcmp(name + n - k, suffix, k) == 0;
}

// Functions that must stay the app's even though the patch has its own copy in __text: a metadata accessor
// (else new code would see the patch's copy of a type) and a global's addressor (it reaches its own storage and
// once token directly, so swapping it would give old and new code the patch's fresh, re-initialized storage).
static inline int hr_must_keep_function(const char *name) {
    if (!hr_is_swift_symbol(name)) return 0;
    size_t n = strlen(name);
    if (hr_ends_with(name, n, "Ma")) return 1;
    if (n > 4 && name[n - 1] == 'Z') n--;
    if (n < 4 || name[n - 3] != 'v' || (name[n - 2] != 'a' && name[n - 2] != 'l')) return 0;
    char kind = name[n - 1];
    return kind == 'u' || kind == 'O' || kind == 'o' || kind == 'p';
}

static inline int hr_is_async_function_pointer(const char *name) {
    return hr_is_swift_symbol(name) && hr_ends_with(name, strlen(name), "Tu");
}

// `in_text`: defined in the patch's __TEXT,__text; `in_app`: the app exports the same name.
static inline hr_sym_kind hr_classify(const char *name, int in_text, int in_app) {
    if (!in_app) return HR_SYM_NEW;
    if (hr_is_async_function_pointer(name)) return HR_SYM_ASYNC;
    if (in_text && !hr_must_keep_function(name)) return HR_SYM_SWAP;
    return HR_SYM_KEEP;
}

// The context size an async function pointer record asks callers to allocate (bytes 4..7, little endian on arm64).
static inline uint32_t hr_async_context_size(const void *record) {
    uint32_t size;
    memcpy(&size, (const uint8_t *)record + 4, sizeof size);
    return size;
}

// Callers in the app allocate the context size of the app's record and then call whatever the pointers reach, so
// a patched async function may use at most that much. A smaller one is fine.
static inline int hr_async_context_grew(const void *app_record, const void *patch_record) {
    return hr_async_context_size(patch_record) > hr_async_context_size(app_record);
}

#endif
