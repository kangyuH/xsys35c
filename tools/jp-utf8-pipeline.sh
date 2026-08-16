#!/usr/bin/env bash
# Rebuild the Japanese original of Kichikuou Rance as a UTF-8 (Unicode mode)
# runnable package.
#
#   unpack    official SA.ALD  -> 201 UTF-8 ADV baseline (read-only deliverable)
#   build     baseline         -> Unicode SA.ALD, built twice, structurally checked
#   verify    Unicode SA.ALD   -> re-decompiled and semantically compared
#   assemble  Unicode SA.ALD   -> runnable xsystem35-sdl2 package + symlink
#   all       every stage above, in order
#
# No Japanese text is modified and nothing is translated. The disc directory is
# only ever read.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)

DISC_DIR=${DISC_DIR:-/mnt/f/game/disc}
GAME_DEV=${GAME_DEV:-/mnt/f/game/rance_dev}
RUNTIME_VERSION=${RUNTIME_VERSION:-2.19.1}
RUNTIME_SHA256=0b8de1567813f7b831e4588f2561f2e89c687946e81fc76a04850ea2a8d5d7ea
RUNTIME_EXE_SHA256=857bc61d622930ca562bd592b337ac6920141a7ebe6b1181978cb015af46c2d2
RUNTIME_URL="https://github.com/kichikuou/xsystem35-sdl2/releases/download/v${RUNTIME_VERSION}/xsystem35-${RUNTIME_VERSION}-windows-64bit.zip"
FONT_PATH=${FONT_PATH:-C:/Windows/Fonts/msyh.ttc}
# Optional cross-check against a previously accepted baseline.
REFERENCE_BASELINE=${REFERENCE_BASELINE:-/home/davidhachirou/github/xsys35c_backup/workspace/kichikuou-jp/decompiled}

workspace="$repo_root/workspace"
baseline="$workspace/source/adv_scripts"
stage="$workspace/.stage/adv_scripts"
build_root="$workspace/build"
verify_root="$workspace/verify"
reports="$workspace/reports"
runtime_cache="$workspace/runtime"
runtime_zip=${RUNTIME_ZIP:-$runtime_cache/xsystem35-${RUNTIME_VERSION}-windows-64bit.zip}
runtime_extract="$runtime_cache/windows-x64"

gamedata="$DISC_DIR/GAMEDATA"
official_sa="$gamedata/鬼畜王SA.ALD"
ald_basename='鬼畜王'
sa_name="${ald_basename}SA.ALD"

compiler="$repo_root/build/xsys35c"
decompiler="$repo_root/build/xsys35dc"
ald_tool="$repo_root/build/ald"
semantic_compare="$script_dir/compare-semantic-source.py"

# volume basename -> SHA-256 of the pristine official archive
declare -A OFFICIAL_SHA256=(
  ["${ald_basename}GA.ALD"]=5ae182d7a79be5818d0df3cecf269e68a1d951fdc1c195c8f8240aa2f394eeb7
  ["${ald_basename}GB.ALD"]=5e829bee326c679995108db8fe1be5d517062e47a93b30d02772fc28bd4d3adb
  ["${ald_basename}SA.ALD"]=2fda9060fa41095086025009357c0df0c47674a5af8557acdc5ccfc88a059328
  ["${ald_basename}WA.ALD"]=159612c81aa858fcd54a20215e6771bc55901b7c298a4302b4fd65398b810ce1
)

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

require_tools() {
  for tool in "$compiler" "$decompiler" "$ald_tool"; do
    [[ -x "$tool" ]] || die "missing executable: $tool (run ninja -C build first)"
  done
  [[ -x "$semantic_compare" ]] || chmod +x "$semantic_compare"
}

expect_sha256() {
  local file=$1 expected=$2 actual
  actual=$(sha256sum "$file" | cut -d' ' -f1)
  [[ "$actual" == "$expected" ]] ||
    die "SHA-256 mismatch for $file: expected $expected, got $actual"
}

# Every file must decode strictly as UTF-8 and must not contain U+FFFD.
check_strict_utf8() {
  local dir=$1 file
  while IFS= read -r -d '' file; do
    iconv -f UTF-8 -t UTF-8 "$file" >/dev/null ||
      die "not valid UTF-8: $file"
    if LC_ALL=C grep -q $'\xef\xbf\xbd' "$file"; then
      die "replacement character U+FFFD found in $file"
    fi
  done < <(find "$dir" -maxdepth 1 -type f \
    \( -iname '*.ADV' -o -name '*.hed' -o -name '*.txt' -o -name '*.cfg' \) -print0)
}

