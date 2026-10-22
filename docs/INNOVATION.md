# 工程特色、已有方案与验证边界

2026-10-22 新增的工程改进：原生可伸展数学字形的可见边界、保留双重来源的跨页公式续接、图内标签任务与区域化背景检查，以及按原 PDF 裁剪栈核对路径。实现和剩余问题见 [Rosen 进展](ROSEN_PROGRESS.md)。尚未完成同条件竞品实测，不据此宣称行业首创或整体领先。

核查日期：2026-10-21。使用 GitHub 官方 API/gh 读取官方 README 和源码；以下链接固定到核查时默认分支的具体提交。**本工具不是 OCR、公式保护、术语表、翻译缓存或并发翻译的首创项目。** 这些已有成熟实现，公开介绍应明确复用关系。


## 五项差异化取舍

这些是面向教材阅读的工程选择，不是行业首创声明：

1. **正文可读，空间不足就续页。** 统一正文、习题和图注字号，允许增页，用原书页码帮助查找。[排版器](../textbook_pdf/layout.py) · [Typst 模板](../textbook_pdf/templates/book.typ)
2. **原始数学对象参与重排。** 数字页使用原始矢量对象，扫描页保留原合成图像中的公式与代码，正文另行翻译。[数字解析](../textbook_pdf/extraction.py) · [扫描解析](../textbook_pdf/scan_adapter.py)
3. **API 与代理共用一份任务进度。** 领取、续租、提交和版本校验走同一个任务库；来源变化后拒绝旧回复。[任务库](../textbook_pdf/store.py) · [代理入口](../tp_worker.py)
4. **问题有位置，验收有证据。** 分开记录来源、有效译文、保护对象、语义复核与成品检查；默认全流程（`--stage all`）有来源阻断时先停止进入付费翻译。[质量检查](../textbook_pdf/qa.py) · [对象像素核对](../textbook_pdf/visual_checks.py)
5. **扫描与数字排版隔离。** 无文字层、无书签的扫描件可进入本地流程；修正绑定原图和 OCR 行版本，纸色在独立背景层处理。[扫描用法](SCANNED_PDF.md) · [纸色包装层](../textbook_pdf/scan_layout.py)

## 已验证与尚未证明

| 已有实现或验证 | 不能由此推出 |
|---|---|
| 数字代表页、连续小样和无文字层/无书签的扫描小样有内部记录；公开自造输入与回归用例可以单独查看 | 任意教材、所有扫描件或整本书均已完成验收 |
| 特定保护对象经过自动像素核对，部分例外另有目视说明 | 公式语义100%正确、OCR无漏字，或所有保护对象均已自动通过 |
| 已记录特定样本的独立语义复核与直接目视范围 | 全书所有段落都经过人工复核、风格完全一致或达到出版质量 |
| 事务、租约、版本失效、扫描裁片、标点、纸色等有软件回归用例 | 所有中断方式、供应商错误和特殊版面均已穷尽 |
| 分阶段时间、内存和供应商用量可以记录 | 已证明优于竞品的速度/费用/质量，或已有可靠全书时间预测 |

具体数量、保留提醒、内部记录与公开可复现范围见[验证记录](VALIDATION.md)；未通过边界见[限制](LIMITATIONS.md)。内部教材材料不公开，不把内部结果冒充公开 benchmark。

## 核查快照

