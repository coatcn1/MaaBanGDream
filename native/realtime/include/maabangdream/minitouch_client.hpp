#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace mbdr {

// minitouch 脚本发布端：把 TouchScriptCompiler 产出的整曲脚本通过 TCP
// 一次性写入设备上的 minitouch 进程。传输本身不参与时序——时序由脚本内
// 的 w(ait) 命令在 minitouch 进程内部按毫秒执行。
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
    // 一次性写入全部脚本字节；失败返回 false 并断开连接。
    bool publish(std::string_view bytes);
    // 读取最多 max_bytes；timeout_ms 内无数据返回空串。用于读取 minitouch
    // 连接后的握手头（v/^/$ 行）。
    std::string receive(std::size_t max_bytes, int timeout_ms);
    void close() noexcept;
    bool connected() const noexcept { return socket_ != kInvalidSocket; }

private:
    static constexpr std::uintptr_t kInvalidSocket = ~static_cast<std::uintptr_t>(0);
    std::uintptr_t socket_ = kInvalidSocket;
};

}  // namespace mbdr
