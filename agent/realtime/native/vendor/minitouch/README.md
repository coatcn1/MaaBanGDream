# minitouch ver.EvATive7 预编译二进制

来源：<https://github.com/EvATive7/minitouch>（Apache License 2.0）

- 发布包：<https://github.com/EvATive7/minitouch/releases/latest/download/minitouch.zip>
- 选用原因：MaaFw 自带的 minitouch 是 openatx minitouch-prebuilt 版本，
  支持 `w <ms>` 与 `-f/-i`，但**不含逐命令 `jlog` 耗时回读**；本项目的
  分类型延迟自校准（`LatencyCalibrator`）依赖 jlog，因此固定版本化
  EvATive7 fork 的二进制。
- 协议：连接后回发 `v` / `^` / `$` 握手行，之后每条命令回发
  `jlog {"st":..,"et":..,"c":..,"cmd":..}`；`w <ms>` 在设备端 usleep。

## 文件清单（SHA-256）

| 文件 | SHA-256 |
| --- | --- |
| arm64-v8a/minitouch | 08ACF564CAC475788B57C2A8C7D1CE6FFB465FDE1294BBDAFBFDE65595879BF3 |
| armeabi-v7a/minitouch | E69F95CA091EC2C8921589C5B3C17A6C7C5F281BBE94239C70CE9CAF63116AAE |
| riscv64/minitouch | B124B1819922DFC29EB078D23E4346B57619D357D94C4B7152D11720FD23E8CA |
| x86/minitouch | AD6DD1A4C7363AA9797D4D2A5BF3069C8247C73EEA0E100182BAD603645E5F2B |
| x86_64/minitouch | 230F502C57FD5580D1A47509C596B6F4081FC9E3A7217915592C57EA48883A2B |

各 ABI 的 `minitouch-nopie` 与 `minitouch` 内容一致，供无 PIE 支持的旧
内核使用；LDPlayer（x86_64）使用 `x86_64/minitouch`。

设备编排由 `agent/realtime/native_minitouch.py` 完成：push 到
`/data/local/tmp/`、`chmod 777`、以唯一 abstract socket 名启动、
`adb forward` 后用 C++ `MinitouchClient` 发布脚本并回读 jlog。