| 官方项目 | 默认分支及固定提交 | 提交时间（UTC） | 核查范围 |
|---|---|---|---|
| BabelDOC | `main`：[38d3896dcde9b5a940c62cf5563cadea673a64d3](https://github.com/funstory-ai/BabelDOC/commit/38d3896dcde9b5a940c62cf5563cadea673a64d3) | 2026-08-05 20:39:53 | README、公式占位与匹配、排版缩放、缓存、扫描检测 |
| PDFMathTranslate-next | `main`：[f8dffcf4c3a33b254391d43514439b975ce8d966](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/commit/f8dffcf4c3a33b254391d43514439b975ce8d966) | 2026-05-15 09:42:54 | README、配置模型、供应商基类、限流实现 |
| Docling | `main`：[29caea8381e885872905dc9438f10014368a2a25](https://github.com/docling-project/docling/commit/29caea8381e885872905dc9438f10014368a2a25) | 2026-10-21 12:18:37 | 官方 README 的功能和定位；未做完整源码审计 |

提交时间是 Git 提交的 committer 时间，不是版本发布日期。默认分支以后可能变化，永久链接仍指向上述快照。本工具实际固定依赖 BabelDOC0.6.4、Typst0.15.1；不能把比较对象的当前 `main` 与本工具依赖版本混为一谈。

## 上游已经具备什么

**BabelDOC** 定位于 PDF 学术文档翻译与双语对照，提供 CLI 和 Python API。官方已有大文档分片、实验性表格文字翻译、公式字体/字符模式、扫描检测与 OCR workaround 开关。README 对 workaround 明确注明白底黑字假设；不能仅凭开关名称推断任意纯图片 PDF 的全部识别能力。[官方说明与参数](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/README.md#L132-L183)

它也已有公式占位对象、翻译结果中的占位匹配检查，不能把“用占位符避免模型修改公式”或“检查公式标记”写成本工具首创。[公式占位](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/babeldoc/format/pdf/document_il/midend/il_translator.py#L108-L134)、[占位匹配路径](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/babeldoc/format/pdf/document_il/midend/il_translator.py#L790-L819)

术语文件、自动术语提取、QPS、工作线程数量、缓存开关和离线模型/字体包也已存在。[术语与并发参数](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/README.md#L171-L230)、[离线资源](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/README.md#L242-L254)、[缓存实现](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/babeldoc/translator/cache.py#L54-L126)

**PDFMathTranslate-next** 官方说明其基于 BabelDOC，也是调用 BabelDOC 的官方参考实现；已有公式、图表、目录和批注保留的功能说明、多语言和多供应商、CLI、WebUI、Docker。不能称它只是简单机器翻译或只支持单一 API。[官方定位与功能](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/f8dffcf4c3a33b254391d43514439b975ce8d966/README.md#L37-L41)

源码也已配置术语表、自动术语提取、提词阶段的 QPS/线程池、翻译缓存和限流。[配置模型](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/f8dffcf4c3a33b254391d43514439b975ce8d966/pdf2zh_next/config/model.py#L97-L125)、[缓存与限流调用](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/f8dffcf4c3a33b254391d43514439b975ce8d966/pdf2zh_next/translator/base_translator.py#L72-L96)、[QPS 限流器](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/f8dffcf4c3a33b254391d43514439b975ce8d966/pdf2zh_next/translator/rate_limiter/qps_rate_limiter.py#L7-L48)

**Docling** 官方已列出多格式解析、版面和阅读顺序、表格、代码、公式、统一文档结构、Markdown/HTML/JSON 等导出、扫描 PDF/图片 OCR，以及本地和隔离网络环境执行。它主要介绍文档理解与转换，因此这里把它作为解析能力的对照，不把它当作已经完成同一套中文教材排版验收的直接竞品。[官方功能](https://github.com/docling-project/docling/blob/29caea8381e885872905dc9438f10014368a2a25/README.md#L33-L47)

## 对照当前代码，本工具可以准确强调什么

下表是工程取舍和可查实现，**不是对其他项目缺少同类机制的证明，也不是新算法或行业首创结论**。

| 取舍或组合 | 当前实现证据 | 公开介绍的准确表述 |
|---|---|---|
| 中文长篇阅读优先，允许增页 | [layout.py](../textbook_pdf/layout.py)、[templates/book.typ](../textbook_pdf/templates/book.typ) 按正文/习题/图注角色设定稳定字号，交给 Typst 正常断行和分页；资产保留原尺寸，越界进入问题报告 | “采用适合中文阅读的增页重排，保留来源页码”；不能保证译本逐页复刻原版 |
| 解析与最终排版分开 | [babel_adapter.py](../textbook_pdf/babel_adapter.py) 接入固定版本的 BabelDOC；最终排版由本项目 Typst 模板承担 | “复用成熟解析，独立实现教材排版流程”；不能写成从零发明 PDF 解析 |
| API 与外部代理使用同一任务协议 | [translation.py](../textbook_pdf/translation.py)、[store.py](../textbook_pdf/store.py)、[tp_worker.py](../tp_worker.py) 共享任务包、SQLite 权威状态、租约及提交校验；核对正文、只读上下文、术语规则和保护资产版本 | “让 API 与代理在同一套可恢复、可校验任务中协作”；并发和缓存本身不是创新 |
| 保护对象可追踪到来源和成品 | [models.py](../textbook_pdf/models.py)、[extraction.py](../textbook_pdf/extraction.py)、[layout.py](../textbook_pdf/layout.py) 记录原始对象、来源位置、内容哈希及 `layout_map`；源元素保留处理记录 | “把公式/代码保护与来源追踪串成可审计链路”；不能把有归属记录等同于公式语义正确 |
| 将扫描 OCR 与数学保护分开 | [scan_adapter.py](../textbook_pdf/scan_adapter.py)、[ocr_engine.py](../textbook_pdf/ocr_engine.py)、[scan_detectors.py](../textbook_pdf/scan_detectors.py) 在本机识别正文，先保护公式/代码/图形的原合成画面；不要求文字层或书签；修正记录绑定图像与 OCR 行映射 | “在现成本地 OCR/检测模型上，增加教材对象保护、修正及核验流程”；不能声称首创 OCR 或扫描支持 |
| 把自动通过与正式验收分开 | [qa.py](../textbook_pdf/qa.py)、[visual_checks.py](../textbook_pdf/visual_checks.py)、[scan_quality.py](../textbook_pdf/scan_quality.py) 分别检查版本、结构、保护对象像素、OCR 置信度和未归属墨迹；[cli.py](../textbook_pdf/cli.py) 在默认全流程（`--stage all`）中对来源阻断先报告、停止进入付费翻译；缺译预览单独标记 | “优先暴露可定位的失败，并保留复核证据”；像素相同和高置信度都不是语义正确率 |
| 扫描纸色作为独立包装层 | [scan_layout.py](../textbook_pdf/scan_layout.py) 在原排版后填背景，绑定源图与包装层版本；数字文档仍走原排版器 | “扫描纸色处理与内容排版隔离”；这不是通用复原任意纸张纹理或颜色的算法 |

一个可核实的排版策略差异是：所查 BabelDOC 排版源码存在 `min_scale = 0.1` 的缩放搜索及兜底返回路径；本工具选择角色字号与正常增页。**0.1 是比例，不是0.1磅字号；这条源码证据不表示每个 BabelDOC 文档都会缩至该比例，也不构成整体质量优劣结论。** [缩放搜索](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/babeldoc/format/pdf/document_il/midend/typesetting.py#L956-L976)、[兜底返回](https://github.com/funstory-ai/BabelDOC/blob/38d3896dcde9b5a940c62cf5563cadea673a64d3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1073-L1076)

术语方面，本工具当前是固定术语规则、提议收集和协调更新；不能把实施计划中的“翻译前自动生成并验完全书术语”当作已经实现的交付。冻结风格规则和抽样复核，也不等于已经证明全书风格完全一致。

### rc.3 中可以复现的原生数学对象修复

在固定 PyMuPDF/MuPDF 1.28.2 路径中，名义字体框和字形 advance 不能单独代表可见笔画范围。rc.3 分别处理两个可复现问题：高括号等可伸展字形的真实墨迹超出名义框；零字宽组合否定斜线因空裁剪框被删除。前者按原嵌入字体及原 glyph ID 测量笔画范围，后者保留原生文字操作符，不将公式改写为 Unicode 近似或图片重绘。[原生裁剪适配](../textbook_pdf/native_clipping.py)、[字形几何](../textbook_pdf/glyph_geometry.py)

另一种情况是组合符的 `rawdict` 逻辑原点随相邻上下文变化，而原 PDF 操作符实际没有移动。普通字符继续采用原有位置容差；仅对有零 advance 证据的组合符，要求唯一原生 glyph ID、同一嵌入字体程序及完整有效操作符矩阵相符。该替代证据使用更严格的原点容差，歧义或真实移位仍阻断。公开自创小字体可重现这些情况，无需教材或系统字体。[回归用例](../tests/test_native_operator_identity.py)

这些是特定 PDF 处理链的工程修复与可审计证据，不据此宣称原创 PDF 引擎、研究首创或整体领先。源字形存在、可见裁框、成品像素和数学含义仍分别验收。

裁片中“未被选中的字符”也不一定是可删除的污染：它可能是二项式、矩阵或上下限的成员。rc.3的全书排查保留这类尚未证实的结构阻断；仅有所选字形一致、零外字或零裁框越界，均不足以关闭它。局部修复须证明选取集合本身包含完整数学关系。

## 项目定位与明确边界

> 面向大型教材的中文 PDF 翻译与阅读排版工作流。复用 BabelDOC、Typst、PyMuPDF 和本地 OCR/检测组件，重点处理原始数学对象、允许增页的中文排版、来源页码，以及 API/代理共用的可恢复任务与质量记录。它是持续验证中的工程集成，不承诺任意教材自动无损转换。

本项目不作“首创公式保护”“首个扫描 PDF 翻译”“唯一术语一致性方案”“100%保留公式/排版”“全自动无需复核”“已证明速度质量领先”等主张。本次没有对不同项目运行同一套输入、模型、成本和人工评分的统一基准，不能发布横向速度、质量或准确率排名。复杂教材仍可能出现显式阻断，样本测试通过也不能替代整书验收。

## 本工具证据版本与引用范围

本仓库实现按上文模块核查。关键模块的 SHA256 如下，供核对公开快照；源码变化后应重新核实相应表述。本页不包含个人路径、教材文本、教材截图或真实供应商配置。

| 模块 | SHA256 |
|---|---|
| [textbook_pdf/layout.py](../textbook_pdf/layout.py) | `8cfe6bf20152fc01e2343f18da6e240328c06d9765229b4728912d55f0a62854` |
| [textbook_pdf/templates/book.typ](../textbook_pdf/templates/book.typ) | `81bd5b1b2cb7cb6e2879618170d772e99776816c3cb60468c028fe27904b445e` |
| [textbook_pdf/store.py](../textbook_pdf/store.py) | `a04b81800205a903e546a304baa00ab7881b0bbb536e10d331d8c4265313fd85` |
| [textbook_pdf/translation.py](../textbook_pdf/translation.py) | `37d5318c55232a75ddb9a5916ac4f5f4440bd7f3479df1c10e90c709ecf88c78` |
| [textbook_pdf/scan_adapter.py](../textbook_pdf/scan_adapter.py) | `59dd267550f498d4a99d334858a5bc3ba26df3e5c78ab7d5656fe896bca94863` |
| [textbook_pdf/qa.py](../textbook_pdf/qa.py) | `e7dc501309bcd8cc3b13f43edf3aa59e47e824da570a83391b8cae5fe088c592` |

上游 README 的功能说明属于项目自身声明；源码检查证明相应路径存在，不证明所有输入都能成功。未检出的功能不能据此断言“不存在”。本次只选 Docling 作为第三个解析项目，没有对 Marker/MinerU 或整个领域做穷尽式查新。
