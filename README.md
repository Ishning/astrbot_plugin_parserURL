
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_parser?name=astrbot_plugin_parser&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_parserURL

_✨ 链接解析器 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)

</div>

## 📖 介绍
一款基于Astrbot的多媒体解析插件，项目由 [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) 而来，修复了错误和解决了使用上的一些问题并扩展了插件新功能。修改与更新的`change log` 详情请看[更新日志 v2.0.0开始](./CHANGELOG.md#v200)

项目将持续维护，有什么新功能欢迎提issue或PR

当前支持的平台和类型：

| 平台    | 触发的消息形态                    | 视频 | 图集 | 音频 | 主动推送 |
| ------ | ----------------------------    | ---- | --- | --- | ------  |
| B 站    | av 号/BV 号/链接/短链/卡片/小程序  | ✅​  | ✅​  | ✅​  | ✅​     |
| 抖音    | 链接(分享链接，兼容电脑端链接)      | ✅​  | ✅​  | ❌️  |❌️      |
| 微博    | 链接(博文，视频，show, 文章)       | ✅​  | ✅​  | ❌️  |❌️      |
| 小红书  | 链接(含短链)/卡片                 | ✅​  | ✅​  | ❌️  |❌️      |
| 小黑盒  | 链接/卡片                        | ✅​  | ✅​  | ❌️  |❌️      |
| 知乎    | 链接/卡片                        | ✅​  | ✅​  | ❌️  |❌️      |
| 快手    | 链接(包含标准链接和短链)           | ✅​  | ✅​  | ❌️  |❌️      |
| acfun   | 链接                            | ✅​  | ❌️  | ❌️  |❌️      |
| youtube | 链接(含短链)                     | ✅​  | ❌️  | ✅​  |❌️      |
| tiktok  | 链接                            | ✅​  | ❌️  | ❌️  |❌️      |
| instagram | 链接                          | ✅​  | ✅​  | ❌️  |❌️      |
| twitter | 链接                            | ✅​  | ✅​  | ❌️  |❌️      |

本插件目标：凡是链接皆可解析！尽请期待更新（如果可以,请提交PR）

---

## 🎨 效果图

插件默认启用 PIL 实现的通用媒体卡片渲染，效果图如下

<div align="center">

<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/9_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/4_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_2_pic.png" width="160" />

</div>

---

## 💿 安装

- 通过在的插件市场搜索点击安装完成
- 通过`插件市场-安装插件-从链接安装`填入该项目GitHub链接地址`https://github.com/Ishning/astrbot_plugin_parserURL`安装
- 通过以下指令进行安装：
```shell
plugin i https://github.com/Ishning/astrbot_plugin_parserURL
```

## ⚙️ 配置

请在astrbot的插件配置面板查看并修改

## 🎉 指令

| 指令 | 权限 | 说明 | 别名 |
| :---: | :---: | :---: | :---: |
| **开启解析** | ADMIN | 开启当前会话的解析功能 | 可在后管理的插件-管理行为自行自定义 |
| **关闭解析** | ADMIN | 关闭当前会话的解析功能 | 可在后管理的插件-管理行为自行自定义 |
| **blogin** | ADMIN | 扫码获取 B 站凭证 | `blogin`、`登录b站`，也可在后管理的插件-管理行为自行自定义 |
| **订阅up** | ADMIN | 订阅B站用户并进行主动推送至相关用户动态群内 | 可在后管理的插件-管理行为自行自定义 |
| **取消订阅up** | ADMIN | 取消B站用户动态更新主动推送至群内 | 可在后管理的插件-管理行为自行自定义 |
| **查询订阅up列表** | ADMIN | 查询订阅了哪些B站用户 | 可在后管理的插件-管理行为自行自定义 |
| **查询订阅up列表详细** | ADMIN | 查询订阅了哪些B站用户带上了发送至哪些人和群 | 可在后管理的插件-管理行为自行自定义 |
| **查询up直播状态** | ADMIN | 查询订阅的B站用户直播状态 | 可在后管理的插件-管理行为自行自定义 |

---

## 🧠 插件工作流程

当插件运行后，每一条消息的处理流程如下：

1. **消息接收**  
   监听所有消息事件，获取消息链与原始文本内容  
   - 支持普通文本、链接、卡片（Json 组件）

2. **基础过滤**  
   - 跳过已被禁用的会话  
   - 跳过空消息  
   - 若消息首段为 `@` 且目标不是本 Bot，则不解析

3. **链接提取与匹配**  
   - 若为卡片消息，先从 Json 中提取 URL  
   - 使用「关键词 + 正则」双重匹配，定位对应解析器  
   - 未匹配到解析规则则直接退出

4. **仲裁判定（Emoji Like Arbiter）**  
   - 仅在 `aiocqhttp` 平台生效  
   - 通过固定表情进行 Bot 间仲裁  
   - 未胜出的 Bot 自动放弃解析

5. **防抖判定（Link Debouncer）**  
   - 对同一会话内的相同链接进行时间窗口限制  
   - 命中防抖规则则跳过解析，避免短时间重复处理

6. **内容解析**  
   - 调用对应平台解析器获取媒体信息  
   - 生成统一的 `ParseResult` 数据结构

7. **媒体下载与消息构建**  
   - 下载视频 / 图片 / 音频 / 文件  
   - 根据配置决定音频发送方式  
   - 可按配置提示下载失败项

8. **卡片渲染（可选）**  
   - 在非简洁模式或无直传媒体时生成媒体卡片  
   - 使用 PIL 渲染并缓存图片

9. **消息合并与发送**  
    - 当消息段数量超过阈值时自动合并为转发消息  
    - 最终将结果发送到对应会话

---

## 🧩 扩展

插件支持自定义解析器，通过继承 `BaseParser` 类并实现 `platform`, `handle` 即可。

示例解析器请看 [示例解析器](https://github.com/Zhalslar/astrbot_plugin_parser/blob/main/core/parsers/example.py)

---

## 🎉 致谢
本项目由[astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)项目发展而来，原项目[astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)核心代码来自[nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，喜欢的话请前往原仓库给作者分别点个Star!
