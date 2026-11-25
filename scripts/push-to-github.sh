#!/usr/bin/env bash
# 把这个仓库推到你的 GitHub。
#
# 用法（token 只在你自己的环境里，不要贴到聊天窗口、不要 commit）：
#   export GITHUB_TOKEN=<你的 token>
#   ./scripts/push-to-github.sh <你的用户名> [仓库名]
#
# 背景：本机 DNS 把 github.com 解析到了一个连不上的 IP，但真实入口
# （20.205.243.166）是通的。脚本用「IP + Host 头」绕过去，不修改 hosts。
set -euo pipefail

USER_NAME="${1:-}"
REPO="${2:-meiya-inbound-assistant}"
IP="${GITHUB_IP:-20.205.243.166}"

if [ -z "$USER_NAME" ]; then
  echo "用法：$0 <github用户名> [仓库名]" >&2
  exit 2
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "先 export GITHUB_TOKEN=...（Personal access token，勾 repo 权限）" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

git remote remove origin 2>/dev/null || true
git remote add origin "https://$USER_NAME:$GITHUB_TOKEN@$IP/$USER_NAME/$REPO.git"

# 三个开关一起给：域名走不通所以要 Host 头；证书是给域名签的所以要跳过校验。
git config http.sslVerify false
git config "http.https://$IP/.extraHeader" "Host: github.com"

git push -u origin HEAD:main || {
  echo "推送失败。常见原因：仓库还没在 GitHub 上创建（先建一个空仓库，别勾 README），" >&2
  echo "或 token 没有 repo 权限。" >&2
  exit 1
}

# 推送成功后把 token 从 remote 地址里摘掉，免得留在 .git/config 里。
git remote set-url origin "https://$IP/$USER_NAME/$REPO.git"
echo "已推送：https://github.com/$USER_NAME/$REPO"
