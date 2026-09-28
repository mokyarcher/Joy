# 微信小表情资源

## 权威包（你已提供）

`docs/JoyWeChatEmojiNamed_v1/`

- **109** 枚透明 PNG（面板截图直裁，无 AI 重绘）
- `emoji_map.json` / `.csv` / `.js`：`[标签] → 文件名`
- `named_contact_sheet.png`：总览表
- 命名：`[666]` → `666.png`

## 运行时目录

同步后：

```text
src/joy/assets/wechat_emoji/official/
  emoji_map.json
  微笑.png …
```

同步命令：

```bash
python -c "from joy.core.wechat_emoji import sync_official_from_package; print(sync_official_from_package())"
```

或：

```bash
python scripts/import_wechat_emoji_official.py docs/JoyWeChatEmojiNamed_v1
```

## 当前用法

识别流程按 `docs/Joy_微信表情识别与比对算法方案.md`：

```text
裁剪 → 64×64 归一化 → pHash Top15 → SSIM Top5
→ 0.45·SSIM + 0.30·Pixel + 0.15·Edge + 0.10·Color
→ Top1/Top2 Margin → high|medium|low
```

- 实现：`joy.capture.emoji_match.WeChatEmojiMatcher`
- 低置信度不写死上下文；系统 Unicode 😂 不进微信包硬匹配
- 特征缓存：`official/emoji_features.json`（启动预计算 pHash）

`tiles/` 为旧面板裁切兜底，有 official 后不再优先。