hash_tree() {
  local dir=$1
  (cd "$dir" && find . -maxdepth 1 -type f -iname '*.ADV' -print0 |
    sort -z | xargs -0 sha256sum)
}

write_environment_report() {
  {
    echo "date=$(date -Is)"
    echo "repository_commit=$(git -C "$repo_root" rev-parse HEAD)"
    echo "repository_branch=$(git -C "$repo_root" rev-parse --abbrev-ref HEAD)"
    echo "repository_status=$(git -C "$repo_root" status --short | wc -l) changed tracked/untracked paths"
    echo "compiler=$("$compiler" --version)"
    echo "decompiler=$("$decompiler" --version)"
    echo "ald=$("$ald_tool" version)"
    echo "platform=$(uname -srm)"
    echo "disc_dir=$DISC_DIR"
    echo "game_dev=$GAME_DEV"
  } >"$reports/environment.txt"
  sha256sum "$compiler" "$decompiler" "$ald_tool" >"$reports/tooling.sha256"
}

stage_unpack() {
  require_tools
  mkdir -p "$reports"

  info "checking official disc input (read-only): $gamedata"
  [[ -d "$gamedata" ]] || die "GAMEDATA directory not found: $gamedata"
  : >"$reports/disc-input.sha256"
  for volume in "${!OFFICIAL_SHA256[@]}"; do
    [[ -f "$gamedata/$volume" ]] || die "missing official archive: $gamedata/$volume"
    expect_sha256 "$gamedata/$volume" "${OFFICIAL_SHA256[$volume]}"
    sha256sum "$gamedata/$volume" >>"$reports/disc-input.sha256"
  done
  sort -k2 -o "$reports/disc-input.sha256" "$reports/disc-input.sha256"
  write_environment_report

  info "decompiling $sa_name into a staging directory"
  rm -rf "$stage"
  mkdir -p "$stage"
  "$decompiler" -o "$stage" "$official_sa" \
    >"$reports/decompile-official.stdout.log" 2>"$reports/decompile-official.stderr.log"
  [[ ! -s "$reports/decompile-official.stderr.log" ]] ||
    die "xsys35dc emitted warnings, see $reports/decompile-official.stderr.log"

  local adv_count
  adv_count=$(find "$stage" -maxdepth 1 -type f -iname '*.ADV' | wc -l)
  [[ "$adv_count" -eq 201 ]] || die "expected 201 ADV files, found $adv_count"

  for required in xsys35dc.hed variables.txt xsys35c.cfg; do
    [[ -f "$stage/$required" ]] || die "decompiler did not produce $required"
  done
  grep -qx "ald_basename = $ald_basename" "$stage/xsys35c.cfg" ||
    die "xsys35c.cfg lacks 'ald_basename = $ald_basename'"
  grep -qx 'encoding = utf8' "$stage/xsys35c.cfg" ||
    die "xsys35c.cfg lacks 'encoding = utf8'"
  ! grep -q '^unicode' "$stage/xsys35c.cfg" ||
    die "baseline xsys35c.cfg must stay pristine (no unicode line)"

  info "verifying strict UTF-8 encoding of $adv_count ADV plus metadata"
  check_strict_utf8 "$stage"

  if [[ -d "$REFERENCE_BASELINE" ]]; then
    info "cross-checking against previously accepted baseline"
    if diff -r "$REFERENCE_BASELINE" "$stage" >"$reports/reference-baseline.diff" 2>&1; then
      echo "reference_baseline=identical" >>"$reports/environment.txt"
    else
      echo "reference_baseline=differs (see reference-baseline.diff)" >>"$reports/environment.txt"
      die "baseline differs from $REFERENCE_BASELINE, see $reports/reference-baseline.diff"
    fi
  else
    echo "reference_baseline=unavailable" >>"$reports/environment.txt"
  fi

  if [[ -d "$baseline" ]]; then
    info "baseline already exists, proving the run is reproducible"
    diff -r "$baseline" "$stage" >"$reports/baseline-rerun.diff" 2>&1 ||
      die "regenerated baseline differs from $baseline, see $reports/baseline-rerun.diff"
    rm -rf "$stage"
  else
    mkdir -p "$(dirname "$baseline")"
    mv "$stage" "$baseline"
  fi
  rmdir "$workspace/.stage" 2>/dev/null || true

  hash_tree "$baseline" >"$reports/baseline-adv.sha256"
  (cd "$baseline" && sha256sum xsys35dc.hed variables.txt xsys35c.cfg) \
    >"$reports/baseline-meta.sha256"
  info "unpack PASS: 201 UTF-8 ADV at $baseline"
}

