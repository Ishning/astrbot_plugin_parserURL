# main.py

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        # config目录的下的配置json读写锁
        self._plugin_config_lock = asyncio.Lock()


    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin
        logger.debug(f"DEBUG: 当前消息的 session 字符串是: {event.unified_msg_origin}")
        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 卡片解析：解析Json组件，提取URL
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")

        if not text:
            return

        self_id = event.get_self_id()

        # 指定机制：专门@其他bot的消息不解析
        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return

            try:
                msg_id = int(raw["message_id"])
                msg_time = int(raw["time"])
                bot_self_id = int(raw["self_id"])
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"获取仲裁所需字段失败。错误信息: {e}, raw_message: {raw}")
                return

            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=msg_id,
                    msg_time=msg_time,
                    self_id=bot_self_id,
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        parse_res = await self.parser_map[keyword].parse(keyword, searched)

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        await self.sender.send_parse_result(event, parse_res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
            qrcode = await parser.login.login_with_qrcode()
            yield event.chain_result([Image.fromBytes(qrcode)])
            async for msg in parser.login.check_qr_state():
                yield event.plain_result(msg)

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            import traceback
            logger.error(f"[bili_登录] 扫码登录发生异常: {traceback.format_exc()}")
            yield event.plain_result(f"登录过程中发生错误，请稍后再试: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("订阅up")
    async def subscribe_bili_up(self, event: AstrMessageEvent, uid_str: str = ""):
        """添加B站up主动态订阅"""
        try:
            if not uid_str or not uid_str.isdigit():
                yield event.plain_result("UID 不能为空且为数字\n示例：订阅up 114514")
                return

            uid = int(uid_str)
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore

            is_group = False
            target_id = ""

            if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
                is_group = True
                raw = event.message_obj.raw_message

                if isinstance(raw, dict):
                    target_id = str(raw.get("group_id", ""))
                else:
                    target_id = str(getattr(event.message_obj, "group_id", ""))

                if not target_id:
                    yield event.plain_result("获取群号失败")
                    return
            else:
                target_id = str(event.get_sender_id())

            target_type = "groups" if is_group else "users"

            up_name = await parser.get_up_info(uid=uid)

            # 更新内存 sub_map
            if uid not in parser.sub_map:
                parser.sub_map[uid] = {"groups": [], "users": []}

            if target_id in parser.sub_map[uid][target_type]:
                yield event.plain_result(f"当前通过{'群' if is_group else '私聊'}订阅的UP主：{up_name}，uid：{uid} 已被订阅")
                return

            parser.sub_map[uid][target_type].append(target_id)

            await self.save_to_plugin_config()
            yield event.plain_result(f"成功订阅 UP主：{up_name}，uid：{uid}")

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            import traceback
            logger.error(f"[bili_订阅] 添加订阅失败: {traceback.format_exc()}")
            yield event.plain_result(f"错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消订阅up")
    async def unsubscribe_bili_up(self, event: AstrMessageEvent, uid_str: str = ""):
        """取消B站UP主订阅动态推送"""
        try:
            if not uid_str or not uid_str.isdigit():
                yield event.plain_result("UID 不能为空且为数字\n示例：取消订阅up 114514")
                return

            uid = int(uid_str)
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            is_group = False
            target_id = ""

            if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
                is_group = True
                raw = event.message_obj.raw_message
                if isinstance(raw, dict):
                    target_id = str(raw.get("group_id", ""))
                else:
                    target_id = str(getattr(event.message_obj, "group_id", ""))
            else:
                target_id = str(event.get_sender_id())

            target_type = "groups" if is_group else "users"

            up_name = await parser.get_up_info(uid=uid)

            #检测是否存在
            if uid not in parser.sub_map or target_id not in parser.sub_map[uid][target_type]:
                yield event.plain_result(f"当前{'群' if is_group else '私聊'}并没有订阅 UP主：{up_name}，uid：{uid}")
                return

            #仅移除针对会话发起的群或个人私聊
            parser.sub_map[uid][target_type].remove(target_id)

            if not parser.sub_map[uid]["groups"] and not parser.sub_map[uid]["users"]:
                parser.sub_map.pop(uid, None)
                logger.info(f"[bili_订阅] UID {uid} 无任何订阅者从内存移除")

            await self.save_to_plugin_config()
            yield event.plain_result(f"成功取消订阅 UP主：{up_name}，uid：{uid}")

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            import traceback
            logger.error(f"[bili_订阅] 取消订阅失败: {traceback.format_exc()}")
            yield event.plain_result(f"错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询订阅up列表")
    async def check_subscribe_bili_up(self, event: AstrMessageEvent):
        """查询订阅列表，仅基础信息"""
        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            if not parser.sub_map:
                yield event.plain_result("当前没有任何B站 up订阅记录")
                return

            msg_lines = ["B站up订阅列表"]
            for uid in parser.sub_map.keys():
                up_name = parser.uid_name_cache.get(uid, f"[查询量过快可能触发风控，稍后再查询]")
                msg_lines.append(f"up名：{up_name}，uid：{uid}，地址：https://space.bilibili.com/{uid}")

            yield event.plain_result("\n".join(msg_lines))

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            import traceback
            logger.error(f"[bili_订阅] 查询订阅失败: {traceback.format_exc()}")
            yield event.plain_result(f"查询错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询订阅up列表详细")
    async def check_subscribe_bili_up_all(self, event: AstrMessageEvent):
        """查询订阅列表，详细信息带上了发送至哪些人和群"""
        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            if not parser.sub_map:
                yield event.plain_result("当前没有任何B站 up订阅记录")
                return

            msg_lines = ["B站up订阅详细列表"]
            for uid, targets in parser.sub_map.items():
                groups = targets.get("groups", [])
                users = targets.get("users", [])

                up_name = parser.uid_name_cache.get(uid, f"[查询量过快可能触发风控，稍后再查询]")

                group_str = "、".join(groups) if groups else "无"
                user_str = "、".join(users) if users else "无"

                msg_lines.append(
                    f"up名：{up_name}，uid：{uid}，地址：https://space.bilibili.com/{uid}， 发送至 群：{group_str}，个人：{user_str}"
                )

            yield event.plain_result("\n".join(msg_lines))

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            import traceback
            logger.error(f"[bili_订阅] 查询详细订阅失败: {traceback.format_exc()}")
            yield event.plain_result(f"查询错误: {e}")

    async def save_to_plugin_config(self):
        """将 sub_map 写入到 AstrBot 插件的配置文件对应位置"""
        import json
        import os

        current_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_name = os.path.basename(current_dir)
        config_path = f"data/config/{plugin_name}_config.json"

        #增加一层 asyncio.Lock 异步锁保护
        #读写配置文件，看框架用的是 utf-8-sig 格式保持统一
        async with self._plugin_config_lock:
            try:
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8-sig") as f:
                        current_config = json.load(f)
                else:
                    current_config = {"parsers_template": []}

                parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore
                formatted_list = []

                for uid, targets in parser.sub_map.items():
                    parts = [str(uid)]
                    parts.extend([f"g{g_id}" for g_id in targets.get("groups", [])])
                    parts.extend([f"u{u_id}" for u_id in targets.get("users", [])])
                    formatted_list.append("-".join(parts))

                parsers_template = current_config.get("parsers_template", [])
                target_node = next((t for t in parsers_template if t.get("__template_key") == "bilibili"), None)

                if target_node:
                    target_node["sub_uids_users"] = formatted_list
                else:
                    parsers_template.append({
                        "__template_key": "bilibili",
                        "sub_uids_users": formatted_list
                    })
                    current_config["parsers_template"] = parsers_template

                with open(config_path, "w", encoding="utf-8-sig") as f:
                    json.dump(current_config, f, ensure_ascii=False, indent=2)

                logger.info(f"[bili_订阅] 配置写入成功，先有 {len(formatted_list)} 条订阅记录。")

            except ValueError as e:
                if "BilibiliParser" in str(e):
                    logger.error("B站相关功能未开启，请检查后台配置是否开启")
                else:
                    logger.error(f"错误: {e}")

            except Exception as e:
                logger.error(f"[bili_订阅] 写入该插件的配置文件失败: {e}")
                raise e