#include <stdio.h>
#include <sys/statvfs.h>

int main(int argc, char **argv) {
    const char *p = argc > 1 ? argv[1] : "/log";
    struct statvfs s;
    if (statvfs(p, &s) == 0) {
        unsigned long total_mb = (s.f_blocks * s.f_frsize) / (1024 * 1024);
        unsigned long free_mb = (s.f_bfree * s.f_frsize) / (1024 * 1024);
        unsigned long avail_mb = (s.f_bavail * s.f_frsize) / (1024 * 1024);
        printf("%s: Total=%lu MB, Free=%lu MB, Available=%lu MB, BlockSize=%lu B\n",
               p, total_mb, free_mb, avail_mb, (unsigned long)s.f_frsize);
    } else {
        perror("statvfs");
    }
    return 0;
}