assert_baseline_untouched() {
  [[ -f "$reports/baseline-adv.sha256" ]] || return 0
  local current
  current=$(hash_tree "$baseline")
  [[ "$current" == "$(cat "$reports/baseline-adv.sha256")" ]] ||
    die "baseline was modified since unpack"
}

stage_build() {
  require_tools
  [[ -d "$baseline" ]] || die "baseline not found, run 'unpack' first"
  mkdir -p "$build_root/first" "$build_root/second" "$reports" \
    "$verify_root/official-sco" "$verify_root/first-sco" "$verify_root/second-sco"

  info "compiling the baseline twice in Unicode mode"
  local pass
  for pass in first second; do
    rm -f "$build_root/$pass/$sa_name"
    "$compiler" -p "$baseline/xsys35c.cfg" -u -d "$build_root/$pass" \
      >"$reports/compile-$pass.stdout.log" 2>"$reports/compile-$pass.stderr.log"
    [[ ! -s "$reports/compile-$pass.stderr.log" ]] ||
      die "xsys35c ($pass pass) emitted diagnostics, see $reports/compile-$pass.stderr.log"
    [[ -f "$build_root/$pass/$sa_name" ]] || die "$pass pass did not produce $sa_name"
  done
  assert_baseline_untouched

  local first_ald="$build_root/first/$sa_name"
  local second_ald="$build_root/second/$sa_name"

  info "checking ALD structure against the official archive"
  "$ald_tool" list "$official_sa" >"$reports/official-ald-list.txt"
  "$ald_tool" list "$first_ald" >"$reports/unicode-ald-list.txt"
  local entries
  entries=$(wc -l <"$reports/unicode-ald-list.txt")
  [[ "$entries" -eq 201 ]] || die "expected 201 ALD entries, found $entries"

  "$ald_tool" extract -d "$verify_root/official-sco" \
    -m "$reports/official-manifest.txt" "$official_sa" >"$reports/extract-official.log"
  "$ald_tool" extract -d "$verify_root/first-sco" \
    -m "$reports/unicode-manifest.txt" "$first_ald" >"$reports/extract-first.log"
  "$ald_tool" extract -d "$verify_root/second-sco" \
    -m "$reports/second-manifest.txt" "$second_ald" >"$reports/extract-second.log"
  cmp "$reports/official-manifest.txt" "$reports/unicode-manifest.txt" ||
    die "Unicode ALD manifest differs from the official one"
  cmp "$reports/unicode-manifest.txt" "$reports/second-manifest.txt" ||
    die "the two builds produced different manifests"

  info "checking that both builds are reproducible member by member"
  local volume index member
  while IFS=, read -r volume index member; do
    cmp "$verify_root/first-sco/$member" "$verify_root/second-sco/$member" ||
      die "SCO differs between builds: $member"
  done <"$reports/unicode-manifest.txt"

  info "checking the Unicode bytecode marker"
  local first_member header_size marker
  first_member=$(sed -n '1s/^[^,]*,[^,]*,//p' "$reports/unicode-manifest.txt")
  header_size=$(od -An -tu4 -j4 -N4 "$verify_root/first-sco/$first_member" | tr -d ' ')
  marker=$(dd if="$verify_root/first-sco/$first_member" bs=1 skip="$header_size" \
    count=4 status=none | xxd -p)
  [[ "$marker" == "5a55417f" ]] || die "Unicode marker (ZU 1) missing, found $marker"

  cp -f "$first_ald" "$build_root/$sa_name"
  sha256sum "$build_root/$sa_name" >"$reports/unicode-ald.sha256"
  info "build PASS: Unicode $sa_name at $build_root/$sa_name"
}

