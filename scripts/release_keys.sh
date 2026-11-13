#!/bin/bash
# 正式发行的两把长期密钥（docs/release-checklist.md 第 5 节“正式资产接入步骤”）：
#   updater.key        Tauri updater 的 minisign 密钥：签更新包与产品发行清单，公钥编进正式版程序
#   release.key        OpenSSH ed25519：签后台发行包，signer 与 namespace 都是 agent-collab-release
# 两把私钥都用各自的口令加密。口令由操作者输入，保存在密码管理器（或系统钥匙串）里，不写进文件、仓库或日志；
# 备份只复制加密后的私钥与公钥，所以只要有备份与口令，就能在另一台机器上恢复，不依赖原机。
#
# 用法（在唯一的签名机上运行，例如 Mac 构建机）：
#   release_keys.sh generate <托管目录>             生成两把密钥、allowed_signers 与 FINGERPRINTS；目录里已有密钥时拒绝
#   release_keys.sh fingerprints <目录>             打印两份公钥的指纹（可以公开）
#   release_keys.sh check <目录>                    用口令解开两把私钥，核对它们与公钥对应（恢复演练也用这一步）
#   release_keys.sh backup <托管目录> <备份目录>     复制加密私钥、公钥、allowed_signers 与指纹，并逐个核对摘要
# 口令：交互时提示输入（不回显）；非交互时从标准输入按行读取（先 updater，再发行签名）。
# 环境：TAURI 是 tauri 命令（缺省 "cargo tauri"）；需要 ssh-keygen、shasum 与 python3。
set -euo pipefail
TAURI=${TAURI:-cargo tauri}
cmd=${1:?generate|fingerprints|check|backup}
dir=${2:?目录}
FILES="updater.key updater.key.pub release.key release.key.pub allowed_signers FINGERPRINTS"

ask() {
  local value
  if [ -t 0 ]; then read -rsp "$1：" value; echo >&2; else IFS= read -r value; fi
  printf %s "$value"
}

# ssh-keygen 没有从环境或标准输入读口令的选项：经 SSH_ASKPASS（OpenSSH 8.4 起的 SSH_ASKPASS_REQUIRE）从环境变量取，
# 不出现在进程参数里。tauri signer sign 从 TAURI_SIGNING_PRIVATE_KEY_PASSWORD 取。
# tauri signer generate 只能用 -p 传口令（生成只在签名机上做一次）。
askpass() {
  export SSH_ASKPASS="$1/askpass" SSH_ASKPASS_REQUIRE=force AC_RELEASE_PASSPHRASE="$2"
  printf '#!/bin/sh\nprintf "%%s\\n" "$AC_RELEASE_PASSPHRASE"\n' > "$SSH_ASKPASS"
  chmod 700 "$SSH_ASKPASS"
}

fingerprints() {
  echo "updater 公钥文件 SHA-256：$(shasum -a 256 < "$1/updater.key.pub" | cut -c1-64)"
  echo "发行签名公钥：$(ssh-keygen -l -f "$1/release.key.pub")"
}

# minisign 公钥与签名都带 8 字节的密钥 ID：签名的 ID 等于公钥的 ID，说明私钥与这份公钥对应。
same_key_id() {
  "${PYTHON:-python3}" - "$1" "$2" <<'EOF'
import base64, sys
def payload(path):
    return base64.b64decode(base64.b64decode(open(path, 'rb').read()).decode().splitlines()[1])
sys.exit(0 if payload(sys.argv[1])[2:10] == payload(sys.argv[2])[2:10] else 1)
EOF
}

