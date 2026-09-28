# Joy 微信表情识别与比对算法方案

## 1. 目标

用于 Joy 微信个人 Agent：从微信聊天截图中定位对方发送的小表情，与本地 109 个标准微信表情 PNG 比对，输出稳定的 `[xxx]` 标签。

一期范围：
- 只识别微信内置小表情
- 暂不处理自定义表情、GIF、图片、视频
- 不依赖云端 VLM 作为主流程
- VLM 只做低置信度兜底
- 优先准确、快速、本地可运行

## 2. 推荐总体流程

```text
微信截图
  ↓
消息气泡/表情区域定位
  ↓
精确裁剪
  ↓
图像归一化
  ↓
pHash 初筛：109 → Top 15
  ↓
SSIM：Top 15 → Top 5
  ↓
像素差 + Edge + Color 综合评分
  ↓
Top 1 + Top2 Margin
  ↓
高置信度直接确定
中置信度二次精匹配
低置信度 VLM 兜底
  ↓
输出 [666] / [捂脸] / [奸笑] ...
```

## 3. 为什么采用模板比对

你的场景不是通用图像分类，而是固定的 109 类：

```text
输入：一个微信截图中的表情
候选：固定 109 个微信内置小表情
```

因此直接回答“它最像哪个已知模板”，比让大模型自由猜测表情名称更稳定。

## 4. 标准表情资源

建议：

```text
emoji/
├─ 微笑.png
├─ 撇嘴.png
├─ ...
├─ 666.png
├─ 让我看看.png
├─ ...
├─ 旺柴.png
└─ ...
```

映射：

```json
{
  "[666]": "666.png",
  "[奸笑]": "奸笑.png",
  "[旺柴]": "旺柴.png",
  "[捂脸]": "捂脸.png"
}
```

模板的 pHash、Edge、Color Histogram 等特征建议启动时预计算并缓存。

## 5. 第一阶段：表情区域定位

匹配前最重要的是只裁剪表情本体，不要把整个聊天气泡拿来比较。

推荐：

```text
截图
 ↓
消息气泡定位
 ↓
判断消息方向
 ↓
判断是否包含表情
 ↓
定位表情中心
 ↓
裁剪表情
```

优先利用你现有的 OCR、气泡定位和微信 PC UI 几何信息；必要时才使用视觉检测。

## 6. 第二阶段：图像归一化

真实截图可能存在缩放、压缩、轻微模糊、背景差异和空白边缘。

建议：

- 去除尽可能多的聊天气泡背景
- 找非背景像素 bounding box
- 保持长宽比
- 居中
- 统一放入 64×64 画布
- 使用 RGB；必要时同时保留灰度版本

不要直接把原图强制拉伸成正方形。

## 7. 第一级：pHash 初筛

pHash 的职责不是最终判定，而是快速召回。

```text
109 个模板
 ↓
pHash
 ↓
Top 15
```

两个图像的汉明距离越小，一般整体视觉结构越接近。

不要只靠 pHash 最终判断，因为微笑、憨笑、奸笑、坏笑等表情存在较强视觉相似性。

## 8. 第二级：SSIM

SSIM 用于比较图像结构、亮度和对比度。

对截图造成的轻微缩放、压缩、模糊通常比单纯像素差更鲁棒。

流程：

```text
pHash Top 15
 ↓
SSIM
 ↓
Top 5
```

## 9. 第三级：像素差

归一化后的图像可以做逐像素比较：

```text
pixel_distance = mean(abs(input - template))
pixel_similarity = 1 - normalized_distance
```

优点是对固定微信资源的区分能力很强。

缺点是对轻微平移、抗锯齿和背景残留比较敏感，所以不要单独使用。

## 10. 第四级：Edge / 轮廓

对输入和模板提取 Canny Edge，得到轮廓图，再比较 edge_similarity。

这样可以降低亮度、颜色变化对识别的影响。

特别适合区分：

- 眼睛形状
- 嘴巴形状
- 眉毛
- 脸部轮廓
- 特殊装饰

## 11. 第五级：Color

建议使用 HSV 颜色直方图。

微信表情中的黄色脸、红色爱心、绿色帽子、蓝色眼泪等都有稳定的颜色信息。

Color 特征主要用来排除“结构相似但配色明显不同”的表情。

## 12. 综合评分

推荐第一版：

```text
FinalScore
=
0.45 × SSIM
+
0.30 × PixelSimilarity
+
0.15 × EdgeSimilarity
+
0.10 × ColorSimilarity
```

pHash 用于召回，不直接参与最终评分。

这些权重是工程起始值，正式上线前应使用真实截图测试集重新标定。

## 13. Top1 / Top2 Margin

除了 Top1 分数，还要计算：

```text
margin = Top1Score - Top2Score
```

例如：

```text
Top1 = 0.987
Top2 = 0.612
margin = 0.375
```

说明非常明确。

如果：

```text
Top1 = 0.813
Top2 = 0.801
margin = 0.012
```

说明模型犹豫，需要二次判断。

## 14. 初始置信度规则

作为第一版工程阈值，可以先：

```text
score >= 0.92 且 margin >= 0.10
→ high
→ 直接采用
```

```text
0.80 <= score < 0.92
或 margin < 0.10
→ medium
→ 二次精匹配
```

```text
score < 0.80
→ low
→ VLM 兜底
```

这些不是已验证的最终阈值，应根据真实数据调整。

