/* A launcher whose only job is to have an identity.
 *
 * macOS guards ~/Documents with TCC, and a LaunchAgent has no grant there:
 * measured 2026-09-10, launchd could not list the repo, read .env or the
 * SQLite DB, or even exec deploy/run-daily.sh (exit 126), while the identical
 * script from Terminal ran fine. Terminal, VS Code and Claude Code each hold a
 * Documents-folder grant; launchd holds none.
 *
 * A grant has to attach to SOMETHING, and the only thing you can add by hand
 * in System Settings is a binary or an app bundle. Pointing that at /bin/bash
 * would hand full disk access to every background shell this Mac ever runs.
 * So: a real Mach-O inside a real .app bundle, which TCC can name on its own,
 * granted once, covering this job and nothing else. Children inherit the
 * responsible-process attribution, which is how the venv python underneath
 * ends up able to read the repo.
 *
 * It must be compiled rather than a shell script with a shebang -- the kernel
 * would exec /bin/bash for a script, and the grant would land on bash again,
 * which is the whole thing being avoided.
 *
 * Build with deploy/build-launcher.sh. SCRIPT_PATH is baked in at compile time
 * so the bundle carries no argument parsing and no config of its own.
 */
#include <stdlib.h>
#include <unistd.h>

#ifndef SCRIPT_PATH
#error "compile with -DSCRIPT_PATH=\"...\" -- see deploy/build-launcher.sh"
#endif

int main(int argc, char *argv[]) {
    /* /bin/bash, the script, then anything we were called with. The
     * passthrough is what makes `JobPipeDaily status` a free smoke test of the
     * whole chain: launchd -> bundle -> bash -> venv python -> the repo. */
    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (!args) return 127;
    args[0] = "/bin/bash";
    args[1] = SCRIPT_PATH;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = NULL;

    execv("/bin/bash", args);
    return 127;  /* only reached if execv failed */
}