case $cmd in
  generate)
    if [ -e "$dir/updater.key" ] || [ -e "$dir/release.key" ]; then echo "$dir 里已有密钥，不覆盖" >&2; exit 1; fi
    mkdir -p "$dir" && chmod 700 "$dir"
    up=$(ask 'updater 私钥口令'); rp=$(ask '发行签名私钥口令')
    if [ -z "$up" ] || [ -z "$rp" ]; then echo '口令不能为空' >&2; exit 1; fi
    t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
    $TAURI signer generate -w "$dir/updater.key" -p "$up" --ci > /dev/null
    askpass "$t" "$rp"
    ssh-keygen -q -t ed25519 -C agent-collab-release -f "$dir/release.key" < /dev/null
    echo "agent-collab-release namespaces=\"agent-collab-release\" $(cut -d' ' -f1-2 "$dir/release.key.pub")" > "$dir/allowed_signers"
    chmod 600 "$dir/updater.key" "$dir/release.key"
    fingerprints "$dir" | tee "$dir/FINGERPRINTS" ;;
  fingerprints)
    fingerprints "$dir" ;;
  check)
    up=$(ask 'updater 私钥口令'); rp=$(ask '发行签名私钥口令')
    t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
    echo check > "$t/updater-sample"
    TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$up" $TAURI signer sign -f "$dir/updater.key" "$t/updater-sample" > /dev/null
    askpass "$t" "$rp"
    same_key_id "$dir/updater.key.pub" "$t/updater-sample.sig" || { echo 'updater 私钥与公钥不对应' >&2; exit 1; }
    # 密钥 ID 不能证明公钥内容完好（tauri CLI 没有验签命令）：再核对公钥文件与生成时记下的指纹一致。
    grep -q "$(shasum -a 256 < "$dir/updater.key.pub" | cut -c1-64)" "$dir/FINGERPRINTS" || { echo 'updater 公钥与 FINGERPRINTS 记录的指纹不一致' >&2; exit 1; }
    [ "$(ssh-keygen -y -f "$dir/release.key" < /dev/null | cut -d' ' -f1-2)" = "$(cut -d' ' -f1-2 "$dir/release.key.pub")" ] \
      || { echo '发行签名私钥与公钥不对应' >&2; exit 1; }
    # 用解开的临时副本对小文件真实签名，再按 allowed_signers 验签（signer 与 namespace 同正式流程）；副本随临时目录删除。
    echo check > "$t/release-sample"
    cp "$dir/release.key" "$t/release.key" && chmod 600 "$t/release.key"
    ssh-keygen -q -p -N '' -f "$t/release.key" < /dev/null > /dev/null
    ssh-keygen -q -Y sign -f "$t/release.key" -n agent-collab-release "$t/release-sample" 2> /dev/null
    ssh-keygen -Y verify -f "$dir/allowed_signers" -I agent-collab-release -n agent-collab-release -s "$t/release-sample.sig" \
      < "$t/release-sample" > /dev/null || { echo '发行签名验签失败（allowed_signers 与私钥不对应）' >&2; exit 1; }
    echo "两把私钥都能用口令解开并与公钥对应；发行签名密钥对小文件签名并按 allowed_signers 验签通过"
    fingerprints "$dir" ;;
  backup)
    dest=${3:?备份目录}
    if [ -n "$(ls -A "$dest" 2> /dev/null)" ]; then echo "$dest 已存在且不为空，不覆盖" >&2; exit 1; fi
    # 先复制进同一父目录下的临时目录并核对，再整体改名就位：中途失败不会留下让重跑被拒的半份备份。
    mkdir -p "$(dirname "$dest")"
    stage=$(mktemp -d "$(dirname "$dest")/.backup.XXXXXX"); trap 'rm -rf "$stage"' EXIT
    chmod 700 "$stage"
    for f in $FILES; do cp -p "$dir/$f" "$stage/$f"; done
    for f in $FILES; do [ "$(shasum -a 256 < "$dir/$f")" = "$(shasum -a 256 < "$stage/$f")" ] || { echo "$f 复制后不一致" >&2; exit 1; }; done
    rmdir "$dest" 2> /dev/null || true
    mv "$stage" "$dest"
    echo "已备份到 $dest（只有加密私钥、公钥与指纹；口令另行保管）" ;;
  *)
    echo "未知命令：$cmd" >&2; exit 2 ;;
esac
