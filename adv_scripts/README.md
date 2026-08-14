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

## 本次验证

- 201/201 个 ADV 和全部配置文件严格通过 UTF-8 检查。
- 反编译过程无 stderr 输出。
- 未启用 Unicode 时，重新编译的 SA.ALD 与官方包逐成员内容等价。
- 启用 Unicode 后，两次构建的 201 个 SCO 逐文件一致。
- Unicode SA.ALD 再反编译后，201 个 ADV 与本目录语义一致。
- HED 和变量表在 Unicode 回读后逐字节一致。

现行计划见 `docs/kichikuou_translation_plan.md`。
