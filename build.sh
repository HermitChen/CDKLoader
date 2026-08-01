#!/usr/bin/env bash
# CDK Loader Docker 镜像构建与发布脚本。

if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io}"
NAMESPACE="${NAMESPACE:-hermitchen}"
IMAGE_NAME="${IMAGE_NAME:-cdkloader}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

OPT_TAG="$(date +%Y%m%d)"
OPT_PUSH="0"
OPT_LOAD="0"
OPT_NOLATEST="0"
OPT_PLATFORMS=""

print_help() {
    cat <<EOF
CDK Loader Docker 镜像构建与发布

用法：
  bash build.sh [选项]

选项：
  -tag <YYYYMMDD>       指定镜像日期标签，默认当天日期
  -push                 构建 linux/amd64 和 linux/arm64 并推送到 GHCR
  -load                 将单架构镜像加载到本地 Docker（默认行为）
  -nolatest             不添加或更新 latest 标签
  -platform <列表>      覆盖目标架构，默认 ${PLATFORMS}
  -h, -help, --help     显示本帮助

环境变量：
  REGISTRY              默认 ${REGISTRY}
  NAMESPACE             默认 ${NAMESPACE}
  IMAGE_NAME            默认 ${IMAGE_NAME}
  PLATFORMS             默认 ${PLATFORMS}
  DOCKERFILE            默认 ${DOCKERFILE}

示例：
  bash build.sh
  bash build.sh -tag 20260801 -load
  docker login ghcr.io
  bash build.sh -push
  docker pull ghcr.io/hermitchen/cdkloader:$(date +%Y%m%d)
  docker pull ghcr.io/hermitchen/cdkloader:latest
EOF
}

while [ -n "${1:-}" ]; do
    case "$1" in
        -h|-help|--help)
            print_help
            exit 0
            ;;
        -tag)
            if [ -z "${2:-}" ]; then
                echo "错误：-tag 需要 YYYYMMDD 标签。" >&2
                exit 1
            fi
            OPT_TAG="$2"
            shift
            ;;
        -push)
            OPT_PUSH="1"
            ;;
        -load)
            OPT_LOAD="1"
            ;;
        -nolatest)
            OPT_NOLATEST="1"
            ;;
        -platform)
            if [ -z "${2:-}" ]; then
                echo "错误：-platform 需要目标架构列表。" >&2
                exit 1
            fi
            OPT_PLATFORMS="$2"
            shift
            ;;
        *)
            echo "错误：未知参数 $1" >&2
            print_help >&2
            exit 1
            ;;
    esac
    shift
done

if ! [[ "$OPT_TAG" =~ ^[0-9]{8}$ ]]; then
    echo "错误：镜像标签必须是 YYYYMMDD 格式，当前值：$OPT_TAG" >&2
    exit 1
fi

if [ -n "$OPT_PLATFORMS" ]; then
    PLATFORMS="$OPT_PLATFORMS"
fi

if [ "$OPT_PUSH" = "1" ] && [ "$OPT_LOAD" = "1" ]; then
    echo "错误：-push 和 -load 不能同时使用。" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未找到 docker 命令。" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "错误：Docker 服务未运行。" >&2
    exit 1
fi

IMAGE_REPOSITORY="${REGISTRY}/${NAMESPACE}/${IMAGE_NAME}"
IMAGE_REFERENCE="${IMAGE_REPOSITORY}:${OPT_TAG}"
IMAGE_LATEST="${IMAGE_REPOSITORY}:latest"
BUILD_ARGS=(buildx build --file "$DOCKERFILE" --tag "$IMAGE_REFERENCE")

if [ "$OPT_NOLATEST" = "0" ]; then
    BUILD_ARGS+=(--tag "$IMAGE_LATEST")
fi

if [ "$OPT_PUSH" = "1" ]; then
    BUILD_ARGS+=(--platform "$PLATFORMS" --push)
else
    if [[ "$PLATFORMS" == *,* ]]; then
        echo "本地加载只支持单一架构，使用当前主机架构构建。"
    else
        BUILD_ARGS+=(--platform "$PLATFORMS")
    fi
    BUILD_ARGS+=(--load)
fi

echo "========================================================"
echo "镜像：${IMAGE_REFERENCE}"
if [ "$OPT_NOLATEST" = "0" ]; then
    echo "      ${IMAGE_LATEST}"
fi
echo "Dockerfile：${DOCKERFILE}"
if [ "$OPT_PUSH" = "1" ]; then
    echo "目标架构：${PLATFORMS}"
    echo "输出：推送到 GHCR"
else
    echo "输出：加载到本地 Docker"
fi
echo "========================================================"

cd "$PROJECT_DIR"
docker "${BUILD_ARGS[@]}" .

if [ "$OPT_PUSH" = "1" ]; then
    echo
    echo "镜像已推送：${IMAGE_REFERENCE}"
    if [ "$OPT_NOLATEST" = "0" ]; then
        echo "镜像已推送：${IMAGE_LATEST}"
    fi
    echo "拉取命令：docker pull ${IMAGE_REFERENCE}"
    if [ "$OPT_NOLATEST" = "0" ]; then
        echo "最新版本：docker pull ${IMAGE_LATEST}"
    fi
    echo "部署命令：CDK_LOADER_IMAGE=${IMAGE_REFERENCE} docker compose up -d"
else
    echo "本地镜像构建完成：${IMAGE_REFERENCE}"
fi
