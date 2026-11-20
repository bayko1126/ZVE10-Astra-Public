// patch_writer.c - 纯内核系统调用，零 libc 依赖，零动态链接依赖
// 兼容任意 ARM Linux 内核 (2.6 ~ 6.x)

#define __NR_exit 1
#define __NR_read 3
#define __NR_write 4
#define __NR_open 5
#define __NR_close 6
#define __NR_lseek 19
#define __NR_fsync 118

#define O_RDONLY 00
#define O_RDWR   02
#define SEEK_SET 0

static inline long sys_open(const char *path, int flags, int mode) {
    register long r7 __asm__("r7") = __NR_open;
    register long r0 __asm__("r0") = (long)path;
    register long r1 __asm__("r1") = (long)flags;
    register long r2 __asm__("r2") = (long)mode;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static inline long sys_close(int fd) {
    register long r7 __asm__("r7") = __NR_close;
    register long r0 __asm__("r0") = (long)fd;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7) : "memory");
    return r0;
}

static inline long sys_read(int fd, void *buf, unsigned long count) {
    register long r7 __asm__("r7") = __NR_read;
    register long r0 __asm__("r0") = (long)fd;
    register long r1 __asm__("r1") = (long)buf;
    register long r2 __asm__("r2") = (long)count;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static inline long sys_write(int fd, const void *buf, unsigned long count) {
    register long r7 __asm__("r7") = __NR_write;
    register long r0 __asm__("r0") = (long)fd;
    register long r1 __asm__("r1") = (long)buf;
    register long r2 __asm__("r2") = (long)count;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static inline long sys_lseek(int fd, unsigned long offset, int whence) {
    register long r7 __asm__("r7") = __NR_lseek;
    register long r0 __asm__("r0") = (long)fd;
    register long r1 __asm__("r1") = (long)offset;
    register long r2 __asm__("r2") = (long)whence;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static inline long sys_fsync(int fd) {
    register long r7 __asm__("r7") = __NR_fsync;
    register long r0 __asm__("r0") = (long)fd;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7) : "memory");
    return r0;
}

static inline void sys_exit(int code) {
    register long r7 __asm__("r7") = __NR_exit;
    register long r0 __asm__("r0") = (long)code;
    __asm__ volatile("svc 0" : : "r"(r0), "r"(r7) : "memory");
    while (1);
}

static unsigned long parse_offset(const char *s) {
    unsigned long res = 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
        while (*s) {
            char c = *s++;
            if (c >= '0' && c <= '9') res = (res << 4) | (c - '0');
            else if (c >= 'a' && c <= 'f') res = (res << 4) | (c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') res = (res << 4) | (c - 'A' + 10);
            else break;
        }
    } else {
        while (*s >= '0' && *s <= '9') {
            res = res * 10 + (*s++ - '0');
        }
    }
    return res;
}

void _start(void) {
    register long *sp __asm__("sp");
    long argc = *sp;
    char **argv = (char **)(sp + 1);

    if (argc < 4) {
        sys_exit(1);
    }

    const char *target_path = argv[1];
    unsigned long off = parse_offset(argv[2]);
    const char *chunk_path = argv[3];

    long fd_in = sys_open(chunk_path, O_RDONLY, 0);
    if (fd_in < 0) {
        sys_exit(2);
    }

    char buf[128];
    long n = sys_read((int)fd_in, buf, sizeof(buf));
    sys_close((int)fd_in);
    if (n <= 0) {
        sys_exit(3);
    }

    long fd_out = sys_open(target_path, O_RDWR, 0);
    if (fd_out < 0) {
        sys_exit(4);
    }

    if (sys_lseek((int)fd_out, off, SEEK_SET) < 0) {
        sys_close((int)fd_out);
        sys_exit(5);
    }

    long written = sys_write((int)fd_out, buf, (unsigned long)n);
    if (written != n) {
        sys_close((int)fd_out);
        sys_exit(6);
    }

    sys_fsync((int)fd_out);
    sys_close((int)fd_out);

    sys_exit(0);
}