## 15. 二次精匹配

当 Top1 和 Top2 很接近时，不要马上调用 VLM。

可以对局部区域比较：

```text
眼睛区域
嘴巴区域
眉毛区域
装饰区域
```

尤其对于相似表情，可以针对 Top1/Top2 做局部特征比对。

## 16. VLM 兜底

只处理低置信度情况。

推荐把候选限制在 Top 3~5：

```text
候选：
[666]
[奸笑]
[捂脸]
[旺柴]

请选择最匹配的一个。
不要生成其他标签。
```

不要让 VLM 在全部微信标签之外自由生成名称。

## 17. 推荐接口

```python
class WeChatEmojiMatcher:
    def match(self, image):
        ...
```

返回：

```python
@dataclass
class EmojiMatch:
    tag: str
    name: str
    filename: str
    score: float
    margin: float
    alternatives: list
    confidence: str
```

例如：

```python
EmojiMatch(
    tag="[666]",
    name="666",
    filename="666.png",
    score=0.987,
    margin=0.375,
    confidence="high"
)
```

## 18. Agent 上下文格式

不要只记录：

```text
对方发送了一张图片
```

建议：

```text
汤桓宇发送了 [666] 表情。
```

结构化格式：

```json
{
  "sender": "汤桓宇",
  "type": "emoji",
  "tag": "[666]",
  "name": "666",
  "confidence": 0.987
}
```

这样 Agent 才能把表情作为聊天行为参与上下文理解。

## 19. 与发送模块衔接

Agent 生成：

```text
“666 [666]”
```

发送层：

```text
发现 [666]
 ↓
查 emoji_map
 ↓
确认属于白名单
 ↓
输入微信
 ↓
微信客户端自动转换为原生表情
```

禁止模型随意生成不存在的：

```text
[无语脸]
[哈哈脸]
[狗头笑]
```

等标签。

## 20. 性能设计

109 个模板非常少。

启动时预计算：

```text
pHash
灰度图
Edge
Color Histogram
```

保存到：

```text
emoji_features.json
```

实时只计算输入图特征，然后与缓存比较。

不要每次重新读取 109 张 PNG。

## 21. 线程设计

建议：

```text
微信截图监控线程
        ↓
消息检测线程
        ↓
Emoji Worker
        ↓
WeChatEmojiMatcher
        ↓
结果队列
        ↓
Agent Context
```

避免一次表情识别阻塞你的微信监控主流程。

## 22. 防误识别

### 规则 1：纯文字不进入 EmojiMatcher

```text
666
```

是文字，不是 `[666]` 表情。

只有检测到真实的表情图像区域才进入 Matcher。

### 规则 2：区域不稳定时不要强行猜

宁可：

```text
unknown_emoji
```

也不要污染上下文。

### 规则 3：低置信度不要直接写死 Memory

低置信度可以保存：

```json
{
  "type": "emoji",
  "tag": null,
  "candidate": ["[666]", "[奸笑]"],
  "confidence": 0.61
}
```

而不是直接记忆成 `[666]`。

## 23. 离线测试集

上线前建议收集真实微信截图：

```text
666 × 20
捂脸 × 20
奸笑 × 20
旺柴 × 20
……
```

统计：

- Top-1 Accuracy
- Top-3 Accuracy
- 平均识别耗时
- 低置信度比例
- 混淆矩阵

## 24. 混淆矩阵

重点观察：

```text
微笑 ↔ 憨笑
奸笑 ↔ 坏笑
愉快 ↔ 笑脸
难过 ↔ 失望
惊讶 ↔ 疑问
鄙视 ↔ 白眼
```

如果 Top1/Top2 属于同一混淆组，则启动局部精匹配。

## 25. 后续升级

### v1
模板匹配：

```text
Crop
→ Normalize
→ pHash
→ SSIM
→ Pixel
→ Edge
→ Color
```

### v2
局部区域匹配：

```text
眼睛
嘴巴
眉毛
装饰
```

### v3
真实数据足够以后训练 109 类轻量模型，例如 MobileNetV3 / EfficientNet-Lite / ResNet18。

训练增强：

```text
随机缩放
随机平移
轻微模糊
亮度变化
压缩
背景扰动
```

## 26. 最终推荐架构

```text
截图
 ↓
MessageParser
 ↓
 ├── text → TextPipeline
 │
 └── emoji → WeChatEmojiMatcher
                 ↓
              [666]
                 ↓
           ConversationState
                 ↓
              Agent LLM
```

职责分离：

```text
视觉层
负责“看到了什么”

Emoji Matcher
负责“这是哪个微信表情”

状态层
负责“现在发生了什么”

关系层
负责“谁和谁是什么关系”

Agent 层
负责“我应该怎么回应”

发送层
负责“微信应该怎么发出去”
```

## 27. 一句话结论

对于 Joy 当前阶段，优先使用：

> **精准裁剪 + 图像归一化 + pHash 初筛 + SSIM 主判定 + 像素差 + Edge + Color + Top1/Top2 Margin + 低置信度兜底**

不要一开始训练模型，也不要让 VLM 每条表情都参与。

最重要的架构原则：

> **Emoji Matcher 负责识别，Agent 负责理解。**

例如：

```text
Emoji Matcher：
[666]

Agent：
结合上下文判断，这是认可、称赞、调侃还是单纯玩梗。
```

这两个问题不要混在一起。
