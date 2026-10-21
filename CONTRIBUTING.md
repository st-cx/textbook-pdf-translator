# 参与开发

请用可合法共享的最小输入复现问题，保留诊断代码、元素ID、版本和步骤。不要上传密钥、私人文件路径、未授权教材或私人译文。

```powershell
python -m pip install -r requirements-lock.txt
python scripts/install_typst.py
python translate_pdf.py --help
python -m unittest discover -s tests -v
```

公开本地测试使用Windows/Python3.13合成及合同用例，不调用翻译供应商。当前尚未启用托管CI，建议配置见 [CI_SETUP.md](docs/CI_SETUP.md)。可选原书、模型和真实成品标定缺少本地材料时明确跳过。分别记录执行数、失败数、错误数和跳过数，不将内部278项结果等同公开CI覆盖。

`models.py`定义共享结构，`store.py`维护唯一任务真值。新增解析/排版路径须遵守资产不可变、来源可追、提交版本校验和各阶段独立判定；不要另建权威译文缓存。改版式应使旧成品过期，但不使已通过译文失效。

代码遵循仓库AGPL许可。注明复用或参考的上游代码/模型并保留归属；不要把识别权重标成自研。

需要可选集成检查时，由本地操作者明确设置 `TEXTBOOK_TEST_ROSEN_PDF`、`TEXTBOOK_TEST_RENDERED_JOB`（可加 `TEXTBOOK_TEST_LAYOUT_MAP`），或 `TEXTBOOK_OCR_TEST_MODELS` / `TEXTBOOK_OCR_TEST_IMAGES`。默认不读取外部私人文件。请勿把这些输入加入Git。
