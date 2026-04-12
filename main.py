"""
AstrBot 自动签到插件
- 通过 WebUI 可视化操作 Camoufox/Firefox 浏览器登录网站
- 支持录制签到点击操作
- 支持 Cron 表达式定时批量签到
- 签到完成后通过机器人消息通知用户
"""

import asyncio
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from .browser_manager import BrowserManager
from .recorder import Recorder, CheckinManager, run_all_checkins, run_checkin, vision_check


# ==================== Cron 解析 ====================

class CronRule:
    """解析并匹配单条 Cron 表达式（分 时 日 月 周）"""

    def __init__(self, expr: str):
        self.expr = expr.strip()
        parts = self.expr.split()
        if len(parts) != 5:
            raise ValueError(f"Cron 表达式必须为 5 个字段: {self.expr}")
        self._minute = self._parse_field(parts[0], 0, 59)
        self._hour = self._parse_field(parts[1], 0, 23)
        self._dom = self._parse_field(parts[2], 1, 31)
        self._month = self._parse_field(parts[3], 1, 12)
        self._dow = self._parse_field(parts[4], 0, 6)  # 0=周日, 1=周一 ... 6=周六

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> set[int]:
        """解析单个 cron 字段，返回匹配的整数集合"""
        result: set[int] = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                continue
            # */n 步进
            if part.startswith("*/"):
                step = int(part[2:])
                if step <= 0:
                    raise ValueError(f"步进值必须 > 0: {part}")
                result.update(range(lo, hi + 1, step))
            # n-m 范围（可带 /step）
            elif "-" in part or "/" in part:
                step = 1
                if "/" in part:
                    range_part, step_str = part.split("/", 1)
                    step = int(step_str)
                else:
                    range_part = part
                if "-" in range_part:
                    a, b = range_part.split("-", 1)
                    result.update(range(int(a), int(b) + 1, step))
                else:
                    result.update(range(int(range_part), hi + 1, step))
            elif part == "*":
                result.update(range(lo, hi + 1))
            else:
                result.add(int(part))
        return result

    def matches(self, dt: datetime) -> bool:
        """判断 datetime 是否匹配本条规则"""
        # 标准 cron: 0=Sun, 1=Mon ... 6=Sat
        # Python isoweekday(): 1=Mon ... 7=Sun → 转为: Sun=0, Mon=1 ... Sat=6
        dow = dt.isoweekday() % 7  # 1→1, 2→2, ... 6→6, 7→0
        return (
            dt.minute in self._minute
            and dt.hour in self._hour
            and dt.day in self._dom
            and dt.month in self._month
            and dow in self._dow
        )

    def __repr__(self):
        return f"CronRule({self.expr!r})"


def parse_cron_rules(text: str) -> list[CronRule]:
    """从多行文本中解析 cron 表达式列表，跳过空行和注释"""
    rules = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append(CronRule(line))
        except (ValueError, IndexError) as e:
            logger.warning(f"无效的 Cron 表达式 '{line}': {e}")
    return rules


def format_cron_for_display(rules: list[CronRule]) -> str:
    """将 cron 规则列表格式化为可读字符串"""
    if not rules:
        return "未配置"
    return ", ".join(r.expr for r in rules)
from .web_server import WebServer


