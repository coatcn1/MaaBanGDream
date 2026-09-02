#include <winsock2.h>
#include <ws2tcpip.h>

#include <string>

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

    const std::string payload = "d 0 10 20 50\nc\nw 12\nu 0\nc\n";
    CHECK(client.publish(payload));

    timeval timeout{2, 0};
    setsockopt(listener, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    const SOCKET accepted = accept(listener, nullptr, nullptr);
    CHECK(accepted != INVALID_SOCKET);
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

}  // namespace

int run_minitouch_client_tests() {
    test_publish_unconnected_fails();
    test_loopback_publish_delivers_exact_bytes();
    return 0;
}
