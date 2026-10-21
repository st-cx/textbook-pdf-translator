# 安装与运行

当前验证平台是 **Windows x64 + Python 3.13（64位）+ Typst 0.15.1**。本仓库是命令行工具，尚无桌面图形界面。其他平台尚未做完整验收。

## 首次安装

```powershell
git clone https://github.com/st-cx/textbook-pdf-translator.git
cd textbook-pdf-translator
powershell -ExecutionPolicy Bypass -File .\安装依赖.ps1
```

安装器创建本目录 `.venv`，安装 `requirements-lock.txt`，从 Typst 官方 release 下载并核对固定 SHA256，再显式准备本地识别模型。不读取教材、配置密钥或启动翻译。扫描模型的许可及归属见 [THIRD_PARTY.md](../THIRD_PARTY.md)，包括 MFD 权重的上游许可声明差异。源仓库不包含模型、字体、教材或私人任务库。

只需测试、数字排版或先验证环境时可加 `-SkipModels`。需要解析/扫描时再执行：

```powershell
.\.venv\Scripts\python.exe -m textbook_pdf.bootstrap
```

所有准备操作都可能需要外网。普通扫描推理不隐式下载模型，不上传扫描图。BabelDOC 原生解析所需字体可能在首次使用时下载。模型、字体可在安装完成后离线使用。

默认缓存：BabelDOC资源在 `.cache/babeldoc/`，扫描正文模型在 `.cache/scan/ocr/`，公式检测权重在 `.cache/scan/pix2text-mfd-1.5.onnx`。可按扫描文档设置显式本地模型位置。

## 已下载 Typst 或已有本地版本

```powershell
# 验证并安装官方 Windows x64 zip；不接受来源不明的同名压缩包
python scripts/install_typst.py --archive "D:\downloads\typst-x86_64-pc-windows-msvc.zip"

# 或使用自己的固定版本可执行文件
$env:TEXTBOOK_TYPST = 'D:\tools\typst.exe'
```

排版器会检查版本必须为0.15.1。自动安装脚本只处理Windows x64；其他系统需自行安装相同版本并配置字体。本工具默认用本机宋体、黑体及微软雅黑，不分发Windows字体。

## 配置翻译服务

将 `config.example.json` 复制为 `config.json`，在本地填写服务地址、模型和密钥。示例价格只是占位值，应改为服务商实际单价；程序记录的是估算费用，不是账单。不要提交真实配置。代理方式无需配置外部API，可按 [工作流程](WORKFLOW.md) 领取并提交结果。

## 先做少量页

```powershell
# 无 API 调用，先生成识别证据和问题报告；页码是从1起的PDF物理页
.\.venv\Scripts\python.exe translate_pdf.py "书.pdf" --stage prepare --pages 1-3 --out "试译" --no-legacy-import

# 源问题处理后，限制任务数测量速度和费用
.\.venv\Scripts\python.exe translate_pdf.py --out "试译" --stage translate --provider example --workers 2 --max-tasks 2

# 复核、排版与检查
.\.venv\Scripts\python.exe translate_pdf.py --out "试译" --stage review --review-provider example --workers 2
.\.venv\Scripts\python.exe translate_pdf.py --out "试译" --stage layout
.\.venv\Scripts\python.exe translate_pdf.py --out "试译" --stage qa
```

正式样章确认后再扩大范围。默认全流程有解析阻断错误时先停止付费翻译。单独执行阶段时须自己检查前阶段报告。`--allow-incomplete` 只生成标记未完成内容的预览，不构成通过验收。

## 故障处理

| 提示 | 处理 |
|---|---|
| Python版本不匹配 | 用 `-Python` 指定3.13 64位解释器，不修改全局环境 |
| 模型缺失或校验失败 | 恢复网络后重跑显式bootstrap，勿用其他版本同名权重顶替 |
| Typst版本或下载不符 | 使用官方固定版本或离线zip，设置 `TEXTBOOK_TYPST` |
| OCR粘词、公式边界或未归属墨迹 | 查看页图和review记录，按[扫描文档](SCANNED_PDF.md)提供绑定哈希的修正 |
| 缺译、标记不符、复核失败 | 在相同任务目录继续或重做对应元素；保留历史记录 |
| 更换字号、纸型后PDF过期 | 只重排并重跑QA，已通过译文不重复调用服务 |
