# -*- coding: utf-8 -*-
"""端点配置**模板** —— 复制成 `_endpoints.py` 后按自己的环境改。

为什么要模板：真实的 `_endpoints.py` 里有自建下载站地址，属于**私有信息**，
已被 .gitignore 排除、不入公开仓库。但仓库里如果完全不提这个文件，
clone 下来的人会看到 `autoupdate.py` 里 `from _endpoints import SITE_URL`
的 import 一直走 except 分支 —— 不知道是「没配」还是「配错了」。

用法：
    cp _endpoints.example.py _endpoints.py
    # 然后把 SITE_URL 改成你自己的下载站（或保持空串，只走 GitHub）

说明：
  * **不配也能用**：缺失时该源自动禁用，检查更新/下载仍走 GitHub 与公开镜像。
  * 末尾写不写 "/" 都行（autoupdate._site_base() 会统一去掉）。
  * 这个文件只放「地址」这类非机密内容；不要把 token / 密钥写进来。
"""

# 自建下载站根地址。留空表示不启用该源。
# 例：SITE_URL = "https://download.example.com"
SITE_URL = ""
