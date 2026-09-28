# Joy

**看屏副驾**：截取你自己有权查看的屏幕 → 调用国内主流识图 API 读懂内容 → 给出意图与候选回复 → **发不发由你**。

灵感来自 [jev-chat](https://github.com/jev-chat)，独立实现。详见 [NOTICE](NOTICE)。

| | |
|---|---|
| 平台（首期） | Windows 10 / 11 |
| 目标场景 | 微信 Windows 4.x 旁挂；任意窗口可「手动分析一次」 |
| 识图 | 商汤 SenseNova / Kimi / 通义 VL / 智谱 / 硅基流动 / 豆包 / OpenAI / **自定义 OpenAI 兼容** |
| 判断·起草 | DeepSeek 等纯文本模型（可与识图分属两家） |
| 协议 | MIT |

开发文档：[docs/开发文档.md](docs/开发文档.md) · 隐私：[docs/PRIVACY.md](docs/PRIVACY.md)

---

## 和 jev 有什么不同

- **识图层**：不绑定单一判断模型 / 离线 OCR，而是兼容**国内大部分可识图 API**（OpenAI 兼容协议 + 厂商预设）。
- **DeepSeek**：官方 API 当前偏纯文本，适合做判断与起草；识图请选 Kimi、通义 VL、智谱等（或硅基/自定义上的 VL 端点）。
- **技能化**：聊天副驾是第一个 Skill；同一套截图 → 识图内核可挂其它技能。

## 硬边界

- 不 hook、不注入、不读微信数据库。
- **绝不自动发送**；不碰转账 / 红包 / 支付。
- 无项目方中转服务器；请求只打你配置的 API。
- 密钥只存本机；截图默认仅内存流转。

## 快速开始（开发）

需要 Python 3.11+。

```bash
cd Joy
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -e ".[ui,dev]"
python -m joy
```

首次打开设置页，在「视觉」卡选一个预设（如**商汤 SenseNova** / 智谱 / Kimi），填入 API Key；「起草」卡可填 DeepSeek，或同样用商汤。

商汤端点：`https://token.sensenova.cn/v1`，模型默认 `sensenova-6.8-flash-lite`（OpenAI 兼容，支持识图）。控制台与文档见 [platform.sensenova.cn](https://platform.sensenova.cn/docs)。

## 状态

当前：**M3 判断 + 候选回复**。

1. 保存 API Key（视觉 / 判断 / 起草；可都用商汤）
2. 打开「看屏副驾」→ 选微信 → **分析并给回复建议**
3. 下方查看意图与 3 条候选 → **复制推荐回复** → 自己粘贴到微信发送

下一步 M4：一键填入微信输入框（仍不自动发送）。



## 免责声明

只处理你自己设备上、你自己有权查看的画面与聊天。请遵守各软件许可协议与当地法律法规。作者不对使用后果负责。
