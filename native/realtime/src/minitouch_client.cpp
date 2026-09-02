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
    : socket_(other.socket_) {
    other.socket_ = kInvalidSocket;
}

MinitouchClient& MinitouchClient::operator=(MinitouchClient&& other) noexcept {
    if (this != &other) {
        close();
        socket_ = other.socket_;
        other.socket_ = kInvalidSocket;
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
    socket_ = static_cast<std::uintptr_t>(descriptor);
    return true;
}

bool MinitouchClient::publish(std::string_view bytes) {
    if (!connected()) {
        return false;
    }
    const auto handle = reinterpret_cast<SOCKET>(socket_);
    std::size_t sent = 0;
    while (sent < bytes.size()) {
        const int chunk = send(handle, bytes.data() + sent,
                               static_cast<int>(bytes.size() - sent), 0);
        if (chunk <= 0) {
            close();
            return false;
        }
        sent += static_cast<std::size_t>(chunk);
    }
    return true;
}

std::string MinitouchClient::receive(std::size_t max_bytes, int timeout_ms) {
    if (!connected()) {
        return {};
    }
    const auto handle = reinterpret_cast<SOCKET>(socket_);
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
    if (socket_ != kInvalidSocket) {
        closesocket(reinterpret_cast<SOCKET>(socket_));
        socket_ = kInvalidSocket;
    }
}

}  // namespace mbdr
