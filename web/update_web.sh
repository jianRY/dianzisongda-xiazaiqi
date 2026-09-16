#!/bin/bash
# ============================================================
# 法院文书下载器 · 官网更新脚本（宝塔面板专用）
# ------------------------------------------------------------
# 用法（两步）：
#   1. 把下面 WEB_DIR 改成你的网站根目录（宝塔里站点对应的目录）
#   2. 宝塔面板 → 计划任务 → 添加任务 → 任务类型选「Shell 脚本」
#      把本脚本全部内容粘贴进去保存即可
#      · 想自动更新：执行周期随便设（比如每天凌晨 1 次）
#      · 想手动更新：点任务的「执行」按钮
# ------------------------------------------------------------
# 脚本做什么：
#   从 GitHub 拉取最新的网页文件（优先官方源，失败自动切换
#   jsDelivr 国内镜像），校验完整后备份旧网页再原子替换。
#   任何一步失败都会保留原网页，绝不可能把网站搞挂。
# ============================================================

WEB_DIR="/www/wwwroot/你的站点目录"      # ←←← 改成你的网站根目录！
FILE="index.html"
TMP="$WEB_DIR/$FILE.tmp"
BAK_DIR="$WEB_DIR/_webbak"
KEEP_BAK=5                                # 备份保留份数

mkdir -p "$BAK_DIR" 2>/dev/null

echo "[$(date '+%F %T')] 开始更新官网网页 ..."

# ---------- 1. 拉取最新网页（双源容灾） ----------
OK=""
for URL in \
  "https://raw.githubusercontent.com/jianRY/dianzisongda-xiazaiqi/master/web/index.html" \
  "https://cdn.jsdelivr.net/gh/jianRY/dianzisongda-xiazaiqi@master/web/index.html"
do
  echo "  尝试来源: $URL"
  if curl -fsSL --connect-timeout 10 --max-time 60 -o "$TMP" "$URL"; then
    # 校验：非空 + 是完整 HTML
    if [ -s "$TMP" ] && grep -q "<!DOCTYPE html>" "$TMP"; then
      OK="$URL"
      break
    fi
    echo "  内容校验不通过，换下一个源"
  else
    echo "  拉取失败，换下一个源"
  fi
done

if [ -z "$OK" ]; then
  echo "[$(date '+%F %T')] 更新失败：两个源都不可用，保留原网页不变"
  rm -f "$TMP"
  exit 1
fi

# ---------- 2. 备份旧网页（保留最近 N 份） ----------
if [ -f "$WEB_DIR/$FILE" ]; then
  cp -f "$WEB_DIR/$FILE" "$BAK_DIR/$FILE.$(date +%Y%m%d_%H%M%S)"
  ls -1t "$BAK_DIR/$FILE."* 2>/dev/null | tail -n +$((KEEP_BAK + 1)) | xargs -r rm -f
fi

# ---------- 3. 原子替换 ----------
mv -f "$TMP" "$WEB_DIR/$FILE"
chmod 644 "$WEB_DIR/$FILE" 2>/dev/null

echo "[$(date '+%F %T')] 更新成功！来源: $OK"
echo "  网页文件: $WEB_DIR/$FILE"
echo "  历史备份: $BAK_DIR （保留最近 $KEEP_BAK 份）"
exit 0
