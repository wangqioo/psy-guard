# PsyGuard 项目说明（给 Claude Code）

## 项目背景

心理咨询室随身预警系统。咨询师佩戴 XIAO nRF52840 Sense（配电池），持续采集咨询室音频，通过 BLE 传到手机，手机 WebSocket 中继到服务器，服务器语音识别 + LLM 分析，检测到高风险内容（自伤、危机等）时立即推预警到手机。

## 三端架构

```
[XIAO nRF52840 Sense]
  PDM麦克风 16kHz PCM → BLE Nordic UART Service (244字节/包)
         ↓
[iPhone App (Swift)]
  CoreBluetooth 接收 → 4KB缓冲 → WebSocket 推服务器
  接收预警 JSON → SwiftUI 展示
         ↓
[服务器 Docker (Spark2: 150.158.146.192)]
  WebSocket 8097端口 → FunASR转写 → LLM分析 → 推回预警
  公网隧道: ws://150.158.146.192:6146
```

## 目录结构

```
psy-guard/
├── CLAUDE.md                    ← 你正在读这个
├── README.md                    ← 用户文档
├── PsyGuard-Arduino/
│   └── PsyGuard.ino             ← XIAO 固件，基于官方PDM示例改造
├── PsyGuard-iOS/
│   ├── BLEManager.swift         ← CoreBluetooth，扫描/连接/接收音频
│   ├── ServerRelay.swift        ← WebSocket转发 + 预警JSON解析
│   ├── AppViewModel.swift       ← 业务逻辑层，串联BLE和服务器
│   └── ContentView.swift        ← SwiftUI界面，状态/录音/预警列表
└── server/
    ├── server.py                ← Python WebSocket服务主程序
    ├── Dockerfile
    └── docker-compose.yml
```

## 关键技术细节

### BLE UUIDs（Nordic UART Service，三端必须一致）
```
Service:  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
TX notify 设备→手机: 6E400003-B5A3-F393-E0A9-E50E24DCCA9E
RX write  手机→设备: 6E400002-B5A3-F393-E0A9-E50E24DCCA9E
```

### 音频格式
- 单声道，16kHz，16-bit PCM，小端序
- BLE 分包 244 字节/包
- 手机缓冲 4KB 后一次发给服务器

### 控制命令（手机→开发板 RX 特征值）
- `0x01` = 开始录制
- `0x00` = 停止录制

### 服务器预警 JSON 格式
```json
{
  "type": "alert",
  "level": "high",
  "keyword": "不想活了",
  "text": "原始转写片段"
}
```
`level`: `high`（红）/ `medium`（橙）/ `low`（黄）

### 服务器依赖服务
- FunASR HTTP Bridge: `localhost:8094` （语音转文字）
- LLM OpenAI兼容 API: `localhost:8081` （危机内容分析）
- 容器用 `network_mode: host` 访问这两个宿主机服务

## 开发状态（截至 2026-04-16）

### 已完成
- [x] 服务器端：WebSocket服务、FunASR集成、LLM分析、Docker部署
- [x] 服务器已在 Spark2 运行，端口 8097，公网 ws://150.158.146.192:6146
- [x] Arduino 固件：PDM采集 + BLE流式传输
- [x] iOS App：BLE管理、WebSocket中继、SwiftUI预警界面

### 待完成 / 待验证
- [ ] Arduino 固件烧录验证（BLE广播、PDM采集、分包发送）
- [ ] iOS App Xcode 工程搭建（把4个.swift文件加进去）
- [ ] 端到端联调：开发板 → 手机 → 服务器 → 预警
- [ ] BLE 连接稳定性测试（长时间录制掉包/重连）
- [ ] 音频质量调优（PDM.setGain 值、采样率是否够用）
- [ ] 服务器预警规则完善（敏感词库、LLM prompt 优化）
- [ ] iOS 后台 BLE 保持连接（Background Mode 配置）

## Arduino 开发环境

**板卡**：Seeed nRF52 mbed-enabled Boards → Seeed XIAO BLE Sense - nRF52840
（注意：必须是 mbed-enabled 版，不是普通版，否则 PDM.h 不可用）

**依赖库**：`PDM.h` 和 `ArduinoBLE.h` 均内置，无需额外安装

**LED 指示**（低电平点亮）：
- 蓝灯亮 = BLE 广播中等待连接
- 绿灯亮 = 手机已连接
- 红灯亮 = 正在录制中

## iOS 开发环境

**Xcode 工程搭建步骤**：
1. 新建 iOS App，Swift + SwiftUI，Product Name `PsyGuard`
2. 将 `PsyGuard-iOS/` 下 4 个 `.swift` 拖入工程，删掉自动生成的 `ContentView.swift`
3. `Info.plist` 添加：
   - `NSBluetoothAlwaysUsageDescription`
   - `NSBluetoothPeripheralUsageDescription`
4. Signing & Capabilities → 添加 `Background Modes` → 勾选 `Uses Bluetooth LE accessories`
5. 真机运行（BLE 不支持模拟器）

**修改服务器地址**：`ServerRelay.swift` 第 11 行
```swift
private let serverURL = URL(string: "ws://150.158.146.192:6146")!
```

## 测试建议

### 第一步：验证服务器
```bash
npx wscat -c ws://150.158.146.192:6146
# 连上后发任意二进制数据，观察服务器日志
```

### 第二步：验证 BLE
烧录 Arduino 固件后，用 nRF Connect App 扫描 "XIAO-Sense"，
订阅 TX 特征值 Notify，向 RX 写 `0x01`，应有数据持续推过来。

### 第三步：iOS 联调
运行 iOS App，点击麦克风按钮，观察：
1. BLE 状态变"就绪"
2. 服务器状态变"已连接"
3. 说话后服务器日志出现转写文本
4. 说出敏感词后手机收到预警

## 注意事项

- 服务器 Docker 容器名：`psy-guard`，查日志用 `docker logs -f psy-guard`
- Spark2 服务器 SSH：`ssh -p 6002 wq@150.158.146.192`
- frp 隧道由用户自己维护，本地端口 8097 → 公网 6146
- 音频数据不落盘，仅内存处理，注意隐私合规
