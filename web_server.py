"""
WebUI 服务器 - 提供可视化浏览器控制面板
- 通过 WebSocket 实时推送浏览器截图（模拟 VNC）
- 接收用户的鼠标/键盘事件并转发给 Playwright
- 提供站点管理、录制控制等 REST API
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WebServer:
    """WebUI HTTP + WebSocket 服务器"""

    def __init__(self, browser_manager, checkin_manager, recorder,
                 port: int = 9010, screenshot_interval: int = 500,
                 action_delay: int = 1000,
                 webui_token: str = "sk-change-me",
                 webui_session_timeout: int = 30,
                 astrbot_context=None, use_vision_check: bool = False,
                 vision_model_id: str = "", checkin_wait: int = 5):
        self.browser_manager = browser_manager
        self.checkin_manager = checkin_manager
        self.recorder = recorder
        self.port = port
        self.screenshot_interval = screenshot_interval / 1000.0  # 转秒
        self.action_delay = action_delay
        self.webui_token = webui_token or "sk-change-me"
        self.webui_session_timeout = max(int(webui_session_timeout), 1) * 60
        self.astrbot_context = astrbot_context
        self.use_vision_check = use_vision_check
        self.vision_model_id = vision_model_id
        self.checkin_wait = checkin_wait
        self._auth_cookie_name = "autocheckin_webui_session"
        self._sessions: dict[str, dict] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._screenshot_task: asyncio.Task | None = None

    async def start(self):
        """启动 Web 服务器"""
        self._app = web.Application(middlewares=[self._auth_middleware])
        self._setup_routes()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"WebUI 已启动: http://0.0.0.0:{self.port}")

    async def stop(self):
        """停止 Web 服务器"""
        if self._screenshot_task:
            self._screenshot_task.cancel()
            try:
                await self._screenshot_task
            except asyncio.CancelledError:
                pass

        for ws in list(self._ws_clients):
            await ws.close()
        self._ws_clients.clear()

        if self._runner:
            await self._runner.cleanup()
        logger.info("WebUI 已停止")

    def _setup_routes(self):
        """设置路由"""
        app = self._app
        # 页面
        app.router.add_get("/", self._handle_index)
        # 认证
        app.router.add_get("/api/auth/status", self._api_auth_status)
        app.router.add_post("/api/auth/login", self._api_auth_login)
        app.router.add_post("/api/auth/logout", self._api_auth_logout)
        # WebSocket - 浏览器画面流
        app.router.add_get("/ws", self._handle_websocket)
        # REST API
        app.router.add_get("/api/status", self._api_status)
        app.router.add_post("/api/browser/launch", self._api_browser_launch)
        app.router.add_post("/api/browser/shutdown", self._api_browser_shutdown)
        app.router.add_post("/api/browser/navigate", self._api_browser_navigate)
        # 站点管理
        app.router.add_get("/api/sites", self._api_get_sites)
        app.router.add_post("/api/sites", self._api_add_site)
        app.router.add_delete("/api/sites/{name}", self._api_remove_site)
        app.router.add_post("/api/sites/{name}/toggle", self._api_toggle_site)
        app.router.add_post("/api/sites/{name}/actions", self._api_save_actions)
        app.router.add_get("/api/sites/{name}/actions", self._api_get_actions)
        # 兼容旧版接口
        app.router.add_get("/api/forums", self._api_get_sites)
        app.router.add_post("/api/forums", self._api_add_site)
        app.router.add_delete("/api/forums/{name}", self._api_remove_site)
        app.router.add_post("/api/forums/{name}/toggle", self._api_toggle_site)
        app.router.add_post("/api/forums/{name}/actions", self._api_save_actions)
        app.router.add_get("/api/forums/{name}/actions", self._api_get_actions)
        # 录制控制
        app.router.add_post("/api/record/start", self._api_record_start)
        app.router.add_post("/api/record/stop", self._api_record_stop)
        app.router.add_get("/api/record/status", self._api_record_status)
        # 签到操作
        app.router.add_post("/api/checkin/{name}", self._api_checkin_one)
        app.router.add_post("/api/checkin", self._api_checkin_all)
        # 识图验证
        app.router.add_post("/api/sites/{name}/vision", self._api_save_vision)
        app.router.add_post("/api/forums/{name}/vision", self._api_save_vision)
        app.router.add_post("/api/vision/test", self._api_vision_test)
        app.router.add_get("/api/vision/config", self._api_vision_config)

    # ==================== 页面 ====================

    async def _handle_index(self, request: web.Request) -> web.Response:
        """主页面"""
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="WebUI template not found", status=404)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """为 WebUI API 和 WebSocket 提供 token 登录鉴权"""
        path = request.path
        if path == "/" or path.startswith("/api/auth/"):
            return await handler(request)

        refresh_activity = path != "/api/status"
        session_id, reason = self._validate_session(
            request, refresh_activity=refresh_activity)
        if not session_id:
            return self._build_unauthorized_response(path, reason)

        request["auth_session_id"] = session_id
        return await handler(request)

    def _build_unauthorized_response(self, path: str, reason: str):
        """构造未登录或会话失效响应"""
        message = self._auth_reason_message(reason)
        if path == "/ws":
            return web.Response(status=401, text=message)
        return web.json_response({
            "success": False,
            "message": message,
            "reason": reason,
        }, status=401)

    @staticmethod
    def _auth_reason_message(reason: str) -> str:
        """将会话失效原因转换为用户可读文案"""
        if reason == "ip_changed":
            return "检测到访问 IP 已变化，请重新登录 WebUI。"
        if reason == "expired":
            return "登录已超时，请重新输入登录密钥。"
        return "未登录或登录已失效，请重新输入登录密钥。"

    @staticmethod
    def _get_client_ip(request: web.Request) -> str:
        """获取当前请求的客户端 IP"""
        forwarded = request.headers.get("X-Forwarded-For", "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote or "unknown"

    def _cleanup_expired_sessions(self):
        """清理已过期的登录会话"""
        now = time.time()
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.get("last_active", 0) >= self.webui_session_timeout
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)

    def _validate_session(self, request: web.Request,
                          refresh_activity: bool = False) -> tuple[str | None, str]:
        """校验登录会话，支持 IP 绑定与空闲超时"""
        self._cleanup_expired_sessions()

        session_id = request.cookies.get(self._auth_cookie_name, "")
        if not session_id:
            return None, "unauthorized"

        session = self._sessions.get(session_id)
        if not session:
            return None, "unauthorized"

        client_ip = self._get_client_ip(request)
        if session.get("ip") != client_ip:
            self._sessions.pop(session_id, None)
            return None, "ip_changed"

        now = time.time()
        if now - session.get("last_active", 0) >= self.webui_session_timeout:
            self._sessions.pop(session_id, None)
            return None, "expired"

        if refresh_activity:
            session["last_active"] = now

        return session_id, ""

    def _set_auth_cookie(self, response: web.StreamResponse, session_id: str):
        """写入 WebUI 登录 cookie"""
        response.set_cookie(
            self._auth_cookie_name,
            session_id,
            max_age=self.webui_session_timeout,
            httponly=True,
            samesite="Lax",
        )

    def _clear_auth_cookie(self, response: web.StreamResponse):
        """清除 WebUI 登录 cookie"""
        response.del_cookie(self._auth_cookie_name)

    async def _api_auth_status(self, request: web.Request) -> web.Response:
        """查询当前登录状态，不刷新会话活动时间"""
        _, reason = self._validate_session(request, refresh_activity=False)
        if reason:
            return web.json_response({
                "authenticated": False,
                "message": self._auth_reason_message(reason),
                "reason": reason,
            })

        return web.json_response({
            "authenticated": True,
            "message": "已登录",
            "timeout_seconds": self.webui_session_timeout,
        })

    async def _api_auth_login(self, request: web.Request) -> web.Response:
        """通过配置的 token 登录 WebUI"""
        body = await request.json()
        token = str(body.get("token", ""))
        if token != self.webui_token:
            return web.json_response({
                "success": False,
                "message": "登录密钥错误",
            }, status=403)

        session_id = secrets.token_urlsafe(24)
        self._sessions[session_id] = {
            "ip": self._get_client_ip(request),
            "last_active": time.time(),
        }

        response = web.json_response({
            "success": True,
            "message": "登录成功",
            "timeout_seconds": self.webui_session_timeout,
        })
        self._set_auth_cookie(response, session_id)
        return response

    async def _api_auth_logout(self, request: web.Request) -> web.Response:
        """退出当前 WebUI 登录"""
        session_id = request.cookies.get(self._auth_cookie_name, "")
        if session_id:
            self._sessions.pop(session_id, None)

        response = web.json_response({
            "success": True,
            "message": "已退出登录",
        })
        self._clear_auth_cookie(response)
        return response

    # ==================== WebSocket ====================

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket 连接 - 推送截图 + 接收用户输入"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)

        # 确保截图推送任务在运行
        if self._screenshot_task is None or self._screenshot_task.done():
            self._screenshot_task = asyncio.create_task(self._screenshot_loop())

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_ws_message(msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.warning(f"WebSocket 错误: {ws.exception()}")
        finally:
            self._ws_clients.discard(ws)

        return ws

    async def _handle_ws_message(self, data: str):
        """处理来自 WebUI 的鼠标/键盘事件"""
        try:
            msg = json.loads(data)
            action = msg.get("action", "")

            if action == "click":
                x, y = msg.get("x", 0), msg.get("y", 0)
                await self.browser_manager.click(x, y)
                # 如果正在录制，记录操作
                if self.recorder.is_recording:
                    element_info = await self.browser_manager.get_element_at(x, y)
                    selector = ""
                    if element_info and element_info.get("selector"):
                        selector = element_info["selector"]
                    self.recorder.record_click(x, y, selector, element_info)

            elif action == "dblclick":
                x, y = msg.get("x", 0), msg.get("y", 0)
                self.browser_manager.touch()
                await self.browser_manager.page.mouse.dblclick(x, y)
                self.browser_manager.touch()

            elif action == "drag":
                # 拖动已通过 mousedown/mousemove/mouseup 实时执行
                # 此消息仅用于录制完整拖动操作
                if self.recorder.is_recording:
                    fx, fy = msg.get("fromX", 0), msg.get("fromY", 0)
                    tx, ty = msg.get("toX", 0), msg.get("toY", 0)
                    self.recorder.record_drag(fx, fy, tx, ty)

            elif action == "mousedown":
                x, y = msg.get("x", 0), msg.get("y", 0)
                await self.browser_manager.mouse_down(x, y)

            elif action == "mousemove":
                x, y = msg.get("x", 0), msg.get("y", 0)
                await self.browser_manager.mouse_move(x, y)

            elif action == "mouseup":
                x, y = msg.get("x", 0), msg.get("y", 0)
                await self.browser_manager.mouse_up(x, y)

            elif action == "type":
                text = msg.get("text", "")
                await self.browser_manager.type_text(text)
                if self.recorder.is_recording:
                    self.recorder.record_type(text)

            elif action == "keydown":
                key = msg.get("key", "")
                await self.browser_manager.press_key(key)
                if self.recorder.is_recording:
                    self.recorder.record_key(key)

            elif action == "scroll":
                x = msg.get("x", 0)
                y = msg.get("y", 0)
                delta_y = msg.get("deltaY", 0)
                await self.browser_manager.scroll(x, y, delta_y)
                if self.recorder.is_recording:
                    self.recorder.record_scroll(x, y, delta_y)

            elif action == "navigate":
                url = msg.get("url", "")
                await self.browser_manager.navigate(url)
                if self.recorder.is_recording:
                    self.recorder.record_navigate(url)

        except Exception as e:
            logger.warning(f"处理 WebSocket 消息失败: {e}")

    async def _screenshot_loop(self):
        """定时截图推送循环"""
        while self._ws_clients:
            try:
                if self.browser_manager.is_running:
                    img_data = await self.browser_manager.screenshot(quality=50)
                    if img_data:
                        # 向所有连接的客户端推送截图
                        dead_clients = set()
                        for ws in self._ws_clients:
                            try:
                                await ws.send_bytes(img_data)
                            except Exception:
                                dead_clients.add(ws)
                        self._ws_clients -= dead_clients
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"截图推送异常: {e}")

            await asyncio.sleep(self.screenshot_interval)

    # ==================== REST API ====================

    async def _api_status(self, request: web.Request) -> web.Response:
        """获取状态"""
        url = await self.browser_manager.get_current_url()
        return web.json_response({
            "browser_running": self.browser_manager.is_running,
            "current_url": url,
            "recording": self.recorder.is_recording,
            "recording_actions": len(self.recorder.actions),
            "site_count": len(self.checkin_manager.sites),
            "enabled_site_count": len(self.checkin_manager.get_enabled_sites()),
            # 兼容旧版前端字段
            "forum_count": len(self.checkin_manager.sites),
            "enabled_count": len(self.checkin_manager.get_enabled_sites()),
        })

    async def _api_browser_launch(self, request: web.Request) -> web.Response:
        """启动浏览器"""
        try:
            await self.browser_manager.launch()
            return web.json_response({"success": True, "message": "浏览器已启动"})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def _api_browser_shutdown(self, request: web.Request) -> web.Response:
        """关闭浏览器"""
        try:
            await self.browser_manager.shutdown()
            return web.json_response({"success": True, "message": "浏览器已关闭"})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def _api_browser_navigate(self, request: web.Request) -> web.Response:
        """导航到指定 URL"""
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return web.json_response({"success": False, "message": "缺少 URL"}, status=400)
        ok = await self.browser_manager.navigate(url)
        return web.json_response({"success": ok})

    # 站点管理 API
    async def _api_get_sites(self, request: web.Request) -> web.Response:
        return web.json_response(self.checkin_manager.get_all_sites())

    async def _api_add_site(self, request: web.Request) -> web.Response:
        body = await request.json()
        name = body.get("name", "").strip()
        url = body.get("url", "").strip()
        if not name or not url:
            return web.json_response({"success": False, "message": "名称和URL不能为空"}, status=400)
        if self.checkin_manager.get_site(name):
            return web.json_response({"success": False, "message": "站点名称已存在"}, status=400)
        self.checkin_manager.add_site(name, url)
        return web.json_response({"success": True})

    async def _api_remove_site(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        ok = self.checkin_manager.remove_site(name)
        return web.json_response({"success": ok})

    async def _api_toggle_site(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        ok = self.checkin_manager.toggle_site(name)
        return web.json_response({"success": ok})

    async def _api_save_actions(self, request: web.Request) -> web.Response:
        """保存录制的操作到指定站点"""
        name = request.match_info["name"]
        body = await request.json()
        actions = body.get("actions", [])
        self.checkin_manager.update_site_actions(name, actions)
        return web.json_response({"success": True, "count": len(actions)})

    async def _api_get_actions(self, request: web.Request) -> web.Response:
        """获取站点的录制操作"""
        name = request.match_info["name"]
        site = self.checkin_manager.get_site(name)
        if not site:
            return web.json_response({"success": False, "message": "站点不存在"}, status=404)
        return web.json_response({"actions": site.actions})

    # 录制控制 API
    async def _api_record_start(self, request: web.Request) -> web.Response:
        self.recorder.start()
        return web.json_response({"success": True})

    async def _api_record_stop(self, request: web.Request) -> web.Response:
        actions = self.recorder.stop()
        return web.json_response({"success": True, "actions": actions, "count": len(actions)})

    async def _api_record_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "recording": self.recorder.is_recording,
            "action_count": len(self.recorder.actions),
        })

    # 签到 API
    async def _api_checkin_one(self, request: web.Request) -> web.Response:
        """手动签到单个站点"""
        from .recorder import execute_site_checkin
        name = request.match_info["name"]
        site = self.checkin_manager.get_site(name)
        if not site:
            return web.json_response({"success": False, "message": "站点不存在"}, status=404)
        if not self.browser_manager.is_running:
            try:
                await self.browser_manager.launch()
            except Exception as e:
                return web.json_response({"success": False, "result": f"浏览器启动失败: {e}"})
        outcome = await execute_site_checkin(
            self.browser_manager, self.checkin_manager, site,
            action_delay=self.action_delay,
            context=self.astrbot_context,
            vision_model_id=self.vision_model_id,
            use_vision_check=self.use_vision_check,
            checkin_wait=self.checkin_wait,
        )

        return web.json_response({
            "success": outcome["success"],
            "result": outcome["result"],
            "vision_image": outcome["vision_image"],
            "vision_text": outcome["vision_text"],
        })

    async def _api_checkin_all(self, request: web.Request) -> web.Response:
        """手动签到所有站点"""
        from .recorder import run_all_checkins

        results = await run_all_checkins(
            self.browser_manager, self.checkin_manager,
            action_delay=self.action_delay,
            context=self.astrbot_context,
            vision_model_id=self.vision_model_id,
            use_vision_check=self.use_vision_check,
            checkin_wait=self.checkin_wait,
        )
        return web.json_response(results)

    # 识图验证 API
    async def _api_vision_config(self, request: web.Request) -> web.Response:
        """获取识图验证配置状态"""
        return web.json_response({
            "enabled": self.use_vision_check,
            "model_id": self.vision_model_id,
        })

    async def _api_save_vision(self, request: web.Request) -> web.Response:
        """保存站点的识图选区和关键词"""
        name = request.match_info["name"]
        site = self.checkin_manager.get_site(name)
        if not site:
            return web.json_response({"success": False, "message": "站点不存在"}, status=404)
        body = await request.json()
        region = body.get("region", {})
        keywords = body.get("keywords", "")
        self.checkin_manager.update_site_vision(name, region, keywords)
        return web.json_response({"success": True})

    async def _api_vision_test(self, request: web.Request) -> web.Response:
        """测试识图验证 - 截取指定区域发送给大模型"""
        from .recorder import vision_check as do_vision_check
        body = await request.json()
        site_name = body.get("site_name", "") or body.get("forum_name", "")
        # 支持临时传入 region/keywords 进行测试
        temp_region = body.get("region")
        temp_keywords = body.get("keywords")

        if site_name:
            site = self.checkin_manager.get_site(site_name)
            if not site:
                return web.json_response({"success": False, "error": "站点不存在"}, status=404)
        else:
            # 没指定站点时用临时参数构造
            from .recorder import SiteConfig
            site = SiteConfig(name="test", url="")

        if temp_region:
            site.vision_region = temp_region
        if temp_keywords is not None:
            site.vision_keywords = temp_keywords

        if not site.vision_region:
            return web.json_response({"success": False, "error": "未设置识图选区"})

        result = await do_vision_check(
            self.browser_manager, site,
            self.astrbot_context, self.vision_model_id,
        )
        return web.json_response(result)
