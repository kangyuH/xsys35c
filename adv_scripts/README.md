# 《鬼畜王ランス》官方日文 ADV 基线

`project/` 是现行正式汉化工程使用的日文脚本基线。它于 2026-08-14 从官方日文游戏资源重新反编译生成，没有复制此前的半汉化 ADV、既有 UTF-8 迁移副本或中文 PoC。

## 来源

- 输入：`workspace/kichikuou-jp/disc/GAMEDATA/鬼畜王SA.ALD`
- 输入 SHA-256：`2fda9060fa41095086025009357c0df0c47674a5af8557acdc5ccfc88a059328`
- 解码器：`xsys35dc 1.13.0`
- 输出编码：严格 UTF-8，无 BOM
- ADV 页数：201

生成命令：

```bash
build/xsys35dc --encoding=utf8 \
  --outdir /tmp/kichikuou-fresh/project \
  workspace/kichikuou-jp/disc/GAMEDATA/鬼畜王SA.ALD
```

反编译器生成后，只对 `project/xsys35c.cfg` 增加以下现行运行配置：

```ini
unicode = true
```

除此之外，`project/` 应保持为官方日文反编译结果。正式中文译文必须保存在翻译目录中，再由回填工具生成独立构建副本；不要直接在本目录写入中文或手工修改脚本逻辑。

## 一键编译到测试目录

先确保本仓库的 `build/xsys35c` 已经生成，然后在仓库根目录运行：

```bash
./build-adv-dev.sh
```

脚本将 `project/` 编译为 Unicode `鬼畜王SA.ALD`，再部署到 `workspace/dev/`。如果测试目录已有 SA，旧文件会按北京时间归档为：

```text
workspace/dev/archive/鬼畜王SA_YYYYMMDDHHMMSS.ald
```

编译和所有预检完成前不会移动现有 SA；部署、配置或校验失败时会自动恢复原 SA 和原配置。归档文件不会自动删除。

测试目录的 `.xsys35rc` 会保留其他自定义配置，并固定更新以下三个受控键：

```text
ttfont_mincho: C:/Windows/Fonts/msyh.ttc
ttfont_gothic: C:/Windows/Fonts/msyh.ttc
savedir: save
```

微软雅黑常规体是当前 Windows 游戏内测试确认的字体。脚本只引用系统字体，不复制或分发字体文件；修改字体配置后需要完全退出并重新启动 `xsystem35.exe`。

成功后，脚本还会更新 `ADV-SCRIPT-BUILD.txt` 和 `SHA256SUMS.txt`。可通过环境变量覆盖默认路径：

```bash
ADV_DEV_DIR=/path/to/test-game \
XSYS35C_BIN=/path/to/xsys35c \
./build-adv-dev.sh
```

`ADV_DEV_DIR` 必须已经包含 `xsystem35.exe` 以及官方 GA、GB、WA 三个资源包。脚本不会修改 `VERSION.txt`、存档和其他游戏资源。

## 本次验证

- 201/201 个 ADV 和全部配置文件严格通过 UTF-8 检查。
- 反编译过程无 stderr 输出。
- 未启用 Unicode 时，重新编译的 SA.ALD 与官方包逐成员内容等价。
- 启用 Unicode 后，两次构建的 201 个 SCO 逐文件一致。
- Unicode SA.ALD 再反编译后，201 个 ADV 与本目录语义一致。
- HED 和变量表在 Unicode 回读后逐字节一致。

现行计划见 `docs/kichikuou_translation_plan.md`。