stage_verify() {
  require_tools
  local unicode_ald="$build_root/$sa_name"
  [[ -f "$unicode_ald" ]] || die "Unicode ALD not found, run 'build' first"
  mkdir -p "$verify_root/redecompiled" "$reports"

  info "re-decompiling the Unicode ALD"
  rm -rf "$verify_root/redecompiled"
  mkdir -p "$verify_root/redecompiled"
  "$decompiler" -o "$verify_root/redecompiled" "$unicode_ald" \
    >"$reports/decompile-unicode.stdout.log" 2>"$reports/decompile-unicode.stderr.log"
  [[ ! -s "$reports/decompile-unicode.stderr.log" ]] ||
    die "xsys35dc emitted warnings, see $reports/decompile-unicode.stderr.log"

  info "comparing the 201 pages semantically"
  hash_tree "$verify_root/redecompiled" >"$reports/redecompiled-adv.sha256"
  "$semantic_compare" "$baseline" "$verify_root/redecompiled" "$reports" \
    >"$reports/semantic-compare.log"
  cmp "$baseline/xsys35dc.hed" "$verify_root/redecompiled/xsys35dc.hed" ||
    die "xsys35dc.hed changed"
  cmp "$baseline/variables.txt" "$verify_root/redecompiled/variables.txt" ||
    die "variables.txt changed"
  assert_baseline_untouched

  write_verification_report
  info "verify PASS: see $reports/verification.md"
}

