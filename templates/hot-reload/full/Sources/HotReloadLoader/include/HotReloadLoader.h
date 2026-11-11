// iOS Studio hot reload: the in-app loader. See templates/hot-reload/README.md in iOS Studio.
// Implemented only in debug builds (IOSSTUDIO_HOT_RELOAD); the Swift module calls these only then.
#ifndef IOSSTUDIO_HOT_RELOAD_LOADER_H
#define IOSSTUDIO_HOT_RELOAD_LOADER_H

#ifdef __cplusplus
extern "C" {
#endif

/// The runtime's protocol version (1). Exported in every configuration so the app's link can force the
/// loader in with `-u` (a release build has only this constant).
extern const int iosstudio_hot_reload_loader;

/// 1 when the loader is watching for patches in this process.
int iosstudio_hot_reload_is_active(void);

/// The number of patches applied since launch (0 before the first).
long iosstudio_hot_reload_generation(void);

#ifdef __cplusplus
}
#endif

#endif
