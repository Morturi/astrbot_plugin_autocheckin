"""
签到操作录制与回放模块
- 录制模式：捕获用户的点击、输入、导航操作，保存为 JSON 动作序列
- 回放模式：按顺序重放录制的动作序列
"""

import json
import time
import asyncio
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class Action:
    """单个操作动作"""
    type: str  # click, type, press_key, navigate, scroll, wait
    timestamp: float = 0.0
    x: float = 0.0
    y: float = 0.0
    text: str = ""
    selector: str = ""
    url: str = ""
    key: str = ""
    delta_y: float = 0.0
    delay: float = 0.0  # 与上一个动作的时间间隔（秒）
    element_info: dict = field(default_factory=dict)


@dataclass
class ForumConfig:
    """签到配置"""
    name: str
    url: str
    actions: list = field(default_factory=list)  # List[dict] (Action 的序列化)
    enabled: bool = True
    last_checkin: str = ""  # 最后签到时间
    last_result: str = ""  # 最后签到结果
    vision_region: dict = field(default_factory=dict)  # {x, y, width, height}
    vision_keywords: str = ""  # 识图关键词/正则表达式


class Recorder:
    """操作录制器"""

    def __init__(self):
        self.is_recording = False
        self.actions: list[Action] = []
        self._start_time: float = 0.0
        self._last_action_time: float = 0.0

    def start(self):
        """开始录制"""
        self.is_recording = True
        self.actions = []
        self._start_time = time.time()
        self._last_action_time = self._start_time
        logger.info("开始录制签到操作")

    def stop(self) -> list[dict]:
        """停止录制并返回动作列表"""
        self.is_recording = False
        result = [asdict(a) for a in self.actions]
        logger.info(f"录制结束，共 {len(result)} 个操作")
        return result

    def record_action(self, action_type: str, **kwargs):
        """记录一个操作"""
        if not self.is_recording:
            return

        now = time.time()
        delay = now - self._last_action_time
        self._last_action_time = now

        action = Action(
            type=action_type,
            timestamp=now,
            delay=round(delay, 2),
            **kwargs,
        )
        self.actions.append(action)

    def record_click(self, x: float, y: float, selector: str = "", element_info: dict = None):
        self.record_action("click", x=x, y=y, selector=selector,
                           element_info=element_info or {})

    def record_type(self, text: str):
        self.record_action("type", text=text)

    def record_key(self, key: str):
        self.record_action("press_key", key=key)

    def record_navigate(self, url: str):
        self.record_action("navigate", url=url)

    def record_scroll(self, x: float, y: float, delta_y: float):
        self.record_action("scroll", x=x, y=y, delta_y=delta_y)

    def record_drag(self, from_x: float, from_y: float, to_x: float, to_y: float):
        self.record_action("drag", x=from_x, y=from_y,
                           element_info={"toX": to_x, "toY": to_y})

    def record_wait(self, seconds: float):
        self.record_action("wait", delay=seconds)


