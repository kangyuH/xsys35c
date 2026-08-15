#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project="$script_dir/adv_scripts/project"
compiler="${XSYS35C_BIN:-$script_dir/build/xsys35c}"
dev="${ADV_DEV_DIR:-$script_dir/workspace/dev}"
target_name='鬼畜王SA.ALD'
target="$dev/$target_name"
archive_dir="$dev/archive"
config="$dev/.xsys35rc"
build_note="$dev/ADV-SCRIPT-BUILD.txt"
checksums="$dev/SHA256SUMS.txt"
lock_dir="$dev/.adv-build.lock"
font_windows='C:/Windows/Fonts/msyh.ttc'
font_host_dir='/mnt/c/Windows/Fonts'
font_host="$font_host_dir/msyh.ttc"

work_root=''
stage_sa=''
stage_config=''
stage_note=''
stage_checksums=''
archive_path=''
lock_held=0
archive_dir_created=0
transaction_started=0
old_archived=0
new_installed=0
config_installed=0
note_installed=0
checksums_installed=0
had_config=0
had_note=0
had_checksums=0

die() {
  echo "error: $*" >&2
  exit 1
}

restore_file() {
  local installed=$1
  local existed=$2
  local backup=$3
  local destination=$4

  if (( installed == 0 )); then
    return
  fi
  if (( existed == 1 )); then
    cp -a -- "$backup" "$destination" || return 1
  else
    rm -f -- "$destination" || return 1
  fi
}

