import asyncio
import shutil

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .config import PluginConfig


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。
    """

    JOBNAME = "CacheCleaner"

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.scheduler = AsyncIOScheduler(timezone=self.cfg.timezone)
        self.scheduler.start()

        self.register_task()

        logger.info(f"{self.JOBNAME} 已启动，任务周期：{self.cfg.clean_cron}")

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cfg.clean_cron)
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                id=f"{self.JOBNAME}_task",
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
                replace_existing=True,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    async def _clean_plugin_cache(self) -> None:
        """删除并重建缓存目录"""
        loop = asyncio.get_running_loop()

        try:
            def safe_remove():
                if self.cfg.cache_dir.exists():
                    shutil.rmtree(self.cfg.cache_dir, ignore_errors=True)

            await loop.run_in_executor(None, safe_remove)

            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.JOBNAME}] 缓存目录已清理并重建: {self.cfg.cache_dir}")

        except Exception:
            logger.exception(f"[{self.JOBNAME}] 清理缓存目录时发生异常")

    async def stop(self):
        #补上原有缺失的 shutdown
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info(f"[{self.JOBNAME}] 调度器已关闭")
