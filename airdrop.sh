#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "未找到 docker compose 或 docker-compose，请先安装 Docker Compose。" >&2
    exit 1
fi

wait_for_health() {
    for _ in {1..30}; do
        if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
            echo "AirDrop 已启动： http://localhost:8000"
            return 0
        fi
        sleep 2
    done

    echo "AirDrop 启动失败，最近日志：" >&2
    "${COMPOSE[@]}" logs --tail=80 airdrop >&2 || true
    return 1
}

start_airdrop() {
    if ! "${COMPOSE[@]}" up -d --build; then
        if [[ "${COMPOSE[0]}" == "docker-compose" ]]; then
            echo "检测到旧版 docker-compose 重建兼容问题，清理容器实例后重试……"
            "${COMPOSE[@]}" down --remove-orphans
            "${COMPOSE[@]}" up -d --build
        else
            return 1
        fi
    fi
    wait_for_health
}

case "${1:-start}" in
    start|up)
        start_airdrop
        ;;
    restart)
        "${COMPOSE[@]}" down --remove-orphans
        start_airdrop
        ;;
    stop)
        "${COMPOSE[@]}" stop
        ;;
    status)
        "${COMPOSE[@]}" ps
        ;;
    logs)
        "${COMPOSE[@]}" logs -f --tail=100 airdrop
        ;;
    help|-h|--help)
        echo "用法：./airdrop.sh [start|restart|stop|status|logs]"
        ;;
    *)
        echo "未知命令：$1" >&2
        echo "用法：./airdrop.sh [start|restart|stop|status|logs]" >&2
        exit 2
        ;;
esac
