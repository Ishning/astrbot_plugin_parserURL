from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        posix_path = path.as_posix()
        if posix_path.startswith("/"):
            return f"file:////{posix_path.lstrip('/')}"
        return path.as_uri()

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)
        
        render_card = True 
        
        if render_card_override is not None:
            render_card = render_card_override

        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            # "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        # 取消原分开的，只要有渲染卡片就统一加入前面消息列表中
        if plan["render_card"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(Image(self._to_file_uri(image_path)))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败\n"))
                continue

            match cont:
                case ImageContent():
                    segs.append(Image(self._to_file_uri(path)))
                case GraphicsContent() as g:
                    # OneBot/aiocqhttp 本地文件参数要求 file:// URI，而非裸本地路径。
                    segs.append(Image(self._to_file_uri(path)))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                segs.append(Plain("此项媒体超过大小限制\n"))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败\n"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video(self._to_file_uri(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else Record(self._to_file_uri(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        if result.display_url:
            lines.append(result.display_url)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    @staticmethod
    def _build_text_fallback_for_url(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str]=[]
        if result.display_url:
            lines.append(result.display_url)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []


    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
        text_segs: list[BaseMessageComponent] | None = None, #文本接收
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        # 预览卡片和链接文本独立
        preview_segs = []
        if plan["render_card"]:
            if image_path := await self.renderer.render_card(result):
                preview_segs.append(Image(self._to_file_uri(image_path)))
        
        if text_segs:
            preview_segs.extend(text_segs)

        # 生成正文，设置 plan 里的卡片标识防止重复生成ww
        plan["render_card"] = False
        content_segs = await self._build_segments(result, plan)

        try:
            sent = False
            
            # 策略
            if plan["force_merge"]:
                # 卡片+链接先行
                if preview_segs:
                    await event.send(event.chain_result(preview_segs))
                    sent = True
                
                # 剩下都合并
                if content_segs:
                    merged_nodes = self._merge_segments_if_needed(event, content_segs, True)
                    await event.send(event.chain_result(merged_nodes))
                    sent = True
                    
                return sent

            # 4. 未触发以上的策略
            all_segs = preview_segs + content_segs
            if not all_segs:
                return False
                
            normal_segs = []
            heavy_segs = []
            for seg in all_segs:
                if isinstance(seg, (Video, File, Record)):
                    heavy_segs.append(seg)
                else:
                    normal_segs.append(seg)

            if normal_segs:
                await event.send(event.chain_result(normal_segs))
                sent = True
            
            if heavy_segs:
                for heavy_seg in heavy_segs:
                    await event.send(event.chain_result([heavy_seg]))
                    sent = True

            return sent

        except Exception as e:
            seg_meta = self._collect_seg_meta(preview_segs + content_segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return False

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        platform_name = result.platform.display_name
        match platform_name:
            case "B站" | "抖音" :
                segs=self._build_text_fallback_for_url(result)
            # case "微博":
            #     segs = self._build_text_fallback(result)
            case _:
                segs = self._build_text_fallback(result)

        sent = False
        for i, group in enumerate(groups):
            # 防止如果有多个 group 时重复发送文本，这里仅在最后一个 group 附带文字
            current_text_segs = segs if i == len(groups) - 1 else None
            sent = await self._send_group(event, result, group, current_text_segs) or sent 

        if not sent:
            if not segs:
                logger.warning(f"[{platform_name}] 发送结果为空，不执行发送")
                return
            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
                return