class CheckinManager:
    """签到管理器 - 管理论坛列表和执行签到"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_dir / "forums.json"
        self.forums: list[ForumConfig] = []
        self._load_forums()

    def _load_forums(self):
        """加载论坛配置"""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.forums = [ForumConfig(**d) for d in data]
                logger.info(f"已加载 {len(self.forums)} 个论坛配置")
            except Exception as e:
                logger.error(f"加载论坛配置失败: {e}")
                self.forums = []
        else:
            self.forums = []

    def _save_forums(self):
        """保存论坛配置"""
        try:
            data = []
            for f in self.forums:
                d = {
                    "name": f.name,
                    "url": f.url,
                    "actions": f.actions,
                    "enabled": f.enabled,
                    "last_checkin": f.last_checkin,
                    "last_result": f.last_result,
                    "vision_region": f.vision_region,
                    "vision_keywords": f.vision_keywords,
                }
                data.append(d)
            with open(self.config_file, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存论坛配置失败: {e}")

    def add_forum(self, name: str, url: str) -> ForumConfig:
        """添加论坛"""
        forum = ForumConfig(name=name, url=url)
        self.forums.append(forum)
        self._save_forums()
        return forum

    def remove_forum(self, name: str) -> bool:
        """移除论坛"""
        for i, f in enumerate(self.forums):
            if f.name == name:
                self.forums.pop(i)
                self._save_forums()
                return True
        return False

    def get_forum(self, name: str) -> Optional[ForumConfig]:
        """获取指定论坛"""
        for f in self.forums:
            if f.name == name:
                return f
        return None

    def update_forum_actions(self, name: str, actions: list[dict]):
        """更新论坛的签到操作"""
        forum = self.get_forum(name)
        if forum:
            forum.actions = actions
            self._save_forums()

    def update_checkin_result(self, name: str, result: str):
        """更新签到结果"""
        forum = self.get_forum(name)
        if forum:
            from datetime import datetime
            forum.last_checkin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            forum.last_result = result
            self._save_forums()

    def get_enabled_forums(self) -> list[ForumConfig]:
        """获取所有启用的论坛"""
        return [f for f in self.forums if f.enabled]

    def get_all_forums(self) -> list[dict]:
        """获取所有论坛的摘要信息"""
        result = []
        for f in self.forums:
            result.append({
                "name": f.name,
                "url": f.url,
                "enabled": f.enabled,
                "has_actions": len(f.actions) > 0,
                "action_count": len(f.actions),
                "last_checkin": f.last_checkin,
                "last_result": f.last_result,
                "vision_region": f.vision_region,
                "vision_keywords": f.vision_keywords,
            })
        return result

    def toggle_forum(self, name: str) -> bool:
        """切换论坛启用/禁用状态"""
        forum = self.get_forum(name)
        if forum:
            forum.enabled = not forum.enabled
            self._save_forums()
            return True
        return False

    def update_forum_vision(self, name: str, region: dict, keywords: str):
        """更新论坛的识图选区和关键词"""
        forum = self.get_forum(name)
        if forum:
            forum.vision_region = region
            forum.vision_keywords = keywords
            self._save_forums()


async def run_checkin(browser_manager, forum: ForumConfig, action_delay: int = 1000,
                      context=None, vision_model_id: str = "",
                      use_vision_check: bool = False,
                      checkin_wait: int = 5) -> str:
    """
    执行单个论坛的签到操作

    Returns:
        str: "success" 或 "already_checked_in" 或 错误描述
    """
    page = browser_manager.page
    if not page or page.is_closed():
        return "浏览器未启动"

    if not forum.actions:
        return "未录制签到操作"

    try:
        # 导航到论坛
        logger.info(f"正在签到: {forum.name} ({forum.url})")
        try:
            await page.goto(forum.url, wait_until="domcontentloaded",
                            timeout=browser_manager.page_load_timeout)
        except Exception as e:
            return f"页面加载失败: {str(e)[:50]}"

        # 等待页面完全加载
        if checkin_wait > 0:
            logger.info(f"[{forum.name}] 等待页面加载 {checkin_wait} 秒...")
            await asyncio.sleep(checkin_wait)

        # 签到前识图预检：如果已签到则跳过操作
        if (use_vision_check and context and
                forum.vision_region and forum.vision_keywords):
            try:
                logger.info(f"[{forum.name}] 执行签到前识图预检...")
                pre_result = await vision_check(
                    browser_manager, forum, context, vision_model_id)
                if pre_result["success"]:
                    logger.info(
                        f"[{forum.name}] 识图预检发现已签到: {pre_result['matched']}，跳过操作")
                    return "already_checked_in"
                else:
                    reason = pre_result.get("error") or "关键词未匹配"
                    logger.info(f"[{forum.name}] 识图预检未检测到已签到 ({reason})，继续执行签到")
            except Exception as e:
                logger.warning(f"[{forum.name}] 识图预检出错，跳过预检继续签到: {e}")

        # 按顺序执行录制的操作
        for i, action_dict in enumerate(forum.actions):
            action_type = action_dict.get("type", "")
            delay = action_dict.get("delay", action_delay / 1000)
            # 使用录制的延迟或配置的最小延迟
            wait_time = max(delay, action_delay / 1000)
            await asyncio.sleep(wait_time)

            try:
                if action_type == "click":
                    selector = action_dict.get("selector", "")
                    x = action_dict.get("x", 0)
                    y = action_dict.get("y", 0)

                    if selector:
                        # 优先使用选择器（更稳定）
                        try:
                            el = page.locator(selector).first
                            await el.wait_for(state="visible", timeout=5000)
                            await el.click(timeout=5000)
                        except Exception:
                            # 选择器失败则用坐标
                            await page.mouse.click(x, y)
                    else:
                        await page.mouse.click(x, y)

                elif action_type == "type":
                    text = action_dict.get("text", "")
                    await page.keyboard.type(text, delay=50)

                elif action_type == "press_key":
                    key = action_dict.get("key", "")
                    await page.keyboard.press(key)

                elif action_type == "navigate":
                    url = action_dict.get("url", "")
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=browser_manager.page_load_timeout)

                elif action_type == "scroll":
                    delta_y = action_dict.get("delta_y", 0)
                    await page.mouse.wheel(0, delta_y)

                elif action_type == "drag":
                    fx = action_dict.get("x", 0)
                    fy = action_dict.get("y", 0)
                    ei = action_dict.get("element_info", {})
                    tx = ei.get("toX", action_dict.get("toX", fx))
                    ty = ei.get("toY", action_dict.get("toY", fy))
                    await page.mouse.move(fx, fy)
                    await page.mouse.down()
                    steps = max(int(((tx-fx)**2+(ty-fy)**2)**0.5/20), 5)
                    for s in range(1, steps+1):
                        mx = fx + (tx-fx)*s/steps
                        my = fy + (ty-fy)*s/steps
                        await page.mouse.move(mx, my)
                        await asyncio.sleep(0.01)
                    await page.mouse.move(tx, ty)
                    await page.mouse.up()

                elif action_type == "wait":
                    extra_wait = action_dict.get("delay", 1)
                    await asyncio.sleep(extra_wait)

            except Exception as e:
                logger.warning(f"执行操作 {i+1}/{len(forum.actions)} ({action_type}) 失败: {e}")
                # 继续执行后续操作，不中断

        await asyncio.sleep(2)  # 等待最后的操作生效
        logger.info(f"签到完成: {forum.name}")
        return "success"

    except Exception as e:
        error_msg = f"签到异常: {str(e)[:100]}"
        logger.error(f"{forum.name} {error_msg}")
        return error_msg


async def run_all_checkins(browser_manager, checkin_manager: CheckinManager,
                           action_delay: int = 1000,
                           context=None, vision_model_id: str = "",
                           use_vision_check: bool = False,
                           checkin_wait: int = 5) -> dict:
    """
    执行所有启用论坛的签到

    Returns:
        dict: {"success": [...], "failed": [{"name": ..., "error": ...}]}
    """
    forums = checkin_manager.get_enabled_forums()
    if not forums:
        return {"success": [], "failed": [], "message": "没有启用的论坛"}

    # 确保浏览器已启动
    if not browser_manager.is_running:
        try:
            await browser_manager.launch()
        except Exception as e:
            return {"success": [], "failed": [], "message": f"浏览器启动失败: {e}"}

    results = {"success": [], "failed": []}

    for forum in forums:
        result = await run_checkin(
            browser_manager, forum, action_delay,
            context=context, vision_model_id=vision_model_id,
            use_vision_check=use_vision_check,
            checkin_wait=checkin_wait,
        )
        if result in ("success", "already_checked_in"):
            results["success"].append(forum.name)
            msg = "成功 (已签到，跳过)" if result == "already_checked_in" else "成功"
            checkin_manager.update_checkin_result(forum.name, msg)
        else:
            results["failed"].append({"name": forum.name, "error": result})
            checkin_manager.update_checkin_result(forum.name, f"失败: {result}")

        # 论坛之间间隔一段时间
        await asyncio.sleep(3)

    return results


import re
import base64


async def vision_check(browser_manager, forum: ForumConfig,
                       context, vision_model_id: str = "") -> dict:
    """
    使用多模态大模型识别签到结果

    Args:
        browser_manager: 浏览器管理器
        forum: 论坛配置（含 vision_region 和 vision_keywords）
        context: AstrBot Context（用于调用 LLM）
        vision_model_id: 模型 ID，留空使用默认模型

    Returns:
        dict: {"success": bool, "llm_text": str, "matched": str, "image_b64": str}
    """
    region = forum.vision_region
    if not region or not region.get("width") or not region.get("height"):
        return {"success": False, "llm_text": "", "matched": "", "error": "未设置识图选区", "image_b64": ""}

    page = browser_manager.page
    if not page or page.is_closed():
        return {"success": False, "llm_text": "", "matched": "", "error": "浏览器未启动", "image_b64": ""}

    try:
        # 截取指定区域的截图
        clip = {
            "x": region["x"], "y": region["y"],
            "width": region["width"], "height": region["height"],
        }
        img_bytes = await page.screenshot(type="jpeg", quality=80, clip=clip, scale="css")
        img_b64 = base64.b64encode(img_bytes).decode()

        # 构造多模态消息发送给 LLM
        from astrbot.core.agent.message import UserMessageSegment, TextPart, ImageURLPart

        prompt_text = (
            "请仔细查看这张截图，识别其中所有可见的文字内容。"
            "请直接逐行列出你看到的所有文字，不要遗漏任何文字，不要添加解释或分析。"
        )

        user_msg = UserMessageSegment(content=[
            ImageURLPart(image_url=ImageURLPart.ImageURL(
                url=f"data:image/jpeg;base64,{img_b64}"
            )),
            TextPart(text=prompt_text),
        ])

        # 确定使用的模型 ID
        model_id = vision_model_id
        if not model_id:
            # 使用默认模型（需要一个 umo，这里用空字符串尝试）
            try:
                model_id = await context.get_current_chat_provider_id(umo="")
            except Exception:
                return {"success": False, "llm_text": "", "matched": "",
                        "error": "未配置识图模型且无法获取默认模型", "image_b64": img_b64}

        llm_resp = await context.llm_generate(
            chat_provider_id=model_id,
            contexts=[user_msg],
        )
        llm_text = llm_resp.completion_text or ""

        # 关键词/正则匹配
        keywords = forum.vision_keywords.strip()
        if not keywords:
            return {"success": True, "llm_text": llm_text, "matched": "",
                    "error": "未设置识图关键词，仅返回识别结果", "image_b64": img_b64}

        matched = ""
        try:
            pattern = re.compile(keywords)
            match = pattern.search(llm_text)
            if match:
                matched = match.group(0)
        except re.error:
            # 正则无效时当普通文本匹配
            if keywords in llm_text:
                matched = keywords

        return {
            "success": bool(matched),
            "llm_text": llm_text,
            "matched": matched,
            "error": "",
            "image_b64": img_b64,
        }

    except Exception as e:
        logger.error(f"识图验证失败: {e}")
        return {"success": False, "llm_text": "", "matched": "", "error": str(e), "image_b64": ""}
