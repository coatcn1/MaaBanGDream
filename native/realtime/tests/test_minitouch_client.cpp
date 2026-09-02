#include <winsock2.h>
#include <ws2tcpip.h>

#include <atomic>
#include <chrono>
#include <string>
#include <thread>

#include "maabangdream/minitouch_client.hpp"
#include "test_macros.hpp"

namespace {

using namespace mbdr;

void test_publish_unconnected_fails() {
    MinitouchClient client;
    CHECK(!client.connected());
    CHECK(!client.publish("d 0 1 2 3\n"));
}

void test_loopback_publish_delivers_exact_bytes() {
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        return;  // 网络栈不可用时跳过，避免假失败。
    }
    const SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    CHECK(listener != INVALID_SOCKET);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    CHECK(bind(listener, reinterpret_cast<sockaddr*>(&address),
               sizeof(address)) == 0);
    CHECK(listen(listener, 1) == 0);
    socklen_t length = sizeof(address);
    CHECK(getsockname(listener, reinterpret_cast<sockaddr*>(&address),
                      &length) == 0);
    const int port = ntohs(address.sin_port);

    MinitouchClient client;
    CHECK(client.connect("127.0.0.1", port));
    CHECK(client.connected());

    timeval timeout{2, 0};
    setsockopt(listener, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    const SOCKET accepted = accept(listener, nullptr, nullptr);
    CHECK(accepted != INVALID_SOCKET);
    // 真实 minitouch 在连接建立后立即下发握手头，客户端必须先读头再发脚本。
    const std::string header = "v 1\n^ 10 1280 720 255\n$ 1234\n";
    CHECK(send(accepted, header.data(), static_cast<int>(header.size()), 0)
          == static_cast<int>(header.size()));
    CHECK_EQ(client.receive(64, 500), header);

    const std::string payload = "d 0 10 20 50\nc\nw 12\nu 0\nc\n";
    CHECK(client.publish(payload));

    std::string received(payload.size(), '\0');
    int total = 0;
    while (total < static_cast<int>(payload.size())) {
        const int chunk = recv(accepted, received.data() + total,
                               static_cast<int>(payload.size()) - total, 0);
        if (chunk <= 0) {
            break;
        }
        total += chunk;
    }
    CHECK_EQ(received.substr(0, total), payload.substr(0, total));
    closesocket(accepted);
    closesocket(listener);
    client.close();
    CHECK(!client.connected());
}

void test_blocked_publish_is_bounded_and_close_safe() {
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        return;
    }
    const SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    CHECK(listener != INVALID_SOCKET);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    CHECK(bind(listener, reinterpret_cast<sockaddr*>(&address),
               sizeof(address)) == 0);
    CHECK(listen(listener, 1) == 0);
    socklen_t length = sizeof(address);
    CHECK(getsockname(listener, reinterpret_cast<sockaddr*>(&address),
                      &length) == 0);

    MinitouchClient client;
    CHECK(client.connect("127.0.0.1", ntohs(address.sin_port)));
    const SOCKET accepted = accept(listener, nullptr, nullptr);
    CHECK(accepted != INVALID_SOCKET);
    int receive_buffer = 1024;
    setsockopt(accepted, SOL_SOCKET, SO_RCVBUF,
               reinterpret_cast<const char*>(&receive_buffer),
               sizeof(receive_buffer));

    // 对端完全不读时制造 send 背压；停止线程并发 close 后，发布必须在
    // SO_SNDTIMEO 约束下退出，不能把 Python 的 500ms 释放门槛拖死。
    const std::string payload(32 * 1024 * 1024, 'x');
    std::atomic<bool> publish_finished{false};
    const auto started = std::chrono::steady_clock::now();
    std::thread publisher([&]() {
        client.publish(payload);
        publish_finished.store(true, std::memory_order_release);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    client.close();
    publisher.join();
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started);

    CHECK(publish_finished.load(std::memory_order_acquire));
    CHECK(elapsed.count() < 1000);
    CHECK(!client.connected());
    closesocket(accepted);
    closesocket(listener);
}

}  // namespace

int run_minitouch_client_tests() {
    test_publish_unconnected_fails();
    test_loopback_publish_delivers_exact_bytes();
    test_blocked_publish_is_bounded_and_close_safe();
    return 0;
}
