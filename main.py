import os
import time
import json
import asyncio
import aiohttp
from typing import Optional, List

from astrbot.api import logger
from astrbot.api.star import register, Star, Context, StarTools
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain, Image, Video, Reply

PLUGIN_NAME = "astrbot_plugin_seedance_video"
SESSION_TIMEOUT = aiohttp.ClientTimeout(total=30) 

@register(PLUGIN_NAME, "开发者", "火山方舟 Seedance 1.5 Pro 视频生成插件", "1.0.0")
class SeedanceVideoPlugin(Star):
    # 👇 修复点：去掉了强制的 config: dict 参数
    def __init__(self, context: Context):
        super().__init__(context)
        
        # 1. 基础配置
        # 建议直接在这里把你的 API_KEY 填入字符串中，最简单粗暴
        self.api_key = "你的火山方舟_API_KEY_填在这里"  
        self.api_endpoint = "https://ark.cn-beijing.volces.com/api/v3"
        self.model_version = "doubao-seedance-1-5-pro-251215"
        
        # --- 如果你更喜欢用本地 json 存配置，可以取消下面这段代码的注释 ---
        # config_path = os.path.join(os.path.dirname(__file__), "config.json")
        # if os.path.exists(config_path):
        #     with open(config_path, "r", encoding="utf-8") as f:
        #         config = json.load(f)
        #         self.api_key = config.get("VOLC_API_KEY", self.api_key).strip()
        
        # 2. 拼接 API 路径
        self.tasks_url = f"{self.api_endpoint.rstrip('/')}/contents/generations/tasks"
        
        # 3. 会话与状态管理
        self._session: Optional[aiohttp.ClientSession] = None
        self.processing_users = set()

        if not self.api_key or self.api_key.startswith("你的"):
            logger.error(f"[{PLUGIN_NAME}] 警告：API KEY 未配置，功能将无法使用！")
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=SESSION_TIMEOUT)
        return self._session

    async def terminate(self):
        """插件卸载时关闭连接"""
        if self._session and not self._session.closed:
            await self._session.close()

    def _extract_image_url(self, event: AstrMessageEvent) -> str:
        """从消息中提取第一张图片的 URL"""
        if hasattr(event, 'message_obj') and event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, Image):
                    if hasattr(component, 'url') and component.url:
                        return component.url.strip()
        return ""

    def _find_video_url(self, data: dict) -> str:
        """递归查找字典中的 mp4/视频 URL，防止 API 返回结构变动"""
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str) and (v.startswith("http") and (".mp4" in v or "video" in v)):
                    return v
                elif isinstance(v, (dict, list)):
                    res = self._find_video_url(v)
                    if res: return res
        elif isinstance(data, list):
            for item in data:
                res = self._find_video_url(item)
                if res: return res
        return ""

    @filter.command("视频豆包")
    async def generate_video(self, event: AstrMessageEvent, prompt: str = ""):
        """
        触发指令：/视频豆包 [提示词]
        支持附带图片或引用图片进行图生视频
        """
        user_id = event.get_sender_id()
        real_prompt = prompt.strip()
        image_url = self._extract_image_url(event)

        if not self.api_key:
            yield event.plain_result("❌ 管理员未配置火山方舟 API KEY。")
            return

        if not real_prompt and not image_url:
            yield event.plain_result("❌ 请提供视频的画面描述（提示词），例如：/视频豆包 女孩在森林里奔跑")
            return

        if user_id in self.processing_users:
            yield event.plain_result("⏳ 你有一个正在生成的视频任务，请耐心等待完成...")
            return

        self.processing_users.add(user_id)
        
        # 1. 先回复用户，告知任务已受理（视频生成需要几分钟，避免用户以为卡死了）
        yield event.plain_result("🎬 视频任务已提交！\nSeedance 1.5 Pro 正在为您渲染...\n⏳ 预计需要 3~5 分钟，完成后会自动发送，请耐心等待。")

        try:
            # --- 步骤 1：提交任务 ---
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            
            content_list = []
            if real_prompt:
                content_list.append({"type": "text", "text": real_prompt})
            if image_url:
                content_list.append({"type": "image_url", "image_url": {"url": image_url}})

            payload = {
                "model": self.model_version,
                "content": content_list,
                "generate_audio": True,
                "ratio": "adaptive",
                "duration": 5,
                "watermark": False
            }

            async with self.session.post(self.tasks_url, headers=headers, json=payload) as resp:
                res_data = await resp.json()
                if resp.status != 200:
                    raise Exception(res_data.get("error", {}).get("message", f"HTTP {resp.status}"))
                
                # 兼容不同的返回字段名
                task_id = res_data.get("id") or res_data.get("task_id")
                if not task_id:
                    raise Exception(f"未获取到 Task ID: {res_data}")

            logger.info(f"[{PLUGIN_NAME}] 视频任务已提交，Task ID: {task_id}")

            # --- 步骤 2：长轮询检查状态 ---
            video_url = ""
            max_retries = 60 # 最多轮询 60 次 (60 * 10s = 10 分钟)
            
            for _ in range(max_retries):
                await asyncio.sleep(10) # 每 10 秒查询一次
                
                poll_url = f"{self.tasks_url}/{task_id}"
                async with self.session.get(poll_url, headers=headers) as poll_resp:
                    poll_data = await poll_resp.json()
                    
                    status = poll_data.get("status", "").lower()
                    
                    if status in ["succeeded", "success", "completed"]:
                        # 尝试提取视频 URL
                        video_url = self._find_video_url(poll_data)
                        break
                    elif status in ["failed", "error", "cancelled"]:
                        error_msg = poll_data.get("error", {}).get("message", "任务失败被服务端终止")
                        raise Exception(f"生成失败: {error_msg}")
                    # 如果是 pending / running / queued，则继续循环

            if not video_url:
                raise Exception("任务已超时或未在返回数据中找到视频 URL。")

            # --- 步骤 3：发送最终视频 ---
            final_res = event.make_result()
            if hasattr(event.message_obj, 'message_id'):
                final_res.chain.append(Reply(id=event.message_obj.message_id))
            
            # 使用 URL 发送视频 (NapCat 等适配器通常支持直传网络视频 URL)
            final_res.chain.append(Video.fromURL(video_url))
            final_res.chain.append(Plain(text=f"\n✅ 视频生成完毕！\n💡 提示词：{real_prompt or '纯图生视频'}"))
            
            # 【关键】因为这里已经脱离了最开始的 yield 生命周期，必须使用 await event.send() 强制发送
            await event.send(final_res)

        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 视频生成异常: {e}")
            error_res = event.make_result().message(f"❌ 视频生成失败：{str(e)}")
            await event.send(error_res)
            
        finally:
            self.processing_users.discard(user_id)
