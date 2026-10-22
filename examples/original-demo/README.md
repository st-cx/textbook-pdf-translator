# 原创双语数学重排演示

本目录中的英文材料、中文译文、简单公式绘图和生成脚本均为本次全新创作，按 CC0-1.0 提供。没有摘录任何教材，没有复制系统字体文件或模型权重，也没有密钥或外部翻译请求。工具本体和依赖仍遵循各自许可证。

## 先看效果

- `generated/original-counting-en.pdf`：全新英文数字版输入，1 页。
- `generated/original-counting-image-only.pdf`：由自有输入栅格化得到的纯图片 PDF，1 页，无文字层、无书签。
- `generated/native/counting-native-zh.pdf`：从数字版来源对象重排的中文成品。
- `generated/raster/counting-raster-zh.pdf`：从自制图片版来源对象重排的中文成品。
- `generated/source-preview.png` 与两目录下的 `preview-001.png`：原文和成品的整页预览。

同一份材料包含标题、正文、两个行内公式、一个完整分式公式、五行 Python 代码、代码图注、小练习与脚注。中文正文固定为 10.5 磅，练习 9.5 磅，图注/脚注 8.5 磅；原页来源显示在页边。行内公式和代码来自各自输入的不可变 PDF 裁片，未重新输入为近似数学字符，也未缩小塞入原位置。

这是合成重排演示，不是端到端 OCR、版面解析或机器翻译评测。脚本利用作者已知的文本与坐标，直接建立项目的 `Book/Asset/Element` 公共数据合同；中文译文为本演示撰写，经 `Store.put_candidate` 和结构校验保存，随后实际调用工具的中文排版与保护对象像素检查。图片版也复用已知作者坐标，不运行 OCR 或模型。源页的页眉、蓝色装饰带及英文页脚属于演示源文件的边缘装饰，未声称逐像素复刻整页设计。

## 本次实际检查

rc.3重新生成数字版与图片版，各输出 1 页；9 条自有译文全部通过结构校验；每份 3 个公式和 1 个代码对象全部进行像素核对，各 4/4 项通过，0 错误、0 提醒。已直接目视英文源页和两份中文成品，未见公式损坏、代码截断、明显重叠或异常小字。示例没有机器可核验的印刷页码，统一明确标注“原 PDF 第1页”，不沿用旧示例人工指定的印刷页。当前成品和预览的哈希绑定见 `generated/manual_review.json`；重新生成后应重新目视，旧签收记录不能自动沿用。

自动报告 `generated/*/quality_report.json` 的验收边界保留原样；其技术检查不能代替整页观感或独立语义审校。正文变短后保留自然留白，没有拉伸文字来填满原页。这个小样本也不能用来预测大型书籍的增页率、OCR 准确率、速度或翻译质量。

## 本地复现

需要项目运行环境：Python 3.13、项目已固定的 PyMuPDF 版本、Typst 0.15.1，以及本机已安装的 Noto Serif SC / Noto Sans SC。不会自动联网下载组件。字体仅由本地排版程序读取；目录中不分发字体文件。成品 PDF 使用所需开放字体的嵌入子集；英文输入使用标准 PDF 基础字体。

在公开仓库根目录运行（已先准备好项目的 `.venv`）：

```powershell
.\.venv\Scripts\python.exe examples/original-demo/build_demo.py --implementation .
```

也可用其他已配置好的 Python，从仓库根目录运行：

```powershell
python examples/original-demo/build_demo.py --implementation .
```

脚本放在 `examples/original-demo` 时，省略参数也默认寻找仓库根目录；`--implementation` 可显式指定包含 `textbook_pdf` 的源码目录。

Typst 不在工具目录或 PATH 时，先设置 `TEXTBOOK_TYPST` 为已安装的 0.15.1 可执行文件。脚本只在它自己的 `generated` 目录写产物，并保存 `original_content.json`。不调用外部 API、不读取教材或模型缓存。

运行过程是：绘制原创英文输入 → 将同一页转为纯图片输入 → 按作者清单裁取原公式/代码 → 构建 Book → 保存自有译文 → Typst 重排 → 自动像素检查 → 导出整页预览。每次重建会覆盖本演示产物；PDF 文件标识可能变化，不承诺二进制逐字节可重复。

本次可公开的汇总、字体名称和来源哈希见 `demo-summary.json`，逐页签收见 `generated/manual_review.json`。重新运行后还会生成包含本机运行位置的详细 `generated/demo_report.json`。英文数学关系是等差标记行计数；短代码直接累加每次停靠留下的标记数，与分式公式表达同一结果。循环保留原代码拼写和缩进，正文与图注译为中文。
