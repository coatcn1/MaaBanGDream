#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

enum { WIRE_SIZE = 128, VERSION = 1, QUERY = 1, STOP = 2, IO_TIMEOUT_MS = 2000 };
static volatile sig_atomic_t interrupted = 0;
static void on_signal(int signo) { (void)signo; interrupted = 1; }
static uint32_t get32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}
static uint64_t get64(const uint8_t *p) { return ((uint64_t)get32(p) << 32) | get32(p + 4); }
static void put32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v;
}
static void put64(uint8_t *p, uint64_t v) { put32(p, (uint32_t)(v >> 32)); put32(p + 4, (uint32_t)v); }
static int timestamp(clockid_t clock_id, uint64_t *out) {
    struct timespec ts;
    if (clock_gettime(clock_id, &ts) || ts.tv_sec < 0) return -1;
    *out = (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
    return 0;
}
static int64_t monotonic_ms(void) {
    uint64_t ns;
    if (timestamp(CLOCK_MONOTONIC, &ns)) return -1;
    return (int64_t)(ns / UINT64_C(1000000));
}
static int wait_ready(int fd, short events, int64_t deadline) {
    while (!interrupted) {
        int64_t now = monotonic_ms();
        if (now < 0 || now >= deadline) return -1;
        struct pollfd p = { .fd = fd, .events = events, .revents = 0 };
        int rc = poll(&p, 1, (int)(deadline - now));
        if (rc < 0 && errno == EINTR) continue;
        if (rc <= 0 || (p.revents & (POLLERR | POLLNVAL))) return -1;
        if (p.revents & events) return 0;
        if (p.revents & POLLHUP) return -1;
    }
    return -1;
}
static int transfer(int fd, uint8_t *buf, size_t length, int writing) {
    int64_t now = monotonic_ms();
    if (now < 0) return -1;
    int64_t deadline = now + IO_TIMEOUT_MS;
    size_t done = 0;
    while (done < length && !interrupted) {
        if (wait_ready(fd, writing ? POLLOUT : POLLIN, deadline)) return -1;
        ssize_t rc = writing ? send(fd, buf + done, length - done, MSG_NOSIGNAL | MSG_DONTWAIT)
                             : recv(fd, buf + done, length - done, MSG_DONTWAIT);
        if (rc < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (rc <= 0) return -1;
        done += (size_t)rc;
    }
    return done == length ? 0 : -1;
}
static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}
static int read_nonce(uint8_t *nonce, char *hex) {
    // 密钥不放 argv/启动输出；仅从有界 stdin 读取一次，超时则清理退出。
    int64_t now = monotonic_ms();
    if (now < 0) return -1;
    for (int i = 0; i < 33; ++i) {
        uint8_t c;
        if (wait_ready(STDIN_FILENO, POLLIN, now + IO_TIMEOUT_MS) || read(STDIN_FILENO, &c, 1) != 1) return -1;
        if (i == 32) { if (c != '\n') return -1; break; }
        if (hexval(c) < 0) return -1;
        hex[i] = (char)c;
    }
    hex[32] = 0;
    for (int i = 0; i < 16; ++i) nonce[i] = (uint8_t)((hexval(hex[i * 2]) << 4) | hexval(hex[i * 2 + 1]));
    return 0;
}
static int zero_range(const uint8_t *p, size_t n) {
    uint8_t diff = 0;
    for (size_t i = 0; i < n; ++i) diff |= p[i];
    return diff == 0;
}
static int equal_nonce(const uint8_t *a, const uint8_t *b) {
    uint8_t diff = 0;
    for (size_t i = 0; i < 16; ++i) diff |= a[i] ^ b[i];
    return diff == 0;
}
int main(int argc, char **argv) {
    int listener = -1, peer = -1, result = 1;
    uint8_t nonce[16] = {0}, request[WIRE_SIZE], response[WIRE_SIZE];
    char nonce_hex[33] = {0}, *end = NULL;
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa)); sa.sa_handler = on_signal; sigemptyset(&sa.sa_mask);
    if (sigaction(SIGTERM, &sa, NULL) || sigaction(SIGINT, &sa, NULL)) goto cleanup;
    if (argc != 3 || argv[1][0] < '1' || argv[1][0] > '9') goto cleanup;
    if (strlen(argv[2]) != 32) goto cleanup;
    for (int i = 0; i < 32; ++i) if (hexval(argv[2][i]) < 0) goto cleanup;
    for (const char *p = argv[1]; *p; ++p) if (*p < '0' || *p > '9') goto cleanup;
    errno = 0;
    uint64_t generation = strtoull(argv[1], &end, 10);
    if (errno || !end || *end || !generation || read_nonce(nonce, nonce_hex)) goto cleanup;
    struct sockaddr_un address;
    memset(&address, 0, sizeof(address)); address.sun_family = AF_UNIX;
    int name_len = snprintf(address.sun_path + 1, sizeof(address.sun_path) - 1,
                            "mbd.clock.v1.g%" PRIu64 ".n%s", generation, argv[2]);
    if (name_len < 1 || name_len >= (int)sizeof(address.sun_path) - 1) goto cleanup;
    listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (listener < 0 || bind(listener, (struct sockaddr *)&address,
        (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + (size_t)name_len)) || listen(listener, 1)) goto cleanup;
    // socket 属于本进程，既不扫描其他进程，也不 unlink 任何文件路径。
    printf("clock-rpc-v1 pid=%ld generation=%" PRIu64 " socket=%s\n", (long)getpid(), generation, address.sun_path + 1);
    fflush(stdout);
    int64_t now = monotonic_ms();
    if (now < 0 || wait_ready(listener, POLLIN, now + 10000)) goto cleanup;
    peer = accept4(listener, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (peer < 0) goto cleanup;
    close(listener); listener = -1;
    uint64_t last_seq = 0;
    while (!interrupted) {
        if (transfer(peer, request, WIRE_SIZE, 0)) goto cleanup;
        uint64_t rx_rt, rx_mono, tx_rt, tx_mono;
        if (timestamp(CLOCK_REALTIME, &rx_rt) || timestamp(CLOCK_MONOTONIC, &rx_mono)) goto cleanup;
        uint32_t kind = ((uint32_t)request[6] << 8) | request[7];
        uint64_t seq = get64(request + 16);
        if (get32(request) != UINT32_C(0x4d424443) || request[4] != 0 || request[5] != VERSION ||
            (kind != QUERY && kind != STOP) || get32(request + 8) != WIRE_SIZE ||
            get32(request + 12) != (uint32_t)getpid() || last_seq == UINT64_MAX || seq != last_seq + 1 ||
            get64(request + 24) != generation || !equal_nonce(request + 32, nonce) ||
            get32(request + 48) != (uint32_t)CLOCK_REALTIME || get32(request + 52) != (uint32_t)CLOCK_MONOTONIC ||
            !zero_range(request + 56, WIRE_SIZE - 56)) goto cleanup;
        last_seq = seq;
        memcpy(response, request, WIRE_SIZE); response[6] |= 0x80;
        put64(response + 64, rx_rt); put64(response + 80, rx_mono);
        // 顺序采样的误差由 host 显式容忍，不能把两个 clock_gettime 当同时发生。
        if (timestamp(CLOCK_REALTIME, &tx_rt) || timestamp(CLOCK_MONOTONIC, &tx_mono)) goto cleanup;
        put64(response + 72, tx_rt); put64(response + 88, tx_mono);
        if (transfer(peer, response, WIRE_SIZE, 1)) goto cleanup;
        if (kind == STOP) { result = 0; goto cleanup; }
    }
cleanup:
    if (peer >= 0) close(peer);
    if (listener >= 0) close(listener);
    memset(nonce, 0, sizeof(nonce)); memset(nonce_hex, 0, sizeof(nonce_hex));
    // EOF、超时、协议错误和信号均只终止本 helper，不向任何其他进程发信号。
    return interrupted ? 0 : result;
}
