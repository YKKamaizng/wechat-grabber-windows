# 微信抢图助手（Windows 重建版）

这是根据原有 Windows EXE 恢复功能后重建的版本，目标是保持“单个 EXE 直接运行”，不要求用户安装 Python、Wireshark、Npcap、WinDump、Charles 等环境。

## 本版改动

1. **阿里云 NTP 每 30 秒自动同步**
   - 启动立即同步；
   - 每轮对 `ntp.aliyun.com` 快速采样 3 次；
   - 使用低 RTT 样本的中位数作为时间偏差；
   - NTP 失败时尝试 HTTP 备用时间源；
   - 某一轮刷新失败会继续保留上一次有效偏差。

2. **Windows 抓包改用系统内置 PktMon**
   - 不再依赖 WinDump/tcpdump/Npcap；
   - 自动检测 `WeChat.exe`、`WeChatApp.exe`、`Weixin.exe`；
   - 根据微信进程 PID 从 `netstat` 获取当前 TCP 端口；
   - 只在“校准”时请求管理员权限；
   - 使用 PktMon 按微信端口过滤并实时捕获；
   - 多轮测试后计算平均值、标准差、最小/最大、P80，并给出“提前发射”建议值。

## 直接在 GitHub 自动构建 EXE

把整个项目上传到 GitHub 仓库后：

1. 打开仓库的 **Actions**；
2. 选择 **Build Windows EXE**；
3. 点击 **Run workflow**；
4. 构建完成后，在页面底部下载 `WeChatGrabber-Windows` Artifact；
5. 解压后得到 `WeChatGrabber.exe`。

电脑本地不需要安装 Python 或开发环境。

## 使用抓包校准前

- Windows 10 / 11；
- 微信已经登录且保持联网；
- 点击“校准”页的“开始 PktMon 抓包测试”；
- 程序会请求 Windows 管理员权限；
- 点“是”后重新进入校准页继续测试。

PktMon 是 Windows 自带组件，不会安装额外网络驱动。

## 当前注意事项

这是根据旧 EXE 的 Python 3.12 字节码结构、界面文本、函数/类结构和常量恢复后重建的版本，并非原始源码逐行还原。因此建议先在非关键抢图场景做 10~20 次测试，重点确认：

- 微信窗口能否被正确激活；
- Ctrl+V 和 Enter 行为是否与当前微信版本一致；
- 当前微信版本是否能从 `netstat` 检出稳定 TCP 端口；
- PktMon 是否能在发送测试消息后稳定抓到包；
- 校准建议值是否在重复测试中稳定。
