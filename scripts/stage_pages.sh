#!/bin/bash
# 把一个通道的通道文件组装成 GitHub Pages 的目录（docs/release-checklist.md 第 5 节“托管”）。只在本机组装，不推送、不启用 Pages。
# 用法：stage_pages.sh <dist 目录> <通道> <Pages 工作目录>
# 结果：<Pages 工作目录>/.nojekyll 与 <Pages 工作目录>/desktop/<通道>/{latest.json, product-manifest.json, product-manifest.json.sig}，
# 对应桌面端读取的 <通道根>/<通道>/…，通道根为 https://<owner>.github.io/<repo>/desktop。
# 更新包不放 Pages：latest.json 里的地址必须是 https 的版本化下载地址（manifest --download-base，例如 GitHub Release 附件）。
set -euo pipefail
dist=${1:?dist 目录}; channel=${2:?通道}; out=${3:?Pages 工作目录}
src="$dist/$channel"
for f in latest.json product-manifest.json product-manifest.json.sig; do
  [ -f "$src/$f" ] || { echo "缺 $src/$f（先运行 build_desktop.py manifest）" >&2; exit 1; }
done
"${PYTHON:-python3}" - "$src/latest.json" <<'EOF'
import json, sys
latest = json.load(open(sys.argv[1], encoding='utf-8'))
urls = [p['url'] for p in latest['platforms'].values()]
bad = [u for u in urls if not u.startswith('https://') or '/releases/download/' not in u]
if bad or not urls:
    sys.exit('latest.json 的更新包地址应指向 https 的版本化下载位置（manifest --download-base）：%s' % (bad or '没有平台'))
print('latest.json %s：%s' % (latest['version'], ', '.join(sorted(latest['platforms']))))
EOF
mkdir -p "$out/desktop/$channel"
touch "$out/.nojekyll"
cp "$src/latest.json" "$src/product-manifest.json" "$src/product-manifest.json.sig" "$out/desktop/$channel/"
(cd "$out/desktop/$channel" && for f in latest.json product-manifest.json product-manifest.json.sig; do echo "$(shasum -a 256 < "$f" | cut -c1-64)  desktop/$channel/$f"; done)
