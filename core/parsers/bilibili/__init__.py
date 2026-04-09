import asyncio
import json
from re import Match
from typing import ClassVar

from bilibili_api import request_settings, select_client
from bilibili_api.opus import Opus
from bilibili_api.video import Video, VideoCodecs, VideoQuality
from msgspec import convert

from astrbot.api import logger

from ...config import PluginConfig
from ...data import ImageContent, MediaContent, Platform
from ...exception import DownloadException, DurationLimitException
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    handle,
)
from .login import BilibiliLogin

from .dynamic import DynamicInfo

# 选择客户端
select_client("curl_cffi")
# 模拟浏览器，第二参数数值参考 curl_cffi 文档
# https://curl-cffi.readthedocs.io/en/latest/impersonate.html
request_settings.set("impersonate", "chrome131")


class BilibiliParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="B站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.bilibili
        self.headers.update(
            {
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )

        self.video_quality = getattr(
            VideoQuality, str(self.mycfg.video_quality).upper(), VideoQuality._720P
        )
        self.video_codecs = getattr(
            VideoCodecs, str(self.mycfg.video_codecs).upper(), VideoCodecs.AVC
        )

        self.login = BilibiliLogin(config)

        #订阅处理
        self.sub_enable = getattr(self.mycfg, "sub_enable", False)
        if self.sub_enable is None:
            self.sub_enable = False

        self.sub_interval = getattr(self.mycfg, "sub_interval", None) or 3
        self.sub_delay = getattr(self.mycfg, "sub_delay", None) or 5
        self.platforms = getattr(self.mycfg, "platform_name", ["default"]) or ["default"]
        self.platform_botid = getattr(self.mycfg, "platform_botid", None) or []
        self.only_previewCard = getattr(self.mycfg, "only_previewCard", None) or False

        #获取订阅配置
        self.sub_uids_users = getattr(self.mycfg, "sub_uids_users", None) or []
        self.sub_map = {}
        for item in self.sub_uids_users:
            item_str = str(item).strip()
            if not item_str: continue

            parts = item_str.split('-')
            uid_str = parts[0]

            try:
                uid = int(uid_str)
            except ValueError:
                logger.error(f" [bili订阅] 配置错误: '{item_str}' 的 UID 部分不是有效的数字")
                continue

            if len(parts) == 1:
                logger.error(f" [bili订阅] 配置错误: UID {uid} 下该 {parts} 未指定任何群(g)或个人(u)")
                continue

            target_groups = []
            target_users = []
            has_error = False

            for target in parts[1:]:
                target = target.strip()
                if not target: continue
                
                prefix = target[0].lower()
                id_part = target[1:]
                
                if prefix == 'g':
                    try:
                        target_groups.append(str(int(id_part))) #验证是否为数字以防填写错误，然后再转回来
                    except ValueError:
                        logger.error(f" [bili订阅] UID {uid} 的群号格式错误: '{target}'")
                        has_error = True
                elif prefix == 'u':
                    try:
                        target_users.append(str(int(id_part)))
                    except ValueError:
                        logger.error(f" [bili订阅] UID {uid} 的个人号格式错误: '{target}'")
                        has_error = True
                else:
                    logger.error(f" [bili订阅] 配置非法: UID {uid} 中的 '{target}' 未以群(g)或个人(u)开头")
                    has_error = True

            if not has_error and (target_groups or target_users):
                if uid not in self.sub_map:
                    self.sub_map[uid] = {
                        "groups": [],
                        "users": []
                    }
                # 追加同时去重，乙方出现填写重复
                self.sub_map[uid]["groups"].extend(target_groups)
                self.sub_map[uid]["groups"] = list(set(self.sub_map[uid]["groups"]))

                self.sub_map[uid]["users"].extend(target_users)
                self.sub_map[uid]["users"] = list(set(self.sub_map[uid]["users"]))

        #初始化状态缓存
        self._last_dynamic_cache = {}

        #用于存放订阅一些信息
        self.bili_data_dir = config.data_dir / "bilibili"
        self.bili_data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.bili_data_dir / "bilibili_sub_cache.json"
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self._last_dynamic_cache = json.load(f)
                logger.info(f"[bili_订阅] 已加载状态缓存，目前记录数为: {len(self._last_dynamic_cache)}")
            except Exception as e:
                logger.error(f"[bili_订阅] 加载失败: {e}")

        #解决热重载导致的僵尸任务
        f_old_task=False
        for task in asyncio.all_tasks():
            if task.get_name()=="task_bili_subscription_loop":
                task.cancel()
                f_old_task = True
        if f_old_task:
            logger.info("[bili_订阅] 热重载，已清理旧 task_bili_subscription_loop 任务")

        #如果开启了订阅且有配置 UID，启动后台轮询任务
        if self.sub_enable and self.sub_map:
            logger.info(
                f"启动 B 站动态订阅，加载 {len(self.sub_map)}个"
            )
            self._polling_task = asyncio.create_task(
                self._subscription_loop(),
                name="task_bili_subscription_loop")

    @handle("b23.tv", r"b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        """解析短链"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/BV",
        r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_bv(self, searched: Match[str]):
        """解析视频信息"""
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("bm", r"^bm(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s(?P<page_num>\d{1,3}))?$")
    async def _parse_bv_bm(self, searched: Match[str]):
        bvid = searched.group("bvid")
        page = int(searched.group("page_num") or 1)
        _, a_url = await self.extract_download_urls(bvid=bvid, page_index=page - 1)
        if not a_url:
            raise ParseException("未找到音频链接")
        audio = self.create_audio_content(a_url)
        return self.result(
            title=f"BiliBili_audio_{bvid}",
            contents=[audio],
            url=a_url,
        )

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/av",
        r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_av(self, searched: Match[str]):
        """解析视频信息"""
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: Match[str]):
        """解析动态信息"""
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: Match[str]):
        """解析直播信息"""
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: Match[str]):
        """解析收藏夹信息"""
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: Match[str]):
        """解析专栏信息"""
        # read_id = int(searched.group("read_id"))
        # return await self.parse_read_with_opus(read_id)
        # 调整为走 _parse_opus_obj
        from bilibili_api.article import Article

        read_id = int(searched.group("read_id"))
        article = Article(read_id)
        opus = await article.turn_to_opus()
        return await self._parse_opus_obj(opus)

    @handle("/opus/", r"bilibili\.com/opus/(?P<opus_id>\d+)")
    async def _parse_opus(self, searched: Match[str]):
        """解析图文动态信息"""
        opus_id = int(searched.group("opus_id"))
        return await self.parse_opus(opus_id)

    @handle("parserurl_test_push", r"^(?:/parserurl_test_push|parserurl_test_push)\s+(?P<dyid>\d+)$")
    async def _test_push_manual(self, searched: Match[str]):
        """手动触发订阅推送测试 (用于测试)"""
        dynamic_id = int(searched.group("dyid"))
        
        from ...render import Renderer
        from ...sender import MessageSender
        from bilibili_api.opus import Opus
        import traceback

        try:
            # opus_obj = Opus(int(dynamic_id), await self.login.credential)

            test_groups = set()
            test_users = set()

            for targets in self.sub_map.values():
                test_groups.update(targets["groups"])
                test_users.update(targets["users"])

            if not test_groups and not test_users:
                return self.result(
                    title="测试中断",
                    text="未在配置中检测到任何有效的推送目标（群或个人）。请先在设置中配置 sub_uids_users。",
                    contents=[]
                )

            parsed_result = await self.parse_dynamic(dynamic_id)

            if parsed_result:
                renderer = Renderer(self.cfg)
                sender = MessageSender(self.cfg, renderer)
                
                await sender.send_proactive_msg(
                    context=self.cfg.context,
                    result=parsed_result,
                    sub_groups=list(test_groups),
                    sub_users=list(test_users),
                    platforms=self.platforms,
                    dynamic_id=str(dynamic_id),
                    platform_botid=self.platform_botid,
                    only_previewCard=self.only_previewCard,
                )

                return self.result(
                    title="主动推送测试已触发",
                    text=f"动态 {dynamic_id} 解析成功，已调用推送接口发送至目标群/人，请检查是否收到。\n（若没收到请检查日志，以及相关配置是否配置好）",
                    contents=[]
                )
            else:
                return self.result(
                    title="测试失败",
                    text="动态解析为空，请检查该动态是否存在",
                    contents=[]
                )

        except Exception as e:
            logger.error(f"手动推送测试发生异常: {traceback.format_exc()}")
            return self.result(
                title="测试执行异常",
                text=str(e),
                contents=[]
            )

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_num (int): 页码
        """

        from .video import AIConclusion, VideoInfo

        video = await self._get_video(bvid=bvid, avid=avid)
        # 转换为 msgspec struct
        video_info = convert(await video.get_info(), VideoInfo)
        # 获取简介
        text = f"简介: {video_info.desc}" if video_info.desc else None
        # up
        author = self.create_author(video_info.owner.name, video_info.owner.face)
        # 处理分 p
        page_info = video_info.extract_info_with_page(page_num)

        # 获取 AI 总结
        if self.login._credential:
            cid = await video.get_cid(page_info.index)
            ai_conclusion = await video.get_ai_conclusion(cid)
            ai_conclusion = convert(ai_conclusion, AIConclusion)
            ai_summary = ai_conclusion.summary
        else:
            ai_summary: str = "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结"

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        # 视频下载 task
        async def download_video():
            output_path = self.cfg.cache_dir / f"{video_info.bvid}-{page_num}.mp4"
            if output_path.exists():
                return output_path
            v_url, a_url = await self.extract_download_urls(
                video=video, page_index=page_info.index
            )
            if page_info.duration > self.cfg.max_duration:
                raise DurationLimitException
            if a_url is not None:
                return await self.downloader.download_av_and_merge(
                    v_url,
                    a_url,
                    output_path=output_path,
                    headers=self.headers,
                    proxy=self.proxy,
                )
            else:
                return await self.downloader.streamd(
                    v_url,
                    file_name=output_path.name,
                    headers=self.headers,
                    proxy=self.proxy,
                )

        video_task = asyncio.create_task(download_video())
        video_content = self.create_video_content(
            video_task,
            page_info.cover,
            page_info.duration,
        )

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            extra={"info": ai_summary},
        )

    async def parse_dynamic(self, dynamic_id: int):
        """解析动态信息，含专栏等

        Args:
            dynamic_id (int): 动态 id
        """
        from bilibili_api.dynamic import Dynamic

        from .dynamic import DynamicData

        dynamic_ = Dynamic(dynamic_id, await self.login.credential)

        #动态为专栏时候的
        if await dynamic_.is_article():
            return await self.parse_read_with_opus(dynamic_id)
        
        dynamic_info = convert(await dynamic_.get_info(), DynamicData).item

        return await self._parse_dynamic_info(dynamic_info)

    async def _parse_dynamic_info(self, dynamic_info: DynamicInfo):
        """解析动态信息

        Args:
            dynamic_info (DynamicInfo)
        """

        #增加堆转发内容判断
        repost = None
        if dynamic_info.type == "DYNAMIC_TYPE_FORWARD" and dynamic_info.orig is not None:
            repost = await self._parse_dynamic_info(dynamic_info.orig)

        # 媒体内容
        author = self.create_author(dynamic_info.name, dynamic_info.avatar)
        contents: list[MediaContent] = []

        for image_url in dynamic_info.image_urls:
            img_task = self.downloader.download_img(
                image_url, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(img_task))

        return self.result(
            title=dynamic_info.title,
            text=dynamic_info.text,
            timestamp=dynamic_info.timestamp,
            author=author,
            contents=contents,
            repost=repost
        )

    async def parse_opus(self, opus_id: int):
        """解析图文动态信息

        Args:
            opus_id (int): 图文动态 id
        """
        opus = Opus(opus_id, await self.login.credential)
        return await self._parse_opus_obj(opus)

    async def parse_read_with_opus(self, read_id: int):
        """解析动态和图文, 使用 Opus 接口
        Args:
            read_id (int): 专栏 id
        """
        # from bilibili_api.article import Article

        from bilibili_api.dynamic import Dynamic

        # article = Article(read_id)
        # return await self._parse_opus_obj(await article.turn_to_opus())

        dynamic = Dynamic(read_id, await self.login.credential)
        return await self._parse_opus_obj(dynamic.turn_to_opus())

    async def _parse_opus_obj(self, bili_opus: Opus):
        """解析图文动态信息
        Args:
            opus_id (int): 图文动态 id
        Returns:
            ParseResult: 解析结果
        """
        import re
        from .opus import ImageNode, OpusItem

        opus_info = await bili_opus.get_info()
        # import json
        # with open("bili_debug.json", "w", encoding="utf-8") as f:
        #     json.dump(opus_info, f, ensure_ascii=False, indent=2)

        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")
        
        # 结构体
        raw_title = None
        try:
            def _find_title(data, found):
                if isinstance(data, dict):
                    for t in ["opus", "draw", "article"]:
                        if t in data and isinstance(data[t], dict) and data[t].get("title"):
                            found["major_title"] = str(data[t]["title"])
                    for v in data.values():
                        if "major_title" in found: return
                        _find_title(v, found)
                elif isinstance(data, list):
                    for item in data:
                        if "major_title" in found: return
                        _find_title(item, found)

            found_titles = {}
            _find_title(opus_info, found_titles)
            raw_title = found_titles.get("major_title")

        except Exception as e:
            logger.warning(f"扫描标题失败: {e}")

        #判断给的opus链接是专栏还是动态
        is_article = await self._check_is_article(opus_info)

        #先试试能否在接口返回的 json里找到封面,若没有只能从网页拉取，目前是否有其它相关接口提供了封面还不清楚，没去具体查找
        raw_cover_url = None
        if is_article:
            raw_cover_url = await self.__cover_from_json(opus_info)
            if not raw_cover_url:
                raw_cover_url = await self._get_web_cover(bili_opus)
                logger.info(f"获取封面url: {raw_cover_url}")
        
        #加上大小限制免得拿到原图过大
        if raw_cover_url:
            if raw_cover_url.startswith("//"):
                raw_cover_url = f"https:{raw_cover_url}"
            if "@" not in raw_cover_url:
                raw_cover_url += "@700w.webp"

        #下载封面
        cover_task = None
        if raw_cover_url:
            cover_task = self.downloader.download_img(
                raw_cover_url, headers=self.headers, proxy=self.proxy
            )

        # 转换为结构体
        opus_data = convert(opus_info, OpusItem)
        author = self.create_author(*opus_data.name_avatar)

        # 按顺序处理图文内容
        contents: list[MediaContent] = []
        current_text = ""
        full_text = "" #用于全局的提取,因为有些不在一起可能会分开，不然 current_text可能会清空导致提取不到

        for node in opus_data.gen_text_img():
            if isinstance(node, ImageNode):
                contents.append(
                    self.create_graphics_content(
                        node.url, current_text.strip(), node.alt
                    )
                )
                current_text = ""
            elif hasattr(node, "text"):
                text_part = str(node.text)
                current_text += text_part
                full_text += text_part

        # 提取标题，大概类似 #www#这样 -> www,要是多个就凭借起来
        topic_matches = re.findall(r"#([^#]+)#", full_text)
        topics_title = " ".join(topic_matches) if topic_matches else None

        # 排列标题优先级,专栏，动态，最后都没有就默认 b站的标题去掉了小尾巴，去不去都行
        final_title = raw_title
        if not final_title:
            final_title = topics_title
        if not final_title:
            final_title = opus_data.title
            if final_title and final_title.endswith(" - 哔哩哔哩"):
                final_title = final_title.replace(" - 哔哩哔哩", "")

        return self.result(
            title=final_title,
            author=author,
            timestamp=opus_data.timestamp,
            cover=cover_task, #增加封面
            contents=contents,
            text=current_text.strip(),
        )
    
    async def _check_is_article(self, opus_info: dict) -> bool:
        """检查是否为专栏文章

        Args:
            opus_info (dict): Opus 信息字典

        Returns:
            bool: 是否为专栏文章，专栏 comment_type 为固定的12值
        """
        try:
            item_basic = opus_info.get("item", {}).get("basic", {})
            if item_basic.get("comment_type") == 12:
                return True
        except Exception as e:
            logger.warning(f"判断文章类型失败: {e}")
            return False
        return False

    async def __cover_from_json(self, opus_info: dict) -> str | None:
        """尝试从 Opus JSON 中提取封面 URL

        Args:
            opus_info (dict): Opus 信息字典

        Returns:
            str | None: 封面 URL,如果未找到则返回 None
        """
        try:
            modules_data = opus_info.get("item", {}).get("modules", [])
            mod_list = modules_data if isinstance(modules_data, list) else list(modules_data.values()) if isinstance(modules_data, dict) else []
            for mod in mod_list:
                if not isinstance(mod, dict): continue
                major = mod.get("module_dynamic", {}).get("major", {})
                for t in ["opus", "article", "common"]:
                    target = major.get(t, {})
                    if isinstance(target, dict):
                        cov = target.get("cover") or target.get("summary", {}).get("cover")
                        if isinstance(cov, str) and cov:
                            return cov
                        elif isinstance(target.get("covers"), list) and target["covers"]:
                            return str(target["covers"][0])
        except Exception as e:
            logger.warning(f"从接口Json中提取封面失败: {e}")
        return None

    async def _get_web_cover(self, bili_opus: Opus) -> str | None:
        """从网页源码中提取封面 URL，从接口中没找到封面相关的字段，
           就采用这种网页爬虫的方式来提取封面的url链接，若有找到接口以后可以取消掉这个方法
           毕竟不太好，可能会被反爬针对，暂时先这么写着后面花时间再找找

        Args:
            bili_opus (Opus): Opus 对象

        Returns:
            str | None: 封面 URL,如果未找到则返回 None
        """
        import re
        import json
        import aiohttp
        try:
            if not hasattr(bili_opus, "get_opus_id"):
                return None
                
            opus_url = f"https://www.bilibili.com/opus/{bili_opus.get_opus_id()}"
            logger.info(f"使用网页获取封面: {opus_url}")
            
            async with aiohttp.ClientSession() as session:
                proxy_url = getattr(self, "proxy", None)
                req_headers = getattr(self, "headers", {})
                
                async with session.get(opus_url, headers=req_headers, proxy=proxy_url, timeout=5.0) as resp:
                    html_text = await resp.text()
                    
                    # 找寻 dom window.__INITIAL_STATE__
                    state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html_text)
                    if state_match:
                        try:
                            state_json = json.loads(state_match.group(1))
                            cov = state_json.get("detail", {}).get("basic", {}).get("cover") or \
                                  state_json.get("detail", {}).get("modules", [{}])[0].get("module_dynamic", {}).get("major", {}).get("opus", {}).get("summary", {}).get("cover")
                            if cov: return cov
                        except: pass

                    # 匹配 b-img__inner 字段
                    img_match = re.search(r'b-img__inner.*?src="([^"]+?hdslb\.com/bfs/new_dyn/[^"]+)"', html_text, re.S)
                    if img_match: return img_match.group(1)

                    # 上面要是都找不到直接正则匹配图片域名,比较的暴力匹配╰(*°▽°*)╯
                    brute_match = re.search(r'//i0\.hdslb\.com/bfs/new_dyn/[a-zA-Z0-9_\.]+', html_text)
                    if brute_match: return brute_match.group(0)
                    
        except Exception as e:
            logger.warning(f"网页获取失败: {e}")
        return None

    async def parse_live(self, room_id: int):
        """解析直播信息

        Args:
            room_id (int): 直播 id

        Returns:
            ParseResult: 解析结果
        """
        from bilibili_api.live import LiveRoom

        from .live import RoomData

        room = LiveRoom(room_display_id=room_id, credential=await self.login.credential)
        info_dict = await room.get_room_info()

        room_data = convert(info_dict, RoomData)
        contents: list[MediaContent] = []
        # 下载封面
        if cover := room_data.cover:
            cover_task = self.downloader.download_img(
                cover, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(cover_task))

        # 下载关键帧
        if keyframe := room_data.keyframe:
            keyframe_task = self.downloader.download_img(
                keyframe, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(keyframe_task))

        author = self.create_author(room_data.name, room_data.avatar)

        #修改 url直播链接
        # url = f"https://www.bilibili.com/blackboard/live/live-activity-player.html?enterTheRoom=0&cid={room_id}"
        url = f"https://live.bilibili.com/{room_id}"
        return self.result(
            url=url,
            title=room_data.title,
            text=room_data.detail,
            contents=contents,
            author=author,
        )

    async def parse_favlist(self, fav_id: int):
        """解析收藏夹信息

        Args:
            fav_id (int): 收藏夹 id

        Returns:
            list[GraphicsContent]: 图文内容列表
        """
        from bilibili_api.favorite_list import get_video_favorite_list_content

        from .favlist import FavData

        # 只会取一页，20 个
        fav_dict = await get_video_favorite_list_content(fav_id)

        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")

        favdata = convert(fav_dict, FavData)

        return self.result(
            title=favdata.title,
            timestamp=favdata.timestamp,
            author=self.create_author(favdata.info.upper.name, favdata.info.upper.face),
            contents=[
                self.create_graphics_content(fav.cover, fav.desc)
                for fav in favdata.medias
            ],
        )

    async def _get_video(
        self, *, bvid: str | None = None, avid: int | None = None
    ) -> Video:
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
        """
        if avid:
            return Video(aid=avid, credential=await self.login.credential)
        elif bvid:
            return Video(bvid=bvid, credential=await self.login.credential)
        else:
            raise ParseException("avid 和 bvid 至少指定一项")

    async def extract_download_urls(
        self,
        video: Video | None = None,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_index: int = 0,
    ) -> tuple[str, str | None]:
        """解析视频下载链接

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_index (int): 页索引 = 页码 - 1
        """

        from bilibili_api.video import (
            AudioStreamDownloadURL,
            VideoDownloadURLDataDetecter,
            VideoStreamDownloadURL,
        )

        if video is None:
            video = await self._get_video(bvid=bvid, avid=avid)

        # 获取下载数据
        download_url_data = await video.get_download_url(page_index=page_index)
        detecter = VideoDownloadURLDataDetecter(download_url_data)
        streams = detecter.detect_best_streams(
            video_max_quality=self.video_quality,
            codecs=[self.video_codecs],
            no_dolby_video=True,
            no_hdr=True,
        )
        video_stream = streams[0]
        if not isinstance(video_stream, VideoStreamDownloadURL):
            raise DownloadException("未找到可下载的视频流")
        logger.debug(
            f"视频流质量: {video_stream.video_quality.name}, 编码: {video_stream.video_codecs}"
        )

        audio_stream = streams[1]
        if not isinstance(audio_stream, AudioStreamDownloadURL):
            return video_stream.url, None
        logger.debug(f"音频流质量: {audio_stream.audio_quality.name}")
        return video_stream.url, audio_stream.url

    async def _subscription_loop(self):
        """进行订阅轮询

        Args:
            

        Returns:
            
        """
        import traceback
        from bilibili_api import user

        await asyncio.sleep(5.0) # 用float

        while True:
            try:
                poll_queue=[]
                for uid, targets in self.sub_map.items():
                    poll_queue.append((str(uid), targets["groups"], targets["users"]))

                #遍历，请求。rule -> 根据 targets 发送到具体的群或人
                for uid_str, target_groups, target_users in poll_queue:
                    if not uid_str: continue

                    uid = int(str(uid_str).strip())
                    cache_key = str(uid)

                    newest_item = None

                    u = user.User(uid=uid, credential=await self.login.credential)
                    try:
                        resp = await u.get_dynamics_new()
                    except Exception as e:
                        logger.warning(f"[bili_订阅] 获取 UP主 {uid}, 动态失败: {e}")
                        await asyncio.sleep(float(self.sub_delay))
                        continue

                    items = resp.get("items", [])
                    if not items:
                        await asyncio.sleep(float(self.sub_delay))
                        continue

                    #寻找非顶置的最新动态
                    for item in items:
                        try:
                            is_pinned = item["modules"]["module_tag"]["text"] == "置顶"
                        except:
                            is_pinned = False
                        if not is_pinned:
                            newest_item = item
                            break

                    if not newest_item:
                        await asyncio.sleep(float(self.sub_delay))
                        continue

                    #记录id状态
                    dynamic_id = str(newest_item["id_str"])
                    if cache_key not in self._last_dynamic_cache:
                        self._last_dynamic_cache[cache_key] = dynamic_id
                        await self._save_cache()
                        # self._last_dynamic_cache[cache_key]="FORCE_TRIGGER"
                        logger.info(f"[bili_订阅] 初始化记录 UP主 {uid} 最新动态: {dynamic_id}")
                    elif self._last_dynamic_cache[cache_key] != dynamic_id:
                        logger.info(f"[bili_订阅] 检测到发现 UP 主 {uid} 动态更新，id 为: {dynamic_id}")
                        self._last_dynamic_cache[cache_key] = dynamic_id
                        await self._save_cache()

                        try:
                            int_dynamic_id = int(dynamic_id)
                            parsed_result = await self.parse_dynamic(int_dynamic_id)

                            if parsed_result:
                                from ...render import Renderer
                                from ...sender import MessageSender

                                renderer = Renderer(self.cfg)
                                sender = MessageSender(self.cfg, renderer)

                                if not target_groups and not target_users:
                                    continue

                                await sender.send_proactive_msg(
                                    context=self.cfg.context,
                                    result=parsed_result,
                                    sub_groups=list(target_groups),
                                    sub_users=list(target_users),
                                    platforms=self.platforms,
                                    dynamic_id=dynamic_id,
                                    platform_botid=self.platform_botid,
                                    only_previewCard=self.only_previewCard
                                )
                        except Exception as e:
                                logger.error(f"[bili_订阅] 解析并推送动态 {dynamic_id} 失败: {traceback.format_exc()}")

                    await asyncio.sleep(float(self.sub_delay))

            except Exception as e:
                logger.error(f"[bili_订阅] 动态订阅循环发生异常: {traceback.format_exc()}")

            await asyncio.sleep(float(self.sub_interval) * 60.0)

    async def _save_cache(self):
        """将缓存保存到对于data本地数据目录下"""
        import json
        try:
            max_entries = 100
            while len(self._last_dynamic_cache) > max_entries:
                oldest_key = next(iter(self._last_dynamic_cache))
                self._last_dynamic_cache.pop(oldest_key)
                logger.debug(f"数量达到最大 {max_entries}，移除旧纪录: {oldest_key}")

            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._last_dynamic_cache, f, ensure_ascii=False)

        except Exception as e:
            logger.error(f"保存失败: {e}")