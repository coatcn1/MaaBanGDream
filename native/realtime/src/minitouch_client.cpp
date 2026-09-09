#include "maabangdream/minitouch_client.hpp"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#error "MinitouchClient 当前仅支持 Windows"
#endif

#include <cstring>

namespace mbdr {
namespace {

// 进程级 WSA 初始化：只做一次，进程退出时由系统回收。
bool ensure_wsa() {
    WSADATA data;
    static const int result = WSAStartup(MAKEWORD(2, 2), &data);
    return result == 0;
}

}  // namespace

MinitouchClient::MinitouchClient(MinitouchClient&& other) noexcept
    : socket_(other.socket_.exchange(
          kInvalidSocket, std::memory_order_acq_rel)) {
    const MinitouchPublishDiagnostics diagnostics =
        other.last_publish_diagnostics();
    last_payload_bytes_.store(
        diagnostics.payload_bytes, std::memory_order_relaxed);
    last_send_calls_.store(
        diagnostics.send_calls, std::memory_order_relaxed);
    last_sent_bytes_.store(
        diagnostics.sent_bytes, std::memory_order_relaxed);
    last_publish_success_.store(
        diagnostics.success, std::memory_order_relaxed);
}

MinitouchClient& MinitouchClient::operator=(MinitouchClient&& other) noexcept {
    if (this != &other) {
        close();
        socket_.store(
            other.socket_.exchange(
                kInvalidSocket, std::memory_order_acq_rel),
            std::memory_order_release);
        const MinitouchPublishDiagnostics diagnostics =
            other.last_publish_diagnostics();
        last_payload_bytes_.store(
            diagnostics.payload_bytes, std::memory_order_relaxed);
        last_send_calls_.store(
            diagnostics.send_calls, std::memory_order_relaxed);
        last_sent_bytes_.store(
            diagnostics.sent_bytes, std::memory_order_relaxed);
        last_publish_success_.store(
            diagnostics.success, std::memory_order_relaxed);
    }
    return *this;
}

MinitouchClient::~MinitouchClient() {
    close();
}

bool MinitouchClient::connect(const std::string& host, int port) {
    if (!ensure_wsa()) {
        return false;
    }
    close();
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    addrinfo* resolved = nullptr;
    if (getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints,
                    &resolved) != 0 || resolved == nullptr) {
        return false;
    }
    const auto descriptor = socket(resolved->ai_family, resolved->ai_socktype,
                                   resolved->ai_protocol);
    if (descriptor == INVALID_SOCKET) {
        freeaddrinfo(resolved);
        return false;
    }
    const int result = ::connect(
        descriptor, resolved->ai_addr,
        static_cast<int>(resolved->ai_addrlen));
    freeaddrinfo(resolved);
    if (result != 0) {
        closesocket(descriptor);
        return false;
    }
    // localhost/adb forward 正常发送应立即完成；100ms 上限确保 panic reset
    // 不会因对端停读而把 500ms 停止门槛无限拖长。
    const DWORD send_timeout_ms = 100;
    if (setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO,
                   reinterpret_cast<const char*>(&send_timeout_ms),
                   sizeof(send_timeout_ms)) != 0) {
        closesocket(descriptor);
        return false;
    }
    socket_.store(
        static_cast<std::uintptr_t>(descriptor),
        std::memory_order_release);
    return true;
}

bool MinitouchClient::publish(std::string_view bytes) {
    uint64_t send_calls = 0;
    uint64_t sent_bytes = 0;
    const auto record = [&](bool success) {
        last_payload_bytes_.store(
            static_cast<uint64_t>(bytes.size()), std::memory_order_relaxed);
        last_send_calls_.store(send_calls, std::memory_order_relaxed);
        last_sent_bytes_.store(sent_bytes, std::memory_order_relaxed);
        last_publish_success_.store(success, std::memory_order_release);
    };
    if (!connected()) {
        record(false);
        return false;
    }
    const auto handle = reinterpret_cast<SOCKET>(
        socket_.load(std::memory_order_acquire));
    std::size_t sent = 0;
    while (sent < bytes.size()) {
        ++send_calls;
        const int chunk = send(handle, bytes.data() + sent,
                               static_cast<int>(bytes.size() - sent), 0);
        if (chunk <= 0) {
            record(false);
            close();
            return false;
        }
        sent += static_cast<std::size_t>(chunk);
        sent_bytes += static_cast<uint64_t>(chunk);
    }
    record(true);
    return true;
}

MinitouchPublishDiagnostics
MinitouchClient::last_publish_diagnostics() const noexcept {
    MinitouchPublishDiagnostics result;
    result.payload_bytes =
        last_payload_bytes_.load(std::memory_order_relaxed);
    result.send_calls = last_send_calls_.load(std::memory_order_relaxed);
    result.sent_bytes = last_sent_bytes_.load(std::memory_order_relaxed);
    result.success = last_publish_success_.load(std::memory_order_acquire);
    return result;
}

std::string MinitouchClient::receive(std::size_t max_bytes, int timeout_ms) {
    if (!connected()) {
        return {};
    }
    const auto handle = reinterpret_cast<SOCKET>(
        socket_.load(std::memory_order_acquire));
    const DWORD timeout = timeout_ms < 0 ? 0 : static_cast<DWORD>(timeout_ms);
    setsockopt(handle, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    std::string buffer;
    buffer.resize(max_bytes);
    const int count = recv(handle, buffer.data(),
                           static_cast<int>(max_bytes), 0);
    if (count <= 0) {
        return {};
    }
    buffer.resize(static_cast<std::size_t>(count));
    return buffer;
}

void MinitouchClient::close() noexcept {
    const std::uintptr_t previous = socket_.exchange(
        kInvalidSocket, std::memory_order_acq_rel);
    if (previous != kInvalidSocket) {
        closesocket(reinterpret_cast<SOCKET>(previous));
    }
}

}  // namespace mbdr