write_verification_report() {
  {
    printf 'check\tstatus\tdetail\n'
    printf 'disc_input\tPASS\tFour official ALD match the accepted SHA-256 baseline\n'
    printf 'utf8_baseline\tPASS\t201 ADV plus HED/variables/config decode strictly as UTF-8\n'
    printf 'baseline_pristine\tPASS\tNo unicode line added to xsys35c.cfg; Unicode enabled via -u\n'
    printf 'unicode_compile\tPASS\tTwo builds completed without diagnostics\n'
    printf 'ald_structure\tPASS\t201 entries; volume/index/name manifest equals official SA\n'
    printf 'unicode_marker\tPASS\tFirst SCO starts bytecode with the ZU 1 marker\n'
    printf 'reproducibility\tPASS\tAll 201 SCO members are byte-identical across builds\n'
    printf 'source_roundtrip\tPASS\t201 ADV semantically identical; HED and variables byte-identical\n'
  } >"$reports/verification.tsv"

  cat >"$reports/verification.json" <<JSON
{
  "status": "PASS",
  "adv_pages": 201,
  "ald_entries": 201,
  "unicode_marker": "5a55417f",
  "sco_reproducibility": "byte-identical",
  "source_roundtrip": "semantic-identical after address and SJIS-escape normalization"
}
JSON

  cat >"$reports/verification.md" <<MD
# 日文原版 UTF-8 重建自动验证

自动门禁结果：**PASS**

- 输入的 4 个官方 ALD 与已验收基线 SHA-256 完全一致，\`$DISC_DIR\` 全程只读。
- 201/201 个 ADV 严格通过 UTF-8 检查，且不含 U+FFFD。
- 基线 \`xsys35c.cfg\` 保持原样（无 \`unicode\` 行），Unicode 由命令行 \`-u\` 开启。
- 两次 Unicode 编译均无 stderr 输出。
- Unicode SA 保留官方 201 个条目的卷号、索引、顺序和名称。
- 两次构建的 201 个 SCO 成员逐字节一致。
- 首个 SCO 的字节码以 \`ZU 1\`（\`5a55417f\`）Unicode 标记开始。
- Unicode 会改变字节地址使反编译标签不同，显式 \`<0xHHHH>\` SJIS 字符会变成等价 Unicode 字符；规范化后 201 页语义一致。
- \`xsys35dc.hed\` 与 \`variables.txt\` 逐字节一致。

Windows 游戏内冒烟测试仍需人工执行，清单见 \`$GAME_DEV/SMOKE-TEST.md\`。
MD
}

fetch_runtime() {
  mkdir -p "$runtime_cache"
  if [[ ! -f "$runtime_zip" ]]; then
    local cached=/home/davidhachirou/github/xsys35c_backup/workspace/utf8-migration/xsystem35-${RUNTIME_VERSION}-windows-64bit.zip
    if [[ -f "$cached" ]] &&
      [[ "$(sha256sum "$cached" | cut -d' ' -f1)" == "$RUNTIME_SHA256" ]]; then
      info "reusing the locally cached official runtime archive"
      cp -f "$cached" "$runtime_zip"
    else
      info "downloading $RUNTIME_URL"
      curl -fL --retry 3 -o "$runtime_zip" "$RUNTIME_URL"
    fi
  fi
  expect_sha256 "$runtime_zip" "$RUNTIME_SHA256"

  rm -rf "$runtime_extract"
  mkdir -p "$runtime_extract"
  python3 - "$runtime_zip" "$runtime_extract" <<'PY'
from pathlib import Path
from zipfile import ZipFile
import sys

archive = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
with ZipFile(archive) as zf:
    for info in zf.infolist():
        target = (destination / info.filename).resolve()
        if destination not in target.parents and target != destination:
            raise SystemExit(f"unsafe archive member: {info.filename}")
    zf.extractall(destination)
PY
}

stage_assemble() {
  require_tools
  local unicode_ald="$build_root/$sa_name"
  [[ -f "$unicode_ald" ]] || die "Unicode ALD not found, run 'build' first"
  mkdir -p "$reports"

  fetch_runtime
  info "assembling the runnable package at $GAME_DEV"
  mkdir -p "$GAME_DEV"
  cp -Rf "$runtime_extract"/. "$GAME_DEV"/
  cp -f "$unicode_ald" "$GAME_DEV/$sa_name"
  local volume
  for volume in "${ald_basename}GA.ALD" "${ald_basename}GB.ALD" "${ald_basename}WA.ALD"; do
    cp -f "$gamedata/$volume" "$GAME_DEV/$volume"
    cmp "$gamedata/$volume" "$GAME_DEV/$volume" || die "copy of $volume differs from the disc"
  done
  mkdir -p "$GAME_DEV/save"

  cat >"$GAME_DEV/.xsys35rc" <<RC
ttfont_mincho: $FONT_PATH
ttfont_gothic: $FONT_PATH
savedir: save
RC

  [[ -f "$GAME_DEV/xsystem35.exe" ]] || die "xsystem35.exe missing in $GAME_DEV"
  expect_sha256 "$GAME_DEV/xsystem35.exe" "$RUNTIME_EXE_SHA256"
  [[ -z "$(find -L "$GAME_DEV" -type f -iname 'SYSTEM35.EXE' -print -quit)" ]] ||
    die "the original SYSTEM35.EXE must not be shipped: it cannot run Unicode ALD"

  {
    echo "xsystem35-sdl2 v${RUNTIME_VERSION} Windows 64-bit"
    echo "$RUNTIME_URL"
    echo "official archive sha256 $RUNTIME_SHA256"
    echo "xsys35c $("$compiler" --version | awk '{print $2}')"
    echo "repository $(git -C "$repo_root" rev-parse HEAD) ($(git -C "$repo_root" rev-parse --abbrev-ref HEAD))"
    echo "source baseline workspace/source/adv_scripts (201 UTF-8 ADV, unmodified Japanese)"
    echo "built $(date -Is)"
  } >"$GAME_DEV/VERSION.txt"

  (
    cd "$GAME_DEV"
    sha256sum xsystem35.exe "${ald_basename}GA.ALD" "${ald_basename}GB.ALD" \
      "$sa_name" "${ald_basename}WA.ALD"
  ) >"$GAME_DEV/SHA256SUMS.txt"
  cp -f "$GAME_DEV/SHA256SUMS.txt" "$reports/game-dev.sha256"

  cat >"$GAME_DEV/SMOKE-TEST.md" <<'MD'
# Windows 10/11 x64 人工冒烟测试

在 Windows 中直接运行 `xsystem35.exe`（原版 SYSTEM35.EXE 不支持 Unicode 脚本）。

- [ ] 能进入《鬼畜王ランス》标题画面。
- [ ] 能开始新游戏并推进开场场景。
- [ ] 日文汉字、假名、半角片假名和标点无乱码、缺字。
- [ ] 对话换行、等待和翻页行为正常。
- [ ] 菜单选择、分支和画面切换正常。
- [ ] 能创建新存档，退出后重新启动并成功读档。
- [ ] 未发现崩溃或明显控制流异常。

BGM/CD 音轨不属于本轮编码迁移门禁。发现问题时记录复现步骤、截图和存档。
MD

  local link="$workspace/game_dev"
  if [[ -L "$link" ]]; then
    rm -f "$link"
  elif [[ -e "$link" ]]; then
    die "$link exists and is not a symlink"
  fi
  ln -s "$GAME_DEV" "$link"
  [[ -f "$link/xsystem35.exe" ]] || die "symlink $link does not resolve"
  info "assemble PASS: $GAME_DEV (symlinked as $link)"
}

main() {
  local command=${1:-all}
  case "$command" in
  unpack) stage_unpack ;;
  build) stage_build ;;
  verify) stage_verify ;;
  assemble) stage_assemble ;;
  all)
    stage_unpack
    stage_build
    stage_verify
    stage_assemble
    ;;
  *)
    echo "usage: ${BASH_SOURCE[0]##*/} [unpack|build|verify|assemble|all]" >&2
    exit 1
    ;;
  esac
}

main "$@"