rollback() {
  local failed=0

  echo 'Deployment failed; restoring the previous development package.' >&2
  restore_file "$checksums_installed" "$had_checksums" \
    "$work_root/backup-SHA256SUMS.txt" "$checksums" || failed=1
  restore_file "$note_installed" "$had_note" \
    "$work_root/backup-ADV-SCRIPT-BUILD.txt" "$build_note" || failed=1
  restore_file "$config_installed" "$had_config" \
    "$work_root/backup-xsys35rc" "$config" || failed=1

  if (( new_installed == 1 )) && [[ -f "$target" ]]; then
    rm -f -- "$target" || failed=1
  fi
  if (( old_archived == 1 )) && [[ -f "$archive_path" ]]; then
    mv -- "$archive_path" "$target" || failed=1
  fi
  if (( archive_dir_created == 1 )); then
    rmdir -- "$archive_dir" 2>/dev/null || true
  fi

  if (( failed == 1 )); then
    echo 'error: automatic rollback was incomplete; inspect the development directory.' >&2
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP

  if (( status != 0 && transaction_started == 1 )); then
    rollback
  fi

  [[ -n "$stage_sa" ]] && rm -f -- "$stage_sa"
  [[ -n "$stage_config" ]] && rm -f -- "$stage_config"
  [[ -n "$stage_note" ]] && rm -f -- "$stage_note"
  [[ -n "$stage_checksums" ]] && rm -f -- "$stage_checksums"
  if (( lock_held == 1 )); then
    rmdir -- "$lock_dir" 2>/dev/null || true
  fi
  if [[ -n "$work_root" && "$work_root" == /tmp/xsys35c-adv-build.* ]]; then
    rm -r -- "$work_root" || true
  fi

  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

[[ -x "$compiler" ]] || die "compiler is not executable: $compiler (build xsys35c first)"
[[ -d "$project" ]] || die "ADV project directory does not exist: $project"
[[ -f "$project/xsys35c.cfg" ]] || die "project config does not exist: $project/xsys35c.cfg"
[[ -d "$dev" ]] || die "development directory does not exist: $dev"

for required in xsystem35.exe 鬼畜王GA.ALD 鬼畜王GB.ALD 鬼畜王WA.ALD; do
  [[ -s "$dev/$required" ]] || die "required development file is missing or empty: $dev/$required"
done
for managed in "$target" "$config" "$build_note" "$checksums"; do
  if [[ -e "$managed" && ! -f "$managed" ]]; then
    die "managed development path is not a regular file: $managed"
  fi
done

if [[ -d "$font_host_dir" ]]; then
  [[ -f "$font_host" ]] || die "required Windows font is missing: $font_host"
else
  echo "warning: cannot inspect $font_host_dir; writing the configured Windows font path anyway" >&2
fi

adv_count=$(find "$project" -maxdepth 1 -type f -iname '*.ADV' | wc -l)
[[ "$adv_count" -eq 201 ]] || die "expected 201 ADV files, found $adv_count"
[[ $(grep -Ec '^[[:space:]]*encoding[[:space:]]*=[[:space:]]*utf8[[:space:]]*$' \
  "$project/xsys35c.cfg") -eq 1 ]] || die 'project must contain exactly one encoding = utf8 setting'
[[ $(grep -Ec '^[[:space:]]*unicode[[:space:]]*=[[:space:]]*true[[:space:]]*$' \
  "$project/xsys35c.cfg") -eq 1 ]] || die 'project must contain exactly one unicode = true setting'

mkdir -- "$lock_dir" 2>/dev/null || die "another ADV build is already running: $lock_dir"
lock_held=1
work_root=$(mktemp -d /tmp/xsys35c-adv-build.XXXXXX)
mkdir "$work_root/output"

if ! "$compiler" --project "$project/xsys35c.cfg" --outdir "$work_root/output" \
  >"$work_root/compiler.stdout.log" 2>"$work_root/compiler.stderr.log"; then
  sed -n '1,200p' "$work_root/compiler.stdout.log" >&2
  sed -n '1,200p' "$work_root/compiler.stderr.log" >&2
  die 'ADV compilation failed'
fi
if [[ -s "$work_root/compiler.stderr.log" ]]; then
  sed -n '1,200p' "$work_root/compiler.stderr.log" >&2
  die 'ADV compilation wrote diagnostics to stderr'
fi

compiled="$work_root/output/$target_name"
[[ -s "$compiled" ]] || die "compiler did not produce a non-empty $target_name"
ald_count=$(find "$work_root/output" -maxdepth 1 -type f -iname '*.ALD' | wc -l)
[[ "$ald_count" -eq 1 ]] || die "expected one ALD output, found $ald_count"

stage_sa="$dev/.$target_name.new.$$"
stage_config="$dev/.xsys35rc.new.$$"
stage_note="$dev/.ADV-SCRIPT-BUILD.txt.new.$$"
stage_checksums="$dev/.SHA256SUMS.txt.new.$$"
for staged in "$stage_sa" "$stage_config" "$stage_note" "$stage_checksums"; do
  [[ ! -e "$staged" ]] || die "staging path already exists: $staged"
done

cp -- "$compiled" "$stage_sa"
cmp -- "$compiled" "$stage_sa"

config_input=/dev/null
if [[ -e "$config" ]]; then
  config_input=$config
fi
awk -v mincho="$font_windows" -v gothic="$font_windows" '
  {
    sub(/\r$/, "")
    if ($0 !~ /^[[:space:]]*(ttfont_mincho|ttfont_gothic|savedir)[[:space:]]*:/) {
      lines[++count] = $0
    }
  }
  END {
    while (count > 0 && lines[count] ~ /^[[:space:]]*$/) {
      count--
    }
    for (i = 1; i <= count; i++) {
      print lines[i]
    }
    if (count > 0) {
      print ""
    }
    print "ttfont_mincho: " mincho
    print "ttfont_gothic: " gothic
    print "savedir: save"
  }
' "$config_input" >"$stage_config"
iconv -f UTF-8 -t UTF-8 "$stage_config" >/dev/null

timestamp=$(TZ=Asia/Shanghai date +%Y%m%d%H%M%S)
[[ "$timestamp" =~ ^[0-9]{14}$ ]] || die "invalid Beijing timestamp: $timestamp"
old_sha256=none
archive_relative=none
if [[ -e "$target" ]]; then
  old_sha256=$(sha256sum "$target" | awk '{print $1}')
  archive_name="鬼畜王SA_${timestamp}.ald"
  archive_path="$archive_dir/$archive_name"
  archive_relative="archive/$archive_name"
  [[ ! -e "$archive_path" ]] || die "archive already exists: $archive_path"
fi

new_sha256=$(sha256sum "$stage_sa" | awk '{print $1}')
new_size=$(stat -c '%s' "$stage_sa")
config_sha256=$(sha256sum "$stage_config" | awk '{print $1}')
compiler_version=$("$compiler" --version | sed -n '1p')
repository_commit=$(git -C "$script_dir" rev-parse HEAD)
if [[ -n $(git -C "$script_dir" status --porcelain) ]]; then
  repository_dirty=true
else
  repository_dirty=false
fi

cat >"$stage_note" <<EOF
build_time_beijing=$timestamp
source=adv_scripts/project
repository_commit=$repository_commit
repository_dirty=$repository_dirty
adv_files=$adv_count
compiler=$compiler_version
encoding=utf8
unicode=true
compiler_exit=0
compiler_stderr_bytes=0
sa_filename=$target_name
sa_size=$new_size
sa_sha256=$new_sha256
previous_sa_sha256=$old_sha256
archive=$archive_relative
font_mincho=$font_windows
font_gothic=$font_windows
savedir=save
xsys35rc_sha256=$config_sha256
EOF

hash_xsystem=$(sha256sum "$dev/xsystem35.exe" | awk '{print $1}')
hash_ga=$(sha256sum "$dev/鬼畜王GA.ALD" | awk '{print $1}')
hash_gb=$(sha256sum "$dev/鬼畜王GB.ALD" | awk '{print $1}')
hash_wa=$(sha256sum "$dev/鬼畜王WA.ALD" | awk '{print $1}')
cat >"$stage_checksums" <<EOF
$hash_xsystem  xsystem35.exe
$hash_ga  鬼畜王GA.ALD
$hash_gb  鬼畜王GB.ALD
$new_sha256  $target_name
$hash_wa  鬼畜王WA.ALD
EOF

[[ -e "$config" ]] && { cp -a -- "$config" "$work_root/backup-xsys35rc"; had_config=1; }
[[ -e "$build_note" ]] && { cp -a -- "$build_note" "$work_root/backup-ADV-SCRIPT-BUILD.txt"; had_note=1; }
[[ -e "$checksums" ]] && { cp -a -- "$checksums" "$work_root/backup-SHA256SUMS.txt"; had_checksums=1; }

transaction_started=1
if [[ -f "$target" ]]; then
  if [[ ! -d "$archive_dir" ]]; then
    mkdir -- "$archive_dir"
    archive_dir_created=1
  fi
  mv -- "$target" "$archive_path"
  old_archived=1
fi

mv -- "$stage_sa" "$target"
stage_sa=''
new_installed=1
mv -- "$stage_config" "$config"
stage_config=''
config_installed=1
mv -- "$stage_note" "$build_note"
stage_note=''
note_installed=1
mv -- "$stage_checksums" "$checksums"
stage_checksums=''
checksums_installed=1

(
  cd "$dev"
  sha256sum --check --status SHA256SUMS.txt
) || die 'deployed file checksum verification failed'
[[ $(grep -Ec '^[[:space:]]*ttfont_mincho[[:space:]]*:' "$config") -eq 1 ]]
[[ $(grep -Ec '^[[:space:]]*ttfont_gothic[[:space:]]*:' "$config") -eq 1 ]]
[[ $(grep -Ec '^[[:space:]]*savedir[[:space:]]*:' "$config") -eq 1 ]]

transaction_started=0
echo "ADV build deployed: $target"
if [[ "$archive_relative" != none ]]; then
  echo "Previous SA archived: $dev/$archive_relative"
fi
echo "SHA-256: $new_sha256"
