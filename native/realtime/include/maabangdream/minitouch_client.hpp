#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace mbdr {

struct MinitouchPublishDiagnostics {
    uint64_t payload_bytes = 0;
    uint64_t send_calls = 0;
    uint64_t sent_bytes = 0;
    bool success = false;
};

// minitouch 脚本发布端：支持把 TouchScriptCompiler 产出的滚动脚本块依次
// 写入设备进程。传输只负责保序；绝对时序由 PlaybackSession 的窗口与脚本内
// w(ait) 共同约束。
class MinitouchClient {
public:
    MinitouchClient() = default;
    MinitouchClient(const MinitouchClient&) = delete;
    MinitouchClient& operator=(const MinitouchClient&) = delete;
    MinitouchClient(MinitouchClient&& other) noexcept;
    MinitouchClient& operator=(MinitouchClient&& other) noexcept;
    ~MinitouchClient();

    // 连接设备上 minitouch 的 TCP 端口（由 adb forward 暴露到本机）。
    bool connect(const std::string& host, int port);
    // 完整写入一个脚本块；失败返回 false 并断开连接。
    bool publish(std::string_view bytes);
    // 读取最多 max_bytes；timeout_ms 内无数据返回空串。用于读取 minitouch
    // 连接后的握手头（v/^/$ 行）。
    std::string receive(std::size_t max_bytes, int timeout_ms);
    void close() noexcept;
    bool connected() const noexcept {
        return socket_.load(std::memory_order_acquire) != kInvalidSocket;
    }
    // 只返回最近一次 publish 的发送计数；这些原子快照不参与 socket
    // 保序、失败关闭或停止释放。
    MinitouchPublishDiagnostics last_publish_diagnostics() const noexcept;

private:
    static constexpr std::uintptr_t kInvalidSocket = ~static_cast<std::uintptr_t>(0);
    // 回读线程、发布线程和停止线程会并发观察/关闭同一套接字；原子交换
    // 避免 close 与 send/recv 之间产生 C++ 数据竞争。
    std::atomic<std::uintptr_t> socket_{kInvalidSocket};
    std::atomic<uint64_t> last_payload_bytes_{0};
    std::atomic<uint64_t> last_send_calls_{0};
    std::atomic<uint64_t> last_sent_bytes_{0};
    std::atomic<bool> last_publish_success_{false};
};

}  // namespace mbdr
