"""
Web UI 启动入口
"""

import uvicorn
import logging
import socket
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
# PyInstaller 打包后 __file__ 在临时解压目录，需要用 sys.executable 所在目录作为数据目录
import os
if getattr(sys, 'frozen', False):
    # 打包后：使用可执行文件所在目录
    project_root = Path(sys.executable).parent
    _src_root = Path(sys._MEIPASS)
else:
    project_root = Path(__file__).parent
    _src_root = project_root
sys.path.insert(0, str(_src_root))

from src.core.utils import setup_logging
from src.core.timezone_utils import apply_process_timezone
from src.core.db_logs import install_database_log_handler
from src.database.init_db import initialize_database
from src.config.settings import get_settings
from src.config.project_notice import build_terminal_notice_lines


def _print_project_notice():
    """Print the project notice to the terminal on startup."""
    for line in build_terminal_notice_lines():
        print(line)


def _load_dotenv():
    """加载 .env 文件（可执行文件同目录或项目根目录）"""
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _socket_family_for_host(host: str) -> socket.AddressFamily:
    """根据监听地址推断 socket family。"""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _open_probe_socket(host: str, port: int) -> socket.socket:
    """打开一个临时探测 socket，用于判断端口是否可绑定。"""
    bind_host = host or "0.0.0.0"
    sock = socket.socket(_socket_family_for_host(bind_host), socket.SOCK_STREAM)

    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    elif hasattr(socket, "SO_REUSEADDR"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    if sock.family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

    sock.bind((bind_host, port))
    return sock


def _candidate_fallback_ports(preferred_port: int):
    """优先尝试邻近端口，再退回系统分配。"""
    upper_bound = min(preferred_port + 20, 65535)
    for candidate in range(preferred_port + 1, upper_bound + 1):
        yield candidate
    yield 0


def _select_webui_port(host: str, preferred_port: int) -> tuple[int, bool]:
    """优先使用配置端口；若被占用则自动切换到同 host 下的空闲端口。"""
    probe = None
    bind_host = host or "0.0.0.0"

    try:
        probe = _open_probe_socket(bind_host, preferred_port)
        return preferred_port, False
    except OSError:
        if probe is not None:
            probe.close()

        for fallback_port in _candidate_fallback_ports(preferred_port):
            fallback_probe = None
            try:
                fallback_probe = _open_probe_socket(bind_host, fallback_port)
                return fallback_probe.getsockname()[1], True
            except OSError:
                continue
            finally:
                if fallback_probe is not None:
                    fallback_probe.close()

        raise
    finally:
        if probe is not None:
            probe.close()


def _format_access_host(host: str) -> str:
    """将通配监听地址转换为更适合展示的访问地址。"""
    if host in ("0.0.0.0", "", None):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    return host


def setup_application():
    """设置应用程序"""
    # 统一进程时区为北京时间，避免容器默认 UTC 导致时间错位
    apply_process_timezone()

    # 加载 .env 文件（优先级低于已有环境变量）
    _load_dotenv()

    # 确保数据目录和日志目录在可执行文件所在目录（打包后也适用）
    data_dir = project_root / "data"
    logs_dir = project_root / "logs"
    data_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    # 将数据目录路径注入环境变量，供数据库配置使用
    os.environ.setdefault("APP_DATA_DIR", str(data_dir))
    os.environ.setdefault("APP_LOGS_DIR", str(logs_dir))

    # 初始化数据库（必须先于获取设置）
    try:
        initialize_database()
    except Exception as e:
        print(f"数据库初始化失败: {e}")
        raise

    # 获取配置（需要数据库已初始化）
    settings = get_settings()

    # 配置日志（日志文件写到实际 logs 目录）
    log_file = str(logs_dir / Path(settings.log_file).name)
    setup_logging(
        log_level=settings.log_level,
        log_file=log_file
    )
    install_database_log_handler()

    logger = logging.getLogger(__name__)
    logger.info("数据库初始化完成，地基已经打好")
    logger.info(f"数据目录已安顿好: {data_dir}")
    logger.info(f"日志目录也已就位: {logs_dir}")

    logger.info("应用程序设置完成，齿轮已经咔哒一声卡上了")
    return settings


def start_webui():
    _print_project_notice()
    """启动 Web UI"""
    # 设置应用程序
    settings = setup_application()

    # 导入 FastAPI 应用（延迟导入以避免循环依赖）
    from src.web.app import app

    # 配置 uvicorn
    uvicorn_config = {
        "app": "src.web.app:app",
        "host": settings.webui_host,
        "port": settings.webui_port,
        "reload": settings.debug,
        "log_level": "info" if settings.debug else "warning",
        "access_log": settings.debug,
        "ws": "websockets",
    }

    logger = logging.getLogger(__name__)
    selected_port, port_switched = _select_webui_port(settings.webui_host, settings.webui_port)
    if port_switched:
        logger.warning(
            f"Web UI 端口 {settings.webui_port} 已被占用，已自动切换到可用端口 {selected_port}"
        )
        settings.webui_port = selected_port
        uvicorn_config["port"] = selected_port

    access_host = _format_access_host(settings.webui_host)
    logger.info(f"Web UI 已就位，请走这边: http://{access_host}:{uvicorn_config['port']}")
    logger.info(f"调试模式: {settings.debug}")

    # 启动服务器
    uvicorn.run(**uvicorn_config)


def main():
    """主函数"""
    import argparse
    import os

    parser = argparse.ArgumentParser(description="OpenAI/Codex CLI 自动注册系统 Web UI")
    parser.add_argument("--host", help="监听主机 (也可通过 WEBUI_HOST 环境变量设置)")
    parser.add_argument("--port", type=int, help="监听端口 (也可通过 WEBUI_PORT 环境变量设置)")
    parser.add_argument("--debug", action="store_true", help="启用调试模式 (也可通过 DEBUG=1 环境变量设置)")
    parser.add_argument("--reload", action="store_true", help="启用热重载")
    parser.add_argument("--log-level", help="日志级别 (也可通过 LOG_LEVEL 环境变量设置)")
    parser.add_argument("--access-password", help="Web UI 访问密钥 (也可通过 WEBUI_ACCESS_PASSWORD 环境变量设置)")
    args = parser.parse_args()

    # 更新配置
    from src.config.settings import update_settings

    updates = {}
    
    # 优先使用命令行参数，如果没有则尝试从环境变量获取
    host = args.host or os.environ.get("WEBUI_HOST")
    if host:
        updates["webui_host"] = host
        
    port = args.port or os.environ.get("WEBUI_PORT")
    if port:
        updates["webui_port"] = int(port)
        
    debug = args.debug or os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    if debug:
        updates["debug"] = debug
        
    log_level = args.log_level or os.environ.get("LOG_LEVEL")
    if log_level:
        updates["log_level"] = log_level
        
    access_password = args.access_password or os.environ.get("WEBUI_ACCESS_PASSWORD")
    if access_password:
        updates["webui_access_password"] = access_password

    if updates:
        update_settings(**updates)

    # 启动 Web UI
    start_webui()


if __name__ == "__main__":
    # PyInstaller 打包后 Windows 上 uvicorn 可能拉起多进程，
    # 这里先做 freeze_support，避免 multiprocessing-fork 参数报错。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