@register(
    "astrbot_plugin_autocheckin",
    "StarDev",
    "多网站每日定时自动签到插件，支持 WebUI 可视化操作与签到流程录制",
    "1.0.1",
    "https://github.com/StarDevProcess/astrbot_plugin_autocheckin",
)
class ForumCheckinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 数据目录（持久化存储，不随插件更新丢失）
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            self.data_dir = str(
                Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_autocheckin"
            )
        except ImportError:
            self.data_dir = str(Path("data") / "plugin_data" / "astrbot_plugin_autocheckin")

        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

        # 配置参数
        self.webui_port = int(config.get("webui_port", 9010))
        self.cron_rules = parse_cron_rules(str(config.get("cron_rules", "30 8 * * *")))
        self.timezone = self._parse_timezone(str(config.get("timezone", "Asia/Shanghai")))
        self.headless = bool(config.get("headless", True))
        self.screenshot_interval = int(config.get("screenshot_interval", 500))
        self.page_load_timeout = int(config.get("page_load_timeout", 30))
        self.action_delay = int(config.get("action_delay", 1000))
        self.checkin_wait = int(config.get("checkin_wait", 5))
        self.use_vision_check = bool(config.get("use_vision_check", False))
        self.vision_model_id = str(config.get("vision_model_id", ""))
        self.browser_idle_timeout = int(config.get("browser_idle_timeout", 10))

        # 核心组件
        self.browser_manager = BrowserManager(
            data_dir=self.data_dir,
            headless=self.headless,
            page_load_timeout=self.page_load_timeout,
        )
        self.recorder = Recorder()
        self.checkin_manager = CheckinManager(data_dir=self.data_dir)
        self.web_server = WebServer(
            browser_manager=self.browser_manager,
            checkin_manager=self.checkin_manager,
            recorder=self.recorder,
            port=self.webui_port,
            screenshot_interval=self.screenshot_interval,
            astrbot_context=self.context,
            use_vision_check=self.use_vision_check,
            vision_model_id=self.vision_model_id,
            checkin_wait=self.checkin_wait,
        )

        # 存储 unified_msg_origin 用于主动发送签到结果
        self._notify_targets: list[str] = []
        self._load_notify_targets()

        # 定时任务
        self._scheduler_task: asyncio.Task | None = None
        self._idle_check_task: asyncio.Task | None = None

    async def initialize(self):
        """插件初始化 - 启动 WebUI 和定时任务"""
        # 确保系统依赖和 Camoufox 浏览器二进制已就绪
        self._ensure_system_deps()
        await self._ensure_camoufox_binary()

        try:
            await self.web_server.start()
            logger.info(f"自动签到 WebUI 已启动: http://0.0.0.0:{self.webui_port}")
        except Exception as e:
            logger.error(f"WebUI 启动失败: {e}")

        # 启动定时签到调度器
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"定时签到已设置: {format_cron_for_display(self.cron_rules)} (时区: {self.timezone})")

        # 启动浏览器空闲自动关闭检查
        if self.browser_idle_timeout > 0:
            self._idle_check_task = asyncio.create_task(self._idle_check_loop())
            logger.info(f"浏览器空闲自动关闭已启用: {self.browser_idle_timeout} 分钟")

    # ==================== 系统依赖 ====================

    @staticmethod
    def _parse_timezone(tz_str: str) -> ZoneInfo:
        """解析时区字符串，无效时回退到 Asia/Shanghai"""
        try:
            return ZoneInfo(tz_str)
        except (KeyError, Exception) as e:
            logger.warning(f"无效的时区 '{tz_str}'，使用默认时区 Asia/Shanghai: {e}")
            return ZoneInfo("Asia/Shanghai")

    def _ensure_system_deps(self):
        """在 Linux 上检查并自动安装 Camoufox 所需的系统库"""
        import platform
        if platform.system() != "Linux":
            return

        import ctypes
        required_libs = [
            "libgtk-3.so.0",
            "libdbus-glib-1.so.2",
            "libasound.so.2",
            "libXcomposite.so.1",
            "libXdamage.so.1",
            "libXrandr.so.2",
            "libgbm.so.1",
            "libpango-1.0.so.0",
            "libatk-1.0.so.0",
            "libatk-bridge-2.0.so.0",
            "libcups.so.2",
        ]
        missing = []
        for lib in required_libs:
            try:
                ctypes.cdll.LoadLibrary(lib)
            except OSError:
                missing.append(lib)

        if not missing:
            return

        logger.info(f"检测到缺少 {len(missing)} 个系统库，正在自动安装...")

        # 检测发行版并选择包管理器
        distro_id = self._detect_distro()
        pm_info = self._get_package_manager(distro_id)

        if not pm_info:
            logger.warning(
                f"未识别的 Linux 发行版 ({distro_id})，请手动安装以下库:\n"
                f"  {', '.join(missing)}")
            return

        pm_name = pm_info["name"]
        packages = pm_info["packages"]
        install_cmd = pm_info["install_cmd"]
        update_cmd = pm_info.get("update_cmd")

        logger.info(f"检测到包管理器: {pm_name}")

        import subprocess
        import shutil

        if not shutil.which(pm_name):
            logger.warning(
                f"未找到包管理器 {pm_name}，请手动安装以下库:\n"
                f"  {', '.join(missing)}")
            return

        try:
            if update_cmd:
                subprocess.run(
                    update_cmd, check=True, capture_output=True, timeout=120,
                )
            subprocess.run(
                install_cmd + packages,
                check=True, capture_output=True, timeout=300,
            )
            logger.info("系统依赖安装完成")
        except subprocess.CalledProcessError:
            manual_cmd = " ".join(install_cmd + packages)
            logger.warning(
                f"自动安装系统依赖失败（可能需要 root 权限），请手动执行:\n"
                f"  {manual_cmd}")
        except Exception as e:
            logger.warning(f"检查系统依赖时出错: {e}")

    @staticmethod
    def _detect_distro() -> str:
        """通过 /etc/os-release 检测 Linux 发行版 ID"""
        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ID="):
                        return line.split("=", 1)[1].strip('"').lower()
                    if line.startswith("ID_LIKE="):
                        return line.split("=", 1)[1].strip('"').lower()
        except FileNotFoundError:
            pass
        return ""

    @staticmethod
    def _get_package_manager(distro_id: str) -> dict | None:
        """根据发行版 ID 返回包管理器信息"""
        # Debian / Ubuntu 系
        apt_packages = [
            "libgtk-3-0", "libdbus-glib-1-2", "libasound2",
            "libx11-xcb1", "libxcomposite1", "libxdamage1", "libxrandr2",
            "libgbm1", "libpango-1.0-0", "libatk1.0-0", "libatk-bridge2.0-0",
            "libcups2", "libxkbcommon0", "libatspi2.0-0",
        ]
        # RHEL / CentOS / Fedora 系
        rpm_packages = [
            "gtk3", "dbus-glib", "alsa-lib",
            "libxcb", "libXcomposite", "libXdamage", "libXrandr",
            "mesa-libgbm", "pango", "atk", "at-spi2-atk",
            "cups-libs", "libxkbcommon", "at-spi2-core",
        ]
        # Arch Linux 系
        pacman_packages = [
            "gtk3", "dbus-glib", "alsa-lib",
            "libxcomposite", "libxdamage", "libxrandr",
            "mesa", "pango", "atk", "at-spi2-atk",
            "libcups", "libxkbcommon", "at-spi2-core",
        ]
        # openSUSE 系
        zypper_packages = [
            "gtk3", "dbus-1-glib", "alsa-lib",
            "libX11-xcb1", "libXcomposite1", "libXdamage1", "libXrandr2",
            "libgbm1", "pango", "atk", "at-spi2-atk",
            "libcups2", "libxkbcommon0", "at-spi2-core",
        ]

        # 发行版 → 包管理器映射
        apt_distros = {"debian", "ubuntu", "linuxmint", "pop", "elementary",
                       "zorin", "kali", "raspbian", "deepin", "uos"}
        dnf_distros = {"fedora"}
        yum_distros = {"rhel", "centos", "amzn", "ol", "rocky", "almalinux",
                       "cloudlinux", "eurolinux", "scientific"}
        pacman_distros = {"arch", "manjaro", "endeavouros", "garuda", "artix"}
        zypper_distros = {"opensuse", "sles", "suse"}

        # 支持 ID_LIKE 包含多个值的情况（如 "rhel fedora"）
        ids = set(distro_id.split())

        if ids & apt_distros or "debian" in distro_id or "ubuntu" in distro_id:
            return {
                "name": "apt-get",
                "packages": apt_packages,
                "update_cmd": ["apt-get", "update", "-qq"],
                "install_cmd": ["apt-get", "install", "-y", "-qq"],
            }
        elif ids & dnf_distros:
            return {
                "name": "dnf",
                "packages": rpm_packages,
                "install_cmd": ["dnf", "install", "-y"],
            }
        elif ids & yum_distros or "rhel" in distro_id:
            # 优先尝试 dnf（RHEL 8+ 默认），回退 yum
            import shutil
            pm = "dnf" if shutil.which("dnf") else "yum"
            return {
                "name": pm,
                "packages": rpm_packages,
                "install_cmd": [pm, "install", "-y"],
            }
        elif ids & pacman_distros or "arch" in distro_id:
            return {
                "name": "pacman",
                "packages": pacman_packages,
                "install_cmd": ["pacman", "-S", "--noconfirm", "--needed"],
                "update_cmd": ["pacman", "-Sy"],
            }
        elif ids & zypper_distros or "suse" in distro_id:
            return {
                "name": "zypper",
                "packages": zypper_packages,
                "install_cmd": ["zypper", "install", "-y"],
                "update_cmd": ["zypper", "refresh"],
            }

        return None

    # ==================== Camoufox 初始化 ====================

    async def _ensure_camoufox_binary(self):
        """检查并自动下载 Camoufox 浏览器二进制"""
        try:
            from camoufox.pkgman import launch_path
            exe_path = launch_path()
            if os.path.exists(exe_path):
                logger.info("Camoufox 浏览器二进制已就绪")
                return
        except Exception:
            pass

        logger.info("正在自动下载 Camoufox 浏览器二进制，首次运行需要等待...")
        try:
            from camoufox.pkgman import CamoufoxFetcher
            fetcher = CamoufoxFetcher()
            fetcher.fetch_latest()
            fetcher.install()
            logger.info("Camoufox 浏览器二进制下载完成")
        except Exception as e:
            logger.error(f"自动下载 Camoufox 浏览器失败: {e}，请手动执行: python -m camoufox fetch")

    # ==================== 定时调度 ====================

    async def _scheduler_loop(self):
        """定时签到调度循环 — 每分钟检查 cron 规则是否匹配"""
        if not self.cron_rules:
            logger.warning("未配置有效的 Cron 规则，定时签到已禁用")
            return

        last_fire_minute = ""  # 防止同一分钟内重复触发

        while True:
            try:
                await asyncio.sleep(30)  # 每 30 秒检查一次
                now = datetime.now(self.timezone)
                minute_key = now.strftime("%Y%m%d%H%M")

                if minute_key == last_fire_minute:
                    continue

                for rule in self.cron_rules:
                    if rule.matches(now):
                        last_fire_minute = minute_key
                        logger.info(f"Cron 规则 [{rule.expr}] 触发签到")
                        await self._do_scheduled_checkin()
                        break  # 同一分钟只触发一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时签到调度异常: {e}")
                await asyncio.sleep(60)

    async def _idle_check_loop(self):
        """定期检查浏览器空闲时间，超时则自动关闭"""
        timeout_seconds = self.browser_idle_timeout * 60
        while True:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                if self.browser_manager.is_running:
                    idle = self.browser_manager.idle_seconds
                    if idle >= timeout_seconds:
                        logger.info(
                            f"浏览器已空闲 {idle/60:.1f} 分钟，自动关闭"
                        )
                        await self.browser_manager.shutdown()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"空闲检查异常: {e}")

    async def _do_scheduled_checkin(self):
        """执行定时签到并发送通知"""
        logger.info("开始执行定时签到...")

        results = await run_all_checkins(
            self.browser_manager, self.checkin_manager, self.action_delay,
            context=self.context, vision_model_id=self.vision_model_id,
            use_vision_check=self.use_vision_check,
            checkin_wait=self.checkin_wait,
        )

        # 如果启用了识图验证，对执行了操作的论坛进行签到后二次验证
        if self.use_vision_check:
            verified_success = []
            for forum_name in list(results.get("success", [])):
                forum = self.checkin_manager.get_forum(forum_name)
                # 预检已通过的（already_checked_in）无需再验证
                if (forum and forum.vision_region and forum.vision_keywords
                        and forum.last_result == "成功"):
                    vr = await vision_check(
                        self.browser_manager, forum,
                        self.context, self.vision_model_id,
                    )
                    if vr["success"]:
                        verified_success.append(forum_name)
                        self.checkin_manager.update_checkin_result(
                            forum_name, f"成功 (识图验证: {vr['matched']})")
                    else:
                        # 签到操作本身已成功，识图验证未确认不翻转为失败
                        verified_success.append(forum_name)
                        reason = vr.get("error") or "未匹配关键词"
                        self.checkin_manager.update_checkin_result(
                            forum_name, f"成功 (识图验证未确认: {reason})")
                else:
                    verified_success.append(forum_name)
            results["success"] = verified_success

        # 构建通知消息
        msg = self._format_checkin_result(results)
        logger.info(f"定时签到完成: {msg}")

        # 发送通知给所有绑定的会话
        await self._send_notifications(msg)

    def _format_checkin_result(self, results: dict) -> str:
        """格式化签到结果为消息文本"""
        success_list = results.get("success", [])
        failed_list = results.get("failed", [])
        message = results.get("message", "")

        if message:
            return f"[自动签到] {message}"

        lines = ["[自动签到] 每日签到执行完毕"]
        lines.append(f"成功: {len(success_list)} | 失败: {len(failed_list)}")

        if success_list:
            lines.append(f"\n已签到: {', '.join(success_list)}")

        if failed_list:
            lines.append("\n未成功列表:")
            for f in failed_list:
                lines.append(f"  - {f['name']}: {f['error']}")

        if not failed_list:
            lines.append("\n全部签到完成!")

        return "\n".join(lines)

    # ==================== 通知管理 ====================

    def _load_notify_targets(self):
        """加载通知目标"""
        import json

        target_file = Path(self.data_dir) / "notify_targets.json"
        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    self._notify_targets = json.load(f)
            except Exception:
                self._notify_targets = []

    def _save_notify_targets(self):
        """保存通知目标"""
        import json

        target_file = Path(self.data_dir) / "notify_targets.json"
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(self._notify_targets, f)

    async def _send_notifications(self, msg: str):
        """向所有绑定的会话发送通知"""
        for umo in self._notify_targets:
            try:
                chain = MessageChain().message(msg)
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.warning(f"发送通知到 {umo} 失败: {e}")

    # ==================== 指令组 ====================

    @filter.command_group("签到")
    def checkin_group(self):
        """自动签到管理"""
        pass

    @checkin_group.command("执行")
    async def cmd_checkin_now(self, event: AstrMessageEvent):
        """立即执行全部签到"""
        forums = self.checkin_manager.get_enabled_forums()
        if not forums:
            yield event.plain_result("没有已启用的论坛。请先在 WebUI 中添加论坛并录制签到操作。")
            return

        yield event.plain_result(
            f"开始签到 {len(forums)} 个论坛，请稍候..."
        )

        results = await run_all_checkins(
            self.browser_manager, self.checkin_manager, self.action_delay,
            checkin_wait=self.checkin_wait,
        )
        msg = self._format_checkin_result(results)
        yield event.plain_result(msg)

    @checkin_group.command("状态")
    async def cmd_checkin_status(self, event: AstrMessageEvent):
        """查看签到状态和论坛列表"""
        forums = self.checkin_manager.get_all_forums()
        if not forums:
            yield event.plain_result(
                "暂无论坛配置。\n"
                f"请访问 WebUI 添加论坛: http://127.0.0.1:{self.webui_port}"
            )
            return

        lines = [
            f"[自动签到] 共 {len(forums)} 个站点",
            f"定时计划: {format_cron_for_display(self.cron_rules)}",
            "",
        ]
        for f in forums:
            if f["last_checkin"]:
                lines.append(f"  {f['name']} | {f['last_result']} | {f['last_checkin']}")
            else:
                lines.append(f"  {f['name']} | 从未签到")

        yield event.plain_result("\n".join(lines))

    @checkin_group.command("绑定")
    async def cmd_checkin_bind(self, event: AstrMessageEvent):
        """绑定当前会话接收签到结果通知"""
        umo = event.unified_msg_origin
        if umo in self._notify_targets:
            yield event.plain_result("当前会话已绑定签到通知。")
            return

        self._notify_targets.append(umo)
        self._save_notify_targets()
        yield event.plain_result(
            "已绑定! 每日签到完成后将在此会话推送结果。\n"
            f"计划: {format_cron_for_display(self.cron_rules)}"
        )

    @checkin_group.command("解绑")
    async def cmd_checkin_unbind(self, event: AstrMessageEvent):
        """解除当前会话的签到通知绑定"""
        umo = event.unified_msg_origin
        if umo not in self._notify_targets:
            yield event.plain_result("当前会话未绑定签到通知。")
            return

        self._notify_targets.remove(umo)
        self._save_notify_targets()
        yield event.plain_result("已解绑签到通知。")

    @checkin_group.command("面板")
    async def cmd_checkin_webui(self, event: AstrMessageEvent):
        """获取 WebUI 控制面板地址"""
        yield event.plain_result(
            f"[自动签到] WebUI 控制面板\n"
            f"地址: http://127.0.0.1:{self.webui_port}\n\n"
            f"功能说明:\n"
            f"1. 启动浏览器后，在画面中操作登录论坛\n"
            f"2. 添加论坛并录制签到点击操作\n"
            f"3. 保存录制后即可自动定时签到"
        )

    @checkin_group.command("单签")
    async def cmd_checkin_one(self, event: AstrMessageEvent, forum_name: str):
        """签到指定论坛。用法: /签到 单签 论坛名"""
        forum = self.checkin_manager.get_forum(forum_name)
        if not forum:
            yield event.plain_result(f"未找到论坛: {forum_name}")
            return

        if not forum.actions:
            yield event.plain_result(f"{forum_name} 尚未录制签到操作，请先在 WebUI 中录制。")
            return

        yield event.plain_result(f"正在签到: {forum_name}...")

        if not self.browser_manager.is_running:
            try:
                await self.browser_manager.launch()
            except Exception as e:
                yield event.plain_result(f"浏览器启动失败: {e}")
                return

        result = await run_checkin(
            self.browser_manager, forum, self.action_delay,
            checkin_wait=self.checkin_wait,
        )
        if result == "success":
            self.checkin_manager.update_checkin_result(forum_name, "成功")
            yield event.plain_result(f"{forum_name} 签到成功!")
        else:
            self.checkin_manager.update_checkin_result(forum_name, f"失败: {result}")
            yield event.plain_result(f"{forum_name} 签到失败: {result}")

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载/停用时清理资源"""
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        if self._idle_check_task:
            self._idle_check_task.cancel()
            try:
                await self._idle_check_task
            except asyncio.CancelledError:
                pass

        await self.web_server.stop()
        await self.browser_manager.shutdown()
        logger.info("自动签到插件已停止")
